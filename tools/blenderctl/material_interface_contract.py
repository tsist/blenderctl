# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-safe public request contracts shared by adapters and schema generation."""
from copy import deepcopy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from material_workflow_addon.contract import MANIFEST_SCHEMA, FILE, SHA, ID, NAME, obj, _validate, WorkflowError
from material_workflow_addon.templates import TEMPLATE_SCHEMA
from material_workflow_addon.batch_contract import BATCH_PARAMS_SCHEMA

RUN_PUBLIC_PARAMS_SCHEMA = obj({
    'file': {'type': 'string', 'minLength': 1}, 'expected_sha256': SHA,
    'manifest': MANIFEST_SCHEMA,
    'resources': {'type': 'array', 'items': FILE, 'maxItems': 256},
    'template': FILE, 'bindings': FILE}, ('file', 'expected_sha256', 'resources'))
RUN_PUBLIC_PARAMS_SCHEMA['anyOf'] = [{'required': ['manifest']}, {'required': ['template', 'bindings']}]
RUN_PUBLIC_PARAMS_SCHEMA['dependentRequired'] = {'template': ['bindings'], 'bindings': ['template']}

TEMPLATE_SAVE_PARAMS_SCHEMA = obj({
    'file': {'type': 'string', 'minLength': 1}, 'expected_sha256': SHA,
    'material_id': ID, 'template_id': ID, 'name': NAME,
    'version': TEMPLATE_SCHEMA['properties']['version'],
    'resources': {'type': 'array', 'items': FILE, 'maxItems': 256}},
    ('file', 'expected_sha256', 'material_id', 'template_id', 'name', 'version', 'resources'))

def normalize_public_run_params(value):
    # The add-on's small validator handles nested unions but not same-level
    # anyOf/dependentRequired. Validate the object first, then those clauses.
    schema = deepcopy(RUN_PUBLIC_PARAMS_SCHEMA)
    choices = schema.pop('anyOf')
    dependencies = schema.pop('dependentRequired')
    result = _validate(deepcopy(value), schema, 'params')
    if not any(all(key in result for key in choice['required']) for choice in choices):
        raise WorkflowError('INVALID_REQUEST', 'params requires manifest or template and bindings')
    for key, required in dependencies.items():
        if key in result and any(other not in result for other in required):
            raise WorkflowError('INVALID_REQUEST', 'params.' + key + ' requires ' + ', '.join(required))
    return result

def public_params_schema(command):
    """External manifest references keep request contracts compact."""
    if command == 'material.study':
        from material_study_contract import STUDY_PARAMS_SCHEMA, MAX_CASES, MAX_VIEWS, MAX_TOTAL_VIEWS
        schema = deepcopy(STUDY_PARAMS_SCHEMA)
        schema['properties']['manifest'] = {'$ref': 'material-batch-manifest.schema.json',
            'allOf': [{'properties': {'preview': {'properties': {'views': {'maxItems': MAX_VIEWS}}}}}]}
        schema['allOf'] = [{'if': {'properties': {'study': {'properties': {'cases': {'minItems': count, 'maxItems': count}}}}},
                            'then': {'properties': {'manifest': {'properties': {'preview': {'properties': {
                                'views': {'maxItems': min(MAX_VIEWS, MAX_TOTAL_VIEWS // count)}}}}}}}}
                           for count in range(1, MAX_CASES+1)]
        return schema
    schema = deepcopy({'material.run': RUN_PUBLIC_PARAMS_SCHEMA,
                       'material.batch': BATCH_PARAMS_SCHEMA,
                       'material.template-save': TEMPLATE_SAVE_PARAMS_SCHEMA}[command])
    if command in ('material.run', 'material.batch'):
        filename = 'material-workflow-manifest.schema.json' if command == 'material.run' else 'material-batch-manifest.schema.json'
        schema['properties']['manifest'] = {'$ref': filename}
    return schema

def public_request_schema(command):
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema',
            'title': command + ' request 1.0',
            **obj({'schema_version': {'const': '1.0'}, 'command': {'const': command},
                   'params': public_params_schema(command)}, ('schema_version', 'command', 'params'))}
