"""Strict, host-safe lighting contracts. No Blender import or implicit device."""
from copy import deepcopy
from pathlib import Path
import math
import re

SCHEMA_VERSION = '1.0'


class WorkflowError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def obj(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


def arr(items, minimum=0, maximum=64):
    return {'type': 'array', 'items': items, 'minItems': minimum, 'maxItems': maximum}


def num(lo=-1e6, hi=1e6):
    return {'type': 'number', 'minimum': lo, 'maximum': hi}


NAME = {'type': 'string', 'minLength': 1, 'maxLength': 160}
ID = {'type': 'string', 'pattern': r'^[A-Za-z][A-Za-z0-9_-]{0,63}$'}
SHA = {'type': 'string', 'pattern': r'^[0-9a-f]{64}$'}
BOOL = {'type': 'boolean'}
VEC = arr(num(-1000, 1000), 3, 3)
COLOR = arr(num(0, 1), 3, 3)
FILE = obj({'file': NAME, 'expected_sha256': SHA}, ('file', 'expected_sha256'))
LINK_FIELDS = {'receivers': arr(NAME, 0, 256), 'blockers': arr(NAME, 0, 256),
               'receivers_mode': {'enum': ['INCLUDE', 'EXCLUDE']},
               'blockers_mode': {'enum': ['INCLUDE', 'EXCLUDE']}}
NATIVE_CREATE_FIELDS = {'normalize': BOOL, 'spread_deg': num(0, 180),
                        'diffuse_factor': num(0, 1), 'specular_factor': num(0, 1)}
LIGHT_FIELDS = {'energy': num(0, 1e8), 'color': COLOR, 'location': VEC,
                'size': num(.00001, 10000), 'size_y': num(.00001, 10000),
                'enabled': BOOL, 'use_shadow': BOOL, **LINK_FIELDS}
LIGHT = obj({'id': ID, 'role': NAME, 'type': {'enum': ['AREA', 'POINT', 'SPOT', 'SUN']},
             **LIGHT_FIELDS, **NATIVE_CREATE_FIELDS, 'shape': {'enum': ['SQUARE', 'DISK', 'RECTANGLE', 'ELLIPSE']},
             'aim': VEC, 'angle_deg': num(.001, 180), 'spot_size_deg': num(.001, 179.9),
             'spot_blend': num(0, 1)}, ('id', 'role', 'type', 'location', 'energy', 'color'))
WORLD = obj({'kind': {'enum': ['constant', 'sky', 'hdri']}, 'color': COLOR,
             'strength': num(0, 10000), 'file': NAME, 'rotation_deg': num(-36000, 36000),
             'sky_type': NAME, 'sun_rotation_deg': num(-36000, 36000),
             'sun_elevation_deg': num(-90, 90),
             'sun_disc': {**BOOL, 'description': 'Native Sky sun disc is Cycles-only; Eevee does not support this sun-disc control. Use an explicit SUN light for direct light.'}}, ('kind',))
PATCH = obj({'id': ID, 'values': obj(LIGHT_FIELDS)}, ('id', 'values'))
BOARD_FIELDS = {'location': VEC, 'rotation_deg': arr(num(-36000, 36000), 3, 3),
                'size': arr(num(.00001, 10000), 2, 2), 'color': COLOR,
                'roughness': num(0, 1), 'enabled': BOOL}
BOARD = obj({'id': ID, 'kind': {'enum': ['reflector', 'flag']}, **BOARD_FIELDS},
            ('id', 'kind', 'location', 'rotation_deg', 'size'))
BOARD_PATCH = obj({'id': ID, 'values': obj(BOARD_FIELDS)}, ('id', 'values'))
MANIFEST_SCHEMA = obj({'version': {'const': SCHEMA_VERSION}, 'scene': NAME, 'view_layer': NAME,
    'frame': {'type': 'integer', 'minimum': 1, 'maximum': 100000}, 'rig_id': ID,
    'action': {'enum': ['create', 'patch', 'restore']}, 'targets': arr(NAME, 0, 64),
    'mode': {'enum': ['relight', 'adapt']}, 'anchor': {'enum': ['world', 'target', 'camera']},
    'lights': arr(LIGHT, 0, 32), 'world': WORLD, 'exposure': num(-10, 10),
    'eevee': obj({'use_raytracing': BOOL}, ('use_raytracing',)),
    'background': obj({'color': COLOR, 'roughness': num(0, 1), 'size': num(.1, 1000)}, ('color',)),
    'keep_lights': arr(NAME, 0, 256), 'patches': arr(PATCH, 0, 32),
    'boards': arr(BOARD, 0, 32), 'board_patches': arr(BOARD_PATCH, 0, 32),
    'conflict': {'enum': ['error', 'overwrite']}}, ('version', 'scene', 'view_layer', 'rig_id', 'action'))
DEVICE = obj({'backend': {'enum': ['CPU', 'CUDA', 'OPTIX', 'GRAPHICS']}, 'id': NAME}, ('backend',))
PREVIEW_SCHEMA = obj({'engines': arr({'enum': ['CYCLES', 'BLENDER_EEVEE']}, 1, 2),
    'device': DEVICE, 'width': {'type': 'integer', 'minimum': 16, 'maximum': 2048},
    'height': {'type': 'integer', 'minimum': 16, 'maximum': 2048},
    'samples': {'type': 'integer', 'minimum': 1, 'maximum': 256},
    'frames': arr({'type': 'integer', 'minimum': 1, 'maximum': 100000}, 1, 24),
    'cameras': arr(NAME, 1, 24), 'denoise': BOOL}, ('engines', 'device', 'width', 'height', 'samples'))
RUN_PARAMS_SCHEMA = obj({'file': NAME, 'expected_sha256': SHA, 'resources': arr(FILE, 0, 256),
    'manifest': MANIFEST_SCHEMA, 'preview': PREVIEW_SCHEMA}, ('file', 'expected_sha256', 'resources', 'manifest'))
INSPECT_PARAMS_SCHEMA = obj({'file': NAME, 'expected_sha256': SHA, 'resources': arr(FILE, 0, 256),
    'scene': NAME, 'rig_id': ID}, ('file', 'expected_sha256', 'resources', 'scene'))


def _validate(value, schema, path='$'):
    def fail(message):
        raise WorkflowError('INVALID_REQUEST', path + ': ' + message)
    if 'const' in schema and value != schema['const']:
        fail('wrong constant')
    if 'enum' in schema and value not in schema['enum']:
        fail('unsupported value')
    kind = schema.get('type')
    if kind == 'object':
        if not isinstance(value, dict): fail('expected object')
        unknown = set(value) - set(schema['properties'])
        if unknown: fail('unknown fields: ' + ', '.join(sorted(unknown)))
        if set(schema.get('required', ())) - set(value): fail('missing required fields')
        for key, item in value.items(): _validate(item, schema['properties'][key], path + '.' + key)
    elif kind == 'array':
        if not isinstance(value, list): fail('expected array')
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 100000): fail('array length outside bounds')
        for i, item in enumerate(value): _validate(item, schema['items'], path + '[' + str(i) + ']')
    elif kind == 'string':
        if not isinstance(value, str): fail('expected string')
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 100000): fail('string length outside bounds')
        if '\x00' in value: fail('NUL is not allowed')
        if 'pattern' in schema and not re.fullmatch(schema['pattern'], value): fail('invalid string format')
    elif kind in ('number', 'integer'):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value): fail('expected finite number')
        if kind == 'integer' and not isinstance(value, int): fail('expected integer')
        if not schema.get('minimum', -math.inf) <= value <= schema.get('maximum', math.inf): fail('number outside bounds')
    elif kind == 'boolean' and not isinstance(value, bool):
        fail('expected boolean')
    return value


def _absolute(filename):
    path = Path(filename)
    if not path.is_absolute(): raise WorkflowError('INVALID_REQUEST', 'Absolute path required: ' + filename)
    return str(path.resolve())


def _unique(values, field):
    if len(values) != len(set(values)): raise WorkflowError('INVALID_REQUEST', 'Duplicate ' + field)


def normalize_manifest(value):
    value = deepcopy(value)
    _validate(value, MANIFEST_SCHEMA, 'manifest')
    value.setdefault('conflict', 'error')
    action = value['action']
    if action == 'create':
        if not value.get('targets') or not value.get('lights') or 'mode' not in value or 'anchor' not in value:
            raise WorkflowError('INVALID_REQUEST', 'create requires targets, lights, mode and anchor')
        if value.get('patches') or value.get('board_patches'): raise WorkflowError('INVALID_REQUEST', 'create does not accept patches')
    else:
        forbidden = {'lights', 'boards', 'world', 'exposure', 'background', 'keep_lights', 'mode', 'anchor', 'eevee'} & set(value)
        if forbidden: raise WorkflowError('INVALID_REQUEST', action + ' cannot change: ' + ', '.join(sorted(forbidden)))
        if action == 'patch' and not (value.get('patches') or value.get('board_patches')): raise WorkflowError('INVALID_REQUEST', 'patch requires nonempty patches or board_patches')
        if action == 'restore' and (value.get('patches') or value.get('board_patches')): raise WorkflowError('INVALID_REQUEST', 'restore does not accept patches')
    value.setdefault('targets', [])
    _unique(value['targets'], 'targets')
    _unique(value.get('keep_lights', []), 'keep_lights')
    _unique([row['id'] for row in value.get('lights', [])], 'light id')
    _unique([row['id'] for row in value.get('patches', [])], 'patch id')
    _unique([row['id'] for row in value.get('boards', [])], 'board id')
    _unique([row['id'] for row in value.get('board_patches', [])], 'board patch id')
    for light in value.get('lights', []):
        light.setdefault('enabled', True)
        light.setdefault('use_shadow', True)
        light.setdefault('aim', [0, 0, 0])
        area_only = {'shape', 'size_y', 'spread_deg'} & set(light)
        if area_only and light['type'] != 'AREA': raise WorkflowError('INVALID_REQUEST', 'shape/size_y require AREA')
        if 'size_y' in light and light.get('shape', 'DISK') not in ('RECTANGLE', 'ELLIPSE'):
            raise WorkflowError('INVALID_REQUEST', 'size_y requires RECTANGLE/ELLIPSE AREA')
        if 'angle_deg' in light and light['type'] != 'SUN': raise WorkflowError('INVALID_REQUEST', 'angle_deg requires SUN')
        if {'spot_size_deg', 'spot_blend'} & set(light) and light['type'] != 'SPOT': raise WorkflowError('INVALID_REQUEST', 'spot controls require SPOT')
        if 'size' in light and light['type'] == 'SUN': raise WorkflowError('INVALID_REQUEST', 'SUN uses angle_deg, not size')
    for patch in value.get('patches', []):
        if not patch['values']: raise WorkflowError('INVALID_REQUEST', 'Empty patch values')
    for row in [*value.get('lights', []), *[p['values'] for p in value.get('patches', [])]]:
        for key in ('receivers', 'blockers'):
            if key in row: _unique(row[key], key)
    for row in value.get('lights', []):
        for key in ('receivers', 'blockers'):
            if key + '_mode' in row and key not in row:
                raise WorkflowError('INVALID_REQUEST', key + '_mode requires corresponding list at create')
            if key in row: row.setdefault(key + '_mode', 'INCLUDE')
    for row in [*value.get('lights', []), *[p['values'] for p in value.get('patches', [])]]:
        for key in ('receivers', 'blockers'):
            if key in row and not row[key] and row.get(key + '_mode', 'INCLUDE') != 'INCLUDE':
                raise WorkflowError('INVALID_REQUEST', 'Empty ' + key + ' cancels linking; EXCLUDE mode requires members')
    for board in value.get('boards', []):
        board.setdefault('color', [1, 1, 1] if board['kind'] == 'reflector' else [0, 0, 0])
        board.setdefault('roughness', .8)
        board.setdefault('enabled', True)
    for patch in value.get('board_patches', []):
        if not patch['values']: raise WorkflowError('INVALID_REQUEST', 'Empty board patch values')
    world = value.get('world')
    if world:
        world.setdefault('strength', .2)
        if world['kind'] == 'hdri':
            if 'file' not in world: raise WorkflowError('INVALID_REQUEST', 'hdri requires file')
            world['file'] = _absolute(world['file'])
        elif 'file' in world: raise WorkflowError('INVALID_REQUEST', 'Only hdri accepts file')
        if world['kind'] != 'sky' and 'sky_type' in world: raise WorkflowError('INVALID_REQUEST', 'sky_type requires sky')
        solar = {'sun_rotation_deg', 'sun_elevation_deg', 'sun_disc'} & set(world)
        if solar and world['kind'] != 'sky': raise WorkflowError('INVALID_REQUEST', 'Sun controls require sky World')
        if world['kind'] == 'sky':
            if 'rotation_deg' in world:
                raise WorkflowError('INVALID_REQUEST', 'Sky rotation_deg is unsupported; use sun_rotation_deg explicitly')
            world.setdefault('sky_type', 'MULTIPLE_SCATTERING')
            if solar and world['sky_type'] not in ('SINGLE_SCATTERING', 'MULTIPLE_SCATTERING'):
                raise WorkflowError('INVALID_REQUEST', 'Explicit sun angle/disc controls require SINGLE_SCATTERING or MULTIPLE_SCATTERING sky')
        if world['kind'] != 'constant' and 'color' in world: raise WorkflowError('INVALID_REQUEST', 'color requires constant World')
        if world['kind'] == 'constant' and 'rotation_deg' in world: raise WorkflowError('INVALID_REQUEST', 'constant World has no rotation')
    return value


def normalize_preview(value):
    value = deepcopy(value)
    _validate(value, PREVIEW_SCHEMA, 'preview')
    _unique(value['engines'], 'engines')
    for field in ('frames', 'cameras'):
        if field in value: _unique(value[field], field)
    if len(value['engines']) * len(value.get('frames', [1])) * len(value.get('cameras', ['active'])) > 24:
        raise WorkflowError('INVALID_REQUEST', 'Preview exceeds 24 outputs')
    device = value['device']
    if (device['backend'] in ('OPTIX', 'CUDA')) != ('id' in device):
        raise WorkflowError('INVALID_REQUEST', 'CUDA/OPTIX require exact id; CPU/GRAPHICS have no id')
    if 'CYCLES' in value['engines'] and device['backend'] == 'GRAPHICS':
        raise WorkflowError('INVALID_REQUEST', 'Cycles requires explicit CPU/CUDA/OPTIX device')
    value.setdefault('denoise', False)
    return value


def _inputs(value, schema):
    value = deepcopy(value)
    _validate(value, schema, 'params')
    value['file'] = _absolute(value['file'])
    if Path(value['file']).suffix.lower() != '.blend': raise WorkflowError('INVALID_REQUEST', 'Source must be .blend')
    for row in value['resources']: row['file'] = _absolute(row['file'])
    _unique([row['file'].casefold() for row in value['resources']], 'resource path')
    return value


def normalize_params(value):
    value = _inputs(value, RUN_PARAMS_SCHEMA)
    value['manifest'] = normalize_manifest(value['manifest'])
    if 'preview' in value: value['preview'] = normalize_preview(value['preview'])
    world = value['manifest'].get('world', {})
    if world.get('kind') == 'hdri' and world['file'].casefold() not in {r['file'].casefold() for r in value['resources']}:
        raise WorkflowError('INVALID_REQUEST', 'HDRI must be declared with resource SHA')
    return value


def normalize_inspect_params(value):
    return _inputs(value, INSPECT_PARAMS_SCHEMA)
