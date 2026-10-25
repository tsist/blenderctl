"""Host-safe public contracts for isolated procedural material jobs.

The add-on owns the manifest and export-settings semantics. This adapter owns
the public operation envelope; no Blender import is required for discovery.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

from protocol import Failure, digest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from material_workflow_addon.contract import FILE, NAME, SHA, WorkflowError, _absolute, _validate, obj

COMMANDS = ('material.procedural-run', 'material.procedural-export')
CONTEXT = obj({'scene': NAME, 'view_layer': NAME,
               'frame': {'type': 'integer', 'minimum': 1, 'maximum': 100000, 'default': 1}},
              ('scene', 'view_layer'))
TARGET = obj({'object': NAME, 'material_slot': {'type': 'integer', 'minimum': 0, 'maximum': 63, 'default': 0},
             'uv_layer': NAME}, ('object', 'uv_layer'))


def shared_contract():
    from material_workflow_addon import procedural_contract
    return procedural_contract


def _failure(error):
    if isinstance(error, Failure):
        return error
    return Failure(getattr(error, 'code', 'INVALID_REQUEST'), str(error))


def _base_schema():
    return {'file': {'type': 'string', 'minLength': 1}, 'expected_sha256': SHA,
            'resources': {'type': 'array', 'items': FILE, 'maxItems': 256}}


def params_schema(operation):
    shared = shared_contract()
    base = _base_schema()
    if operation == 'procedural-run':
        return obj({**base, 'manifest': deepcopy(shared.MANIFEST_SCHEMA),
                    'export': {'anyOf': [deepcopy(shared.EXPORT_SCHEMA), {'type': 'null'}]}},
                   ('file', 'expected_sha256', 'resources', 'manifest'))
    if operation == 'procedural-export':
        return obj({**base, 'context': deepcopy(CONTEXT), 'target': deepcopy(TARGET),
                    'export': deepcopy(shared.EXPORT_SCHEMA)},
                   ('file', 'expected_sha256', 'resources', 'context', 'target', 'export'))
    raise Failure('INVALID_REQUEST', 'Unknown procedural material operation: ' + str(operation))


def public_request_schema(operation):
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema',
            'title': 'material.' + operation + ' request 1.0',
            **obj({'schema_version': {'const': '1.0'}, 'command': {'const': 'material.' + operation},
                   'params': params_schema(operation)}, ('schema_version', 'command', 'params'))}


def normalize_params(value):
    """Strip the CLI-only export option before the shared run normalizer."""
    if not isinstance(value, dict):
        raise Failure('INVALID_REQUEST', 'Procedural run params must be an object')
    data = deepcopy(value)
    had_export = 'export' in data
    export = data.pop('export', None)
    try:
        shared = shared_contract()
        result = shared.normalize_params(data)
        if had_export:
            result['export'] = shared.normalize_export(export) if export is not None else None
        return result
    except WorkflowError as error:
        raise _failure(error) from error


def normalize_export_params(value):
    try:
        # Validate the outer shape independently: it intentionally has no authoring manifest.
        schema = obj({**_base_schema(), 'context': CONTEXT, 'target': TARGET,
                      'export': obj({})},
                     ('file', 'expected_sha256', 'resources', 'context', 'target', 'export'))
        # The shared validator rejects unknown settings; leave their shape to it.
        export = value.get('export') if isinstance(value, dict) else None
        if not isinstance(export, dict):
            raise WorkflowError('INVALID_REQUEST', 'params.export must be a settings object')
        outer = deepcopy(value)
        if isinstance(outer, dict) and 'export' in outer:
            outer['export'] = {}
        result = _validate(outer, schema, 'params')
        result['file'] = _absolute(result['file'])
        if Path(result['file']).suffix.lower() != '.blend':
            raise WorkflowError('INVALID_REQUEST', 'Expected .blend source')
        seen = set()
        for resource in result['resources']:
            resource['file'] = _absolute(resource['file'])
            key = str(Path(resource['file']).resolve()).casefold()
            if key in seen:
                raise WorkflowError('INVALID_REQUEST', 'Duplicate resource path')
            seen.add(key)
        result['export'] = shared_contract().normalize_export(export)
        return result
    except WorkflowError as error:
        raise _failure(error) from error


def input_documents(params):
    """Every external image/helper input must be an explicit resource descriptor."""
    return deepcopy(params['resources'])


def verify_input_hashes(params):
    """Defence in depth for direct adapter callers; runner also holds FileGuards."""
    for document in [{'file': params['file'], 'expected_sha256': params['expected_sha256']},
                     *input_documents(params)]:
        path = Path(document['file'])
        if not path.is_file():
            raise Failure('NOT_FOUND', 'Material input not found: ' + str(path))
        if digest(path) != document['expected_sha256']:
            raise Failure('CONFLICT', 'Material input SHA differs: ' + str(path))


REPORT_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'title': 'Procedural material report 1.0', 'type': 'object',
    'properties': {'report_version': {'const': '1.0'}, 'operation': {'enum': list(COMMANDS)},
                   'status': {'enum': ['running', 'pass', 'partial', 'failed']},
                   'source': FILE, 'source_saved': {'const': False},
                   'checks': {'type': 'object'}, 'outputs': {'type': 'array'},
                   'author': FILE, 'reconstructed': FILE, 'export': {'type': 'object'},
                   'error': {'type': 'object'}},
    'required': ['report_version', 'operation', 'status', 'source_saved', 'checks', 'outputs'],
}


def schema_descriptor(schema):
    raw = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    source = Path(__file__).resolve()
    shared = Path(shared_contract().__file__).resolve()
    return {'kind': 'inline', 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
            'contract': {'file': str(source), 'sha256': digest(source)},
            'shared_contract': {'file': str(shared), 'sha256': digest(shared)}}


def describe(operation, include_schema=False, section=None):
    request = public_request_schema(operation)
    params = request['properties']['params']
    manifest = deepcopy(shared_contract().MANIFEST_SCHEMA) if operation == 'procedural-run' else None
    row = {'command': 'material.' + operation, 'execution': 'standalone_job',
           'purpose': ('author a protected procedural material, save/reopen and render; optional PBR export'
                       if manifest else 'bake a managed procedural material, reconstruct from disk maps and reopen'),
           'request_schema': schema_descriptor(request),
           'manifest_schema': schema_descriptor(manifest) if manifest else None,
           'report_schema': schema_descriptor(REPORT_SCHEMA), 'required_params': params['required'],
           'limits': {'output_scope': 'owned job directory only', 'tiles': 1,
                      'external_engine_validation': 'not_provided', 'aesthetic_approval': 'manual'}}
    if manifest:
        choices = manifest.get('properties', {}).get('signals', {}).get('items', {}).get('anyOf', [])
        row['signal_kinds'] = [choice['properties']['type']['const'] for choice in choices]
        row['limits'].update(
            coordinate_order='upstream vector or declared space, then per-signal mapping',
            shader_numbers='float32; reject unrepresentable values and collapsed remap bounds before mutation')
    if include_schema:
        row['schema'] = request
    if section:
        if section == 'manifest' and manifest:
            selected = manifest
        elif section == 'export':
            selected = deepcopy(shared_contract().EXPORT_SCHEMA)
        elif manifest and section in manifest.get('properties', {}):
            selected = manifest['properties'][section]
        elif section in ('target', 'context'):
            selected = params['properties'][section]
        else:
            raise Failure('INVALID_REQUEST', 'Section unavailable: ' + section)
        row.update(section=section, schema=selected)
    return row
