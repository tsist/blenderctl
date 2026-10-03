# SPDX-License-Identifier: GPL-3.0-or-later
"""Host request adapter for the shared add-on contract."""
from pathlib import Path
import sys
import json
import hashlib
from copy import deepcopy
from protocol import Failure

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from material_workflow_addon.contract import MANIFEST_SCHEMA, WorkflowError, normalize_manifest, image_documents
from material_workflow_addon.contract import normalize_params as _normalize_params
from material_workflow_addon.contract import _validate, _absolute, obj, FILE, SHA, ID, NAME
from material_workflow_addon.templates import instantiate_template
from material_interface_contract import normalize_public_run_params, TEMPLATE_SAVE_PARAMS_SCHEMA

def _read_descriptor(value, label):
    descriptor = _validate(value, FILE, label)
    descriptor['file'] = _absolute(descriptor['file'])
    try:
        content = Path(descriptor['file']).read_bytes()
        if hashlib.sha256(content).hexdigest() != descriptor['expected_sha256']:
            raise Failure('CONFLICT', label + ' JSON SHA mismatch')
        return descriptor, json.loads(content.decode('utf-8-sig'))
    except (OSError, ValueError, UnicodeError) as exc:
        raise Failure('INVALID_REQUEST', label + ': ' + str(exc)) from exc

def normalize_params(value):
    try:
        value = normalize_public_run_params(value)
        descriptors = {}
        if 'template' in value or 'bindings' in value:
            if 'template' not in value or 'bindings' not in value:
                raise Failure('INVALID_REQUEST', 'Template requires bindings')
            descriptors['template'], template = _read_descriptor(value.pop('template'), 'template')
            descriptors['bindings'], bindings = _read_descriptor(value.pop('bindings'), 'bindings')
            expanded = instantiate_template(template, bindings)
            if 'manifest' in value and normalize_manifest(value['manifest']) != expanded:
                raise Failure('CONFLICT', 'Expanded template differs from submitted manifest')
            value['manifest'] = expanded
        result = _normalize_params(value)
        result.update(descriptors)
        return result
    except WorkflowError as exc:
        raise Failure(exc.code, str(exc)) from exc

def input_documents(params):
    """Normalized resources cover every layer image with its declared hash."""
    return list(params['resources']) + [params[key] for key in ('template', 'bindings') if key in params]

def normalize_template_save_params(value):
    try:
        result = _validate(deepcopy(value), TEMPLATE_SAVE_PARAMS_SCHEMA, 'template-save')
        result['file'] = _absolute(result['file'])
        if Path(result['file']).suffix.lower() != '.blend': raise WorkflowError('INVALID_REQUEST', 'Expected .blend')
        seen = set()
        for resource in result['resources']:
            resource['file'] = _absolute(resource['file'])
            if resource['file'] in seen: raise WorkflowError('INVALID_REQUEST', 'Duplicate resource path')
            seen.add(resource['file'])
        return result
    except WorkflowError as exc:
        raise Failure(exc.code, str(exc)) from exc
