"""Replay native complete style and brush preset actions on a saved Toon scene."""
import os
from pathlib import Path

from material_draft_handoff import fingerprint, guard_file, read_document, require
from protocol import Failure, digest
import toon_author_helpers_handoff as helpers
import toon_selector_picker_handoff as picker
import toon_pencil_dialog_bindings as dialog_bindings
import toon_viewport_lines_contract as contract

READ = 'inspect-author-file'
READ_PICKER = 'inspect-selector-picker'
EDIT = 'edit-author-file'
EXPORT = 'export-author-file'
OPERATIONS = (READ, READ_PICKER, EDIT, EXPORT)
HELP = ('inspect-author-file reads the native groups, selectors and brush choices. edit-author-file uses author_file_action '
        'NEW, IMPORT, APPEND, CLEANUP, FIX_NAMES, PRESET, RESET or LOAD_PRESET with expected_author_sha256 and --output. '
        'Also supports SHAPE {shape}, WORKSPACE {label}, STORE_PREVIEW {brush}, SELECT_PREVIEW {image}. '
        'inspect-selector-picker reads picker_context {group,kind,query}; use PICK_SELECTORS {picker} for native batch or checkbox acceptance. '
        'DIALOG_BIND {binding} applies a saved object, collection or material to a native selector, line function or merge dialog. '
        'LINE_FUNCTION_CONTROLS {function} applies all four gates plus the chosen edge category replacement without changing targets. '
        'export-author-file uses author_file_action EXPORT, SAVE_PRESET or PREVIEW and emits a job artifact.')
FIELDS = {
    'NEW': {'label', 'objects'}, 'IMPORT': {'file'}, 'APPEND': {'file', 'conflict'},
    'CLEANUP': set(), 'FIX_NAMES': set(), 'PRESET': {'brush', 'preset'}, 'RESET': {'brush'},
    'LOAD_PRESET': {'brush', 'file'}, 'EXPORT': set(), 'SAVE_PRESET': {'brush', 'label'}, 'PREVIEW': {'brush'},
    'SHAPE': {'shape'}, 'WORKSPACE': {'label'}, 'STORE_PREVIEW': {'brush'}, 'SELECT_PREVIEW': {'image'},
    'PICK_SELECTORS': {'picker'},
    'DIALOG_BIND': {'binding'},
    'LINE_FUNCTION_CONTROLS': {'function'},
}
ARTIFACTS = {'EXPORT': 'author-style.json', 'SAVE_PRESET': 'brush-preset.json', 'PREVIEW': 'brush-preview.png'}


def normalize_request(document, editions):
    operation = document['operation']
    core = {key: value for key, value in document.items() if key not in ('author_file_action', 'picker_context')}
    require(not {'scope', 'patch', 'frames', 'elements'} & core.keys(), 'INVALID_REQUEST', 'Use author_file_action')
    require(('picker_context' in document) == (operation == READ_PICKER), 'INVALID_REQUEST', 'Picker inspection needs picker_context')
    if operation in (READ, READ_PICKER):
        require('author_file_action' not in document, 'INVALID_REQUEST', 'Inspection does not change the author')
        core.update(operation='inspect', scope={'groups': ['author-file']})
    else:
        core['operation'] = 'render-camera'
    result = contract.normalize_request(core, editions)
    result.pop('scope', None)
    result['operation'] = operation
    if operation == READ_PICKER:
        picker.validate(document['picker_context'], edit=False)
        result['picker_context'] = document['picker_context']
        return result
    if operation == READ:
        return result
    action = document.get('author_file_action')
    require(type(action) is dict and isinstance(action.get('action'), str) and action['action'] in FIELDS,
            'INVALID_REQUEST', 'Expected a named native author file action')
    kind = action['action']
    require(action.keys() == {'action'} | FIELDS[kind] and (kind in ARTIFACTS) == (operation == EXPORT),
            'INVALID_REQUEST', 'Unexpected fields or operation for this author file action')
    if 'label' in action:
        require(contract.name(action['label'], 128), 'INVALID_REQUEST', 'Expected explicit author name')
    if kind == 'SHAPE':
        helpers.validate_shape(action['shape'])
    if kind == 'PICK_SELECTORS':
        picker.validate(action['picker'], edit=True)
    if kind == 'DIALOG_BIND':
        dialog_bindings.normalize(action['binding'])
    if kind == 'LINE_FUNCTION_CONTROLS':
        dialog_bindings.normalize_controls(action['function'])
    if kind == 'SELECT_PREVIEW':
        require(action['image'] is None or contract.name(action['image']), 'INVALID_REQUEST', 'Expected saved image name or null')
    if 'file' in action:
        row = action['file']
        require(type(row) is dict and row.keys() == {'file', 'sha256'} and isinstance(row['file'], str)
                and Path(row['file']).is_absolute() and Path(row['file']).suffix.lower() == '.json'
                and isinstance(row['sha256'], str) and contract.SHA256.fullmatch(row['sha256']),
                'INVALID_REQUEST', 'Use an absolute JSON file pinned by SHA-256')
    if 'objects' in action:
        objects = action['objects']
        require(type(objects) is list and 1 <= len(objects) <= 4096 and all(contract.name(item) for item in objects)
                and len(objects) == len(set(objects)), 'INVALID_REQUEST', 'Expected distinct local mesh names')
    if 'brush' in action:
        row = action['brush']
        require(type(row) is dict and row.keys() == {'group', 'slot'} and all(contract.name(item) for item in row.values()),
                'INVALID_REQUEST', 'Use explicit native brush group ID and editor slot')
    if 'preset' in action:
        require(contract.name(action['preset'], 64), 'INVALID_REQUEST', 'Expected a native preset name')
    if 'conflict' in action:
        require(action['conflict'] in ('ERROR', 'RENAME', 'REPLACE_LABEL'), 'INVALID_REQUEST', 'Unsupported conflict policy')
    result['author_file_action'] = action
    return result


def guard_inputs(request, guards):
    row = request.get('author_file_action', {}).get('file')
    if row:
        row['file'] = str(guard_file(row['file'], guards, row['sha256']))


def state(bpy, context, author_file, bindings, effects):
    scene = context.scene
    native = scene.toon_viewport_preview
    result = {'style_mode': native.style_mode, 'preset_revision': native.preset_revision,
              'editable_brush': {'group': native.curve_group, 'slot': native.curve_slot}, 'groups': [], 'routes': [],
              'local_meshes': sorted(obj.name for obj in context.view_layer.objects if obj.type == 'MESH' and obj.library is None),
              'shape_outputs': [helpers.shape_state(context, obj) for obj in sorted(scene.objects, key=lambda x: x.name)
                                if obj.get('blenderctl_author_shape_source')],
              'author_workspaces': [helpers.workspace_state(ws) for ws in sorted(bpy.data.workspaces, key=lambda x: x.name)
                                    if ws.get('toon_workspace_origin')],
              'preview_image': helpers.preview_state(scene.toon_author_files.preview_image),
              'preview_choices': sorted(image.name for image in bpy.data.images if image.get('toon_preview_actual')),
              'binding_slots': dialog_bindings.slots(scene)}
    if native.preset_text is None:
        return result
    document = author_file.read_scene(scene)['document']
    records = effects.records(document)
    result['groups'] = [{'id': row.name, 'label': row.label} for row in native.groups]
    result['selectors'] = document['request'].get('selector_bindings', {}).get('groups', [])
    for route in bindings.recover(document)['routes']:
        actual = route['actual_brush_id']
        result['routes'].append({'group': route['group_id'], 'slot': route['slot'], 'actual': actual,
                                 'effects_sha256': fingerprint(records.get(actual, effects.defaults()))})
    return result


def new_style(context, action, author_file):
    target_names = set(action['objects'])
    targets = {obj.name for obj in context.view_layer.objects if obj.name in target_names and obj.type == 'MESH' and obj.library is None}
    require(targets == target_names, 'CONFLICT', 'Named local meshes must exist in the saved view layer')
    objects = tuple(context.view_layer.objects)
    selection = {obj.name: obj.select_get(view_layer=context.view_layer) for obj in objects}
    try:
        for obj in objects:
            obj.select_set(obj.name in targets, view_layer=context.view_layer)
        result = author_file.new_scene_style(context.scene, label=action['label'])
        selectors = result['document']['request']['selector_bindings']['groups'][0]['objects']
        require({ref['name'] for ref in selectors} == targets, 'VALIDATION_FAILED', 'Native style selected different meshes')
        return {'selected_meshes': sorted(targets)}
    finally:
        for obj in objects:
            obj.select_set(selection[obj.name], view_layer=context.view_layer)


def apply_edit(bpy, context, action, job, before, author_file, presets):
    from toon_workflow.toon_viewport_preview import author_helpers

    kind = action['action']
    scene = context.scene
    if kind == 'NEW':
        return new_style(context, action, author_file)
    if kind in ('IMPORT', 'APPEND'):
        author_file.import_style(scene, action['file']['file'], mode='REPLACE' if kind == 'IMPORT' else kind,
                                 conflict=action.get('conflict', 'ERROR'))
        return {'source_style': action['file'], 'conflict': action.get('conflict')}
    if kind in ('CLEANUP', 'FIX_NAMES'):
        return author_file.cleanup_references(scene, remove_missing=kind == 'CLEANUP', match_names=kind == 'FIX_NAMES')
    if kind == 'SHAPE':
        return {'shape': helpers.build_shape(bpy, context, action['shape'], author_helpers)}
    if kind == 'WORKSPACE':
        return {'workspace': helpers.create_workspace(bpy, context, action, author_helpers)}
    if kind == 'SELECT_PREVIEW':
        return helpers.select_preview(bpy, scene, action)
    if kind == 'PICK_SELECTORS':
        return {'picker': picker.run(context, action['picker'], apply=True)}
    if kind == 'DIALOG_BIND':
        return {'binding': dialog_bindings.apply(bpy, context, action['binding'])}
    if kind == 'LINE_FUNCTION_CONTROLS':
        return {'line_function': dialog_bindings.apply_controls(bpy, scene, action['function'])}
    actual = chosen_brush(before, action)
    if kind == 'PRESET':
        presets.apply(scene, actual, action['preset'])
    elif kind == 'RESET':
        presets.reset(scene, actual)
    elif kind == 'STORE_PREVIEW':
        from toon_workflow.toon_viewport_preview import brush_preview
        path = job / ARTIFACTS['PREVIEW']
        image = brush_preview.render(scene, actual, path)
        scene.toon_author_files.preview_image = image
        return {'artifact': {'file': str(path), 'sha256': digest(path)}, 'preview_image': helpers.preview_state(image)}
    else:
        presets.load(scene, actual, action['file']['file'])
    return {'actual': actual}


def chosen_brush(before, action):
    target = action['brush']
    matches = {row['actual'] for row in before['routes'] if row['group'] == target['group'] and row['slot'] == target['slot']}
    require(len(matches) == 1, 'CONFLICT', 'Expected one native brush for this group and editor slot')
    return matches.pop()


def export_artifact(bpy, scene, action, job, before, author_file, presets, effects):
    kind = action['action']
    path = job / ARTIFACTS[kind]
    if kind == 'EXPORT':
        details = author_file.export_style(scene, path)
        author_file.checked(read_document(path))
    else:
        actual = chosen_brush(before, action)
        if kind == 'SAVE_PRESET':
            document = author_file.read_scene(scene)['document']
            authored = effects.records(document).get(actual, effects.defaults())
            presets.save(path, authored, label=action['label'])
            details = {'actual': actual, 'effects_sha256': fingerprint(authored), 'label': action['label']}
        else:
            from toon_workflow.toon_viewport_preview import brush_preview
            image = brush_preview.render(scene, actual, path)
            details = {'actual': actual, 'width': image.size[0], 'height': image.size[1], 'packed_preview': bool(image.packed_file)}
            bpy.data.images.remove(image)
    return {'artifact': {'file': str(path), 'sha256': digest(path)}, 'export': details}


def work(bpy, context, request, job, author, *, author_sha, save_candidate, read_bindings, dependencies):
    from toon_workflow.toon_viewport_preview import brush_effects, file_authoring, preset_catalog, source_bindings
    from toon_workflow.toon_workflow_addon import joint_workflow

    scene = context.scene
    before = state(bpy, context, file_authoring, source_bindings, brush_effects)
    op = request['operation']
    shape = fingerprint([joint_workflow.non_author_signature(scene), read_bindings(), dependencies()])
    response = {'operation': op, 'product': request['product'], 'context': request['context'],
                'source': {'file': request['file'], 'sha256': request['expected_sha256'], 'unchanged': True},
                'runtime': {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(), 'pid': os.getpid()}}
    if op == READ_PICKER:
        try:
            selection = picker.run(context, request['picker_context'], apply=False)
        except (KeyError, RuntimeError, TypeError, ValueError) as error:
            raise Failure('CONFLICT', str(error)) from error
        require(author_sha(scene) == author, 'VALIDATION_FAILED', 'Picker inspection changed the saved author')
        return {**response, 'author_sha256': author, 'picker': selection}
    if op == READ:
        observed = fingerprint(before)
        require(observed == request.get('verify_state_sha256', observed)
                and shape == request.get('verify_scene_sha256', shape), 'VALIDATION_FAILED', 'Cold author file state changed')
        return {**response, 'state': before, 'state_sha256': observed, 'scene_sha256': shape, 'author_sha256': author,
                'presets': list(preset_catalog.NAMES)}
    action = request['author_file_action']
    if action['action'] == 'SHAPE':
        prior_objects = tuple(obj for obj in context.view_layer.objects if obj.type == 'MESH')
        prior_mesh = {obj.name: helpers.shape_signature(context, obj) for obj in prior_objects}
        references = read_bindings(), dependencies()
    try:
        if op == EXPORT:
            result = export_artifact(bpy, scene, action, job, before, file_authoring, preset_catalog, brush_effects)
            require(author_sha(scene) == author, 'CONFLICT', 'Export committed pending curves; save that edit first')
            return {**response, 'event': action['action'], **result}
        event = apply_edit(bpy, context, action, job, before, file_authoring, preset_catalog)
    except (KeyError, RuntimeError, TypeError, ValueError) as error:
        raise Failure('CONFLICT', str(error)) from error
    updated = author_sha(scene)
    after = state(bpy, context, file_authoring, source_bindings, brush_effects)
    require(updated != author or before != after, 'CONFLICT', 'Native operation changed no saved author state')
    observed_shape = fingerprint([joint_workflow.non_author_signature(scene), read_bindings(), dependencies()])
    if action['action'] == 'SHAPE':
        label = event['shape']['object']
        require(updated == author and prior_mesh == {obj.name: helpers.shape_signature(context, obj) for obj in prior_objects}
                and ([row for row in read_bindings() if row['object'] != label], dependencies()) == references,
                'VALIDATION_FAILED', 'Derived shape modified an existing mesh, slot or dependency')
    else:
        require(shape == observed_shape, 'VALIDATION_FAILED', 'Author file operation changed geometry, material slots or dependencies')
    kept = save_candidate(bpy, request)
    return {**response, 'event': {'action': action['action'], **event}, 'state': after, 'state_sha256': fingerprint(after),
            'scene_sha256': observed_shape, 'author_before_sha256': author, 'author_after_sha256': updated,
            'candidate_sha256': digest(request['stage']), 'retained_orphans': kept,
            'protected': {'status': 'passed', 'geometry_material_slots_dependencies': True}}
