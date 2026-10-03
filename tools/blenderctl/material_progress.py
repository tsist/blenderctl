# SPDX-License-Identifier: GPL-3.0-or-later
"""JSON-only observational progress and disjoint monotonic phase accounting.

Observer return values are ignored. Callbacks receive detached documents and
their exceptions are recorded without changing batch execution or its guards.
This is an in-process observer contract, not a sandbox for untrusted Python.
"""
import json
import time

PHASES = ('preflight_resources', 'resume', 'assembly', 'checkpoint_save',
          'checkpoint_reopen', 'preview_build_preflight', 'render_entry',
          'image_sheet', 'final_guards', 'orchestration')


class Progress:
    def __init__(self, observer=None, clock=time.monotonic):
        self.clock = clock
        self.started = self.mark = clock()
        self.phase = 'preflight_resources'
        self.durations = dict.fromkeys(PHASES, 0.0)
        self.observer = observer
        self.observer_errors = []
        self.assignment = self.view = None

    def switch(self, phase):
        if phase not in self.durations:
            raise ValueError('Unknown material timing phase: ' + phase)
        now = self.clock()
        self.durations[self.phase] += now - self.mark
        self.mark, self.phase = now, phase

    def timings(self):
        now = self.clock()
        phases = dict(self.durations)
        phases[self.phase] += now - self.mark
        elapsed = now - self.started
        return {'clock': 'monotonic', 'elapsed_seconds': elapsed,
                'phase_seconds': phases, 'accounted_seconds': sum(phases.values()),
                'phases_overlap': False,
                'render_entry_scope': 'Whole rendering.run entry, including setup, render and validation; not pure GPU time'}

    def document(self, report, stage):
        assignments, views = report['assignments'], report['outputs']
        manifest = report.get('manifest', {})
        planned_a = len(manifest.get('assignments', []))
        planned_v = len(manifest.get('preview', {}).get('views', []))
        completed = len(assignments) + len(views)
        planned = planned_a + planned_v
        recent = next((r for r in reversed(views) if r.get('status') == 'pass' and r.get('file') and r.get('sha256')), None)
        failures = [{'kind': kind, 'id': row.get('id'), 'status': row['status'],
                     'error': row.get('error'), 'code': row.get('code')}
                    for kind, rows in [('assignment', assignments), ('view', views)]
                    for row in rows if row['status'] not in ('pass',)]
        return {'stage': stage, 'status': report['status'],
                'elapsed_seconds': report['timings']['elapsed_seconds'],
                'planned_assignments': planned_a, 'completed_assignments': len(assignments),
                'successful_assignments': sum(r['status'] == 'pass' for r in assignments),
                'planned_views': planned_v, 'completed_views': len(views),
                'successful_views': sum(r['status'] == 'pass' for r in views),
                'current_assignment': self.assignment, 'current_view': self.view,
                'checkpoint': report['checkpoint'],
                'latest_checkpoint': ({k: report['checkpoint'][k] for k in ('file', 'expected_sha256')}
                                      if report['checkpoint'] else None),
                'latest_successful_image': ({'file': recent['file'], 'expected_sha256': recent['sha256']}
                                            if recent else None),
                'completion': {'completed_count': completed, 'planned_count': planned,
                               'percent': 100.0 * completed / planned if planned else None,
                               'basis': 'Terminal assignment and view counts, including failed/blocked items; excludes remaining sheet and final guards'},
                'timings': report['timings'], 'next_action': self.next_action(stage, report),
                'failures': failures, 'error': report.get('error'),
                'observer_errors': list(self.observer_errors)}

    def next_action(self, stage, report):
        if report['status'] in ('pass', 'partial'):
            return 'Read final batch report and artifacts'
        if report['status'] == 'failed':
            return 'Inspect error and verified checkpoint before retry; source is never saved'
        if stage.startswith('rendering:'): return 'Wait for render entry; validate image and clean preview rig'
        if stage.startswith('assignment:'): return 'Apply or reuse assignment; verify preservation and save/reopen checkpoint'
        if stage == 'contact_sheet': return 'Decode/compose sheet, then verify source and resource guards'
        if stage == 'preflight': return 'Verify source/resources and recovery checkpoint'
        if stage == 'resources_ready': return 'Save/reopen initial checkpoint, then process assignments'
        if stage.startswith('preview:'): return 'Build preview rig and preflight, then render or reuse view'
        return 'Continue remaining assignments/views, then sheet and final guards'

    def notify(self, document):
        if self.observer is None: return
        try:
            self.observer(json.loads(json.dumps(document, ensure_ascii=False, allow_nan=False)))
        except Exception as error:
            # Bounded diagnostics; observer cannot cancel work by throwing.
            if len(self.observer_errors) < 8:
                self.observer_errors.append({'type': type(error).__name__, 'message': str(error)[:512]})
