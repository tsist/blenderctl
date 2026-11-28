"""Pure depth prepare/inspect contract. No bpy, file IO, model or network work.

JSON Schemas describe structural/combination rules. normalize_* also enforces
cross-field resource identities using Windows absolute-path syntax, without
probing the filesystem. Host must reject declared extras not used by its actual
authority closure. Numerical decode and bundle authority remain Host work.
"""
import copy
import json
import math
import ntpath
import re


class DepthError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _need(condition, message):
    if not condition:
        raise DepthError('INVALID_REQUEST', message)


def _obj(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


def _const(value):
    return {'const': value}


TEXT = {'type': 'string', 'minLength': 1}
STATEMENT = {'type': 'string', 'minLength': 1, 'pattern': r'\S'}
ABSOLUTE = {'type': 'string', 'pattern': r'^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+(?:[\\/]|$))'}
SHA = {'type': 'string', 'pattern': r'^[0-9a-f]{64}$'}
FILE = _obj({'file': ABSOLUTE, 'expected_sha256': SHA})
SCALAR = {'oneOf': [
    _obj({'file': ABSOLUTE, 'expected_sha256': SHA, 'format': _const('NPY'), 'channel': _const(None)}),
    _obj({'file': ABSOLUTE, 'expected_sha256': SHA, 'format': _const('EXR'),
          'channel': {'anyOf': [{'type': 'null'}, TEXT]}}),
]}
POSITIVE = {'type': 'number', 'exclusiveMinimum': 0}
FINITE = {'type': 'number'}
JUMP = {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1}
NONE = _obj({'method': _const('none')})
EXCLUSION = {'anyOf': [{'type': 'null'}, SCALAR]}
PERSPECTIVE = _obj({'method': _const('central_perspective'), 'fx': POSITIVE, 'fy': POSITIVE,
                    'cx': FINITE, 'cy': FINITE, 'max_relative_jump': JUMP, 'boundary_exclusion': EXCLUSION})
HEIGHTFIELD = _obj({'method': _const('central_heightfield'),
                   'height_scale': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 10},
                   'max_height_jump': JUMP, 'boundary_exclusion': EXCLUSION})
NPY_VECTOR = _obj({'file': ABSOLUTE, 'expected_sha256': SHA, 'format': _const('NPY'), 'channel': _const(None)})
MATRIX3 = {'type': 'array', 'minItems': 3, 'maxItems': 3,
           'items': {'type': 'array', 'minItems': 3, 'maxItems': 3, 'items': FINITE}}
IMPORT_GEOMETRY = _obj({
    'method': _const('import_geometry'), 'data': NPY_VECTOR, 'normal_validity': NPY_VECTOR,
    'encoding': _const('signed-float32-XYZ'),
    'space': {'enum': ['world_Xright_Yforward_Zup', 'camera_Xright_Yup_Zbackward']},
    'world_to_camera': MATRIX3, 'surface_relation': {'enum': ['geometry_surface', 'shader_surface']},
    'source': _obj({'kind': _const('declared_external_normal'), 'statement': STATEMENT}),
    'fx': POSITIVE, 'fy': POSITIVE, 'cx': FINITE, 'cy': FINITE,
    'max_relative_jump': JUMP, 'depth_edit_policy': _const('rederive_affected_from_axial_z'),
})
PERCENTILE = _obj({'method': _const('percentile_near_one'),
                   'low': {'type': 'number', 'minimum': 0, 'maximum': 100},
                   'high': {'type': 'number', 'minimum': 0, 'maximum': 100}})
AXIAL = _obj({'measure': _const('axial_z'), 'near_order': _const('smaller_nearer'), 'units': _const('scene_unit')})
RELATIVE = _obj({'measure': _const('relative_score'),
                  'near_order': {'enum': ['smaller_nearer', 'larger_nearer']}, 'units': _const('unitless')})
RESOURCES = {'type': 'array', 'items': FILE, 'minItems': 1, 'uniqueItems': True}

PREPARE_SCHEMA = _obj({
    'depth_prepare_version': {'enum': ['1.0', '1.1']}, 'rgb': FILE, 'rgb_encoding': _const('display_sRGB_PNG8'),
    'depth': SCALAR,
    'domain': _obj({'width': {'type': 'integer', 'minimum': 4, 'maximum': 2048},
                    'height': {'type': 'integer', 'minimum': 4, 'maximum': 2048},
                    'pixel_aspect': _const([1, 1]), 'origin': _const('top-left'),
                    'pixel_center': _const([.5, .5]), 'row_direction': _const('down'),
                    'column_direction': _const('right')}),
    'semantics': {'oneOf': [AXIAL, RELATIVE]},
    'provenance': {'oneOf': [
        _obj({'kind': _const('declared_external'), 'statement': STATEMENT}),
        _obj({'kind': _const('verified_dav2_single_CG'), 'completion': FILE, 'quality': FILE}),
        _obj({'kind': _const('verified_public_dav2'), 'producer': FILE}),
    ]},
    'validity': {'oneOf': [
        _obj({'kind': _const('explicit_mask'), 'mask': SCALAR}),
        _obj({'kind': _const('all_frame_artistic_declared'), 'statement': STATEMENT}),
    ]},
    'normalization': {'oneOf': [NONE, PERCENTILE]},
    'edits': {'type': 'array', 'maxItems': 8,
              'items': _obj({'mask': SCALAR, 'scale': POSITIVE, 'offset': FINITE})},
    'normal': {'oneOf': [NONE, PERSPECTIVE, HEIGHTFIELD, IMPORT_GEOMETRY]},
    'budget': _obj({'max_pixels': {'type': 'integer', 'minimum': 1, 'maximum': 4194304},
                    'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 1200}}),
    'resources': RESOURCES,
})
PREPARE_SCHEMA['$schema'] = 'http://json-schema.org/draft-07/schema#'
PREPARE_SCHEMA['title'] = 'compositing.depth-prepare params v1.0 / additive import_geometry v1.1'
PREPARE_SCHEMA['allOf'] = [
    {'oneOf': [
        {'properties': {'semantics': AXIAL, 'normalization': NONE, 'normal': {'oneOf': [NONE, PERSPECTIVE, IMPORT_GEOMETRY]},
                        'validity': _obj({'kind': _const('explicit_mask'), 'mask': SCALAR})}},
        {'properties': {'semantics': RELATIVE, 'normalization': PERCENTILE, 'normal': {'oneOf': [NONE, HEIGHTFIELD]}}},
    ]},
    {'if': {'properties': {'validity': {'properties': {'kind': _const('all_frame_artistic_declared')}}}},
     'then': {'properties': {'semantics': RELATIVE, 'normal': HEIGHTFIELD}}},
    {'oneOf': [
        {'properties': {'depth_prepare_version': _const('1.0'), 'normal': {'oneOf': [NONE, PERSPECTIVE, HEIGHTFIELD]}}},
        {'properties': {'depth_prepare_version': _const('1.1'), 'normal': IMPORT_GEOMETRY}},
    ]},
]
PREPARE_SCHEMA['$comment'] = ('Reader rejects duplicate/nonfinite JSON and bool-as-number. Integers are exact Python int; '
                              'normalizer additionally checks low<high and direct-input resource path/SHA coverage. '
                              'Extra resources require actual Host authority use; not certified by this pure schema.')
INSPECT_SCHEMA = _obj({'depth_inspect_version': _const('1.0'), 'bundle': FILE, 'resources': RESOURCES})
INSPECT_SCHEMA['$schema'] = 'http://json-schema.org/draft-07/schema#'
INSPECT_SCHEMA['title'] = 'compositing.depth-inspect params v1.0'
INSPECT_SCHEMA['$comment'] = ('Strict reader/type and direct bundle resource coverage are semantic requirements; '
                              'Host dynamically verifies every extra authority resource is used.')


def _finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _finite_tree(value):
    if type(value) in (int, float):
        _need(_finite_number(value), 'JSON number must be finite and representable')
    elif type(value) is dict:
        _need(all(type(key) is str for key in value), 'Object keys must be strings')
        for item in value.values():
            _finite_tree(item)
    elif type(value) is list:
        for item in value:
            _finite_tree(item)
    else:
        _need(type(value) in (str, bool, type(None)), 'Non-JSON value')


def loads_strict(data):
    """Decode UTF-8 bytes or text with duplicate and all nonfinite rejection."""
    _need(type(data) in (bytes, str), 'JSON input must be UTF-8 bytes or text')
    def pairs(rows):
        result = {}
        for key, value in rows:
            _need(key not in result, 'Duplicate JSON field: ' + key)
            result[key] = value
        return result
    def bad(token):
        raise DepthError('INVALID_REQUEST', 'Nonfinite JSON token: ' + token)
    try:
        value = json.loads(data.decode('utf-8') if type(data) is bytes else data,
                           object_pairs_hook=pairs, parse_constant=bad)
        _finite_tree(value)
        return value
    except (ValueError, UnicodeError) as exc:
        raise DepthError('INVALID_REQUEST', 'Invalid strict JSON: ' + str(exc)) from exc


def _equal(actual, expected):
    # Unlike Python ==, const/enum never equate False with 0 or True with 1.
    if type(expected) in (int, float):
        return _finite_number(actual) and actual == expected
    if type(actual) is not type(expected):
        return False
    if type(expected) is dict:
        return set(actual) == set(expected) and all(_equal(actual[key], expected[key]) for key in expected)
    if type(expected) is list:
        return len(actual) == len(expected) and all(_equal(a, b) for a, b in zip(actual, expected))
    return actual == expected


def _matches(value, schema):
    try:
        _validate(value, schema)
    except DepthError:
        return False
    return True


def _validate(value, schema, path='$'):
    """Validate only the draft07 subset used by these exported contract Schemas."""
    if 'const' in schema:
        _need(_equal(value, schema['const']), path + ': const/type mismatch')
    if 'enum' in schema:
        _need(any(_equal(value, item) for item in schema['enum']), path + ': enum/type mismatch')
    if 'oneOf' in schema:
        _need(sum(_matches(value, child) for child in schema['oneOf']) == 1, path + ': oneOf mismatch')
    if 'anyOf' in schema:
        _need(any(_matches(value, child) for child in schema['anyOf']), path + ': anyOf mismatch')
    for child in schema.get('allOf', []):
        _validate(value, child, path)
    if 'if' in schema and _matches(value, schema['if']):
        _validate(value, schema.get('then', {}), path)
    kind = schema.get('type')
    checks = {'object': lambda: type(value) is dict, 'array': lambda: type(value) is list,
              'string': lambda: type(value) is str, 'integer': lambda: type(value) is int,
              'number': lambda: _finite_number(value), 'null': lambda: value is None}
    if kind:
        _need(kind in checks and checks[kind](), path + ': ' + str(kind) + ' required')
    if type(value) is dict:
        properties = schema.get('properties', {})
        _need(set(schema.get('required', [])) <= set(value), path + ': required fields missing')
        if schema.get('additionalProperties') is False:
            _need(set(value) <= set(properties), path + ': unknown fields')
        for key in value.keys() & properties.keys():
            _validate(value[key], properties[key], path + '/' + key)
    if type(value) is list:
        _need(len(value) >= schema.get('minItems', 0), path + ': too few items')
        if 'maxItems' in schema:
            _need(len(value) <= schema['maxItems'], path + ': too many items')
        if schema.get('uniqueItems'):
            _need(not any(_equal(value[a], value[b]) for a in range(len(value)) for b in range(a)), path + ': duplicate items')
        if 'items' in schema:
            for item in value:
                _validate(item, schema['items'], path + '/*')
    if type(value) is str:
        _need(len(value) >= schema.get('minLength', 0), path + ': string empty')
        if 'pattern' in schema:
            _need(re.search(schema['pattern'], value) is not None, path + ': string pattern mismatch')
    if _finite_number(value):
        for key, check in [('minimum', lambda v: value >= v), ('maximum', lambda v: value <= v),
                           ('exclusiveMinimum', lambda v: value > v), ('exclusiveMaximum', lambda v: value < v)]:
            if key in schema:
                _need(check(schema[key]), path + ': ' + key + ' violated')


def _path_key(file):
    _validate(file, ABSOLUTE)
    _need('\x00' not in file, 'NUL path forbidden')
    return ntpath.normcase(ntpath.normpath(file))


def _collect(value):
    """Only declared FILE/SCALAR objects; never open or expand proof documents."""
    if type(value) is dict:
        if set(value) in ({'file', 'expected_sha256'}, {'file', 'expected_sha256', 'format', 'channel'}):
            _validate(value, FILE if len(value) == 2 else SCALAR)
            return [{'file': value['file'], 'expected_sha256': value['expected_sha256']}]
        return [row for key, item in value.items() if key != 'resources' for row in _collect(item)]
    if type(value) is list:
        return [row for item in value for row in _collect(item)]
    return []


def _merge(rows):
    merged = {}
    for row in rows:
        key = _path_key(row['file'])
        if key in merged:
            _need(merged[key]['expected_sha256'] == row['expected_sha256'], 'Conflicting input path/SHA')
        else:
            merged[key] = copy.deepcopy(row)
    return merged


def _resource_coverage(params):
    direct = _merge(_collect(params))
    resources = params['resources']
    declared = _merge(resources)
    _need(len(declared) == len(resources), 'Duplicate canonical resource path')
    for key, row in direct.items():
        _need(key in declared and declared[key]['expected_sha256'] == row['expected_sha256'],
              'Missing or conflicting direct input resource')
    # Keep explicit additional FILEs. Host must match them to used authority inputs.


def normalize_prepare(value):
    """Return a deep copy; validate exact typed fields, combinations and resources."""
    _finite_tree(value)
    _validate(value, PREPARE_SCHEMA)
    if value['normalization']['method'] == 'percentile_near_one':
        _need(value['normalization']['low'] < value['normalization']['high'], 'Percentile requires low < high')
    if value['normal']['method'] == 'import_geometry':
        normal = value['normal']
        rotation = normal['world_to_camera']
        # Structure and finite real-number types have already been checked.
        for a in range(3):
            for b in range(3):
                expected = 1 if a == b else 0
                _need(abs(sum(rotation[a][i] * rotation[b][i] for i in range(3)) - expected) <= 1e-6,
                      'world_to_camera must be orthogonal')
                _need(abs(sum(rotation[i][a] * rotation[i][b] for i in range(3)) - expected) <= 1e-6,
                      'world_to_camera must be orthogonal')
        r = rotation
        determinant = (r[0][0] * (r[1][1] * r[2][2] - r[1][2] * r[2][1])
                       - r[0][1] * (r[1][0] * r[2][2] - r[1][2] * r[2][0])
                       + r[0][2] * (r[1][0] * r[2][1] - r[1][1] * r[2][0]))
        _need(math.isfinite(determinant) and abs(determinant - 1) <= 1e-6,
              'world_to_camera must be a proper rotation, determinant +1')
        if normal['space'] == 'camera_Xright_Yup_Zbackward':
            _need(all(rotation[a][b] == (1 if a == b else 0) for a in range(3) for b in range(3)),
                  'Camera-space imported normals require identity world_to_camera')
    _resource_coverage(value)
    return copy.deepcopy(value)


def normalize_inspect(value):
    """Pure structural/bundle resource check, not bundle qualification."""
    _finite_tree(value)
    _validate(value, INSPECT_SCHEMA)
    _resource_coverage(value)
    return copy.deepcopy(value)


def input_descriptors(params, operation, include_resources=False):
    """Return direct inputs (optionally plus declared resources), deduped.

    operation is 'depth-prepare' or 'depth-inspect' (optionally compositing. prefix).
    SCALAR projects to FILE. Provenance extracts only explicitly declared FILEs
    (two CG proofs or one public producer); no inferred proof expansion.
    include_resources=False supports Host's exact
    comparison after dynamic authority expansion; True is an explicit protection
    inventory and never a claim that every listed extra was actually used.
    """
    _need(type(operation) is str, 'Operation string required')
    _need(type(include_resources) is bool, 'include_resources must be bool')
    operation = operation.removeprefix('compositing.')
    _need(operation in ('depth-prepare', 'depth-inspect'), 'Unknown depth operation')
    normalized = normalize_prepare(params) if operation == 'depth-prepare' else normalize_inspect(params)
    rows = _collect(normalized)
    if include_resources:
        rows += normalized['resources']
    return list(_merge(rows).values())
