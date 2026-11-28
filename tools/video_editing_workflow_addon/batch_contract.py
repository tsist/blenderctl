"""Host-safe request schema for ordered VSE edits."""
from copy import deepcopy

from .contract import (BOOL, CLIP, DURATION, FILE, FRAME, FRAME_MAX, FRAME_MIN, ID, NAME,
                       SCHEMA_VERSION, _normalize_file, _unique, _validate, arr, integer, obj)

EDGE = {'enum': ['start', 'end']}
OFFSET = integer(FRAME_MIN, FRAME_MAX)
POINT = {'oneOf': [
    obj({'frame': FRAME}, ('frame',)),
    obj({'marker': NAME, 'offset': OFFSET}, ('marker',)),
    obj({'shot_id': ID, 'edge': EDGE, 'offset': OFFSET}, ('shot_id',)),
    obj({'result_of': ID, 'edge': EDGE, 'offset': OFFSET}, ('result_of', 'edge')),
]}

# The operation binds the common insertion frame. Inserted strips may be
# shorter than the interval when their track is intentionally left with a gap.
INSERT_CLIP = {'oneOf': [obj({k: v for k, v in variant['properties'].items() if k != 'start'},
                          [k for k in variant['required'] if k != 'start'])
                        for variant in CLIP['oneOf']]}
CHANNELS = {**arr(integer(1, 128), 1, 128), 'uniqueItems': True}
SHARED = {'id': ID, 'group': ID, 'channels': CHANNELS}
REQUIRED = ('id', 'op', 'channels')
OPERATIONS = {'oneOf': [
    obj({**SHARED, 'op': {'const': 'split'}, 'at': POINT}, (*REQUIRED, 'at')),
    obj({**SHARED, 'op': {'const': 'insert'}, 'at': POINT, 'duration': DURATION,
         'clips': arr(INSERT_CLIP, 1, 128), 'ripple': BOOL},
        (*REQUIRED, 'at', 'duration', 'clips')),
    obj({**SHARED, 'op': {'const': 'remove'}, 'start': POINT, 'end': POINT, 'ripple': BOOL},
        (*REQUIRED, 'start', 'end')),
]}
BATCH_EDIT_SCHEMA = obj({
    'version': {'const': SCHEMA_VERSION},
    'scene': NAME,
    'workflow_id': ID,
    'atomicity': {'enum': ['group', 'batch']},
    'media_probe': FILE,
    'operations': arr(OPERATIONS, 1, 1024),
}, ('version', 'scene', 'workflow_id', 'operations'))


def normalize_batch_edit(value):
    _validate(value, BATCH_EDIT_SCHEMA, 'batch')
    result = deepcopy(value)
    result.setdefault('atomicity', 'group')
    if 'media_probe' in result:
        _normalize_file(result['media_probe'])
    _unique([row['id'] for row in result['operations']], 'operation id')
    for row in result['operations']:
        row['channels'].sort()
        if row['op'] != 'split':
            row.setdefault('ripple', row['op'] == 'insert')
    return result
