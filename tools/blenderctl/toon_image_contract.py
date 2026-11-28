# SPDX-License-Identifier: GPL-3.0-or-later
"""Audit candidate: pure, unregistered, saved-disk single-frame image contract.

No source access, identity computation, Blender import or job creation occurs.
All successful API return values are deeply immutable independent snapshots.
JSON Schema describes structure; mandatory semantic checks remain in normalize.
"""
import math
import ntpath
from pathlib import PureWindowsPath
import re
from types import MappingProxyType


class ImageContractError(ValueError):
    def __init__(self, path, message, code='INVALID_REQUEST'):
        super().__init__(message)
        self.code = code
        self.path = path


def _freeze(value):
    if type(value) is dict:
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze(item) for item in value)
    return value


SOURCE_HARD_LIMITS = _freeze(dict(max_objects=1024, max_vertices=1048576,
    max_triangles=1048576, max_input_bytes=536870912))
PIPELINE_HARD_LIMITS = _freeze(dict(max_pairs=20000000, max_eligible=100000,
    max_narrow_pairs=30000000, max_cells=2000000, max_output_bytes=4294967296,
    max_seconds=3600, max_rational_bits=8192, max_pixels=16777216))


def _obj(properties, required=None):
    return dict(type='object', additionalProperties=False, properties=properties,
                required=list(properties) if required is None else required)


def _array(items, low, high, unique=False):
    result = dict(type='array', items=items, minItems=low, maxItems=high)
    if unique:
        result['uniqueItems'] = True
    return result


def _integer(low, high):
    return dict(type='integer', minimum=low, maximum=high)


def _build_params_schema():
    name = dict(type='string', minLength=1, maxLength=63,
                pattern=r'^[^\x00-\x1f\x7f]+$')
    path = dict(type='string', minLength=1, maxLength=1024,
                pattern=r'^[^\x00-\x1f\x7f]+$', format='absolute-windows-path')
    sha = dict(type='string', pattern='^[0-9a-f]{64}$')
    file = _obj(dict(file=path, expected_sha256=sha))
    color = _array(dict(type='number', minimum=0, maximum=1), 4, 4)
    policy = _obj(dict(model=dict(const='opaque_double_sided_single_group.v1'),
        self_intersection=dict(type='boolean'), visible_enabled=dict(type='boolean'),
        hidden_enabled=dict(type='boolean')))
    style = _obj(dict(width_px=dict(type='number', exclusiveMinimum=0, maximum=256),
        feather_px=dict(type='number', minimum=0, maximum=256),
        samples_per_axis=dict(type='integer', enum=[1, 2, 4]),
        visible_color=color, hidden_color=color))
    budgets = _obj(dict(
        source=_obj({key: _integer(0, cap) for key, cap in SOURCE_HARD_LIMITS.items()}, []),
        pipeline=_obj({key: _integer(0, cap) for key, cap in PIPELINE_HARD_LIMITS.items()}, [])), [])
    recipe = _obj(dict(schema=dict(const='toon-image-recipe.v1'),
        profile=dict(const='intersection-image.v1'), scene=name, view_layer=name,
        frame=_integer(-1048574, 1048574), camera_name=name,
        occluder_object_names=_array(name, 1, SOURCE_HARD_LIMITS['max_objects'], True),
        member_object_names=_array(name, 0, SOURCE_HARD_LIMITS['max_objects'], True),
        policy=policy, style=style, budgets=budgets))
    result = _obj(dict(file=path, expected_sha256=sha, resources=_array(file, 0, 100),
        implementation_sha256=sha, source_mode=dict(const='SAVED_DISK_V1'), recipe=recipe))
    result.update({'$schema': 'https://json-schema.org/draft/2020-12/schema',
        'title': 'toon-image.run params; audit candidate only',
        'description': 'Structural schema only. normalize_params semantic checks are mandatory.',
        'x-semantic-checks': [
            'Only native dict/list/string/bool/int/float inputs; exact int rejects bool and float.',
            'Strings must contain Unicode scalar values encodable as strict UTF-8; isolated surrogates are rejected.',
            'Numbers must be finite; names must contain a non-whitespace character.',
            'Paths are lexical Windows drive/UNC absolute; device namespaces are rejected.',
            'Source suffix is .blend, case-insensitive.',
            'Resource paths are lexically unique, and cannot alias source under ntpath.normcase/normpath.',
            'member_object_names is a lexical, case-sensitive subset of occluder_object_names.',
            'No filesystem, resource closure, SHA identity, object, camera or resolution verification.']})
    return _freeze(result)


_PARAMS = _build_params_schema()
_REQUEST = _freeze(_obj(dict(schema_version=dict(const='1.0'),
    command=dict(const='toon-image.run'), params={})))
# Reuse immutable schemas directly; no exposed mutable schema backing remains.
_REQUEST = MappingProxyType({**_REQUEST,
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'title': 'toon-image.run request; audit candidate only',
    'properties': MappingProxyType({**_REQUEST['properties'], 'params': _PARAMS})})


def _fail(path, message):
    raise ImageContractError(path, message)


def _validate(value, schema, path):
    kind = schema.get('type')
    if kind == 'object':
        if type(value) is not dict or any(type(key) is not str for key in value):
            _fail(path, 'Native object with string keys required')
        if not set(schema['required']) <= set(value) <= set(schema['properties']):
            _fail(path, 'Unknown or missing fields')
        for key, item in value.items():
            _validate(item, schema['properties'][key], path + '.' + key)
    elif kind == 'array':
        if type(value) is not list:
            _fail(path, 'Native array required')
        if not schema['minItems'] <= len(value) <= schema['maxItems']:
            _fail(path, 'Array length outside contract')
        for index, item in enumerate(value):
            _validate(item, schema['items'], path + '[' + str(index) + ']')
        # uniqueItems is used exclusively for bounded name lists.
        if schema.get('uniqueItems') and len(set(value)) != len(value):
            _fail(path, 'Duplicate names')
    elif kind == 'string':
        if type(value) is not str:
            _fail(path, 'Native string required')
        try:
            value.encode('utf-8', errors='strict')
        except UnicodeEncodeError:
            _fail(path, 'Strict UTF-8 string required; isolated surrogates are invalid')
        if len(value) < schema.get('minLength', 0) or len(value) > schema.get('maxLength', 10**9):
            _fail(path, 'String length outside contract')
        if 'pattern' in schema and re.fullmatch(schema['pattern'], value) is None:
            _fail(path, 'String pattern mismatch')
        if not value.strip():
            _fail(path, 'Blank strings are not names or paths')
        if schema.get('format') == 'absolute-windows-path':
            if value.replace('/', '\\').startswith(('\\\\?\\', '\\\\.\\')):
                _fail(path, 'Device namespace paths are outside the first slice')
            if not PureWindowsPath(value).is_absolute():
                _fail(path, 'Lexical Windows drive/UNC absolute path required')
    elif kind == 'boolean':
        if type(value) is not bool:
            _fail(path, 'Explicit boolean required')
    elif kind in ('integer', 'number'):
        valid_type = type(value) is int if kind == 'integer' else type(value) in (int, float)
        if not valid_type:
            _fail(path, 'Exact integer required' if kind == 'integer' else 'Native number required')
        if type(value) is float and not math.isfinite(value):
            _fail(path, 'Finite number required')
        if 'minimum' in schema and value < schema['minimum']:
            _fail(path, 'Number below minimum')
        if 'exclusiveMinimum' in schema and value <= schema['exclusiveMinimum']:
            _fail(path, 'Number must exceed minimum')
        if 'maximum' in schema and value > schema['maximum']:
            _fail(path, 'Number above maximum')
    if 'const' in schema and (type(value) is not type(schema['const']) or value != schema['const']):
        _fail(path, 'Unsupported constant or version')
    if 'enum' in schema and value not in schema['enum']:
        _fail(path, 'Unsupported enumerated value')


def _semantic_params(value, path):
    if PureWindowsPath(value['file']).suffix.lower() != '.blend':
        _fail(path + '.file', 'Source must have .blend suffix')
    # Pure lexical aliases only: no resolve, stat, open, symlink or hardlink claim.
    seen = {ntpath.normcase(ntpath.normpath(value['file']))}
    for index, row in enumerate(value['resources']):
        key = ntpath.normcase(ntpath.normpath(row['file']))
        if key in seen:
            _fail(path + '.resources[' + str(index) + '].file', 'Duplicate lexical input path')
        seen.add(key)
    recipe = value['recipe']
    if not set(recipe['member_object_names']) <= set(recipe['occluder_object_names']):
        _fail(path + '.recipe.member_object_names', 'Members must be a lexical subset of occluders')


def normalize_params(value):
    """Validate only declarations; preserve exact explicit values, then deep-freeze."""
    _validate(value, _PARAMS, '$.params')
    _semantic_params(value, '$.params')
    return _freeze(value)


def normalize_request(value):
    _validate(value, _REQUEST, '$')
    _semantic_params(value['params'], '$.params')
    return _freeze(value)


def run_params_schema():
    return _PARAMS


def run_request_schema():
    return _REQUEST


def describe(operation=None, schema=False):
    """Candidate metadata only; deliberately no implementation pin or job envelope."""
    if operation is not None and (type(operation) is not str or operation != 'run'):
        _fail('$.operation', 'Only run describe is in this candidate')
    if type(schema) is not bool:
        _fail('$.schema', 'Explicit boolean required')
    result = dict(namespace='toon-image', registration_status='candidate_not_registered',
        production_execution_available=False, runtime_verification='not_run',
        contract_scope='pure_parameters_only', operations=[dict(operation='run', command='toon-image.run')],
        source_mode='SAVED_DISK_V1', profile='intersection-image.v1', one_frame=True,
        worker_requirements=['protected saved source and implementation pins',
            'complete resource closure and evaluated named mesh universe',
            'actual scene/view layer/frame/name binding and member IDs',
            'actual ORTHO camera and maximum dimension 2048',
            'worker/parent artifact integrity and acceptance'],
        schema_is_necessary_not_sufficient=True)
    if schema:
        result['params_schema'] = _PARAMS
    return _freeze(result)
