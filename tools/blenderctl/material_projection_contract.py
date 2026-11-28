"""Host-safe public contract for isolated projection bakes.

The Blender add-on owns the Cycles bake and manifest semantics. This adapter
owns the strict public envelope and can be imported without Blender.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

from protocol import Failure, digest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from material_workflow_addon.contract import FILE, NAME, SHA, WorkflowError, _absolute, _validate, num, obj
from material_workflow_addon import contract as _shared_contract

COMMANDS = ('material.projection-bake',)
OPERATION = 'material.projection-bake'
MAX_PAIRS = 16
MAX_SOURCES = 32
CONTEXT = obj({'scene': NAME, 'view_layer': NAME,
               'frame': {'type': 'integer', 'minimum': 1, 'maximum': 100000, 'default': 1}},
              ('scene', 'view_layer'))
SETTINGS = obj({
    'resolution': {'type': 'integer', 'minimum': 64, 'maximum': 2048, 'default': 1024},
    'extrusion': num(0, 10, 0.01),
    'distance': num(0.000001, 1000, 0.02),
    'ao_distance': num(0.000001, 1000, 0.02),
    'curvature_radius': num(0.000001, 100, 0.005),
    'samples': {'type': 'integer', 'minimum': 1, 'maximum': 128, 'default': 16},
    'margin': {'type': 'integer', 'minimum': 0, 'maximum': 256, 'default': 8},
}, default={})
PAIR = obj({
    'target': NAME,
    'sources': {'type': 'array', 'items': NAME, 'minItems': 1, 'maxItems': MAX_SOURCES},
    'uv_layer': NAME,
    'cage': {'anyOf': [NAME, {'type': 'null'}], 'default': None},
    'settings': SETTINGS,
}, ('target', 'sources', 'uv_layer'))
PAIRS = {'type': 'array', 'items': PAIR, 'minItems': 1, 'maxItems': MAX_PAIRS}

OUTPUT_MANIFEST_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'title': 'Material Workflow projection manifest mw-projection-1.0', 'type': 'object',
    'properties': {'schema': {'const': 'mw-projection-1.0'}, 'status': {'const': 'complete'},
                   'blender': {'type': 'string'}, 'target': NAME,
                   'sources': {'type': 'array', 'items': NAME},
                   'uv_layer': NAME, 'identity': {'type': 'object'},
                   'uv_check': {'type': 'object'},
                   'maps': {'type': 'object'}, 'witness': {'type': 'object'},
                   'settings': {'type': 'object'}},
    'required': ['schema', 'status', 'target', 'sources', 'uv_layer', 'maps', 'settings'],
}
MAP_ENTRY = {'type': 'object', 'properties': {
    'file': {'type': 'string'}, 'sha256': SHA, 'bytes': {'type': 'integer', 'minimum': 1},
    'width': {'type': 'integer', 'minimum': 64}, 'height': {'type': 'integer', 'minimum': 64},
    'bit_depth': {'type': 'string'}, 'color_space': {'type': 'string'},
    'format': {'type': 'string'}, 'decoded': {'type': 'boolean'}, 'meaning': {'type': 'string'},
}, 'required': ['file', 'sha256', 'bytes', 'width', 'height', 'bit_depth', 'color_space']}
BAKED_SETTINGS = obj({
    'size': SETTINGS['properties']['resolution'],
    **{name: SETTINGS['properties'][name] for name in
       ('extrusion', 'distance', 'ao_distance', 'curvature_radius', 'samples', 'margin')},
    'ao_scope': {'const': 'hit source object'},
}, ('size', 'extrusion', 'distance', 'ao_distance', 'curvature_radius', 'samples', 'margin', 'ao_scope'))
PAIR_REPORT = obj({
    'index': {'type': 'integer', 'minimum': 1},
    'target': NAME, 'sources': {'type': 'array', 'items': NAME}, 'uv_layer': NAME,
    'cage': {'anyOf': [NAME, {'type': 'null'}]},
    'settings': BAKED_SETTINGS,
    'manifest': {'type': 'object'}, 'identity': {'type': 'object'},
    'uv_check': {'type': 'object'}, 'witness': {'type': 'object'},
    'maps': {'type': 'object', 'additionalProperties': MAP_ENTRY},
}, ('index', 'target', 'sources', 'uv_layer', 'settings', 'manifest', 'maps'))
REPORT_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'title': 'Projection bake report 1.0', 'type': 'object',
    'properties': {'report_version': {'const': '1.0'}, 'operation': {'const': OPERATION},
                   'status': {'enum': ['running', 'pass', 'failed']},
                   'source': FILE, 'source_saved': {'const': False}, 'context': deepcopy(CONTEXT),
                   'environment': {'type': 'object'}, 'checks': {'type': 'object'},
                   'pairs': {'type': 'array', 'items': PAIR_REPORT},
                   'outputs': {'type': 'array'}, 'limitations': {'type': 'array'},
                   'error': {'type': 'object'}},
    'required': ['report_version', 'operation', 'status', 'source', 'source_saved',
                 'context', 'checks', 'pairs', 'outputs'],
}


def _failure(error):
    if isinstance(error, Failure):
        return error
    return Failure(getattr(error, 'code', 'INVALID_REQUEST'), str(error))


def params_schema():
    return obj({'file': {'type': 'string', 'minLength': 1}, 'expected_sha256': SHA,
                'context': CONTEXT, 'pairs': PAIRS},
               ('file', 'expected_sha256', 'context', 'pairs'))


def public_request_schema():
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema',
            'title': OPERATION + ' request 1.0',
            **obj({'schema_version': {'const': '1.0'}, 'command': {'const': OPERATION},
                   'params': params_schema()}, ('schema_version', 'command', 'params'))}


def normalize_params(value):
    """Strictly normalize one source/context and 1..16 target-source pairs."""
    if not isinstance(value, dict):
        raise Failure('INVALID_REQUEST', 'Projection bake params must be an object')
    try:
        result = _validate(deepcopy(value), params_schema(), 'params')
        result['file'] = _absolute(result['file'])
        if Path(result['file']).suffix.lower() != '.blend':
            raise WorkflowError('INVALID_REQUEST', 'Expected .blend source')
        targets = set()
        for index, pair in enumerate(result['pairs'], 1):
            target = pair['target']
            key = target.casefold()
            if key in targets:
                raise WorkflowError('INVALID_REQUEST', 'pairs[%d].target is duplicated' % index)
            targets.add(key)
            names = [target, *pair['sources'], *([pair['cage']] if pair.get('cage') else [])]
            if len({name.casefold() for name in names}) != len(names):
                raise WorkflowError('INVALID_REQUEST',
                                    'pairs[%d] requires distinct target, sources and cage' % index)
            settings = pair['settings']
            if settings['margin'] > settings['resolution'] // 8:
                raise WorkflowError('INVALID_REQUEST',
                                    'pairs[%d].settings.margin exceeds one eighth of resolution' % index)
        return result
    except WorkflowError as error:
        raise _failure(error) from error


def input_documents(params):
    """The projection bake reads mesh objects only; no external image is consumed."""
    return []


def verify_input_hashes(params):
    """Defence in depth for direct adapter callers; runner also holds FileGuards."""
    path = Path(params['file'])
    if not path.is_file():
        raise Failure('NOT_FOUND', 'Projection source not found: ' + str(path))
    if digest(path) != params['expected_sha256']:
        raise Failure('CONFLICT', 'Projection source SHA differs: ' + str(path))


def schema_descriptor(schema):
    raw = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    source = Path(__file__).resolve()
    shared = Path(_shared_contract.__file__).resolve()
    return {'kind': 'inline', 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
            'contract': {'file': str(source), 'sha256': digest(source)},
            'shared_contract': {'file': str(shared), 'sha256': digest(shared)}}


def describe(include_schema=False, section=None):
    request = public_request_schema()
    params = request['properties']['params']
    row = {'command': OPERATION, 'execution': 'standalone_job',
           'purpose': 'bake raw Normal/Curvature/AO projection maps per target/source pair in one owned worker',
           'request_schema': schema_descriptor(request),
           'manifest_schema': schema_descriptor(OUTPUT_MANIFEST_SCHEMA),
           'report_schema': schema_descriptor(REPORT_SCHEMA),
           'required_params': params['required'],
           'limits': {'pairs': MAX_PAIRS, 'sources_per_pair': MAX_SOURCES,
                      'resolution': [64, 2048], 'samples': [1, 128],
                      'output_scope': 'owned job raw directory only', 'tiles': 1,
                      'source_write': 'never saved or modified',
                      'external_engine_validation': 'not_provided',
                      'aesthetic_approval': 'manual'}}
    if include_schema:
        row['schema'] = request
    if section:
        if section == 'manifest':
            selected = deepcopy(OUTPUT_MANIFEST_SCHEMA)
        elif section == 'pairs':
            selected = deepcopy(PAIRS)
        elif section == 'settings':
            selected = deepcopy(SETTINGS)
        elif section in ('context', 'target'):
            selected = deepcopy(CONTEXT if section == 'context' else PAIR['properties']['target'])
        else:
            raise Failure('INVALID_REQUEST', 'Section unavailable: ' + section)
        row.update(section=section, schema=selected)
    return row
