"""Replay native layer-panel edits on a saved scene without applying the shader."""
from contextlib import ExitStack
from copy import deepcopy
import json
import math
import struct

from material_draft_handoff import guard_file, open_saved_context, require, run_operator
from protocol import atomic_json, digest, read_json


def panel_value(scene, prop):
    value = getattr(scene, prop)
    return value if isinstance(value, (str, bool, int, float)) else list(value)


def available_fields(ui, manifest):
    layer = manifest['layers'][0]
    return {prop: keys for prop, keys in ui.FIELDS.items()
            if keys[0] in layer and (len(keys) == 1 or keys[1] in layer[keys[0]])}


def assign(layer, keys, value):
    parent = layer
    for key in keys[:-1]:
        parent = parent[key]
    parent[keys[-1]] = value


def checked_value(scene, prop, value):
    rna = scene.bl_rna.properties[prop]
    if rna.type == 'BOOLEAN':
        require(type(value) is bool, 'INVALID_REQUEST', f'{prop} requires a boolean')
    elif rna.type == 'STRING':
        require(isinstance(value, str) and value.strip() and '\0' not in value,
                'INVALID_REQUEST', f'{prop} requires a layer name')
    elif rna.type == 'FLOAT':
        values = value if rna.is_array else [value]
        require(isinstance(values, list) and len(values) == (rna.array_length if rna.is_array else 1),
                'INVALID_REQUEST', f'Wrong number of components for {prop}')
        for component in values:
            require(type(component) in (float, int) and rna.hard_min <= component <= rna.hard_max
                    and math.isfinite(component), 'INVALID_REQUEST', f'{prop} outside its Blender RNA range')
    else:
        require(False, 'UNSUPPORTED', f'Unsupported panel property type for {prop}')
    setattr(scene, prop, value)
    actual = panel_value(scene, prop)
    if rna.type == 'FLOAT':
        projected = [struct.unpack('f', struct.pack('f', component))[0] for component in values]
        expected = projected if rna.is_array else projected[0]
    else:
        expected = value
    require(actual == expected, 'VALIDATION_FAILED', f'Panel did not retain the requested {prop}')
    return actual


def capture_pending(scene, ui, manifest, fields):
    """Carry saved, uncommitted RNA controls across a selection without rounding untouched JSON fields."""
    index = scene.mw_layer_index
    require(0 <= index < len(manifest['layers']), 'CONFLICT', 'Saved active layer is outside the draft')
    current = {prop: panel_value(scene, prop) for prop in fields}
    extra = ('mw_image_path', 'mw_effect_group', 'mw_effect_parameters', 'mw_preview_engine')
    pending = {prop: panel_value(scene, prop) for prop in extra}
    ui._load_layer(scene)
    require(all(panel_value(scene, prop) == value for prop, value in pending.items()),
            'CONFLICT', 'Commit the pending image, effect or preview-engine panel input before switching layers')
    changed = {}
    for prop, value in current.items():
        if value != panel_value(scene, prop):
            require(index != 0 or prop not in ('mw_enabled', 'mw_opacity'), 'CONFLICT',
                    'The base layer must remain enabled at full opacity')
            assign(manifest['layers'][index], fields[prop], value)
            changed[prop] = value
    return changed


def edit_surface(bpy, ui, scene, manifest, index, request):
    """Merge only the native result of the requested channel, effect or engine operation."""
    layer = manifest['layers'][index]
    operation = request['operation']
    if 'channel' in request:
        channel = request['channel']
        require(isinstance(channel, str) and channel in (*ui.CHANNELS, 'mask'), 'INVALID_REQUEST', 'Unknown channel')
        scene.mw_channel = channel
        run_operator(bpy, 'channel', scene)
        if operation != 'read-channel':
            if operation == 'assign-image':
                scene.mw_image_path = request['image']['file']
            run_operator(bpy, 'image', scene, remove=operation == 'remove-image')
            native = ui._manifest(scene)['layers'][index]
            layer['mask'], layer['channels'] = native['mask'], native['channels']
        image = layer['mask'] if channel == 'mask' else layer['channels'].get(channel)
        return {'channel': channel, 'image': image}
    if operation == 'preview-engine':
        scene.mw_preview_engine = request['engine']
        run_operator(bpy, 'preview_engine', scene)
        manifest['preview'] = ui._manifest(scene)['preview']
        return {'preview': manifest['preview']}
    if operation == 'bind-effect':
        group = bpy.data.node_groups.get(request['group'])
        require(group is not None and group.bl_idname == 'ShaderNodeTree', 'NOT_FOUND', 'Shader node group is absent')
        scene.mw_effect_group = request['group']
        scene.mw_effect_parameters = json.dumps(request['parameters'], allow_nan=False)
    run_operator(bpy, 'effect', scene, remove=operation == 'remove-effect')
    layer['effect'] = ui._manifest(scene)['layers'][index]['effect']
    return {'effect': layer['effect']}


def load_layer_template(request, job):
    import bpy
    from inspection import all_ids, identity
    from material_batch_draft import saved_editor
    from material_procedural_template import author_product, external_dependencies, named_target, save_reopen_scene
    edition = author_product(bpy, request)
    from material_workflow import snapshot
    from material_workflow_addon import core_v2, templates, ui, ui_batch
    from material_workflow_addon.contract import image_documents

    ui.register()
    ui_batch.register()
    context = open_saved_context(bpy, request)
    scene, target = context.scene, request['target']
    obj = named_target(context, target)
    dependencies = external_dependencies(bpy)
    before = snapshot()
    previous = saved_editor(scene, ui)
    batch = scene.mw_batch_json
    template, binding = request['template'], request.get('bindings')
    builtin = template if isinstance(template, str) else ''
    if binding:
        bindings = read_json(binding['file'])
        require(isinstance(bindings, dict) and bindings.get('context') == request['context']
                and bindings.get('target') == target, 'CONFLICT', 'Bindings differ from the requested scene or target')
        document = templates.builtin_template(builtin) if builtin else templates.read_template(template['file'])
        expected = templates.instantiate_template(document, bindings)
    else:
        require(context.object == obj and obj.active_material_index == target['material_slot']
                and obj.data.uv_layers.active and obj.data.uv_layers.active.name == target['uv_layer'],
                'CONFLICT', 'Implicit template bindings require the saved active object, slot and UV')
        expected = None
    scene.mw_template_path = '' if builtin else template['file']
    scene.mw_bindings_path = binding['file'] if binding else ''
    run_operator(bpy, 'template_load', scene, builtin=builtin)
    manifest = ui.normalize_manifest(ui._manifest(scene))
    require(manifest['context'] == request['context'] and manifest['target'] == target and
            (expected is None or manifest == expected), 'VALIDATION_FAILED', 'Panel instantiated unexpected bindings')
    scene.mw_batch_edit_id = ''
    require(scene.mw_batch_json == batch and scene.mw_layer_index == 0,
            'VALIDATION_FAILED', 'Instantiating a draft changed the committed batch or selected the wrong layer')
    after = snapshot()
    def shader_content(state):
        return state['bindings'], [r for r in state['scene']['datablocks'] if r['type'] != 'Scene']
    require(shader_content(before) == shader_content(after), 'VALIDATION_FAILED', 'Draft load changed bound scene content')

    with ExitStack() as guards:
        resources = {}
        for image in image_documents(manifest):
            path, expected_sha = image['file'], image['expected_sha256']
            if path in resources:
                require(resources[path] == expected_sha, 'CONFLICT', 'Image fingerprints differ within the template')
            else:
                guard_file(path, guards, expected_sha)
                resources[path] = expected_sha
        for layer in manifest['layers']:
            effect = layer.get('effect')
            if effect:
                group = bpy.data.node_groups.get(effect['group'])
                require(group and core_v2.group_fingerprint(group) == effect['expected_sha256'],
                        'CONFLICT', 'Template effect group is missing or its content differs')
        retained = []
        for item in all_ids():
            if not item.library and not item.is_embedded_data and item.users == 0 and not item.use_fake_user:
                item.use_fake_user = True
                retained.append(identity(item))
        path, reopened = save_reopen_scene(bpy, request, snapshot(), dependencies)
        saved = reopened.scene
        require(ui._manifest(saved) == manifest and saved.mw_layer_index == 0 and not saved.mw_batch_edit_id
                and saved.mw_batch_json == batch, 'VALIDATION_FAILED', 'Layer draft or committed batch changed after reopen')
        file = job / 'layer-manifest.json'
        atomic_json(file, manifest)
    return {'candidate': {'sha256': digest(path)}, 'manifest': {'file': str(file), 'sha256': digest(file)},
            'product': edition, 'material_id': manifest['material']['id'], 'previous_editor': previous,
            'selected_layer': manifest['layers'][0]['id'], 'layer_ids': [layer['id'] for layer in manifest['layers']],
            'resources': [{'file': p, 'expected_sha256': h} for p, h in resources.items()], 'retained_orphans': retained,
            'source': {'file': request['file'], 'sha256': request['expected_sha256']}, 'template': template,
            'bindings': binding, 'verification': {'reopened': True, 'saved_batch_preserved': True, 'shader_unchanged': True}}


def edit_layer_draft(request, job):
    import bpy
    from material_procedural_template import author_product, external_dependencies, named_target, save_reopen_scene

    edition = author_product(bpy, request)
    from material_workflow import snapshot
    from inspection import all_ids, identity
    from material_workflow_addon import ui
    from material_workflow_addon.contract import image_documents

    ui.register()
    context = open_saved_context(bpy, request)
    named_target(context, request['target'])
    scene = context.scene
    dependencies = external_dependencies(bpy)
    if 'manifest' in request:
        scene.mw_manifest_path = request['manifest']['file']
        run_operator(bpy, 'load', scene)
    require(bool(scene.mw_manifest_json), 'NOT_FOUND', 'This saved scene has no layer draft')
    manifest = ui.normalize_manifest(ui._manifest(scene))
    require(manifest['context'] == request['context'] and manifest['target'] == request['target']
            and manifest['material']['id'] == request['expected_material_id'],
            'CONFLICT', 'Draft context, material identity or object/slot/UV target differs')
    fields = available_fields(ui, manifest)
    ids = [layer['id'] for layer in manifest['layers']]
    require(request['layer_id'] in ids, 'NOT_FOUND', 'Layer identity is absent from this draft')
    index = ids.index(request['layer_id'])
    operation = request['operation']
    captured = capture_pending(scene, ui, manifest, fields)
    ui._store(scene, ui.normalize_manifest(manifest))
    ui._load_layer(scene)
    run_operator(bpy, 'select_layer', scene, index=index)
    require(scene.mw_layer_index == index, 'VALIDATION_FAILED', 'Native layer selection differs')
    # Native selection flushes every float to RNA precision. Keep untouched JSON
    # values so moving a layer cannot rewrite a previously authored material.
    ui._store(scene, manifest)
    ui._load_layer(scene)
    layers = manifest['layers']
    result = {}
    if operation == 'set':
        for prop, value in request['values'].items():
            require(prop in fields, 'INVALID_REQUEST', f'Field {prop} is unavailable in this draft schema')
            require(index != 0 or prop not in ('mw_enabled', 'mw_opacity'), 'INVALID_REQUEST',
                    'The base layer must remain enabled at full opacity')
            assign(layers[index], fields[prop], checked_value(scene, prop, value))
        ui._flush(scene)
    elif operation in ('add', 'delete', 'up', 'down'):
        run_operator(bpy, 'edit_layer', scene, action=operation.upper())
        native = ui._manifest(scene)['layers']
        if operation == 'add':
            added = native[scene.mw_layer_index]
            clone = deepcopy(layers[index])
            clone.update({key: added[key] for key in ('id', 'name', 'opacity', 'enabled')})
            layers.insert(index + 1, clone)
        elif operation == 'delete':
            layers.pop(index)
        else:
            other = index + (-1 if operation == 'up' else 1)
            layers[index], layers[other] = layers[other], layers[index]
        require([layer['id'] for layer in layers] == [layer['id'] for layer in native],
                'VALIDATION_FAILED', 'Native edit produced a different layer order')
    elif operation != 'select':
        result = edit_surface(bpy, ui, scene, manifest, index, request)
    manifest = ui.normalize_manifest(manifest)
    ui._store(scene, manifest)
    ui._load_layer(scene)
    selected = manifest['layers'][scene.mw_layer_index]['id']
    visible = {prop: panel_value(scene, prop) for prop in fields}
    surface_props = ('mw_channel', 'mw_image_path', 'mw_effect_group', 'mw_effect_parameters', 'mw_preview_engine')
    surface_panel = {prop: panel_value(scene, prop) for prop in surface_props}
    with ExitStack() as guards:
        resources = {}
        for image in image_documents(manifest):
            file, sha = image['file'], image['expected_sha256']
            if file in resources:
                require(resources[file] == sha, 'CONFLICT', 'One image path has conflicting fingerprints')
            else:
                guard_file(file, guards, sha)
                resources[file] = sha
        retained = []
        for item in all_ids():
            if not item.library and not item.is_embedded_data and item.users == 0 and not item.use_fake_user:
                item.use_fake_user = True
                retained.append(identity(item))
        path, reopened = save_reopen_scene(bpy, request, snapshot(), dependencies)
        saved = reopened.scene
        require(ui._manifest(saved) == manifest and manifest['layers'][saved.mw_layer_index]['id'] == selected
                and all(panel_value(saved, prop) == value for prop, value in {**visible, **surface_panel}.items()),
                'VALIDATION_FAILED', 'Saved panel differs after reopening the candidate')
        output = job / 'layer-manifest.json'
        atomic_json(output, manifest)
    return {'candidate': {'sha256': digest(path)}, 'manifest': {'file': str(output), 'sha256': digest(output)},
            'selected_layer': selected, 'layer_ids': [layer['id'] for layer in manifest['layers']],
            'operation': operation, 'captured_pending': captured, 'panel': visible, 'surface_panel': surface_panel,
            'retained_orphans': retained, **result,
            'resources': [{'file': file, 'expected_sha256': sha} for file, sha in resources.items()],
            'product': edition, 'runtime': {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode()}}
