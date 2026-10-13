"""Observable wall-clock spans; no inferred Blender internals or double counting."""
import contextvars
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

NOT_OBSERVABLE = [
    "client request dispatch, first token and approval latency (outside worker)",
    "Cycles kernel compilation, path tracing, denoising and PNG/file write inside bpy.ops.render.render(write_still=True) cannot be strictly separated by this trace",
]


def interval_union(intervals):
    """Return union length for half-open intervals, without adding overlaps."""
    rows = sorted((float(a), float(b)) for a, b in intervals if b > a)
    total = 0.0
    end = None
    for a, b in rows:
        if end is None or a > end:
            total += b - a
            end = b
        elif b > end:
            total += b - end
            end = b
    return total


def exclusive_intervals(start, end, children):
    """Subtract the union of direct children, clipped to the parent's interval."""
    cursor = start
    pieces = []
    for a, b in sorted((max(start, a), min(end, b)) for a, b in children):
        if b <= a or b <= cursor:
            continue
        if a > cursor:
            pieces.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < end:
        pieces.append((cursor, end))
    return pieces


def account(spans):
    """Inclusive, exclusive and union values; phase unions may overlap.

    The only wall total is all_interval_union_seconds. Never sum inclusive
    parents/children or per-phase unions to derive elapsed wall time.
    """
    complete = [s for s in spans if s.get('end_monotonic') is not None]
    intervals = [(s['start_monotonic'], s['end_monotonic']) for s in complete]
    by_id = {}
    phase_intervals = {}
    for s in complete:
        children = [(c['start_monotonic'], c['end_monotonic']) for c in complete
                    if c.get('parent_span_id') == s['span_id']]
        pieces = exclusive_intervals(s['start_monotonic'], s['end_monotonic'], children)
        by_id[s['span_id']] = {
            'inclusive_seconds': s['end_monotonic'] - s['start_monotonic'],
            'exclusive_seconds': interval_union(pieces),
        }
        phase_intervals.setdefault(s['name'], []).extend(pieces)
    return {
        'all_interval_union_seconds': interval_union(intervals),
        'per_span': by_id,
        'exclusive_phase_union_seconds': {k: interval_union(v) for k, v in phase_intervals.items()},
        'rule': 'Use interval union for total; phase/resource overlap is not additive; parent exclusive subtracts direct-child union.',
    }


class Trace:
    def __init__(self, path, *, metadata=None):
        self.path = Path(path)
        self.trace_id = uuid.uuid4().hex
        self.metadata = metadata or {}
        self.spans = []
        self.status = 'running'
        self.error = None
        self._parent = contextvars.ContextVar('timing_parent_' + self.trace_id, default=None)
        self.persist()

    @staticmethod
    def _utc():
        return datetime.now(timezone.utc).isoformat()

    @contextmanager
    def span(self, name, *, checkpoint=False, **attributes):
        row = {'span_id': uuid.uuid4().hex, 'parent_span_id': self._parent.get(),
               'name': name, 'attributes': attributes, 'start_utc': self._utc(),
               'start_monotonic': time.monotonic(), 'end_utc': None,
               'end_monotonic': None, 'duration_seconds': None, 'status': 'running', 'error': None}
        self.spans.append(row)
        token = self._parent.set(row['span_id'])
        try:
            if row['parent_span_id'] is None:
                self.persist()
            yield row
        except BaseException as ex:
            row['status'] = 'failed'
            row['error'] = {'type': type(ex).__name__, 'message': str(ex)}
            raise
        else:
            row['status'] = 'passed'
        finally:
            row['end_monotonic'] = time.monotonic()
            row['end_utc'] = self._utc()
            row['duration_seconds'] = row['end_monotonic'] - row['start_monotonic']
            self._parent.reset(token)
            # Avoid quadratic full-file writes for every small nested span.
            # Root, explicit frame checkpoints and failures retain full evidence.
            if row['parent_span_id'] is None or checkpoint or row['status'] == 'failed':
                self.persist()

    def finish(self, error=None):
        self.status = 'failed' if error is not None else 'passed'
        self.error = ({'type': type(error).__name__, 'message': str(error)} if error is not None else None)
        self.persist()

    def document(self):
        return {'timing_version': '1.0', 'trace_id': self.trace_id,
                'clock': 'time.monotonic seconds; UTC timestamps are presentation/correlation only',
                'status': self.status, 'error': self.error, 'metadata': self.metadata,
                'not_observable': NOT_OBSERVABLE, 'spans': self.spans, 'accounting': account(self.spans)}

    def persist(self):
        """Same-directory atomic replace; readers see a complete JSON snapshot."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=self.path.name + '.', suffix='.tmp', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(self.document(), stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def descriptor(self):
        return {'file': str(self.path), 'trace_id': self.trace_id, 'status': self.status,
                'timing_version': '1.0', 'not_observable': NOT_OBSERVABLE,
                'accounting': account(self.spans)}

    def frame_projection(self):
        rows = []
        for parent in self.spans:
            if parent['name'] != 'frame':
                continue
            children = [s for s in self.spans if s['parent_span_id'] == parent['span_id']]
            rows.append({'frame': parent['attributes']['frame'], 'span_id': parent['span_id'],
                         'status': parent['status'], 'duration_seconds': parent['duration_seconds'],
                         'phases': [{'name': s['name'], 'span_id': s['span_id'],
                                     'duration_seconds': s['duration_seconds'], 'status': s['status']} for s in children],
                         'rule': 'frame duration includes phases; do not add them to parent'})
        return rows
