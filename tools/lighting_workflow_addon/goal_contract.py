"""Contract shared by the goal solver and its callers."""

from copy import deepcopy

from .contract import BOOL, DEVICE, NAME, VEC, WorkflowError, _unique, _validate, arr, num, obj


LIMIT = 1e8
INTEGER = {'type': 'integer'}
COUNT = lambda a, b: {**INTEGER, 'minimum': a, 'maximum': b}
ENERGY = num(0, LIMIT)
ROI = arr(num(0, 1), 4, 4)
REGION = obj({'object': NAME, 'roi': ROI})
LIGHT = obj({'name': NAME, 'min_energy': ENERGY, 'max_energy': ENERGY}, ('name',))
FILL = obj({'name': NAME, 'location': VEC, 'aim': VEC, 'size': num(.001, 100),
            'min_energy': ENERGY, 'max_energy': ENERGY},
           ('name', 'location', 'aim', 'size', 'max_energy'))
LINK = obj({'light': NAME, 'receivers': arr(NAME, 0, 256),
            'mode': {'enum': ['INCLUDE', 'EXCLUDE']}}, ('light', 'receivers'))
EXPOSURE = obj({'subject': NAME, 'stops': num(-16, 16), 'tolerance_stops': num(.001, 4),
                'protect': arr(obj({'object': NAME, 'tolerance_stops': num(.001, 4)}, ('object',)), 0, 32)},
               ('subject', 'stops'))
RATIO = obj({'key': arr(NAME, 1, 16), 'fill': arr(NAME, 1, 16), 'region': REGION,
             'ratio': num(.001, 1000), 'tolerance_stops': num(.001, 4)},
            ('key', 'fill', 'region', 'ratio'))
BRIGHTNESS = obj({'roi': ROI, 'min': num(0, 1e6), 'max': num(0, 1e6)}, ('roi', 'min', 'max'))
SCHEMA = obj({
    'scene': NAME, 'view_layer': NAME, 'apply': BOOL,
    'lights': arr(LIGHT, 0, 16), 'add_lights': arr(FILL, 0, 8), 'light_links': arr(LINK, 0, 24),
    'allow_add_lights': BOOL, 'allow_link_changes': BOOL,
    'exposure': arr(EXPOSURE, 0, 16), 'key_fill': arr(RATIO, 0, 8),
    'brightness': arr(BRIGHTNESS, 0, 16),
    'sampling': obj({'width': COUNT(16, 2048), 'samples': COUNT(1, 512), 'threads': COUNT(1, 32),
                     'device': DEVICE}),
}, ('scene', 'view_layer', 'lights'))


def _check_roi(roi):
    if not (roi[0] < roi[2] and roi[1] < roi[3]):
        raise WorkflowError('INVALID_REQUEST', 'ROI requires left < right and top < bottom')


def normalize_request(value):
    value = deepcopy(value)
    _validate(value, SCHEMA, 'goals')
    value.setdefault('apply', True)
    for key in ('exposure', 'key_fill', 'brightness', 'add_lights', 'light_links'):
        value.setdefault(key, [])
    value['sampling'] = {'width': 256, 'samples': 32, 'threads': 4, **value.get('sampling', {})}
    device = value['sampling'].setdefault('device', {'backend': 'CPU'})
    if device['backend'] == 'GRAPHICS' or (device['backend'] != 'CPU' and not device.get('id')):
        raise WorkflowError('INVALID_REQUEST', 'GPU measurement needs exact native CUDA/OPTIX device ID')
    if device['backend'] != 'CPU' and (value['add_lights'] or value['light_links']):
        raise WorkflowError('UNSUPPORTED', 'One-render GPU mode requires fixed light topology and linking')
    if not any(value[key] for key in ('exposure', 'key_fill', 'brightness')):
        raise WorkflowError('INVALID_REQUEST', 'At least one lighting constraint is required')
    for edits, permission in (('add_lights', 'allow_add_lights'), ('light_links', 'allow_link_changes')):
        if value[edits] and not value.get(permission):
            raise WorkflowError('INVALID_REQUEST', edits + ' requires explicit ' + permission)
    names = [row['name'] for row in value['lights'] + value['add_lights']]
    _unique(names, 'light name')
    if not names:
        raise WorkflowError('INVALID_REQUEST', 'At least one light must be named or added')
    _unique([row['light'] for row in value['light_links']], 'light-link emitter')
    for row in value['light_links']:
        if row['light'] not in names:
            raise WorkflowError('INVALID_REQUEST', 'Link emitter must be a named adjustment: ' + row['light'])
        _unique(row['receivers'], 'receiver')
        row.setdefault('mode', 'INCLUDE')
        if not row['receivers'] and row['mode'] != 'INCLUDE':
            raise WorkflowError('INVALID_REQUEST', 'An empty receiver list removes the link and requires INCLUDE')
    for row in value['add_lights']:
        if row['location'] == row['aim']:
            raise WorkflowError('INVALID_REQUEST', 'Fill location and aim must differ: ' + row['name'])
    for row in value['exposure']:
        row.setdefault('tolerance_stops', .10)
        row.setdefault('protect', [])
        _unique([row['subject'], *[item['object'] for item in row['protect']]], 'exposure object')
        for protected in row['protect']:
            protected.setdefault('tolerance_stops', .05)
    for row in value['key_fill']:
        _unique(row['key'] + row['fill'], 'key/fill member')
        if set(row['key'] + row['fill']) - set(names):
            raise WorkflowError('INVALID_REQUEST', 'Key/fill lights must be named adjustments')
        if len(row['region']) != 1:
            raise WorkflowError('INVALID_REQUEST', 'Key/fill region needs one object or one ROI')
        if 'roi' in row['region']:
            _check_roi(row['region']['roi'])
        row.setdefault('tolerance_stops', .10)
    for row in value['brightness']:
        _check_roi(row['roi'])
        if row['min'] >= row['max']:
            raise WorkflowError('INVALID_REQUEST', 'Brightness min must be below max')
    return value
