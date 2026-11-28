"""Resolve submission-time VSE anchors and build independent work groups."""
from collections import defaultdict
from copy import deepcopy

from .contract import FRAME_MAX, FRAME_MIN, WorkflowError


def _fail(code, message):
    raise WorkflowError(code, message)


def _resolve(point, channels, roots, markers):
    if 'frame' in point:
        return point['frame']
    if 'result_of' in point:
        return None
    offset = point.get('offset', 0)
    if 'marker' in point:
        frames = markers.get(point['marker'], [])
        if len(frames) != 1:
            _fail('CONFLICT' if frames else 'NOT_FOUND', 'Expected one marker: ' + point['marker'])
        return frames[0] + offset
    matches = [row for row in roots if row['shot_id'] == point['shot_id']
               and row['kind'] not in ('TEXT', 'SOUND') and row['channel'] in channels]
    if not matches:
        _fail('NOT_FOUND', 'Shot is absent on the scoped visual tracks: ' + point['shot_id'])
    edge = point.get('edge', 'start')
    # Split descendants retain shot identity. Adjacent descendants across tracks
    # make one span. Repeated, disjoint occurrences need a marker or frame.
    spans = sorted((row['start'], row['end']) for row in matches)
    start, end = spans[0]
    for left, right in spans[1:]:
        if left > end:
            _fail('CONFLICT', 'Shot has disjoint occurrences: ' + point['shot_id'])
        end = max(end, right)
    return (start if edge == 'start' else end) + offset


def _footprint(row, estimates, channel, roots):
    points = row['submitted']
    if any(points[key] is None and estimates.get(key) is None for key in points):
        return FRAME_MIN, FRAME_MAX + 1
    if row['op'] == 'split':
        begin = points['at'] if points['at'] is not None else estimates['at']
        end = begin + 1
    elif row['op'] == 'insert':
        begin = points['at'] if points['at'] is not None else estimates['at']
        end = FRAME_MAX + 1 if row['ripple'] else begin + row['duration']
    else:
        begin = points['start'] if points['start'] is not None else estimates['start']
        finish = points['end'] if points['end'] is not None else estimates['end']
        end = FRAME_MAX + 1 if row['ripple'] else max(finish, begin + 1)
    # Two cuts against the same source strip must either both save, or neither.
    targets = [s for s in roots if s['channel'] == channel
               and (s['start'] < begin < s['end'] if row['op'] == 'split' else
                    s['start'] < end and begin < s['end'])]
    if row['op'] == 'split' and not targets and points['at'] is not None:
        return None
    return min([begin] + [s['start'] for s in targets]), max([end] + [s['end'] for s in targets])


def build_groups(operations, atomicity, roots, markers):
    rows = deepcopy(operations)
    parents = list(range(len(rows)))

    def root(index):
        while index != parents[index]:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def join(first, second):
        a, b = sorted((root(first), root(second)))
        parents[b] = a

    seen, explicit, named_inserts = {}, {}, {}
    claims = defaultdict(list)
    for index, row in enumerate(rows):
        keys = ('start', 'end') if row['op'] == 'remove' else ('at',)
        row['submitted'], estimates = {}, {}
        row['index'] = index
        if atomicity == 'batch':
            join(0, index)
        if 'group' in row:
            join(explicit.setdefault(row['group'], index), index)
        for clip in row.get('clips', ()):
            for key in [('id', clip['id']), ('name', clip.get('name', clip['id']))]:
                join(named_inserts.setdefault(key, index), index)
        for key in keys:
            point = row[key]
            row['submitted'][key] = None
            try:
                if 'result_of' in point:
                    source = seen.get(point['result_of'])
                    if source is None:
                        _fail('INVALID_REQUEST', 'Result must refer to an earlier operation: ' + point['result_of'])
                    join(source['index'], index)
                    if not set(row['channels']).issubset(source['channels']):
                        _fail('CONFLICT', 'Result track scope differs: ' + point['result_of'])
                    approximate = source['estimated_result'][point['edge']]
                    if approximate is not None:
                        estimates[key] = approximate + point.get('offset', 0)
                else:
                    row['submitted'][key] = _resolve(point, row['channels'], roots, markers)
            except WorkflowError as error:
                row.setdefault('error', {'code': error.code, 'message': str(error)})
        begin = row['submitted'].get(keys[0])
        if begin is None:
            begin = estimates.get(keys[0])
        end = begin
        if row['op'] == 'insert' and begin is not None:
            end = begin + row['duration']
        elif row['op'] == 'remove' and not row['ripple']:
            end = row['submitted']['end']
            if end is None:
                end = estimates.get('end')
        row['estimated_result'] = {'start': begin, 'end': end}
        seen[row['id']] = row
        if 'error' not in row:
            for channel in row['channels']:
                area = _footprint(row, estimates, channel, roots)
                if area is None:
                    continue
                start, finish = area
                for low, high, prior in claims[channel]:
                    if start < high and low < finish:
                        join(index, prior)
                claims[channel].append((start, finish, index))
    groups = defaultdict(list)
    for index, row in enumerate(rows):
        groups[root(index)].append(row)
    return list(groups.values())


def map_points(row, changes, results):
    """Apply intervening ripple transforms to each coordinate per track."""
    output = {}
    for key, submitted in row['submitted'].items():
        since = -1
        if submitted is None:
            point = row[key]
            result = results[point['result_of']]
            submitted = result['result'][point['edge']] + point.get('offset', 0)
            since = result['index']
        frames = set()
        for channel in row['channels']:
            frame = submitted
            for change in changes[channel]:
                if change['index'] <= since:
                    continue
                start, end, delta = change['start'], change['end'], change['delta']
                if delta < 0 and start < frame < end:
                    _fail('CONFLICT', f'Frame {submitted} was removed on channel {channel}')
                if frame >= end:
                    frame += delta
            frames.add(frame)
        if len(frames) != 1:
            _fail('CONFLICT', f'Frame {submitted} maps differently across scoped channels')
        output[key] = frames.pop()
    return output
