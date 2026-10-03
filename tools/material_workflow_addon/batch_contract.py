# SPDX-License-Identifier: GPL-3.0-or-later
"""Strict host-safe material batch contracts and dependency ordering."""
from copy import deepcopy
import os
from pathlib import Path
from .contract import (_validate, _fail, _absolute, obj, ID, NAME, FILE, SHA,
    MANIFEST_V2_SCHEMA, LAYER_V2, PREVIEW, VIEW, normalize_manifest as normalize_material_manifest, image_documents)

STRING_LIST = {'type': 'array', 'items': NAME, 'minItems': 1, 'maxItems': 64}
ASSIGNMENT = obj({'id': ID, 'target': MANIFEST_V2_SCHEMA['properties']['target'],
    'material': MANIFEST_V2_SCHEMA['properties']['material'],
    'layers': {'type': 'array', 'items': LAYER_V2, 'minItems': 1, 'maxItems': 8},
    'faces': {'anyOf': [{'type': 'null'}, {'type': 'array', 'items': {'type': 'integer', 'minimum': 0}, 'minItems': 1, 'maxItems': 100000, 'uniqueItems': True}], 'default': None},
    'depends_on': {'type': 'array', 'items': ID, 'maxItems': 64, 'uniqueItems': True, 'default': []}},
    ('id', 'target', 'material', 'layers'))
BATCH_VIEW = deepcopy(VIEW)
BATCH_VIEW['properties']['objects'] = STRING_LIST
BATCH_PREVIEW = deepcopy(PREVIEW)
BATCH_PREVIEW['properties']['views'] = {'type': 'array', 'items': BATCH_VIEW, 'minItems': 1, 'maxItems': 24}
BATCH_MANIFEST_SCHEMA = {'$schema': 'https://json-schema.org/draft/2020-12/schema', 'title': 'Material batch manifest 1.0',
    **obj({'schema_version': {'const': '1.0'}, 'context': MANIFEST_V2_SCHEMA['properties']['context'],
        'objects': {**STRING_LIST, 'uniqueItems': True},
        'assignments': {'type': 'array', 'items': ASSIGNMENT, 'minItems': 1, 'maxItems': 64},
        'preview': BATCH_PREVIEW}, ('schema_version', 'context', 'objects', 'assignments', 'preview'))}
BATCH_PARAMS_SCHEMA = obj({'file': {'type': 'string', 'minLength': 1}, 'expected_sha256': SHA,
    'manifest': BATCH_MANIFEST_SCHEMA, 'resources': {'type': 'array', 'items': FILE, 'maxItems': 4096}, 'resume': FILE},
    ('file', 'expected_sha256', 'manifest', 'resources'))

def _unique(values, path):
    if len(values) != len(set(values)): _fail(path, 'duplicate values')

def topological_order(manifest):
    assignments = manifest['assignments']
    by_id = {a['id']: a for a in assignments}
    order, visited, active = [], set(), set()
    def visit(identity):
        if identity in active: _fail('depends_on', 'dependency cycle')
        if identity in visited: return
        if identity not in by_id: _fail('depends_on', 'unknown assignment ' + identity)
        active.add(identity)
        for dep in by_id[identity]['depends_on']: visit(dep)
        active.remove(identity); visited.add(identity); order.append(identity)
    for assignment in assignments: visit(assignment['id'])
    return order

def assignment_manifest(manifest, assignment):
    preview = deepcopy(manifest['preview'])
    preview['views'] = [{k:v for k,v in manifest['preview']['views'][0].items() if k != 'objects'}]
    return normalize_material_manifest({'schema_version': '1.1', 'context': manifest['context'],
        'target': assignment['target'], 'material': assignment['material'], 'layers': assignment['layers'], 'preview': preview})

def normalize_manifest_batch(value):
    result = _validate(deepcopy(value), BATCH_MANIFEST_SCHEMA, 'batch')
    _unique(result['objects'], 'objects')
    _unique([a['id'] for a in result['assignments']], 'assignments.id')
    _unique([a['material']['id'] for a in result['assignments']], 'assignments.material.id')
    _unique([(a['target']['object'], a['target']['material_slot']) for a in result['assignments']], 'assignments.target')
    names = set(result['objects'])
    faces, known = {}, {}
    for assignment in result['assignments']:
        target = assignment['target']['object']
        if target not in names: _fail('target.object', 'must belong to objects')
        _unique(assignment['depends_on'], 'depends_on')
        if assignment['id'] in assignment['depends_on']: _fail('depends_on', 'self dependency')
        if assignment['faces'] is not None:
            _unique(assignment['faces'], 'faces')
            selected = set(assignment['faces'])
            if selected.intersection(faces.get(target, set())): _fail('faces', 'overlapping face assignments')
            faces.setdefault(target, set()).update(selected)
        spec = assignment_manifest(result, assignment)
        assignment.update({key:spec[key] for key in ('target', 'material', 'layers')})
        for image in image_documents(spec):
            path = os.path.normcase(image['file'])
            if path in known and known[path] != image['expected_sha256']: _fail('images', 'conflicting image SHA')
            known[path] = image['expected_sha256']
    ids = []
    reserved = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1,10)), *(f'lpt{i}' for i in range(1,10))}
    for view in result['preview']['views']:
        key = view['id'].casefold()
        ids.append(key)
        if key in reserved: _fail('views.id', 'Windows reserved directory name')
        view.setdefault('objects', list(result['objects']))
        _unique(view['objects'], 'views.objects')
        if not set(view['objects']).issubset(names): _fail('views.objects', 'must be objects subset')
    _unique(ids, 'views.id')
    topological_order(result)
    return result

def normalize_params(value):
    result = _validate(deepcopy(value), BATCH_PARAMS_SCHEMA, 'params')
    result['file'] = _absolute(result['file'])
    if Path(result['file']).suffix.lower() != '.blend': _fail('file', 'expected .blend')
    result['manifest'] = normalize_manifest_batch(result['manifest'])
    declared = {}
    for row in result['resources']:
        row['file'] = _absolute(row['file']); key = os.path.normcase(row['file'])
        if key in declared: _fail('resources', 'duplicate physical resource path')
        declared[key] = row['expected_sha256']
    for assignment in result['manifest']['assignments']:
        for image in image_documents(assignment):
            if declared.get(os.path.normcase(image['file'])) != image['expected_sha256']:
                _fail('resources', 'each layer image requires matching declaration')
    if 'resume' in result: result['resume']['file'] = _absolute(result['resume']['file'])
    return result

def input_documents(params):
    # New branch resources must remain isolatable; runtime locks each assignment.
    return [params['resume']] if 'resume' in params else []

normalize_manifest = normalize_manifest_batch
