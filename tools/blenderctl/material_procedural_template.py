"""Save and edit Material Workflow authoring candidates on disk."""
import argparse
from array import array
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
sys.path.insert(0, str(ENTRY.parent))

from filesystem import FileGuard, PathLocks
from material_draft_handoff import (
    EDITION_HELP, Parser, active_target, canonical, failure, fingerprint, guard_file, open_saved_context, prepare,
    require, select_product,
)
from processes import ProcessTree
from protocol import CODES, DEFAULT_BLENDER, DEFAULT_JOBS, Failure, atomic_json, digest, envelope, read_json, timeout_value


def parser():
    result = Parser(description=__doc__)
    result.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    result.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS)
    result.add_argument('--transactions-dir', help=argparse.SUPPRESS)
    result.add_argument('--timeout', type=float, default=120)
    mode = result.add_mutually_exclusive_group()
    for flag in ('--human', '--compact', '--full'):
        mode.add_argument(flag, action='store_true')
    actions = result.add_subparsers(dest='action', required=True, parser_class=Parser)
    save = actions.add_parser('save-procedural-template',
        epilog='Params require file, expected_sha256, edition, context(scene/view_layer/frame), '
               'target(object/material_slot/uv_layer), and optionally expected_material_id.')
    save.add_argument('--params', type=Path, required=True, help='UTF-8 JSON. ' + EDITION_HELP)
    save.add_argument('--output', type=Path, required=True, help='New absolute JSON file')
    load = actions.add_parser('load-procedural-template',
        description='Apply a procedural template or manifest to a named object slot in a new .blend',
        epilog='Params: file, expected_sha256, edition, context(scene/view_layer/frame), '
               'target(object/material_slot/uv_layer), template(file/expected_sha256), '
               'optional material_name and mesh_map_size (128, 256, 512, 1024, 2048).')
    load.add_argument('--params', type=Path, required=True, help='UTF-8 JSON with absolute paths')
    load.add_argument('--output', type=Path, required=True, help='New absolute .blend file')
    create = actions.add_parser('create-procedural',
        description='Create and assign a native panel preset to a named material slot in a new .blend.',
        epilog='Params: file, expected_sha256, edition, context, target, expected_slot_material (name or null), '
               'family (painted_metal, wood, glass_clear, glass_tinted, glass_frosted). Optional material_name, '
               'mesh_map_size, seed, weathering, wood grain_axis/timber_scale or painted_metal metal_region. '
               'Omitted preset controls use the saved panel values.')
    create.add_argument('--params', type=Path, required=True, help='UTF-8 JSON with absolute input paths')
    create.add_argument('--output', type=Path, required=True, help='New absolute .blend file')
    update = actions.add_parser('update-procedural',
        description='Refresh or update author controls of a named procedural material slot in a new .blend',
        epilog='Params: file, expected_sha256, edition, context(scene/view_layer/frame), '
               'target(object/material_slot/uv_layer), expected_material_id, optional '
               'edits[{control, value, optional expected_live, optional expected_baseline}].')
    update.add_argument('--params', type=Path, required=True, help='UTF-8 JSON with absolute paths')
    update.add_argument('--output', type=Path, required=True, help='New absolute .blend file')
    draft = actions.add_parser('edit-layer-draft',
        description='Edit a saved layer draft without applying the material. Save a new .blend and a portable manifest.',
        epilog='Params: file, expected_sha256, edition, context, target, expected_material_id, layer_id, '
               'operation select/set/add/delete/up/down, read-channel, assign-image, remove-image, preview-engine, '
               'bind-effect or remove-effect. set takes values such as mw_roughness; image actions take channel, '
               'assign-image also takes image {file,expected_sha256}; preview-engine takes engine; '
               'bind-effect takes group and parameters. '
               'Optional manifest {file,expected_sha256} explicitly loads a different draft before editing.')
    draft.add_argument('--params', type=Path, required=True, help='UTF-8 JSON with absolute paths')
    draft.add_argument('--output', type=Path, required=True, help='New absolute .blend file')
    layer_template = actions.add_parser('load-layer-template',
        description='Instantiate a panel layer template as an editable draft in a new .blend.',
        epilog='Params: file, expected_sha256, edition, context, target, template and optional bindings. '
               'File templates require both template and bindings as {file,expected_sha256}. '
               'Built-in base_overlay or single_layer can use the saved active object, slot and UV.')
    layer_template.add_argument('--params', type=Path, required=True, help='UTF-8 JSON with absolute paths')
    layer_template.add_argument('--output', type=Path, required=True, help='New absolute .blend file')
    return result


def slot_state(bpy):
    """Inventory object identities and material assignments without changing active context."""
    return {obj.name_full: [obj.data.name_full if obj.data else None,
                            [[slot.link, slot.material.name_full if slot.material else None] for slot in obj.material_slots]]
            for obj in bpy.data.objects}


def live_image_paths(bpy, tree):
    visited, paths = set(), set()

    def visit(current):
        if current in visited:
            return
        visited.add(current)
        for node in current.nodes:
            if node.type == 'GROUP' and node.node_tree:
                visit(node.node_tree)
            if node.type == 'TEX_IMAGE' and node.image and not node.image.packed_file and node.image.source == 'FILE':
                paths.add(bpy.path.abspath(node.image.filepath, library=node.image.library))

    visit(tree)
    return paths


def observed_changes(core, material, manifest):
    authored = json.loads(material[core.MANIFEST])
    changes = []
    for name, path in sorted(json.loads(material['mwp_controls']).items()):
        before, live = core._get_path(authored, path), core._get_path(manifest, path)
        if not core._same_control(before, live):
            changes.append({'control': name, 'saved_request': before, 'live': live})
    return {'count': len(changes), 'entries': changes[:64], 'truncated': len(changes) > 64}


def author_product(bpy, request):
    guarded_flags = {'--factory-startup', '--disable-autoexec', '--offline-mode'}
    require(bpy.app.background and guarded_flags.issubset(sys.argv) and not bpy.app.online_access
            and not bpy.context.preferences.filepaths.use_scripts_auto_execute,
            'UNSUPPORTED', 'Expected offline factory startup with automatic scripts disabled')
    return select_product(request, bpy)


def template_product(bpy, request):
    edition = author_product(bpy, request)
    for name in ('procedural_ui.py', 'procedural_core.py', 'procedural_contract.py'):
        edition['files'][name] = digest(Path(edition['directory']) / name)
    return edition


def save_template(request, job):
    import bpy

    edition = template_product(bpy, request)
    from material_workflow_addon import procedural_core as core
    from material_workflow_addon.procedural_contract import image_documents

    _, obj = active_target(bpy, request)
    index = request['target']['material_slot']
    material = obj.material_slots[index].material if index < len(obj.material_slots) else None
    require(material and core.MANIFEST in material and core.STATE in material and material.node_tree,
            'NOT_FOUND', 'The selected slot does not hold an authored procedural PBR material')
    require(material.library is None and not material.override_library,
            'CONFLICT', 'The authored material must be local')
    with ExitStack() as guards:
        for path in sorted(live_image_paths(bpy, material.node_tree)):
            guard_file(path, guards)
        state = core._snapshot(material)
        live = core.get_stored_manifest(material)
        identity = live['material']['id']
        require(live['context'] == request['context'] and live['target'] == request['target']
                and material.get(core.KEY) == identity and request.get('expected_material_id', identity) == identity,
                'CONFLICT', 'Saved context, target or procedural material identity differs')
        resources = {}
        for ref in image_documents(live):
            if ref['file'] not in resources:
                guard_file(ref['file'], guards, ref['expected_sha256'])
                resources[ref['file']] = ref['expected_sha256']
        before = slot_state(bpy)
        output = job / 'template.json'
        exported = core.save_template(material, output)
        require(read_json(output) == {'schema': 'material-procedural-template-1.0', 'manifest': live},
                'VALIDATION_FAILED', 'Template does not match the actual procedural author controls')
        require(slot_state(bpy) == before and core._snapshot(material) == state and core.get_stored_manifest(material) == live,
                'VALIDATION_FAILED', 'Saving the template changed slots or authored material content')

    return {'template': exported, 'schema': 'material-procedural-template-1.0', 'product': edition,
            'material_id': identity, 'material': material.name, 'context': request['context'], 'target': request['target'],
            'source': {name: request[name] for name in ('file', 'expected_sha256')},
            'resources': [{'file': path, 'expected_sha256': sha} for path, sha in sorted(resources.items())],
            'control_changes': observed_changes(core, material, live),
            'covered_content_unchanged': {'slots_sha256': fingerprint(before), 'managed_graph_sha256': fingerprint(state)},
            'runtime': {'process': 'cold_open_saved_source', 'blender': bpy.app.version_string,
                        'build': bpy.app.build_hash.decode(), 'pid': os.getpid()},
            'consumer': 'material_workflow.procedural_load'}


def panel_apply(bpy, context, obj, target, job, operation, settings=None):
    scene = context.scene
    fields = {'mwp_ui_work_dir': str(job), 'mwp_ui_slot': target['material_slot'],
              'mwp_ui_uv': target['uv_layer'], **(settings or {})}
    touched = (*fields, 'mwp_ui_result_dir', 'mwp_ui_last_report', 'mwp_ui_status', 'mwp_ui_error')
    stored = {key: copy_custom_value(scene[key], key) for key in touched if key in scene}
    try:
        for key, value in fields.items():
            setattr(scene, key, value)
        with context.temp_override(object=obj, active_object=obj):
            try:
                status = getattr(bpy.ops.material_workflow, operation)()
            except RuntimeError as exc:
                raise Failure('VALIDATION_FAILED', scene.mwp_ui_error or str(exc)) from exc
        require(status == {'FINISHED'}, 'VALIDATION_FAILED', scene.mwp_ui_error or f'Panel {operation} failed')
        report = Path(scene.mwp_ui_last_report)
        require(report.is_relative_to(job) and report.is_file(), 'VALIDATION_FAILED', 'Panel report is missing')
        return report
    finally:
        for key in touched:
            if key in stored:
                scene[key] = stored[key]
            elif key in scene:
                del scene[key]


def named_target(context, target):
    obj = context.view_layer.objects.get(target['object'])
    require(obj is not None and obj.type == 'MESH', 'NOT_FOUND', 'Named mesh is absent from the saved view layer')
    require(context.mode == 'OBJECT' and obj.mode == 'OBJECT' and not (obj.library or obj.override_library
            or obj.data.library or obj.data.override_library), 'CONFLICT', 'Expected a local mesh in Object Mode')
    require(target['uv_layer'] in obj.data.uv_layers, 'NOT_FOUND', 'Named UV layer is missing')
    slot = target['material_slot']
    require(any(poly.material_index == slot for poly in obj.data.polygons), 'INVALID_REQUEST', 'No face uses the named material slot')
    return obj


def external_dependencies(bpy):
    from inspection import path_value

    return {path_value(path) for path in bpy.utils.blend_paths(absolute=True, packed=False)}


def save_reopen_scene(bpy, request, expected, source_dependencies):
    from material_workflow import snapshot
    import scenes

    path = Path(request['candidate_stage'])
    bpy.context.preferences.filepaths.save_version = 0
    require(bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True, relative_remap=True, check_existing=False)
            == {'FINISHED'}, 'IO_ERROR', 'Could not save procedural material candidate')
    reopened = open_saved_context(bpy, {**request, 'file': str(path)})
    observed = snapshot()
    def block_keys(state):
        return {(row['type'], row['name']) for row in state['scene']['datablocks']}
    old, new = block_keys(expected), block_keys(observed)
    require(old == new, 'VALIDATION_FAILED', f'Datablocks changed after reopening: removed={sorted(old-new)}, added={sorted(new-old)}')
    mismatch = scenes.compare(expected, observed)
    require(mismatch is None, 'VALIDATION_FAILED', f'Candidate changed after reopening: {mismatch}')
    require(source_dependencies == external_dependencies(bpy), 'VALIDATION_FAILED',
            'External dependencies changed after reopening')
    return path, reopened


def save_reopen(bpy, request, expected, source_dependencies):
    path, reopened = save_reopen_scene(bpy, request, expected, source_dependencies)
    target = request['target']
    return path, reopened.view_layer.objects[target['object']].material_slots[target['material_slot']].material


def verify_packed_images(bpy, images, resources):
    packed = {hashlib.sha256(bpy.data.images[name].packed_file.data).hexdigest()
              for _, name, _ in images if bpy.data.images[name].packed_file}
    require(set(resources.values()) <= packed, 'VALIDATION_FAILED', 'A declared material image was not preserved in the candidate')


def apply_procedural(request, job):
    import bpy

    edition = template_product(bpy, request)
    from inspection import all_ids, identity
    from material_procedural import _graph_blocks, _verify_preservation
    from material_workflow import snapshot
    from material_workflow_addon import procedural_core as core, procedural_ui as ui
    from material_workflow_addon.procedural_contract import image_documents, normalize_manifest

    from material_workflow_addon.procedural_examples import make_recipe

    creating = request['action'] == 'create-procedural'
    provenance = {}
    resources = {}
    context = open_saved_context(bpy, request)
    target = request['target']
    obj = named_target(context, target)
    slot = target['material_slot']
    if creating:
        current = obj.material_slots[slot].material if slot < len(obj.material_slots) else None
        require((current.name if current else None) == request['expected_slot_material'],
                'CONFLICT', 'Slot material differs from expected_slot_material')
    require(slot < len(obj.material_slots) or obj.data.users == 1,
            'CONFLICT', 'Cannot append a material slot to shared mesh data')
    mesh_name = obj.data.name
    before = snapshot()
    original_in_use = {item for item in all_ids() if item.users > 0}
    original_ids = {mat.get(core.KEY) for mat in bpy.data.materials if isinstance(mat.get(core.KEY), str)}
    source_dependencies = external_dependencies(bpy)

    with ExitStack() as guards:
        ui.register()
        try:
            scene = context.scene
            size = str(request.get('mesh_map_size', scene.mwp_ui_mesh_size))
            if creating:
                settings = {'mwp_ui_family': request['family'], 'mwp_ui_mesh_size': size}
                if 'material_name' in request:
                    settings['mwp_ui_name'] = request['material_name']
                for field in ('seed', 'weathering', 'grain_axis', 'timber_scale', 'metal_region'):
                    if field in request:
                        settings['mwp_ui_' + field] = request[field]
                panel_report = panel_apply(bpy, context, obj, target, job, 'procedural_create', settings)
                recipe = read_json(panel_report.parent / 'request.json')
                # RNA stores artist controls as single-precision floats.
                from ctypes import c_float
                def control(field):
                    return settings.get('mwp_ui_' + field, getattr(scene, 'mwp_ui_' + field))
                expected = make_recipe(request['family'], request['context'], target, recipe['material']['id'],
                        control('name') or None, seed=control('seed'), weathering=c_float(control('weathering')).value,
                        grain_axis=control('grain_axis'), metal_region=control('metal_region'),
                        timber_scale=tuple(c_float(v).value for v in control('timber_scale')))
                expected['mesh_map_size'] = int(size)
                require(recipe == normalize_manifest(expected), 'VALIDATION_FAILED', 'Native preset differs from requested controls')
                provenance = {'family': request['family'], 'previous_material': request['expected_slot_material'],
                              'recipe': {'file': str(panel_report.parent / 'request.json'),
                                         'sha256': digest(panel_report.parent / 'request.json')}}
            else:
                value = read_json(request['template']['file'])
                wrapped = isinstance(value, dict) and value.get('schema') == 'material-procedural-template-1.0'
                if wrapped:
                    require(set(value) == {'schema', 'manifest'}, 'INVALID_REQUEST', 'Unexpected fields in procedural template wrapper')
                recipe = normalize_manifest(value['manifest'] if wrapped else value)
                for resource in image_documents(recipe):
                    path = guard_file(resource['file'], guards, resource['expected_sha256'])
                    resources[path] = resource['expected_sha256']
                panel_report = panel_apply(bpy, context, obj, target, job, 'procedural_load', {
                    'mwp_ui_json_path': request['template']['file'], 'mwp_ui_name': request.get('material_name', ''),
                    'mwp_ui_mesh_size': size})
                provenance = {'template': {'file': request['template']['file'], 'sha256': request['template']['expected_sha256'],
                                           'schema': 'material-procedural-template-1.0' if wrapped else 'procedural_manifest'}}
        finally:
            ui.unregister()
        applied = read_json(panel_report)['result']
        material = obj.material_slots[slot].material
        new_id = applied['material_id']
        require(material is not None and material.get(core.KEY) == new_id and new_id not in original_ids
                and ((new_id == recipe['material']['id']) if creating else (new_id != recipe['material']['id']))
                and obj.material_slots[slot].link == 'OBJECT',
                'VALIDATION_FAILED', 'Panel did not create and bind a new material identity')
        recipe['context'], recipe['target'] = request['context'], target
        recipe['material'].update(id=new_id, name=request.get('material_name', recipe['material']['name']))
        recipe['mesh_map_size'] = int(size)
        require(core.get_stored_manifest(material) == normalize_manifest(recipe),
                'VALIDATION_FAILED', 'Live material differs from the rebound template')
        material_name = material.name
        groups, images = _graph_blocks(material)
        additions = groups | images | {('Material', material_name, None)}
        _verify_preservation(before, snapshot(), target, mesh_name, additions)

        # Swapping an object slot may leave its former material without users.
        # Retain any newly orphaned source datablocks in the saved candidate.
        retained = []
        for item in all_ids():
            if (item in original_in_use and not item.library and not item.is_embedded_data
                    and item.users == 0 and not item.use_fake_user):
                item.use_fake_user = True
                retained.append(identity(item))
        path, verified = save_reopen(bpy, request, snapshot(), source_dependencies)
        require(verified.name == material_name and core.get_stored_manifest(verified) == normalize_manifest(recipe),
                'VALIDATION_FAILED', 'Material binding or live parameters changed after reopening')
        verify_packed_images(bpy, images, resources)

    return {'candidate': {'file': str(path), 'sha256': digest(path)}, **provenance,
            'source': {key: request[key] for key in ('file', 'expected_sha256')}, 'product': edition,
            'material_id': new_id, 'material': material_name, 'context': request['context'], 'target': target,
            'mesh_map_size': int(size), 'panel_report': {'file': str(panel_report), 'sha256': digest(panel_report)},
            'resources': [{'file': str(path), 'sha256': sha} for path, sha in sorted(resources.items())],
            'retained_orphans': retained, 'verification': {'status': 'passed', 'reopened': True,
                'non_target_content': 'preserved_in_scene_snapshot', 'external_dependency_paths': 'preserved'},
            'runtime': {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(), 'pid': os.getpid()}}


def control_value_matches_type(value, exemplar):
    if isinstance(exemplar, list):
        return (isinstance(value, list) and len(value) == len(exemplar)
                and all(control_value_matches_type(a, b) for a, b in zip(value, exemplar)))
    if type(exemplar) is int:
        return type(value) is int and abs(value) <= 3.402823466e38
    return type(value) in (int, float) and abs(value) <= 3.402823466e38


def update_recipes(request, material, baseline, live, core, normalize):
    edits = request.get('edits', [])
    require(isinstance(edits, list), 'INVALID_REQUEST', 'edits must be an array')
    controls = json.loads(material['mwp_controls'])
    incoming, expected = deepcopy(baseline), deepcopy(live)
    incoming['context'] = expected['context'] = request['context']
    changed, confirmed_overlaps = {}, []
    for edit in edits:
        require(isinstance(edit, dict) and {'control', 'value'} <= set(edit)
                <= {'control', 'value', 'expected_live', 'expected_baseline'},
                'INVALID_REQUEST', 'Each control edit requires control and value plus optional expected_live and expected_baseline')
        name = edit['control']
        require(isinstance(name, str) and name in controls and name not in changed,
                'INVALID_REQUEST', f'Expected a unique known author control: {name}')
        path = controls[name]
        old_request, actual = core._get_path(baseline, path), core._get_path(live, path)
        require(all(control_value_matches_type(value, actual) for key, value in edit.items() if key != 'control'),
                'INVALID_REQUEST', f'Invalid value shape for {name}')
        for key, observed in (('expected_live', actual), ('expected_baseline', old_request)):
            if key in edit:
                require(core._same_control(edit[key], observed), 'CONFLICT', f'{key} differs for {name}')
        if not core._same_control(old_request, actual) and not core._same_control(edit['value'], actual):
            require('expected_live' in edit, 'CONFLICT', f'Manual edit overlaps {name}; provide the confirmed expected_live')
            confirmed_overlaps.append((name, old_request))
        core._set_path(incoming, path, edit['value'])
        core._set_path(expected, path, edit['value'])
        changed[name] = actual
    expected = normalize(expected)
    edited = [{'control': name, 'before': actual, 'after': core._get_path(expected, controls[name])}
              for name, actual in changed.items()]
    preserved = [{'control': name, 'saved_request': core._get_path(baseline, path), 'value': core._get_path(live, path)}
                 for name, path in sorted(controls.items()) if name not in changed
                 and not core._same_control(core._get_path(baseline, path), core._get_path(live, path))]
    return normalize(incoming), expected, edited, preserved, confirmed_overlaps


def graph_without_layout(graph):
    result = deepcopy(graph)
    pending = [result]
    while pending:
        for node in pending.pop()['nodes']:
            node['properties'].pop('location_absolute', None)
            if 'group' in node:
                pending.append(node['group'])
    return result


def allow_node_layout(core, material):
    baseline = json.loads(material[core.STATE])
    current = core._snapshot(material)
    if current != baseline:
        require(graph_without_layout(current) == graph_without_layout(baseline), 'CONFLICT',
                'Shader graph edits other than node positions cannot be represented by the stored recipe')
        material[core.STATE] = core._json(current)


def copy_custom_value(value, name):
    from idprop.types import IDPropertyArray, IDPropertyGroup

    if isinstance(value, IDPropertyArray):
        return array(value.typecode, value.to_list())
    if isinstance(value, IDPropertyGroup):
        return {key: copy_custom_value(item, f'{name}.{key}') for key, item in value.items()}
    if isinstance(value, list):
        return [copy_custom_value(item, name) for item in value]
    require(type(value) in (bool, int, float, str), 'UNSUPPORTED',
            f'Cannot restore custom property {name}: unsupported {type(value).__name__}')
    return value


def custom_state(owner, reserved):
    from bpy.utils import escape_identifier

    fields = {}
    for name in sorted(set(owner.keys()) - reserved):
        value = copy_custom_value(owner[name], f'{owner.name}.{name}')
        metadata = owner.id_properties_ui(name).as_dict() if type(value) in (bool, int, float, str, array) else None
        custom_path = f'["{escape_identifier(name)}"]'
        fields[name] = (value, metadata, owner.is_property_overridable_library(custom_path))
    return fields


def restore_custom_state(owner, fields):
    from bpy.utils import escape_identifier

    for name, (value, metadata, overridable) in fields.items():
        owner[name] = value
        if metadata:
            owner.id_properties_ui(name).update(**metadata)
        owner.property_overridable_library_set(f'["{escape_identifier(name)}"]', overridable)


def editable_values(owner, names):
    values = {}
    for name in names:
        value = getattr(owner, name)
        values[name] = value if type(value) in (bool, int, float, str) else tuple(value)
    return values


def node_identities(tree):
    nodes = {(node.get('mwp_node'), node.bl_idname): node for node in tree.nodes}
    require(len(nodes) == len(tree.nodes) and all(name for name, _ in nodes), 'UNSUPPORTED',
            f'Cannot restore artist layout for {tree.name}: managed node names are missing or ambiguous')
    return nodes


def tree_artist_state(tree):
    note_fields = ('name', 'label', 'location', 'width', 'height', 'color', 'use_custom_color', 'hide',
                   'show_options', 'show_preview', 'show_texture', 'select')
    nodes = node_identities(tree)
    return {'custom': custom_state(tree, {'mwp_id'}),
            'active': next((key for key, node in nodes.items() if node == tree.nodes.active), None),
            'nodes': {key: {'display': editable_values(node, note_fields),
                           'custom': custom_state(node, {'mwp_node', 'mwp_channel'}),
                           'group': tree_artist_state(node.node_tree) if node.type == 'GROUP' else None}
                      for key, node in nodes.items()}}


def restore_tree_artist_state(tree, state):
    nodes = node_identities(tree)
    require(nodes.keys() == state['nodes'].keys(), 'VALIDATION_FAILED',
            f'Cannot restore artist layout for {tree.name}: managed node identities changed during rebuild')
    restore_custom_state(tree, state['custom'])
    # Move renamed nodes out of the way first so exchanged names remain exact.
    for key, node in nodes.items():
        if node.name != state['nodes'][key]['display']['name']:
            node.name = uuid.uuid4().hex
    for key, node in nodes.items():
        saved = state['nodes'][key]
        for name, value in saved['display'].items():
            setattr(node, name, value)
        restore_custom_state(node, saved['custom'])
        if saved['group'] is not None:
            restore_tree_artist_state(node.node_tree, saved['group'])
    tree.nodes.active = nodes[state['active']] if state['active'] is not None else None


def material_artist_state(material):
    require(material.asset_data is None and material.preview is None, 'UNSUPPORTED',
            'A material with asset metadata or a saved preview cannot yet be rebuilt without losing that data')
    managed = {'mwp_id', 'mwp_manifest', 'mwp_state', 'mwp_group_name', 'mwp_controls', 'mwp_height_distance'}
    runtime = {'name', 'use_nodes', 'use_fake_user', 'use_extra_user', 'is_runtime_data', 'tag'}
    values = editable_values(material, (prop.identifier for prop in material.bl_rna.properties
        if not prop.is_readonly and prop.type in {'STRING', 'BOOLEAN', 'INT', 'FLOAT', 'ENUM'}
        and prop.identifier not in runtime))
    return {'display': values, 'custom': custom_state(material, managed), 'tree': tree_artist_state(material.node_tree)}


def restore_material_artist_state(material, state):
    # Apply deprecated aliases before their effective replacements.
    for name in ('blend_method', 'show_transparent_back', 'use_screen_refraction'):
        if name in state['display']:
            setattr(material, name, state['display'][name])
    for name, value in state['display'].items():
        if name not in {'blend_method', 'show_transparent_back', 'use_screen_refraction'}:
            setattr(material, name, value)
    restore_custom_state(material, state['custom'])
    restore_tree_artist_state(material.node_tree, state['tree'])


def update_procedural(request, job):
    import bpy

    edition = template_product(bpy, request)
    from material_procedural import _graph_blocks, _verify_preservation
    from material_workflow import snapshot
    from material_workflow_addon import procedural_core as core, procedural_ui as ui
    from material_workflow_addon.contract import WorkflowError
    from material_workflow_addon.procedural_contract import image_documents, normalize_manifest

    context = open_saved_context(bpy, request)
    target, mid = request['target'], request['expected_material_id']
    obj = named_target(context, target)
    index = target['material_slot']
    slot = obj.material_slots[index] if index < len(obj.material_slots) else None
    old = slot.material if slot else None
    require(old and old.node_tree and core.MANIFEST in old and core.STATE in old,
            'NOT_FOUND', 'Named slot has no managed procedural material')
    require(old.get(core.KEY) == mid and slot.link == 'OBJECT', 'CONFLICT', 'Expected material ID or object binding differs')
    require(old.asset_data is None and old.preview is None, 'UNSUPPORTED',
            'A material with asset metadata or a saved preview cannot yet be rebuilt without losing that data')
    require(old.library is None and not (old.override_library or old.animation_data or old.use_fake_user),
            'CONFLICT', 'Material must be local, static and eligible for native update')
    require(old.users == 1 and not any(s.material == old and (other != obj or i != index)
            for other in bpy.data.objects for i, s in enumerate(other.material_slots)),
            'CONFLICT', 'Shared material must be isolated before a local update')
    require(sum(mat.get(core.KEY) == mid for mat in bpy.data.materials) == 1,
            'CONFLICT', 'Expected one material with this managed identity')
    try:
        allow_node_layout(core, old)
        live = core.get_stored_manifest(old)
    except WorkflowError as error:
        raise Failure('CONFLICT', str(error)) from error
    require(live['target'] == target and live['material']['id'] == mid, 'CONFLICT', 'Original managed binding differs')
    baseline = json.loads(old[core.MANIFEST])
    try:
        incoming, expected, edited, preserved, overlaps = update_recipes(request, old, baseline, live, core, normalize_manifest)
    except WorkflowError as error:
        raise Failure('INVALID_REQUEST', str(error)) from error
    before, dependencies = snapshot(), external_dependencies(bpy)
    artist = material_artist_state(old)
    old_name = old.name
    group = old.node_tree.nodes['MWP Controls'].node_tree
    replaced = {('Material', old_name, None)}
    if group.users == 1 and group.get(core.KEY) == mid:
        replaced.add((group.bl_rna.identifier, group.name, None))
    _, old_image_keys = _graph_blocks(old)
    with ExitStack() as guards:
        resources = {}
        for ref in image_documents(incoming):
            path = guard_file(ref['file'], guards, ref['expected_sha256'])
            resources[path] = ref['expected_sha256']
        for path in sorted(live_image_paths(bpy, old.node_tree)):
            guard_file(path, guards)
        if edited:
            atomic_json(job / 'control-request.json', incoming)
            sockets = old.node_tree.nodes['MWP Controls'].inputs
            # Treat a confirmed overlapping value as a new explicit author edit.
            # Untargeted controls still merge through the native three-way logic.
            for name, old_value in overlaps:
                sockets[name].default_value = old_value
            try:
                applied = core.apply_material(incoming, work_dir=job / 'procedural-update')
            except WorkflowError as error:
                raise Failure('VALIDATION_FAILED', str(error)) from error
            report = job / 'update-result.json'
            atomic_json(report, {'operation': 'update', 'result': applied})
        else:
            ui.register()
            try:
                report = panel_apply(bpy, context, obj, target, job, 'procedural_update')
            finally:
                ui.unregister()
        material = obj.material_slots[index].material
        require(material and material.get(core.KEY) == mid and core.get_stored_manifest(material) == expected,
                'VALIDATION_FAILED', 'Updated material differs from the requested and preserved controls')
        require(old_name not in bpy.data.materials, 'VALIDATION_FAILED', 'Prior target material was not replaced')
        restore_material_artist_state(material, artist)
        allow_node_layout(core, material)
        groups, images = _graph_blocks(material)
        additions = groups | images | {('Material', material.name, None)}
        for key in old_image_keys:
            _, image_name, library = key
            image = bpy.data.images[image_name]
            if library is None and image.users == 0 and not image.use_fake_user and image.get(core.KEY) == mid:
                replaced.add(key)
                bpy.data.images.remove(image)
        _verify_preservation(before, snapshot(), target, obj.data.name, additions, replaced)
        # The native update builds the replacement before releasing the old name.
        # Restore that name before publishing the candidate.
        material.name = old_name
        path, verified = save_reopen(bpy, request, snapshot(), dependencies)
        require(verified and verified.name == old_name and core.get_stored_manifest(verified) == expected,
                'VALIDATION_FAILED', 'Material identity or control values changed after reopening')
        require(material_artist_state(verified) == artist, 'VALIDATION_FAILED',
                'Material or node author metadata changed after reopening')
        verify_packed_images(bpy, images, resources)

    return {'candidate': {'file': str(path), 'sha256': digest(path)},
            'source': {key: request[key] for key in ('file', 'expected_sha256')}, 'product': edition,
            'material_id': mid, 'material': old_name, 'context': request['context'], 'target': target,
            'mode': 'control_patch' if edited else 'panel_refresh', 'control_edits': edited,
            'preserved_manual_controls': preserved, 'panel_report': {'file': str(report), 'sha256': digest(report)},
            'verification': {'status': 'passed', 'reopened': True, 'non_target_content': 'preserved_in_scene_snapshot',
                             'external_dependency_paths': 'preserved', 'artist_state': 'preserved'},
            'runtime': {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(), 'pid': os.getpid()}}


def worker(request_path):
    try:
        from material_layer_draft import edit_layer_draft, load_layer_template

        request = read_json(request_path)
        handler = {'load-procedural-template': apply_procedural, 'create-procedural': apply_procedural,
                   'save-procedural-template': save_template,
                   'update-procedural': update_procedural, 'edit-layer-draft': edit_layer_draft,
                   'load-layer-template': load_layer_template}[request['action']]
        value = {'ok': True, 'data': handler(request, request_path.parent)}
    except Exception as error:
        value = {'ok': False, 'error': failure(error)}
    atomic_json(request_path.parent / 'worker-result.json', value)


def execute_worker(blender, job, timeout):
    profile = job / 'profile'
    profile.mkdir()
    env = {**os.environ, 'BLENDER_USER_RESOURCES': str(profile), 'TEMP': str(job), 'TMP': str(job), 'TMPDIR': str(job)}
    for key in ('CONFIG', 'SCRIPTS', 'EXTENSIONS', 'DATAFILES'):
        env['BLENDER_USER_' + key] = str(profile / key.lower())
    argv = [str(blender), '--background', '--factory-startup', '--offline-mode', '--disable-autoexec',
            '--threads', '2', '--python-exit-code', '23', '--python', str(ENTRY), '--', str(job / 'request.json')]
    with (job / 'worker.log').open('xb') as log:
        tree = ProcessTree(argv, cwd=str(job), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            code = tree.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            tree.terminate()
            raise Failure('TIMEOUT', 'Procedural template worker timed out') from exc
        except BaseException:
            tree.terminate()
            raise
        finally:
            tree.close()
    path = job / 'worker-result.json'
    require(code == 0 and path.is_file(), 'WORKER_FAILED', f'Worker exited {code}; see worker.log')
    return read_json(path)


def run(args):
    started = time.monotonic()
    budget = timeout_value(args.timeout)
    job = args.jobs_dir.absolute() / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    command = 'material.' + args.action
    saving_template = args.action == 'save-procedural-template'
    output_key, suffix = ('template', '.json') if saving_template else ('candidate', '.blend')
    artifacts = {'job_directory': str(job), 'result': str(job / 'result.json')}
    data = error = None
    try:
        require(args.output.is_absolute() and args.output.suffix.lower() == suffix,
                'INVALID_REQUEST', f'Output requires an absolute {suffix} path')
        with ExitStack() as guards:
            request = prepare(args, guards)
            parent = canonical(args.output.parent)
            output = parent / args.output.name
            for directory in reversed((parent, *parent.parents)):
                if directory != Path(directory.anchor):
                    guards.enter_context(FileGuard(directory, directory=True))
            guards.enter_context(PathLocks([output]))
            require(not os.path.lexists(output), 'CONFLICT', f'Output already exists: {output}')
            if not saving_template:
                # Relative Blender dependencies must resolve from the final parent.
                with tempfile.NamedTemporaryFile(prefix='.material-template-', suffix=suffix,
                                                 dir=parent, delete=False) as reserved:
                    staged = Path(reserved.name)
                guards.callback(staged.unlink, missing_ok=True)
                request['candidate_stage'] = str(staged)
            atomic_json(job / 'request.json', request)
            left = budget - (time.monotonic() - started)
            require(left > 0, 'TIMEOUT', 'Input verification exceeded the job timeout')
            observed = execute_worker(canonical(args.blender), job, left)
            if not observed['ok']:
                error = observed['error']
            else:
                result = observed['data']
                expected = result[output_key]['sha256']
                if saving_template:
                    template = job / 'template.json'
                    directory = guards.enter_context(tempfile.TemporaryDirectory(prefix='.material-template-', dir=parent))
                    staged = Path(directory) / 'template.json'
                    with FileGuard(template) as original:
                        require(original.sha256() == expected, 'CONFLICT', 'Worker template SHA changed')
                        shutil.copyfile(template, staged)
                with FileGuard(staged, renameable=True) as stage:
                    require(stage.sha256() == expected, 'CONFLICT', 'Staged output SHA differs')
                    require(time.monotonic() - started < budget, 'TIMEOUT', 'Publication exceeded the job timeout')
                    stage.rename_new(output)
                artifacts[output_key] = str(output)
                data = {**result, output_key: {'file': str(output), 'sha256': expected}, 'source_unchanged': True}
    except (ValueError, OSError, RuntimeError, Failure) as exc:
        error = failure(exc, 'IO_ERROR')
    for name in ('worker-result.json', 'worker.log'):
        if (job / name).is_file():
            artifacts[name] = str(job / name)
    result = envelope(command, job.name, data, error, artifacts)
    atomic_json(job / 'result.json', result)
    return result


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    args = None
    try:
        args = parser().parse_args()
        result = run(args)
    except (OSError, Failure) as exc:
        result = envelope(error=failure(exc, 'IO_ERROR'))
    if args and args.human:
        output_key = 'template' if args.action == 'save-procedural-template' else 'candidate'
        print(result['artifacts'][output_key] if result['ok'] else result['error']['message'])
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['ok'] else CODES[result['error']['code']]


if __name__ == '__main__':
    if '--background' in sys.argv and '--' in sys.argv:
        worker(Path(sys.argv[-1]))
    else:
        raise SystemExit(main())
