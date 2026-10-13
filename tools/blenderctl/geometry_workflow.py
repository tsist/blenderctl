"""One protected geometry work unit with per-target recovery and independent reopen."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

import bpy
from protocol import Failure, atomic_json, digest, read_json
from geometry_workflow_contract import normalize_params, normalize_inspect_params
from geometry_identity import implementation_sha256
from geometry_nodes_workflow_addon import core, inspection
from geometry_nodes_workflow_addon.contract import WorkflowError
from geometry_verifier import activate, near, stable_evaluation


def descriptor(path):
    return {'file': str(path), 'expected_sha256': digest(path)}


def _signature(params):
    body = {key: params.get(key) for key in ('file', 'expected_sha256', 'resources', 'manifest', 'cache_manifest', 'implementation_sha256')}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _resource_check(resources):
    declared = {os.path.normcase(str(Path(row['file']).resolve())): row['expected_sha256'] for row in resources}
    rows = []
    def checked(path, kind, owner, **details):
        path = str(Path(path).resolve())
        key = os.path.normcase(path)
        if key not in declared: raise Failure('CONFLICT', 'Undeclared geometry project resource: ' + path)
        if not Path(path).is_file() or digest(path) != declared[key]:
            raise Failure('CONFLICT', 'Geometry resource SHA mismatch: ' + path)
        rows.append({'kind': kind, 'owner': owner, 'file': path, 'sha256': declared[key], **details})
    for collection in ('images', 'fonts', 'libraries', 'volumes', 'sounds', 'movieclips'):
        for item in getattr(bpy.data, collection, ()):
            raw = getattr(item, 'filepath', '')
            packed = bool(getattr(item, 'packed_file', None)) or bool(getattr(item, 'packed_files', []))
            if not raw or raw == '<builtin>' or packed: continue
            if getattr(item, 'source', '') in ('TILED', 'SEQUENCE', 'MOVIE') or any(x in raw for x in ('<UDIM>', '####')):
                raise Failure('UNSUPPORTED', 'Dynamic resource needs an explicit member workflow: ' + item.name)
            path = str(Path(bpy.path.abspath(raw, library=getattr(item, 'library', None))).resolve())
            checked(path, collection, item.name, stored_path_before=raw)
    for tree in bpy.data.node_groups:
        if tree.bl_idname != 'GeometryNodeTree': continue
        for node in tree.nodes:
            for socket in node.inputs:
                if socket.bl_idname != 'NodeSocketStringFilePath': continue
                owner = tree.name + '/' + node.name + '/' + socket.identifier
                if socket.is_linked:
                    raise Failure('UNSUPPORTED', 'Linked node filepaths require an explicit path-resolution workflow: ' + owner)
                raw = socket.default_value
                if not raw: continue
                if raw.startswith('//') or not Path(raw).is_absolute() or any(x in raw for x in ('<UDIM>', '####')):
                    raise Failure('UNSUPPORTED', 'Node filepaths currently require an absolute static file: ' + owner)
                checked(raw, 'node_filepath', tree.name, node=node.name, socket=socket.identifier)
    return {'status': 'closed_for_checked_static_paths', 'external': rows,
            'scope': 'Static ID and absolute unlinked node filepaths; geometry caches are checked separately with immutable cache receipts'}


def _restore_resource_paths(closure):
    for row in closure.get('external', []):
        if row['kind'] == 'node_filepath': continue  # Absolute strings need no save-copy remapping.
        collection = getattr(bpy.data, row['kind'], ())
        item = collection.get(row['owner']) if hasattr(collection, 'get') else None
        if item is not None: item.filepath = row['file']


def _loaded_cache_check(params):
    """Existing persistent GN caches need an explicit immutable receipt."""
    existing = []
    for obj in bpy.data.objects:
        for mod in obj.modifiers:
            if mod.type != 'NODES' or not mod.node_group: continue
            for item in mod.bakes:
                target = item.bake_target if item.bake_target != 'INHERIT' else mod.bake_target
                raw = item.directory if item.use_custom_path else mod.bake_directory
                if target == 'DISK' and raw:
                    directory = Path(bpy.path.abspath(raw)).resolve()
                    if any(p.is_file() for p in directory.rglob('*')):
                        existing.append((obj.name, mod.name, item.bake_id))
    if existing and not params.get('cache_manifest'):
        raise Failure('CONFLICT', 'Persistent geometry caches require cache_manifest: ' + str(existing))
    if params.get('cache_manifest'):
        from geometry_temporal import validate_cache, validate_loaded_bindings
        cache = validate_cache(params['cache_manifest'],
            candidate={'file': params['file'], 'sha256': params['expected_sha256']}, identity=params['implementation_sha256'])
        validate_loaded_bindings(cache)
        recorded = {(b['object'], b['modifier'], b['bake_id']) for b in cache['bakes']}
        if set(existing) != recorded:
            raise Failure('CONFLICT', 'Loaded persistent cache set differs from receipt')
        return cache


def _replay_loaded_cache(cache):
    """Compare saved temporal witnesses in their declared scrambled order."""
    frame_before = bpy.context.scene.frame_current
    references = {sample['frame']: sample for sample in cache['samples']}
    targets = {target['id']: target for target in cache['targets']}
    rows = []
    try:
        for frame in cache['readback_order']:
            bpy.context.scene.frame_set(frame); bpy.context.view_layer.update()
            row = {'frame': frame, 'status': 'passed', 'targets': []}
            rows.append(row)
            for expected in references[frame]['targets']:
                target = targets[expected['target']]
                selected = [o for o in cache['observations'] if
                    (not o.get('object') or o['object'] == target['object']) and
                    (not o.get('modifier') or o['modifier'] == target['modifier'])]
                actual = stable_evaluation(inspection.inspect_target(target['object'], target['modifier'], selected), True)
                mismatch = near(expected['evaluation'], actual)
                row['targets'].append({'target': target['id'], 'status': 'failed' if mismatch else 'passed', 'mismatch': mismatch})
                if mismatch:
                    row['status'] = 'failed'
                    raise Failure('VALIDATION_FAILED', 'Cache replay differs at frame ' + str(frame) + ': ' + mismatch)
        return {'status': 'passed', 'readback_order': cache['readback_order'], 'samples': rows}
    finally:
        bpy.context.scene.frame_set(frame_before); bpy.context.view_layer.update()


def _source_groups(exclude=()):
    return {tree.name: core.tree_fingerprint(tree) for tree in bpy.data.node_groups
            if tree.bl_idname == 'GeometryNodeTree' and tree.name not in set(exclude)}


def _retain_unlinked_ids():
    """Keep existing zero-user data through Blender's save/open checkpoints.

    This is candidate-only persistence metadata, recorded as an explicit
    exception; the source file remains unchanged and UI apply copies targets.
    """
    rows = []
    for prop in bpy.data.bl_rna.properties:
        if prop.type != 'COLLECTION': continue
        for item in getattr(bpy.data, prop.identifier):
            if isinstance(item, bpy.types.ID) and item.users == 0 and not item.use_fake_user and not item.library:
                try: item.use_fake_user = True
                except (AttributeError, RuntimeError, TypeError): continue
                rows.append({'collection': prop.identifier, 'name': item.name, 'use_fake_user_before': False, 'use_fake_user_after': True})
    return rows


def _check_recipe(target):
    recipe = target.get('recipe')
    current = {}
    obj = bpy.data.objects.get(target['object'])
    modifier = obj.modifiers.get(target['modifier']) if obj else None
    if modifier and modifier.type == 'NODES' and modifier.node_group:
        current = core.parameter_values(modifier)
        if not recipe:
            try:
                recipe = json.loads(modifier.node_group.get(core.STATE, '{}')).get('recipe')
            except (WorkflowError,ValueError,TypeError): pass
    if recipe in ('spiral_stair','curve_fence','surface_scatter'):
        from geometry_nodes_workflow_addon.recipes import recipe_defaults
        values = recipe_defaults(recipe)
        values.update({key:value for key,value in current.items() if key in values})
        values.update(target.get('parameters', {}))
        if not math.isclose(bpy.context.scene.unit_settings.scale_length,1,abs_tol=1e-9):
            raise WorkflowError('UNSUPPORTED','Built-in metre-based recipes currently require scene unit scale 1; generic graphs retain native units')
        if recipe == 'spiral_stair':
            if type(values['steps']) is not int or values['steps'] < 1 or values['height'] <= 0:
                raise WorkflowError('INVALID_REQUEST','Positive height and integer steps >=1 required')
            if values['column_radius'] >= values['radius']:
                raise WorkflowError('INVALID_REQUEST', 'column_radius must be smaller than radius')
            if values['tread_thickness'] >= values['height'] / values['steps']:
                raise WorkflowError('INVALID_REQUEST', 'tread_thickness must be smaller than per-step rise')
        elif recipe == 'curve_fence' and values.get('use_external_path'):
            reference = values.get('path_object')
            name = reference.get('name') if isinstance(reference, dict) else reference
            obj = bpy.data.objects.get(name or '')
            if obj is None or obj.type != 'CURVE' or obj.name == target['object']:
                raise WorkflowError('INVALID_REQUEST', 'External fence path must be an explicit non-self Curve object')


def _outside_consumers(targets):
    names = {target['object'] for target in targets}
    conflicts = []
    def tree_refs(tree, seen):
        if not tree or tree.as_pointer() in seen: return set()
        seen.add(tree.as_pointer()); refs = set()
        for node in tree.nodes:
            for socket in node.inputs:
                value = getattr(socket, 'default_value', None)
                if isinstance(value, bpy.types.Object): refs.add(value.name)
                if isinstance(value, bpy.types.Collection): refs.update(o.name for o in value.all_objects)
            if getattr(node, 'node_tree', None): refs |= tree_refs(node.node_tree, seen)
        return refs
    for obj in bpy.data.objects:
        if obj.name in names: continue
        refs = set()
        for modifier in obj.modifiers:
            if modifier.type == 'NODES' and modifier.node_group:
                refs |= tree_refs(modifier.node_group, set())
                for value in core.parameter_values(modifier).values():
                    if isinstance(value, dict) and value.get('kind') == 'OBJECT': refs.add(value['name'])
                    if isinstance(value, dict) and value.get('kind') == 'COLLECTION':
                        collection = bpy.data.collections.get(value['name'])
                        if collection: refs.update(o.name for o in collection.all_objects)
            for prop in modifier.bl_rna.properties:
                if prop.type == 'POINTER':
                    value = getattr(modifier, prop.identifier, None)
                    if isinstance(value, bpy.types.Object): refs.add(value.name)
        if refs & names: conflicts.append({'consumer': obj.name, 'targets': sorted(refs & names)})
    return conflicts


def _independent_verify(candidate, context, targets, job, identity, protection=None, temporal=None):
    output = job / 'geometry-reopen.json'
    request = {'candidate': {'file': str(candidate), 'sha256': digest(candidate)},
               'context': context, 'targets': targets, 'implementation_sha256': identity, 'output': str(output), 'protection': protection,
               'temporal': temporal}
    path = job / 'geometry-verification-request.json'; atomic_json(path, request)
    command = [bpy.app.binary_path, '--background', '--factory-startup', '--disable-autoexec',
               '--python-exit-code', '1', '--python', str(Path(__file__).with_name('geometry_verifier.py')), '--', str(path)]
    with (job / 'geometry-reopen.log').open('w', encoding='utf-8') as log:
        process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=180,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if process.returncode != 0 or not output.is_file():
        raise Failure('VALIDATION_FAILED', 'Independent geometry reopen failed; see geometry-reopen.log')
    report = read_json(output)
    if report.get('status') != 'passed': raise Failure('VALIDATION_FAILED', 'Independent geometry verification failed')
    return {'status': 'passed', 'report': descriptor(output), 'candidate_sha256': digest(candidate)}


def _preview(target, spec, job):
    from mathutils import Vector
    original_scene, original_layer = bpy.context.scene, bpy.context.view_layer
    source = bpy.data.objects[target['object']]
    summary = inspection.inspect_target(source.name, target['modifier'])
    bounds = summary.get('evaluated', {}).get('bounds')
    all_points = []
    dg = bpy.context.evaluated_depsgraph_get()
    evaluated = source.evaluated_get(dg)
    mesh = evaluated.to_mesh()
    try: all_points.extend(evaluated.matrix_world @ v.co for v in mesh.vertices)
    finally: evaluated.to_mesh_clear()
    for instance in dg.object_instances:
        if instance.is_instance and instance.parent and instance.parent.original == source:
            inst_mesh = instance.object.to_mesh()
            try: all_points.extend(instance.matrix_world @ v.co for v in inst_mesh.vertices)
            finally: instance.object.to_mesh_clear()
    if not all_points: raise Failure('VALIDATION_FAILED', 'Cannot preview empty evaluated geometry')
    lo = Vector(tuple(min(p[i] for p in all_points) for i in range(3)))
    hi = Vector(tuple(max(p[i] for p in all_points) for i in range(3)))
    center = (lo + hi) * .5; extent = max(hi - lo)
    scene = bpy.data.scenes.new('GNW_Preview_' + target['id'])
    scene.frame_set(original_scene.frame_current)
    scene.render.fps = original_scene.render.fps; scene.render.fps_base = original_scene.render.fps_base
    temporary = []; light_data = []
    material = bpy.data.materials.new('GNW_PreviewMaterial'); material.use_nodes = True
    principled = material.node_tree.nodes.get('Principled BSDF')
    principled.inputs['Base Color'].default_value = (.24, .33, .42, 1)
    principled.inputs['Roughness'].default_value = .35
    principled.inputs['Metallic'].default_value = .45
    world = bpy.data.worlds.new('GNW_PreviewWorld'); world.use_nodes = True
    world.node_tree.nodes.get('Background').inputs['Color'].default_value = (.12, .14, .18, 1)
    world.node_tree.nodes.get('Background').inputs['Strength'].default_value = .5
    scene.world = world
    clone = source.copy(); scene.collection.objects.link(clone); temporary.append(clone)
    camera_data = bpy.data.cameras.new('GNW_PreviewCamera'); camera = bpy.data.objects.new('GNW_PreviewCamera', camera_data)
    scene.collection.objects.link(camera); temporary.append(camera); scene.camera = camera
    direction = Vector((1.5, -2.1, 1.3)).normalized()
    camera.location = center + direction * max(extent * 2.2, 2)
    camera.rotation_euler = (center - camera.location).to_track_quat('-Z', 'Y').to_euler()
    camera_data.type = 'ORTHO'; camera_data.ortho_scale = max(extent * 1.4, 1)
    for index, position in enumerate(((1, -1, 2), (-1, 0, 1.2))):
        data = bpy.data.lights.new('GNW_PreviewArea' + str(index), 'AREA'); data.energy = 1100 * max(extent, 1) ** 2
        light_data.append(data)
        data.shape = 'DISK'; data.size = max(extent, 1)
        light = bpy.data.objects.new(data.name, data); scene.collection.objects.link(light); temporary.append(light)
        light.location = center + Vector(position) * max(extent, 1)
        light.rotation_euler = (center - light.location).to_track_quat('-Z', 'Y').to_euler()
    try:
        bpy.context.window.scene = scene
        layer = scene.view_layers[0]; layer.material_override = material
        scene.render.engine = spec.get('engine', 'CYCLES')
        if scene.render.engine == 'CYCLES': scene.cycles.device = 'CPU'; scene.cycles.samples = spec.get('samples', 24)
        scene.render.resolution_x = spec.get('width', 960); scene.render.resolution_y = spec.get('height', 960)
        scene.render.resolution_percentage = 100; scene.render.film_transparent = False
        scene.render.image_settings.file_format = 'PNG'; scene.render.image_settings.color_mode = 'RGBA'
        scene.view_settings.view_transform = 'AgX'
        token = hashlib.sha256(target['id'].encode()).hexdigest()[:16]
        path = job / ('geometry-preview-' + token + '.png'); scene.render.filepath = str(path)
        bpy.ops.render.render(write_still=True)
        return {'file': str(path), 'sha256': digest(path), 'width': scene.render.resolution_x,
                'height': scene.render.resolution_y, 'engine': scene.render.engine, 'device': 'CPU' if scene.render.engine == 'CYCLES' else 'EEVEE',
                'scope': 'Isolated technical lighting/material override; source geometry and user scene retained'}
    finally:
        bpy.context.window.scene = original_scene; bpy.context.window.view_layer = original_layer
        for obj in temporary:
            if obj.name in bpy.data.objects: bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.scenes.remove(scene)
        for data in (camera_data, material, world):
            collection = bpy.data.cameras if isinstance(data, bpy.types.Camera) else bpy.data.materials if isinstance(data, bpy.types.Material) else bpy.data.worlds
            if data.users == 0: collection.remove(data)
        for data in light_data:
            if data.users == 0: bpy.data.lights.remove(data)


def inspect(params, job):
    params = normalize_inspect_params(params)
    cache = None
    if params.get('cache_manifest'):
        from geometry_temporal import validate_cache
        cache = validate_cache(params['cache_manifest'],
            candidate={'file': params['file'], 'sha256': params['expected_sha256']}, identity=params['implementation_sha256'])
    bpy.ops.wm.open_mainfile(filepath=params['file'], load_ui=False, use_scripts=False)
    _loaded_cache_check(params)
    context = params.get('context') or {'scene': bpy.context.scene.name, 'view_layer': bpy.context.view_layer.name, 'frame': bpy.context.scene.frame_current}
    closure = _resource_check(params['resources'])
    activate(context)
    if cache:
        from geometry_cache_reader import prepare_read_snapshot
        from geometry_temporal import validate_loaded_bindings
        validate_loaded_bindings(cache)
        cache=prepare_read_snapshot(cache,job,
            {'file':params['file'],'sha256':params['expected_sha256']},cache['observe_frames'])
        activate(context)
    replay = _replay_loaded_cache(cache) if cache else None
    reader_witness=None
    targets = params.get('targets') or [{'object': o.name, 'modifier': m.name} for o in bpy.context.scene.objects for m in o.modifiers if m.type == 'NODES']
    rows = [inspection.inspect_target(t['object'], t.get('modifier'), params.get('observations')) for t in targets]
    report = {'geometry_report_version': '1.0', 'report_version': '1.0', 'operation': 'inspect', 'status': 'passed',
              'source': {'file': params['file'], 'sha256': params['expected_sha256']}, 'targets': rows,
              'blender': {'version': bpy.app.version_string, 'version_tuple': list(bpy.app.version), 'build_hash': bpy.app.build_hash.decode()},
              'implementation_sha256': params['implementation_sha256'],
              'resources': closure, 'checks': {'read_only': 'passed'},
              'cache_validation': {'status': 'passed', 'manifest': params['cache_manifest']} if cache else None,
              'cache_replay': replay, 'cache_read_snapshot':reader_witness,
              'discovery': inspection.discover_native_nodes() if params.get('discover') else None}
    if cache:
        from geometry_temporal import validate_cache
        reader_path=Path(job)/'geometry-cache-read-snapshot.json'
        witness=read_json(reader_path)
        validate_cache(witness['manifest'],candidate={'file':params['file'],'sha256':params['expected_sha256']},identity=params['implementation_sha256'])
        witness['status']='revalidated_after_inspect';atomic_json(reader_path,witness)
        report['cache_read_snapshot']=descriptor(reader_path)
    path = Path(job) / 'geometry-report.json'; atomic_json(path, report)
    return {'status': report['status'], 'report': descriptor(path), 'targets': rows,
            'discovered_count': report['discovery'].get('count') if isinstance(report['discovery'], dict) else None}


def run(params, job):
    params = normalize_params(params); job = Path(job); manifest = params['manifest']
    temporal_spec = manifest.get('temporal')
    if temporal_spec and params.get('resume'):
        raise Failure('UNSUPPORTED', 'Temporal recovery restarts from the protected source in a new job; partial frame caches are not resume inputs')
    if temporal_spec:
        from geometry_temporal import execute, finalize_manifest, prepare_target, validate_cache
    if params.get('cache_manifest'):
        from geometry_temporal import validate_cache
        validate_cache(params['cache_manifest'],
            candidate={'file': params['file'], 'sha256': params['expected_sha256']}, identity=params['implementation_sha256'])
    started = time.monotonic(); identity = params['implementation_sha256']; signature = _signature(params)
    if params.get('file'):
        bpy.ops.wm.open_mainfile(filepath=params['file'], load_ui=False, use_scripts=False)
        _loaded_cache_check(params)
    else:
        if not manifest.get('initial_scene'): raise Failure('INVALID_REQUEST', 'New geometry project requires initial_scene')
        for obj in list(bpy.data.objects): bpy.data.objects.remove(obj, do_unlink=True)
        bpy.context.scene.name = manifest['initial_scene']
        bpy.context.scene.unit_settings.system='METRIC';bpy.context.scene.unit_settings.scale_length=1
    closure = _resource_check(params['resources'])
    activate(manifest['context'])
    _restore_resource_paths(closure)
    retained_ids = _retain_unlinked_ids()
    progress = {'geometry_progress_version': '1.0', 'status': 'running', 'signature': signature,
                'implementation_sha256': identity, 'blender_build': bpy.app.build_hash.decode(),
                'targets': [], 'completed': 0, 'total': len(manifest['targets']), 'checkpoint': None, 'resources': closure, 'retained_ids': retained_ids}
    if params.get('resume'):
        prior = read_json(params['resume']['file'])
        if prior.get('signature') != signature or prior.get('implementation_sha256') != identity or prior.get('blender_build') != progress['blender_build']:
            raise Failure('CONFLICT', 'Geometry resume source, manifest, build or implementation differs')
        checkpoint = prior.get('checkpoint')
        if not checkpoint or digest(checkpoint['file']) != checkpoint['expected_sha256']:
            raise Failure('CONFLICT', 'Geometry resume checkpoint is absent or changed')
        bpy.ops.wm.open_mainfile(filepath=checkpoint['file'], load_ui=False, use_scripts=False)
        _restore_resource_paths(prior.get('resources', {})); activate(manifest['context'])
        progress['targets'] = prior['targets']; progress['completed'] = prior['completed']
        retained_ids = prior.get('retained_ids', [])
        progress['retained_ids'] = retained_ids
    outside = _outside_consumers(manifest['targets'])
    if outside: raise Failure('CONFLICT', 'Unselected objects consume target geometry: ' + json.dumps(outside))
    names = [t['object'] for t in manifest['targets']]
    before = inspection.scene_fingerprint(exclude_objects=names, persistence_exceptions=retained_ids)
    protection_diagnostics = {}
    def protection_status():
        try:
            matches = inspection.scene_fingerprint(exclude_objects=names, persistence_exceptions=retained_ids) == before
            if not matches: protection_diagnostics['error'] = {'code': 'VALIDATION_FAILED', 'message': 'Non-target protection failed after batch'}
            return 'passed' if matches else 'failed'
        except Exception as exc:
            protection_diagnostics['error'] = {'code': getattr(exc, 'code', 'VALIDATION_FAILED'), 'message': str(exc)}
            return 'failed'

    atomic_json(job / 'geometry-before.json', {'non_target_sha256': before, 'resources': closure})
    atomic_json(job / 'geometry-manifest.json', manifest)
    atomic_json(job / 'geometry-progress.json', progress)
    completed = {row['id'] for row in progress['targets'] if row.get('status') == 'passed'}
    for index, target in enumerate(manifest['targets']):
        if target['id'] in completed: continue
        checkpoint = job / ('geometry-checkpoint-' + str(index) + '.blend')
        retained_ids.extend(_retain_unlinked_ids()); progress['retained_ids'] = retained_ids
        bpy.ops.wm.save_as_mainfile(filepath=str(checkpoint), check_existing=False, relative_remap=False, copy=True)
        _restore_resource_paths(closure)
        owned = []
        obj = bpy.data.objects.get(target['object'])
        modifier = obj.modifiers.get(target['modifier']) if obj else None
        if modifier and modifier.type == 'NODES' and modifier.node_group and target['action'] in ('update','parameters'):
            owned.append(modifier.node_group.name)
            try:
                node = core._core_node(modifier.node_group)
                if node and node.node_tree: owned.append(node.node_tree.name)
            except WorkflowError: pass
        groups = _source_groups(owned)
        try:
            _check_recipe(target)
            action = core.apply_target(target, before_evaluate=lambda: _resource_check(params['resources']),
                before_first_evaluation=(lambda mod: prepare_target(temporal_spec, target, job, modifier=mod)) if temporal_spec else None)
            closure = _resource_check(params['resources'])
            if inspection.scene_fingerprint(exclude_objects=names, persistence_exceptions=retained_ids) != before:
                raise WorkflowError('VALIDATION_FAILED', 'Non-target content changed')
            for name, sha in groups.items():
                if name not in bpy.data.node_groups or core.tree_fingerprint(bpy.data.node_groups[name]) != sha:
                    raise WorkflowError('VALIDATION_FAILED', 'Original/source group content changed: ' + name)
            binding = core.inspect_binding(target['object'], target['modifier'])
            if binding.get('has_temporal_nodes') and not temporal_spec:
                raise WorkflowError('INVALID_REQUEST', 'Dynamic geometry run requires a temporal bake specification')
            observations = [o for o in manifest.get('observations', []) if not o.get('object') or o['object'] == target['object']]
            evaluation = inspection.inspect_target(target['object'], target['modifier'], observations)
            row = {'id': target['id'], 'object': target['object'], 'modifier': target['modifier'], 'status': 'passed',
                   'binding': binding, 'core_sha256': binding.get('core_sha256'), 'parameters': binding.get('parameters'),
                   'action': action, 'evaluation': evaluation}
        except Exception as exc:
            bpy.ops.wm.open_mainfile(filepath=str(checkpoint), load_ui=False, use_scripts=False)
            _restore_resource_paths(closure); activate(manifest['context'])
            row = {'id': target['id'], 'object': target['object'], 'modifier': target['modifier'], 'status': 'failed',
                   'rolled_back': True, 'error': {'code': getattr(exc, 'code', 'VALIDATION_FAILED'), 'message': str(exc)}}
        progress['targets'] = [old for old in progress['targets'] if old['id'] != row['id']] + [row]
        progress['completed'] = len(progress['targets']); progress['current_target'] = target['id']
        stable = job / ('geometry-completed-' + str(index) + '.blend')
        retained_ids.extend(_retain_unlinked_ids()); progress['retained_ids'] = retained_ids
        bpy.ops.wm.save_as_mainfile(filepath=str(stable), check_existing=False, relative_remap=False, copy=True)
        progress['checkpoint'] = descriptor(stable)
        atomic_json(job / 'geometry-progress.json', progress)
    successful = [row for row in progress['targets'] if row['status'] == 'passed']
    temporal_report = None
    if temporal_spec:
        def temporal_progress(value):
            progress['temporal'] = value
            atomic_json(job / 'geometry-progress.json', progress)
        try:
            if len(successful) != len(manifest['targets']):
                raise Failure('VALIDATION_FAILED', 'Temporal execution requires all graph targets to pass; checkpoints and progress are retained')
            temporal_report = execute(temporal_spec, manifest['targets'], manifest.get('observations', []), job,
                                      progress_callback=temporal_progress)
        except Exception as exc:
            recovery = {'strategy': 'restart_temporal_from_source_in_a_new_job',
                        'source': {'file': params['file'], 'expected_sha256': params['expected_sha256']} if params.get('file') else None,
                        'manifest': descriptor(job / 'geometry-manifest.json'), 'checkpoint': progress['checkpoint'],
                        'partial_caches': 'diagnostic_only; not accepted for replay or resume'}
            progress.update(status='failed', temporal_error={'code': getattr(exc, 'code', 'VALIDATION_FAILED'), 'message': str(exc)}, recovery=recovery)
            atomic_json(job / 'geometry-progress.json', progress)
            failure_report = {'geometry_report_version': '1.0', 'report_version': '1.0', 'operation': 'run',
                              'status': 'failed', 'targets': progress['targets'], 'manifest': manifest,
                              'checks': {'non_target_protection': protection_status()},
                              'implementation_sha256': identity, 'recovery': recovery,
                              'finalization': {'status': 'failed', 'stage': 'temporal', 'error': progress['temporal_error']}}
            diagnostic = job / 'geometry-temporal-report.json'
            if diagnostic.is_file(): failure_report['temporal_diagnostic'] = descriptor(diagnostic)
            atomic_json(job / 'geometry-report.json', failure_report)
            raise
        activate(manifest['context'])
    # Later targets may intentionally change an earlier target's dependencies.
    # Witness the actual final batch, rather than the intermediate checkpoint.
    for row in successful:
        observations = [o for o in manifest.get('observations', []) if not o.get('object') or o['object'] == row['object']]
        row['binding'] = core.inspect_binding(row['object'], row['modifier'])
        row['evaluation'] = inspection.inspect_target(row['object'], row['modifier'], observations)
        row['parameters'] = row['binding'].get('parameters'); row['core_sha256'] = row['binding'].get('core_sha256')
    outcome = 'passed' if len(successful) == len(manifest['targets']) else 'partial' if successful else 'failed'
    report = {'geometry_report_version': '1.0', 'report_version': '1.0', 'operation': 'run',
              'status': 'failed', 'target_outcome': outcome, 'finalization': {'status': 'pending'},
              'source': {'file': params['file'], 'sha256': params['expected_sha256']} if params.get('file') else None,
              'implementation_sha256': identity, 'blender': {'version': bpy.app.version_string, 'version_tuple': list(bpy.app.version), 'build_hash': bpy.app.build_hash.decode()},
              'manifest': manifest, 'targets': progress['targets'], 'resources': closure,
              'temporal': temporal_report,
              'checks': {'non_target_protection': protection_status()},
              'previews': [], 'verification': {}, 'metrics': {}, 'retained_ids': retained_ids,
              'protection_scope': 'Non-target content with only exact recorded local-ID false-to-true fake-user transitions canonicalized; all other fields and graphs remain protected; source bytes unchanged'}
    if report['checks']['non_target_protection'] != 'passed':
        error = protection_diagnostics.get('error', {'code': 'VALIDATION_FAILED', 'message': 'Non-target protection failed after batch'})
        recovery = {'strategy': 'restart_from_protected_source_in_a_new_job',
                    'source': {'file': params['file'], 'expected_sha256': params['expected_sha256']} if params.get('file') else None,
                    'manifest': descriptor(job / 'geometry-manifest.json'), 'checkpoint': progress['checkpoint'],
                    'partial_caches': 'diagnostic_only; not accepted for replay or resume'}
        report.update(status='failed', recovery=recovery,
                      finalization={'status': 'failed', 'stage': 'non_target_protection', 'error': error})
        path = job / 'geometry-report.json'; atomic_json(path, report)
        progress.update(status='failed', error=error, recovery=recovery, report=descriptor(path))
        atomic_json(job / 'geometry-progress.json', progress)
        raise Failure(error['code'], error['message'])
    candidate = job / 'geometry-candidate.blend'
    path = job / 'geometry-report.json'
    try:
        retained_ids.extend(_retain_unlinked_ids())
        bpy.ops.wm.save_as_mainfile(filepath=str(candidate), check_existing=False, relative_remap=False)
        report['candidate'] = {'file': str(candidate), 'sha256': digest(candidate)}; report['candidate_sha256'] = digest(candidate)
        if temporal_report:
            temporal_report['cache_manifest'] = finalize_manifest(temporal_report, candidate, identity)
        atomic_json(path, report)
    except Exception as exc:
        report['finalization'] = {'status': 'failed', 'stage': 'candidate_persistence',
            'error': {'code': getattr(exc, 'code', 'IO_ERROR'), 'message': str(exc)}}
        report['metrics'] = {'wall_seconds': time.monotonic() - started}
        report['recovery'] = {'checkpoint': progress['checkpoint'],
            'strategy': 'restart_from_protected_source_in_new_job', 'candidate_accepted': False}
        atomic_json(path, report)
        progress.update(status='failed', report=descriptor(path), recovery=report['recovery'])
        atomic_json(job / 'geometry-progress.json', progress)
        raise
    try:
        if successful:
            witnesses = [{'id': row['id'], 'object': row['object'], 'modifier': row['modifier'],
                          'binding': row['binding'], 'evaluation': row['evaluation'],
                          'observations': [o for o in manifest.get('observations', []) if not o.get('object') or o['object'] == row['object']]} for row in successful]
            report['verification']['independent_reopen'] = _independent_verify(candidate, manifest['context'], witnesses, job, identity,
                {'exclude_objects': names, 'non_target_sha256': before, 'persistence_exceptions': retained_ids}, temporal_report)
            if manifest.get('preview', {}).get('enabled'):
                for row in successful: report['previews'].append(_preview(row, manifest['preview'], job))
        report['status'] = outcome; report['finalization'] = {'status': 'passed'}
    except Exception as exc:
        report['status'] = 'failed'
        report['finalization'] = {'status': 'failed', 'error': {'code': getattr(exc, 'code', 'VALIDATION_FAILED'), 'message': str(exc)}}
        report['metrics'] = {'wall_seconds': time.monotonic() - started}
        atomic_json(path, report)
        progress['status'] = 'failed'; progress['report'] = descriptor(path)
        atomic_json(job / 'geometry-progress.json', progress)
        raise
    report['metrics'] = {'wall_seconds': time.monotonic() - started, 'targets_passed': len(successful),
                         'targets_failed': len(manifest['targets']) - len(successful)}
    progress['status'] = report['status']; progress['report'] = descriptor(path)
    atomic_json(path, report); progress['report'] = descriptor(path); atomic_json(job / 'geometry-progress.json', progress)
    if not successful: raise Failure('VALIDATION_FAILED', 'All geometry targets failed; read geometry-report.json')
    return {'status': report['status'], 'candidate': str(candidate), 'candidate_sha256': digest(candidate),
            'report': descriptor(path), 'progress': descriptor(job / 'geometry-progress.json'),
            'targets': [{k: row[k] for k in ('id', 'object', 'modifier', 'status')} for row in report['targets']],
            'previews': report['previews'], 'verification': report['verification'],
            'cache_manifest': temporal_report['cache_manifest'] if temporal_report else None}
