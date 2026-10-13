"""Static M1/M2 saved-file compositor Host with independent candidate and output readers."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from contextlib import ExitStack, contextmanager

import bpy
from protocol import Failure, atomic_json, digest, read_json
from compositing_workflow_contract import normalize_params, normalize_inspect_params
from compositing_identity import implementation_sha256
from compositing_workflow_addon import core
from compositing_workflow_addon.contract import WorkflowError
from compositing_verifier import (near, runtime_identity, scene_configuration, resource_snapshot, decode_image,
                                 color_identity, output_color_space, color_file_descriptors,
                                 image_file_snapshot, binding_snapshot, render_pass_snapshot)
import compositing_media as media
from compositing_temporal import project as project_temporal, inflate as inflate_temporal, TemporalProjectionError, fresh_verification_budget


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except WorkflowError as exc:
        error = Failure(exc.code, str(exc))
        error.details = getattr(exc, 'details', None)
        raise error from exc


def _descriptor(path):
    return {'file': str(Path(path).resolve()), 'sha256': digest(path)}


def _signature(params):
    original = {key: value for key, value in params.items() if key != 'resume'}
    return hashlib.sha256(json.dumps(original, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def _inputs(params, stack):
    from filesystem import FileGuard
    rows = [{'file': params['file'], 'expected_sha256': params['expected_sha256']}, *params['resources']]
    source = params.get('manifest', {}).get('source', {})
    if source.get('kind') == 'image':
        rows.append({'file': source['file'], 'expected_sha256': source['expected_sha256']})
    for node in params.get('manifest', {}).get('graph', {}).get('nodes', []):
        binding = node.get('binding', {})
        if binding.get('kind') == 'image':
            rows.append({'file': binding['file'], 'expected_sha256': binding['expected_sha256']})
    if params.get('resume'):
        rows.append(params['resume'])
    rows.extend(media.documents(params.get('manifest', {}), params.get('preview', {}).get('video')))
    checked = {}
    for row in rows:
        path = str(Path(row['file']).resolve())
        key = os.path.normcase(path)
        if key in checked:
            if checked[key]['sha256'] != row['expected_sha256']:
                raise Failure('CONFLICT', 'Conflicting input SHAs: ' + path)
            continue
        guard = stack.enter_context(FileGuard(path))
        actual = guard.sha256()
        if actual != row['expected_sha256']:
            raise Failure('CONFLICT', 'Compositor input SHA mismatch: ' + path)
        checked[key] = {'file': path, 'sha256': actual, 'guard': guard}
    if implementation_sha256() != params['implementation_sha256']:
        raise Failure('CONFLICT', 'Compositor implementation changed before execution')
    return checked


def _recheck(checked, params):
    for row in checked.values():
        if row['guard'].sha256() != row['sha256']:
            raise Failure('CONFLICT', 'Protected input changed: ' + row['file'])
    if implementation_sha256() != params['implementation_sha256']:
        raise Failure('CONFLICT', 'Compositor implementation changed during execution')


def _guard_color_inputs(color, stack, checked):
    """Lock precisely the discovered files used by this color-identity proof."""
    from filesystem import FileGuard
    rows = color_file_descriptors(color)
    for row in rows:
        key = os.path.normcase(row['file'])
        if key in checked:
            if checked[key]['sha256'] != row['sha256']:
                raise Failure('CONFLICT', 'Color proof and protected input SHA differ: ' + row['file'])
            continue
        guard = stack.enter_context(FileGuard(row['file']))
        if guard.sha256() != row['sha256']:
            raise Failure('CONFLICT', 'Color proof input changed before guard: ' + row['file'])
        checked[key] = {**row, 'guard': guard}
    return {'status': 'guarded' if rows else 'not_observed', 'files': rows, 'count': len(rows),
            'scope': 'SHA-identified OCIO configuration and resolved LUT files only; guard held for this request'}


def _canonicalize(closure):
    changed = []
    # Library locations are canonical first; linked ID-relative members retain
    # their original library-relative spelling and resolve against this anchor.
    rows = sorted(closure['external'], key=lambda row: row['kind'] != 'libraries')
    for row in rows:
        item = getattr(bpy.data, row['kind']).get(row['name'])
        if item is None:
            raise Failure('CONFLICT', 'Dependency ID disappeared before remap')
        if not item.library and item.filepath != row['file']:
            changed.append({'kind': row['kind'], 'name': row['name'], 'before': item.filepath, 'after': row['file']})
            item.filepath = row['file']
    return changed


def _inspection_file_preflight(scene, closure):
    """M2 container inventory before core hydration; no actual layer selection inferred."""
    try:
        state = json.loads(scene.get(core.STATE, '{}'))
    except (TypeError, ValueError):
        return {'status': 'not_run_invalid_state', 'inputs': []}
    if not isinstance(state, dict):
        return {'status': 'not_run_invalid_state', 'inputs': []}
    if state.get('workflow_version') != '1.1':
        return {'status': 'not_applicable_no_m2_state', 'inputs': []}
    rows = []
    for resource in closure['external']:
        if resource['kind'] != 'images':
            continue
        image = bpy.data.images.get(resource['name'])
        spec = {'file': resource['file'], 'expected_sha256': resource['sha256'],
                'colorspace': image.colorspace_settings.name, 'alpha_mode': image.alpha_mode}
        rows.append({'image': image.name, 'independent_file': image_file_snapshot(spec, inventory_only=True)})
    return {'status': 'passed', 'inputs': rows,
            'scope': 'Declared external Image containers decoded before core structure/identity checks; no layer is assumed'}


def _trees():
    rows = list(bpy.data.node_groups)
    rows += [item.node_tree for item in [*bpy.data.materials, *bpy.data.worlds] if item.node_tree]
    rows += [scene.compositing_node_group for scene in bpy.data.scenes if scene.compositing_node_group]
    seen = set()
    for tree in rows:
        if tree.as_pointer() not in seen:
            seen.add(tree.as_pointer())
            yield tree


def _static_execution_gate(resources):
    """M1-specific explicit adapter; never delegates to a permissive bypass."""
    from rigging import animation_safe
    animation_safe()  # All ID scripted drivers/tweak modes remain protected.
    if bpy.data.cache_files:
        raise Failure('UNSUPPORTED', 'M1 has no persistent cache-file contract')
    for scene in bpy.data.scenes:
        if scene.rigidbody_world:
            raise Failure('UNSUPPORTED', 'M1 requires a dedicated rigid-body cache workflow')
    for obj in bpy.data.objects:
        if obj.particle_systems:
            raise Failure('UNSUPPORTED', 'M1 has no particle/point-cache receipt contract')
        for modifier in obj.modifiers:
            if modifier.type in ('CLOTH', 'SOFT_BODY', 'FLUID', 'MESH_CACHE', 'MESH_SEQUENCE_CACHE', 'DYNAMIC_PAINT'):
                raise Failure('UNSUPPORTED', 'M1 has no modifier cache contract: ' + obj.name + '/' + modifier.name)
            if modifier.type == 'NODES' and modifier.node_group:
                for bake in getattr(modifier, 'bakes', ()):
                    if getattr(bake, 'directory', '') or getattr(modifier, 'bake_directory', ''):
                        raise Failure('UNSUPPORTED', 'M1 cannot validate node bake cache paths')
    declared = {os.path.normcase(str(Path(row['file']).resolve())): row['expected_sha256'] for row in resources}
    node_paths = []
    for tree in _trees():
        for node in tree.nodes:
            if node.bl_idname in ('ShaderNodeScript', 'GeometryNodeBake', 'GeometryNodeSimulationInput', 'GeometryNodeSimulationOutput'):
                raise Failure('UNSUPPORTED', 'M1 has no scripted/dynamic node contract: ' + tree.name + '/' + node.name)
            for socket in node.inputs:
                if socket.bl_idname != 'NodeSocketStringFilePath':
                    continue
                raw = socket.default_value
                if socket.is_linked:
                    raise Failure('UNSUPPORTED', 'Linked filepath socket has no static dependency contract')
                if not raw:
                    continue
                if not Path(raw).is_absolute() or any(token in raw for token in ('<UDIM>', '<udim>', '####')):
                    raise Failure('UNSUPPORTED', 'M1 filepath socket must be an absolute static file')
                absolute = str(Path(raw).resolve())
                key = os.path.normcase(absolute)
                if key not in declared or digest(absolute) != declared[key]:
                    raise Failure('CONFLICT', 'Undeclared or changed node filepath dependency: ' + absolute)
                node_paths.append({'tree': tree.name, 'node': node.name, 'socket': socket.identifier,
                                   'file': absolute, 'sha256': declared[key]})
    return {'status': 'passed', 'node_paths': node_paths,
            'scope': 'All saved ID/scripted-driver, shader-script and cache gates; static filepath sockets; no visibility exemption'}


def _activate(manifest):
    scene = bpy.data.scenes.get(manifest['scene'])
    if scene is None:
        raise Failure('NOT_FOUND', 'Target compositor Scene missing')
    layer = scene.view_layers.get(manifest['view_layer'])
    if layer is None:
        raise Failure('NOT_FOUND', 'Target compositor ViewLayer missing')
    bpy.context.window.scene = scene
    bpy.context.window.view_layer = layer
    if 'reference_frame' in manifest or 'frame' in manifest:
        scene.frame_set(manifest.get('reference_frame', manifest.get('frame')))
    layer.update()
    return scene, layer


def _graph_render_gate(scene, layer, workflow_version='1.0'):
    root = scene.compositing_node_group
    if root is None:
        raise Failure('UNSUPPORTED', 'Preview has no defined compositing output after management operation')
    visited, active, render_layers, denoise_nodes = set(), set(), [], []
    node_total = 0
    def visit(tree):
        nonlocal node_total
        pointer = tree.as_pointer()
        if pointer in active:
            raise Failure('UNSUPPORTED', 'Recursive compositor node group')
        if pointer in visited:
            return
        visited.add(pointer)
        active.add(pointer)
        node_total += len(tree.nodes)
        if len(visited) > 128 or node_total > 8192:
            raise Failure('RESOURCE_LIMIT', 'Existing compositor graph exceeds M1 execution budget')
        for node in tree.nodes:
            if node.bl_idname == 'CompositorNodeOutputFile':
                raise Failure('UNSUPPORTED', 'Unowned File Output node may write externally: ' + tree.name + '/' + node.name)
            if node.bl_idname == 'CompositorNodeDenoise':
                quality = next((socket for socket in node.inputs if socket.identifier == 'Quality'), None)
                denoise_nodes.append({'tree': tree.name, 'node': node.name, 'mute': node.mute,
                    'quality_socket': quality.identifier if quality else None,
                    'quality_linked': quality.is_linked if quality else False,
                    'quality_setting': quality.default_value if quality and not quality.is_linked else 'not_observed'})
            if node.bl_idname == 'CompositorNodeRLayers':
                if node.scene and node.scene != scene:
                    raise Failure('UNSUPPORTED', 'Cross-scene Render Layers requires a dedicated write/dependency contract')
                if node.layer not in ('', layer.name):
                    raise Failure('UNSUPPORTED', 'Render Layers uses another ViewLayer')
                required = [socket.identifier for socket in node.outputs if socket.is_linked]
                record = {'tree': tree.name, 'node': node.name, 'view_layer': node.layer or layer.name,
                          'linked_output_identifiers': required}
                if workflow_version in ('1.1', '1.2', '1.3'):
                    record['required_passes'] = render_pass_snapshot(scene, layer, required)
                render_layers.append(record)
            child = getattr(node, 'node_tree', None)
            if child:
                visit(child)
        active.remove(pointer)
    visit(root)
    editor = scene.sequence_editor
    if scene.render.use_sequencer and editor:
        strips = getattr(editor, 'strips', getattr(editor, 'sequences', ()))
        if len(strips):
            raise Failure('UNSUPPORTED', 'M1 cannot preserve an active VSE post-compositor pipeline')
    return {'status': 'passed', 'root': root.name, 'groups': len(visited), 'nodes': node_total,
            'render_layers': render_layers, 'denoise_nodes': denoise_nodes,
            'source_mode': 'scene_render_and_compositor' if render_layers else 'image_graph_via_native_render',
            'renderer': 'bpy.ops.render.render', 'scope': 'All bound compositor nodes, including muted/unconnected outputs; same Scene/ViewLayer only'}


def _preview_spec(params, scene, layer, inspection, gate):
    supplied = params.get('preview', {})
    spec = {'width': 512, 'height': 512, 'samples': 16, 'engine': 'SOURCE',
            'compositor_device': 'GPU', 'allow_cpu_fallback': True, 'precision': 'FULL',
            'pixel_frame_budget': 32000000, 'baseline': True, **copy.deepcopy(supplied)}
    timeline = spec.get('timeline')
    frames = list(range(timeline['scene_start'], timeline['scene_start'] + timeline['frame_count'])) if timeline else spec.get('frames') or [scene.frame_current]
    if len(frames) != 1 and params['manifest']['workflow_version'] not in ('1.2', '1.3'):
        raise Failure('UNSUPPORTED', 'M1 previews exactly one static scene frame')
    if timeline:
        import math
        fps = timeline['scene_fps']
        if not math.isclose(scene.render.fps / scene.render.fps_base, fps['numerator'] / fps['denominator'], rel_tol=1e-7):
            raise Failure('CONFLICT', 'Preview rational FPS differs from original Scene')
        for binding in inspection.get('temporal_bindings', []):
            mapping = binding['declaration']['mapping']
            if fps != mapping['scene_fps'] or frames[0] < mapping['scene_start'] or frames[-1] >= mapping['scene_start'] + mapping['frame_count']:
                raise Failure('CONFLICT', 'Preview timeline exceeds common media domain/FPS')
        spec['fps_observed'] = {'fps': scene.render.fps, 'fps_base': scene.render.fps_base, 'rational_request': fps}
    spec['frames'] = frames
    roles = 2 if spec['baseline'] else 1
    pixels = spec['width'] * spec['height'] * len(frames) * roles
    if pixels > spec['pixel_frame_budget']:
        raise Failure('RESOURCE_LIMIT', 'Baseline/result pixel-frame budget exceeded')
    engine = scene.render.engine if spec['engine'] == 'SOURCE' else spec['engine']
    if engine not in ('CYCLES', 'BLENDER_EEVEE', 'BLENDER_WORKBENCH'):
        raise Failure('UNSUPPORTED', 'Native source render engine is outside M1: ' + engine)
    spec['engine'] = engine
    camera_name = spec.get('camera') or (scene.camera.name if scene.camera else None)
    camera = bpy.data.objects.get(camera_name) if camera_name else None
    if camera is None or camera.type != 'CAMERA' or camera.name not in layer.objects:
        if params['manifest']['workflow_version'] not in ('1.1', '1.2', '1.3') or gate['render_layers'] or supplied.get('camera'):
            raise Failure('UNSUPPORTED', 'Preview requires an actual Camera in the target ViewLayer; saved candidate retained')
        spec['camera'] = None
        spec['camera_execution'] = 'native_static_compositor_without_render_layers; no temporary Camera created'
    else:
        spec['camera'] = camera.name
        spec['camera_execution'] = 'existing target ViewLayer Camera'
    if engine != 'CYCLES' and any(requirement['output'] == 'Noisy Image' or requirement['output'].startswith('Denoising ')
            for record in gate['render_layers'] for requirement in record.get('required_passes', {}).get('requirements', [])):
        raise Failure('UNSUPPORTED', 'Required noisy/denoising Pass outputs require the actual Cycles preview engine')
    noisy_consumers = [record for record in gate['render_layers']
                       if 'Noisy Image' in record['linked_output_identifiers']]
    if noisy_consumers:
        # _temporary_scene disables native denoising. Cycles only generates
        # Noisy Image while both Scene and ViewLayer native denoising are on;
        # source RNA/socket registration cannot certify a temporary render.
        error = Failure('UNSUPPORTED', 'Preview disables native Cycles denoising and does not generate Noisy Image; Image is the non-denoised beauty output in this preview route')
        error.details = {'preview_native_denoising': False,
                         'required_output': 'Noisy Image',
                         'consumers': [{'tree': item['tree'], 'node': item['node'],
                                        'view_layer': item['view_layer']} for item in noisy_consumers],
                         'scope': 'Actual preview policy, not all Blender rendering or source socket availability'}
        raise error
    source = inspection.get('source') or {}
    if source.get('kind') == 'image':
        image_info = source.get('image') or {}
        image = bpy.data.images.get(image_info.get('name', ''))
        if params['manifest']['workflow_version'] == '1.0' and (image is None or not image.has_data or tuple(image.size) != (spec['width'], spec['height'])):
            raise Failure('UNSUPPORTED', 'M1 external-image preview requires its actual unscaled dimensions to match width/height')
    if params['manifest']['workflow_version'] in ('1.1', '1.2', '1.3'):
        spec['dimension_scope'] = 'Explicit output width/height; inputs retain decoded dimensions; actual graph nodes determine scaling and canvas domain'
    spec['denoise_execution'] = _denoise_execution(scene, spec, gate, selected_compositor=spec['compositor_device'])
    return spec


def _denoise_execution(scene, spec, gate, *, selected_compositor):
    """RNA selection is not a measurement of OIDN GPU kernels or native fallback."""
    device = scene.render.compositor_denoise_device
    nodes = gate.get('denoise_nodes', [])
    if nodes and device in ('AUTO', 'GPU') and not spec['allow_cpu_fallback']:
        error = Failure('UNSUPPORTED', 'Denoise SOURCE device AUTO/GPU can fall back internally to CPU; strict no-CPU-fallback cannot be proved')
        error.details = {'device': device, 'allow_cpu_fallback': False, 'used_backend': 'not_observed',
                         'nodes': [{'tree': row['tree'], 'node': row['node']} for row in nodes]}
        raise error
    qualities = []
    for row in nodes:
        quality = row['quality_setting']
        effective = scene.render.compositor_denoise_final_quality if quality == 'Follow Scene' else quality
        qualities.append({**row, 'resolved_quality_setting': effective,
            'quality_scope': 'Linked menu result not observed' if row['quality_linked'] else
                             'bpy.ops.render.render is final rendering: Follow Scene resolves final_quality, not viewport preview_quality'})
    return {'status': 'source_RNA_selected' if nodes else 'not_applicable_no_denoise_nodes',
            'request_mode': 'SOURCE', 'requested_device': device, 'selected_device': device,
            'selected_compositor_context': selected_compositor,
            'resolved_device_request': selected_compositor if device == 'AUTO' else device,
            'used_backend': 'not_observed',
            'native_cpu_fallback': 'possible_unobserved' if nodes and device in ('AUTO', 'GPU') else 'not_requested',
            'allow_cpu_fallback': spec['allow_cpu_fallback'],
            'source_preview_quality': scene.render.compositor_denoise_preview_quality,
            'source_final_quality': scene.render.compositor_denoise_final_quality,
            'nodes': qualities,
            'scope': 'Source device and quality RNA retained; AUTO follows compositor selection, GPU may internally fall back to CPU. Actual OIDN kernel backend and fallback occurrence are not profiled; compositor GPU is not denoiser GPU proof.'}


def _restore(owner, values):
    for name, value in values.items():
        actual = getattr(owner, name)
        if isinstance(value, list):
            actual = list(actual)
        if actual != value:
            setattr(owner, name, value)


def _runtime_format_initialization(before, after):
    """Recognize only the measured unset inactive color enum; no other delta is allowed."""
    wanted = before['image_format_color']
    actual = after['image_format_color']
    if wanted['has_linear_colorspace'] or actual['has_linear_colorspace'] or wanted['linear']['name'] != '' or not actual['linear']['name']:
        return None
    comparable = copy.deepcopy(after)
    comparable['image_format_color']['linear']['name'] = ''
    if near(before, comparable):
        return None
    return {'field': 'image_format_color.linear.name', 'before': '', 'after': actual['linear']['name'],
            'reason': 'Inactive unset RNA color enum initializes during output-format switching and has no writable empty reset',
            'restore': 'mandatory reopen of owned SHA-bound saved candidate; not claimed restored in current runtime yet'}


@contextmanager
def _temporary_scene(scene, layer, spec):
    before = scene_configuration(scene)
    context = (bpy.context.window.scene, bpy.context.window.view_layer)
    preferences = bpy.context.preferences.addons['cycles'].preferences if spec['engine'] == 'CYCLES' else None
    cycles_preferences = (preferences.compute_device_type, [(item.id, item.use) for item in preferences.devices]) if preferences else None
    try:
        bpy.context.window.scene = scene
        bpy.context.window.view_layer = layer
        scene.camera = bpy.data.objects[spec['camera']] if spec['camera'] else None
        for item in scene.view_layers:
            item.use = item == layer
        for marker in scene.timeline_markers:
            marker.camera = None
        render = scene.render
        render.engine = spec['engine']
        render.resolution_x, render.resolution_y = spec['width'], spec['height']
        render.resolution_percentage = 100
        render.pixel_aspect_x = render.pixel_aspect_y = 1
        render.use_border = render.use_crop_to_border = render.use_multiview = False
        render.use_compositing = True
        render.use_sequencer = False
        render.use_motion_blur = False
        render.use_persistent_data = False
        render.use_render_cache = render.use_texture_cache = render.use_auto_generate_texture_cache = False
        render.use_stamp = False
        render.compositor_precision = 'FULL'
        if spec['engine'] == 'CYCLES':
            scene.cycles.samples = spec['samples']
            scene.cycles.use_adaptive_sampling = False
            scene.cycles.use_denoising = False
            scene.cycles.seed = 0
            scene.cycles.use_animated_seed = False
        elif spec['engine'] == 'BLENDER_EEVEE':
            scene.eevee.taa_render_samples = spec['samples']
        yield
    finally:
        _restore(scene.render, before['render'])
        _restore(scene.render.image_settings, before['image_format'])
        _restore(scene.render.image_settings.view_settings, before['image_format_color']['view'])
        _restore(scene.render.image_settings.display_settings, before['image_format_color']['display'])
        if before['image_format_color']['linear']['name']:
            _restore(scene.render.image_settings.linear_colorspace_settings, before['image_format_color']['linear'])
        _restore(scene.view_settings, before['view'])
        _restore(scene.display_settings, before['display'])
        _restore(scene.cycles, before['cycles'])
        _restore(scene.eevee, before['eevee'])
        scene.camera = bpy.data.objects.get(before['camera']) if before['camera'] else None
        for item in scene.view_layers:
            item.use = before['layers'][item.name]
        for marker in scene.timeline_markers:
            old = before['markers'][marker.name]
            marker.camera = bpy.data.objects.get(old) if old else None
        scene.frame_set(before['frame'], subframe=before['subframe'])
        layer.update()
        if preferences:
            preferences.compute_device_type = cycles_preferences[0]
            old_use = dict(cycles_preferences[1])
            for item in preferences.devices:
                if item.id in old_use:
                    item.use = old_use[item.id]
        bpy.context.window.scene, bpy.context.window.view_layer = context
        after = scene_configuration(scene)
        mismatch = near(before, after)
        if mismatch:
            initialization = _runtime_format_initialization(before, after)
            if initialization is None:
                raise Failure('VALIDATION_FAILED', 'Temporary preview settings were not restored: ' + mismatch)
            spec['runtime_format_initialization'] = initialization


def _cycles_device(scene, spec):
    if spec['engine'] != 'CYCLES':
        if spec.get('device', {}).get('backend', 'GRAPHICS') != 'GRAPHICS':
            raise Failure('INVALID_REQUEST', 'Non-Cycles engine uses the GRAPHICS device context')
        return {'requested': spec.get('device', {'backend': 'GRAPHICS'}),
                'used': {'backend': 'GRAPHICS', 'id': None}, 'fallback': None}
    import rendering
    requested = copy.deepcopy(spec.get('device'))
    if requested is None:
        discovered = rendering.devices()['devices']
        choices = [row for row in discovered if row.get('backend') == 'OPTIX' and row.get('id')]
        if not choices:
            choices = [row for row in discovered if row.get('backend') == 'CUDA' and row.get('id')]
        requested = {'backend': choices[0]['backend'], 'id': choices[0]['id']} if choices else {'backend': 'CPU'}
    if requested['backend'] == 'GRAPHICS':
        raise Failure('INVALID_REQUEST', 'Cycles requires CPU/CUDA/OPTIX, not GRAPHICS')
    fallback = None
    try:
        used = rendering.select_device(scene, {'engine': 'CYCLES', 'device': requested})
    except Failure as exc:
        if requested['backend'] not in ('CUDA', 'OPTIX') or not spec['allow_cpu_fallback'] or exc.code not in ('UNSUPPORTED', 'NOT_FOUND'):
            raise
        used = rendering.select_device(scene, {'engine': 'CYCLES', 'device': {'backend': 'CPU'}})
        fallback = {'stage': 'cycles_device_selection', 'reason': str(exc)}
    return {'requested': requested, 'used': used, 'fallback': fallback,
            'selection_scope': 'exact discovered device id; scene worker preferences never saved'}


def _gpu_unavailable(exc):
    # Device-specific messages only; no retry of graph, resource or scene errors.
    message = str(exc).lower()
    phrases = ('gpu context', 'gpu device', 'gpu initialization', 'gpu backend',
               'failed to initialize gpu', 'failed to create gpu', 'cuda error',
               'optix error', 'no compatible gpu', 'out of gpu memory',
               'cuda device', 'optix device', 'compute device unavailable')
    return any(phrase in message for phrase in phrases)


def _render_once(scene, layer, device, spec):
    scene.render.compositor_device = device
    outcome = bpy.ops.render.render(write_still=False, scene=scene.name, layer=layer.name)
    if 'FINISHED' not in outcome:
        raise Failure('WORKER_FAILED', 'Native compositor render did not finish')
    result = bpy.data.images.get('Render Result')
    if result is None or result.type != 'RENDER_RESULT':
        raise Failure('VALIDATION_FAILED', 'Native compositor produced no Render Result image')
    # Blender 5.2.1 Render Result can have has_data=False, size=(0,0)
    # and no exposed Python pixels after FINISHED, while save_render writes
    # valid buffers. Container/dimension/finite checks use independent decoding.
    return result


def _write_outputs(result, scene, directory, spec, role, frame, device, color, denoise):
    outputs = []
    # Render Result buffers are Scene-bound; a format-only Scene copy cannot
    # save this buffer. The measured unset-color initialization is restored
    # by reopening the owned candidate after all output files are decoded.
    output_scene = scene
    try:
        fmt = output_scene.render.image_settings
        for format_name, extension, depth in [('PNG', '.png', '8'), ('OPEN_EXR', '.exr', '32')]:
            fmt.media_type = 'IMAGE'
            fmt.file_format = format_name
            fmt.color_mode = 'RGBA'
            fmt.color_depth = depth
            fmt.color_management = 'FOLLOW_SCENE'
            if format_name == 'OPEN_EXR':
                fmt.exr_codec = 'ZIP'
            path = directory / ('compositing-' + role + extension)
            if path.exists():
                raise Failure('CONFLICT', 'Owned preview output already exists: ' + str(path))
            result.save_render(str(path), scene=output_scene)
            row = {**_descriptor(path), 'format': format_name, 'depth': depth, 'role': role,
                   'frame': frame, 'width': spec['width'], 'height': spec['height'],
                   'requested_device': copy.deepcopy({key: value['requested'] for key, value in device.items()}),
                   'used_device': copy.deepcopy({key: value['used'] for key, value in device.items()}),
                   'device_fallback': copy.deepcopy({key: value['fallback'] for key, value in device.items()}),
                   'device_scope': 'Blender request and selected RNA/device context at this output; GPU kernel utilization not profiled',
                   'denoise_execution': copy.deepcopy(denoise),
                   'render_result_rna': {'type': result.type, 'has_data': result.has_data, 'size': list(result.size)},
                   'color_scope': 'source display/view applied' if format_name == 'PNG' else 'float EXR in actual output-format linear space; display transform not baked; primaries not inferred',
                   'working_space': copy.deepcopy(color['working_space']),
                   'output_color_space': output_color_space(output_scene, color, format_name),
                   'output_settings_scope': 'Temporary target Scene settings; measured unset inactive color enum requires owned candidate reopen before restoration is accepted',
                   'ocio_identity': {key: color['ocio'][key] for key in ('status', 'file', 'sha256') if key in color['ocio']}}
            outputs.append(decode_image(row))
    finally:
        pass
    return outputs


def _previews_batch(params, inspection, scene, layer, job, progress, report):
    report['render_gate'] = _graph_render_gate(scene, layer, params['manifest']['workflow_version'])
    spec = _preview_spec(params, scene, layer, inspection, report['render_gate'])
    report['preview_spec'] = spec
    report['baseline_definition'] = inspection.get('baseline_definition', 'main bridge input only')
    bridge = None
    if inspection.get('status') == 'managed':
        root = bpy.data.node_groups.get(inspection['root_group'])
        if root is None or root != scene.compositing_node_group:
            raise Failure('CONFLICT', 'Managed root differs before preview')
        matches = [node for node in root.nodes if node.get(core.OWNER) == params['manifest']['workflow_id']
                   and node.get(core.ROLE) == 'bridge' and node.get(core.NODE_ID) == '$bridge']
        if len(matches) != 1 or matches[0].name != inspection.get('bridge_node'):
            raise Failure('CONFLICT', 'Managed baseline bridge is not uniquely identified')
        bridge = matches[0]
    elif spec['baseline']:
        # A detached image-only workflow has no source to bypass. Never invent
        # a 3D baseline for that earlier external image.
        if params['manifest'].get('source', {}).get('kind') == 'image':
            raise Failure('UNSUPPORTED', 'Detached image workflow has no unambiguous baseline source')
    baseline_mute = bridge.mute if bridge else None
    color_before = scene_configuration(scene)
    report.setdefault('frame_receipts', [])
    report.setdefault('temporal_evaluations', [])
    report.setdefault('temporal_definitions', {})
    if params['manifest']['workflow_version'] in ('1.2', '1.3') and 'temporal_reference' not in report:
        saved_inspection = report['saved_expectation']['inspection']
        report['temporal_reference'] = project_temporal({
            'scene_frame': saved_inspection['reference_frame'],
            'reference_frame': saved_inspection['reference_frame'],
            'animation': saved_inspection['animation'],
            'temporal_bindings': saved_inspection['temporal_bindings']}, report['temporal_definitions'])
    reuse = params.get('_resume_outputs', {})
    try:
        with _temporary_scene(scene, layer, spec):
            report['runtime_render_gate'] = _graph_render_gate(scene, layer, params['manifest']['workflow_version'])
            cycles = _cycles_device(scene, spec)
            gpu = {'requested': spec['compositor_device'], 'used': spec['compositor_device'], 'fallback': None}
            if spec['compositor_device'] == 'GPU':
                try:
                    import gpu as gpu_module
                    gpu_module.init()
                    gpu['platform'] = {name: getattr(gpu_module.platform, name)() for name in
                                       ('backend_type_get', 'device_type_get', 'vendor_get', 'renderer_get', 'version_get')}
                except Exception as exc:
                    if not spec['allow_cpu_fallback']:
                        raise Failure('UNSUPPORTED', 'GPU initialization unavailable: ' + str(exc)) from exc
                    gpu.update(used='CPU', fallback={'stage': 'gpu_init', 'reason': str(exc)})
            report['device'] = {'compositor': gpu, 'renderer': cycles}
            pairs = [(frame, role) for frame in spec['frames'] for role in (['baseline', 'result'] if spec['baseline'] else ['result'])]
            for frame, role in pairs:
                progress('preview', role=role, frame=frame, completed=len(report['outputs']))
                temporal = None
                if params['manifest']['workflow_version'] in ('1.2', '1.3'):
                    # Core's bounded evaluator restores reference before returning;
                    # render frame is set separately immediately before native render.
                    if bridge:
                        bridge.mute = baseline_mute
                    actual_temporal = _call(core.temporal_evaluation, scene.name, params['manifest']['workflow_id'], frame)
                    try:
                        temporal = project_temporal(actual_temporal, report['temporal_definitions'])
                    except TemporalProjectionError as exc:
                        raise Failure('VALIDATION_FAILED', 'Temporal evidence projection: ' + str(exc)) from exc
                    def definition_refs(value):
                        return {'animation': value['animation']['definition'],
                            'bindings': sorted((row['node_id'], row['declaration'], row['resolved_media'])
                                               for row in value['temporal_bindings'])}
                    if definition_refs(temporal) != definition_refs(report['temporal_reference']):
                        raise Failure('CONFLICT', 'Temporal canonical definition differs from freshly verified saved reference')
                    if not any(item['scene_frame'] == frame for item in report['temporal_evaluations']):
                        report['temporal_evaluations'].append(temporal)
                directory = job / 'previews' / (role + '-' + str(frame))
                directory.mkdir(parents=True, exist_ok=False)
                prior = reuse.get((role, frame))
                if prior:
                    if prior.get('temporal_evaluation') != temporal:
                        raise Failure('CONFLICT', 'Resume frame dynamic bindings/values differ')
                    decoded = []
                    for original in prior['outputs']:
                        decode_image(original)
                        destination = directory / Path(original['file']).name
                        shutil.copyfile(original['file'], destination)
                        row = {**copy.deepcopy(original), 'file': str(destination)}
                        if digest(destination) != original['sha256']:
                            raise Failure('CONFLICT', 'Reused output copy bytes differ')
                        decoded.append(decode_image(row))
                    report['baselines' if role == 'baseline' else 'outputs'].extend(decoded)
                    report['frame_receipts'].append({**copy.deepcopy(prior),
                        'status': 'pending_reference_restoration' if params['manifest']['workflow_version'] in ('1.2', '1.3') else 'reused',
                        **({'reference_restoration': 'pending'} if params['manifest']['workflow_version'] in ('1.2', '1.3') else {}),
                        'outputs': decoded,
                        'candidate': report['candidate'],
                        'reused_from': [{'file': r['file'], 'sha256': r['sha256']} for r in prior['outputs']]})
                    atomic_json(job / 'compositing-report.json', report)
                    continue
                scene.frame_set(frame)
                layer.update()
                if bridge:
                    bridge.mute = True if role == 'baseline' else baseline_mute
                try:
                    rendered = _render_once(scene, layer, gpu['used'], spec)
                except Exception as exc:
                    renderer_gpu = cycles['used']['backend'] in ('CUDA', 'OPTIX')
                    if (gpu['used'] != 'GPU' and not renderer_gpu) or not spec['allow_cpu_fallback'] or not _gpu_unavailable(exc):
                        raise
                    gpu.update(used='CPU', fallback={'stage': 'native_render', 'reason': str(exc)})
                    if renderer_gpu:
                        import rendering
                        cycles['used'] = rendering.select_device(scene, {'engine': 'CYCLES', 'device': {'backend': 'CPU'}})
                        cycles['fallback'] = {'stage': 'native_render', 'reason': str(exc)}
                    rendered = _render_once(scene, layer, 'CPU', spec)
                report['denoise_execution'] = _denoise_execution(scene, spec, report['runtime_render_gate'],
                                                               selected_compositor=scene.render.compositor_device)
                decoded = _write_outputs(rendered, scene, directory, spec, role, frame, report['device'],
                                         report['source_color_identity'], report['denoise_execution'])
                if role == 'baseline':
                    report['baselines'].extend(decoded)
                else:
                    report['outputs'].extend(decoded)
                report['frame_receipts'].append({'frame': frame, 'role': role,
                    'status': 'pending_reference_restoration' if params['manifest']['workflow_version'] in ('1.2', '1.3') else 'verified',
                    'outputs': decoded, 'temporal_evaluation': temporal,
                    'candidate': report['candidate'], 'signature': report['signature'],
                    'implementation_sha256': report['implementation_sha256'], 'runtime': report['runtime'],
                    'source_color_identity': report['source_color_identity']})
                if bridge:
                    bridge.mute = baseline_mute
                scene.frame_set(color_before['frame'], subframe=color_before['subframe'])
                layer.update()
                atomic_json(job / 'compositing-report.json', report)
                progress('preview_output_verified', role=role, frame=frame)
    finally:
        if bridge:
            bridge.mute = baseline_mute
        layer.update()
    mismatch = near(color_before, scene_configuration(scene))
    if mismatch:
        initialization = _runtime_format_initialization(color_before, scene_configuration(scene))
        if initialization is None:
            raise Failure('VALIDATION_FAILED', 'Scene configuration changed across preview: ' + mismatch)
        report['runtime_format_initialization'] = initialization
    report['checks']['preview_settings_restored'] = 'pending_owned_candidate_reopen' if spec.get('runtime_format_initialization') else 'passed'
    report['temporal'] = {'mode': 'static_scene_frame', 'scene_frame': spec['frames'][0],
                          'source_mapping': 'existing static input, no sequence/video retime contract',
                          'frames_checked': 1}
    if params['manifest']['workflow_version'] in ('1.2', '1.3'):
        report['temporal'] = {'mode': ('owned_movie_sequence_and_parameter_animation' if params['manifest']['workflow_version'] == '1.3' else 'owned_image_sequence_and_parameter_animation'),
            'reference_frame': inspection['reference_frame'], 'frames_checked': len(spec['frames']),
            'scene_frames': spec['frames'], 'fps': spec.get('fps_observed'),
            'mapping_scope': 'Original source_frame and native stage_index are separate explicit receipt fields'}
        params['_temporal_evaluations'] = report['temporal_evaluations']
        params['_temporal_definitions'] = report['temporal_definitions']
    return spec


def _previews(params, inspection, scene, layer, job, progress, report):
    if params['manifest']['workflow_version'] not in ('1.2', '1.3'):
        return _previews_batch(params, inspection, scene, layer, job, progress, report)
    gate = _graph_render_gate(scene, layer, params['manifest']['workflow_version'])
    complete = _preview_spec(params, scene, layer, inspection, gate)
    reopened = False
    # Every completed frame is its own temporary-settings scope and durable
    # receipt. Cancellation cannot promote an un-restored frame to reusable.
    for frame in complete['frames']:
        single = copy.deepcopy(params)
        single['preview'] = copy.deepcopy(params.get('preview', {}))
        single['preview'].pop('timeline', None)
        single['preview'].pop('video', None)
        single['preview']['frames'] = [frame]
        checkpoint_count = len(report.get('frame_receipts', []))
        try:
            _previews_batch(single, inspection, scene, layer, job, progress, report)
            if report['checks']['preview_settings_restored'] == 'pending_owned_candidate_reopen':
                scene, layer = _reload_preview_candidate(report['candidate'], params['manifest'], report)
                reopened = True
            actual = {'inspection': _call(core.inspect, scene.name, params['manifest']['workflow_id']),
                'protection': _call(core.snapshot_protection, scene.name, params['manifest']['workflow_id']),
                'source_config': scene_configuration(scene), 'color_identity': color_identity(scene)}
            for key, value in actual.items():
                mismatch = near(report['saved_expectation'][key], value)
                if mismatch:
                    raise Failure('VALIDATION_FAILED', 'Per-frame reference restoration differs at ' + key + ': ' + mismatch)
            for receipt in report['frame_receipts'][checkpoint_count:]:
                receipt['reference_restoration'] = 'passed'
                receipt['status'] = 'reused' if receipt.get('reused_from') else 'verified'
            atomic_json(job / 'compositing-report.json', report)
        except Exception:
            # Outputs remain audit evidence but only receipts with complete
            # restoration proof qualify as verified/reused on a later attempt.
            for receipt in report.get('frame_receipts', [])[checkpoint_count:]:
                receipt['status'] = 'not_verified_restoration'
            raise
    report['preview_spec'] = complete
    report['temporal'] = {'mode': ('owned_movie_sequence_and_parameter_animation' if params['manifest']['workflow_version'] == '1.3' else 'owned_image_sequence_and_parameter_animation'),
        'reference_frame': inspection['reference_frame'], 'frames_checked': len(complete['frames']),
        'scene_frames': complete['frames'], 'fps': complete.get('fps_observed'),
        'mapping_scope': 'Explicit original/stage/scene receipt; complete reference restoration after every frame'}
    params['_temporal_evaluations'] = report['temporal_evaluations']
    params['_temporal_definitions'] = report['temporal_definitions']
    if reopened:
        # The caller still holds pre-reopen RNA references; force its normal
        # recovery branch to obtain live Scene/ViewLayer references too.
        report['checks']['preview_settings_restored'] = 'pending_owned_candidate_reopen'
    return complete


def _reload_preview_candidate(candidate, manifest, report):
    """Recover this background worker's own immutable checkpoint, never a live user Scene."""
    if digest(candidate['file']) != candidate['sha256']:
        raise Failure('CONFLICT', 'Owned preview recovery candidate changed before reopen')
    bpy.ops.wm.open_mainfile(filepath=candidate['file'], load_ui=False, use_scripts=False)
    scene, layer = _activate(manifest)
    saved = report['saved_expectation']
    observed = {'inspection': _call(core.inspect, scene.name, manifest['workflow_id']),
                'protection': _call(core.snapshot_protection, scene.name, manifest['workflow_id']),
                'source_config': scene_configuration(scene), 'color_identity': color_identity(scene)}
    observed['input_bindings'] = binding_snapshot(observed['inspection'])
    for label, actual in observed.items():
        mismatch = near(saved[label], actual)
        if mismatch:
            raise Failure('VALIDATION_FAILED', 'Owned candidate preview recovery differs at ' + label + ': ' + mismatch)
    if digest(candidate['file']) != candidate['sha256']:
        raise Failure('CONFLICT', 'Owned preview recovery candidate changed during reopen')
    report['checks']['preview_settings_restored'] = 'passed_via_owned_candidate_reopen'
    report['preview_runtime_recovery'] = {'status': 'passed', 'candidate': candidate,
        'scope': 'Own saved background-worker candidate reopened with autoexec disabled; complete config/inspection/protection/color/binding equality; original source and user GUI untouched'}
    return scene, layer


def _fresh(candidate, params, saved, job, progress, *, phase, outputs=()):
    config = {'candidate': candidate, 'runtime': params['_runtime'],
              'implementation_sha256': params['implementation_sha256'],
              'scene': params['manifest']['scene'], 'view_layer': params['manifest']['view_layer'],
              'workflow_id': params['manifest']['workflow_id'], 'frame': saved['source_config']['frame'],
              'inspection': saved['inspection'], 'protection': saved['protection'],
              'source_config': saved['source_config'], 'resources': params['resources'],
              'color_identity': saved.get('color_identity'), 'input_bindings': saved.get('input_bindings'),
              'outputs': list(outputs), 'output': str(job / ('compositing-reopen-' + phase + '.json'))}
    config['staged_resources'] = params.get('_staged_resources', [])
    config['persisted_documents'] = params.get('_persisted_documents', [])
    config['temporal_evaluations'] = params.get('_temporal_evaluations', [])
    config['temporal_definitions'] = params.get('_temporal_definitions', {})
    config['verification_budget'] = fresh_verification_budget(len(config['temporal_evaluations']), len(config['outputs']))
    request = job / ('compositing-verification-' + phase + '.json')
    if request.exists() or Path(config['output']).exists():
        raise Failure('CONFLICT', 'Owned verification phase already exists')
    atomic_json(request, config)
    args = [bpy.app.binary_path, '--background', '--factory-startup', '--disable-autoexec', '--offline-mode',
            '--python', str(Path(__file__).with_name('compositing_verifier.py')), '--', str(request)]
    deadline = time.monotonic() + config['verification_budget']['timeout_seconds']
    proc = None
    try:
        with (job / ('compositing-reopen-' + phase + '.log')).open('xb') as log:
            proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            while proc.poll() is None:
                progress('independent_reopen', phase=phase)
                if time.monotonic() > deadline:
                    raise Failure('TIMEOUT', 'Fresh compositor reader exceeded declared work budget of '
                                  + str(config['verification_budget']['timeout_seconds']) + ' seconds')
                time.sleep(.1)
        value = read_json(config['output']) if Path(config['output']).is_file() else {'status': 'failed', 'error': {'message': 'No fresh reader result'}}
        if proc.returncode or value.get('status') != 'passed':
            error = value.get('error', {})
            raise Failure(error.get('code', 'VALIDATION_FAILED'), 'Fresh compositor reader: ' + str(error.get('message', error)))
        return value
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def run(params, job, *, cancel_parent=None):
    explicit_preview = 'preview' in params
    params = normalize_params(params)
    original_request = copy.deepcopy({key: value for key, value in params.items() if key != 'resume'})
    signature = _signature(params)
    job = Path(job).resolve()
    started = time.monotonic()
    report = {'compositing_report_version': '1.0', 'status': 'running',
              'source': {'file': params['file'], 'sha256': params['expected_sha256']},
              'implementation_sha256': params['implementation_sha256'], 'signature': signature,
              'original_request': original_request, 'resolved_manifest': params['manifest'],
              'outputs': [], 'baselines': [], 'recovery': {'available': False},
              'checks': {'visual_quality': 'not_received', 'HDR10': 'not_run'},
              'support': {'milestone': {'1.0': 'M1', '1.1': 'M2', '1.2': 'M3', '1.3': 'M3b'}[params['manifest']['workflow_version']],
                          'scope': 'managed static bound-input graph and actual saved-file previews only; no sequence/animation/HDR production',
                          'not_run': ['all native node modes/assets', 'sequences/videos', 'GPU failure injection', 'HDR10', 'GUI apply'],
                          'limitations': ['Source unchanged; no all-asset preservation guarantee', 'one static frame; no hard OS VRAM/memory quota']}}
    params['_runtime'] = runtime_identity()
    if params['manifest']['workflow_version'] in ('1.2', '1.3'):
        report['support'].update(scope='owned finite image sequences, explicit parameter animation/tracking and continuous SDR previews',
            not_run=['movie inputs', 'HDR10', 'GUI apply', 'GPU kernel utilization'],
            limitations=['Source unchanged; finite maximum 300 frames; no hard OS VRAM/memory quota'])
    if params['manifest']['workflow_version'] == '1.3':
        report['support'].update(scope='owned finite image sequences and explicit full-CFR SDR8 H264 movie conversion, parameter animation/tracking and continuous SDR previews',
            not_run=['other movie codecs/metadata/chroma/TRC', 'HDR10', 'GUI apply', 'GPU kernel utilization'],
            limitations=['Source unchanged; maximum 300 frames and explicit pixel budget; decoded SDR view cannot recover original scene HDR; no hard OS VRAM/memory quota'])
    report['runtime'] = params['_runtime']
    last_progress = [None, 0.0]
    def progress(stage, **extra):
        if (job / 'cancel.request').exists() or (cancel_parent and (Path(cancel_parent) / 'cancel.request').exists()):
            raise Failure('CANCELLED', 'Compositing cancelled at ' + stage)
        # Cancellation is checked every call; metadata is bounded rather than
        # rewritten at reader poll frequency.
        now = time.monotonic()
        if stage != last_progress[0] or now - last_progress[1] >= 1:
            atomic_json(job / 'compositing-report.json', report)
            atomic_json(job / 'compositing-progress.json', {'stage': stage, 'status': 'running',
                        'seconds': now - started, **extra})
            last_progress[:] = [stage, now]
    try:
        with ExitStack() as stack:
            progress('source_open')
            checked = _inputs(params, stack)
            provider = None
            if params['manifest']['workflow_version'] in ('1.2', '1.3'):
                provider = media.guarded_digest_scope(stack, checked)
                report['immutable_media_digest_scope'] = provider.stats
            bpy.ops.wm.open_mainfile(filepath=params['file'], load_ui=False, use_scripts=False)
            persisted = media.persisted_documents(stack=stack, checked=checked)
            media.guard_rows(persisted, stack, checked)
            params['_persisted_documents'] = persisted
            _, inherited = media.persisted_media()
            media.guard_rows(inherited, stack, checked)
            report['staged_resources'] = inherited
            params['_staged_resources'] = inherited
            closure = resource_snapshot(params['resources'])
            report['resources'] = closure
            report['execution_gate'] = _static_execution_gate(params['resources'])
            report['path_remaps'] = _canonicalize(closure)
            from geometry_workflow import _retain_unlinked_ids
            report['persistence_exceptions'] = _retain_unlinked_ids()
            scene, layer = _activate(params['manifest'])
            report['source_configuration'] = scene_configuration(scene)
            report['source_color_identity'] = color_identity(scene)
            report['color_immutable_inputs'] = _guard_color_inputs(report['source_color_identity'], stack, checked)
            if params['manifest']['workflow_version'] in ('1.1', '1.2', '1.3') and not params.get('resume'):
                graph = params['manifest'].get('graph', {})
                report['binding_preflight'] = []
                if params['manifest'].get('source', {}).get('kind') == 'image':
                    report['binding_preflight'].append({'node_id': '$source',
                        'independent_file': image_file_snapshot(params['manifest']['source'], ['Image'])})
                for node in graph.get('nodes', []):
                    if node.get('binding', {}).get('kind') == 'image':
                        required = [link['from_socket'] for link in graph.get('links', []) if link['from_node'] == node['id']]
                        report['binding_preflight'].append({'node_id': node['id'],
                            'independent_file': image_file_snapshot(node['binding'], required)})
            if params.get('resume'):
                old = read_json(params['resume']['file'])
                if old.get('signature') != signature or old.get('original_request') != original_request or old.get('runtime') != params['_runtime'] or old.get('implementation_sha256') != params['implementation_sha256']:
                    raise Failure('CONFLICT', 'Resume original request/runtime/implementation differs')
                if not old.get('recovery', {}).get('available') or not old.get('saved_expectation') or not old.get('candidate'):
                    raise Failure('CONFLICT', 'Resume lacks a freshly verified saved expectation')
                media.guard_rows(old.get('staged_resources', []), stack, checked)
                media.guard_rows([*old.get('outputs', []), *old.get('baselines', [])], stack, checked)
                report['staged_resources'] = copy.deepcopy(old.get('staged_resources', []))
                params['_staged_resources'] = report['staged_resources']
                report['media_receipt'] = copy.deepcopy(old.get('media_receipt'))
                params['_resume_outputs'] = {}
                for receipt in old.get('frame_receipts', []):
                    if receipt.get('status') not in ('verified', 'reused'):
                        continue
                    if params['manifest']['workflow_version'] in ('1.2', '1.3') and receipt.get('reference_restoration') != 'passed':
                        raise Failure('CONFLICT', 'Resume frame lacks complete reference restoration proof')
                    if params['manifest']['workflow_version'] in ('1.2', '1.3'):
                        try:
                            inflate_temporal(receipt['temporal_evaluation'], old.get('temporal_definitions', {}))
                        except (TemporalProjectionError, KeyError) as exc:
                            raise Failure('CONFLICT', 'Resume temporal canonical evidence differs: ' + str(exc)) from exc
                    if any(receipt.get(key) != old.get(key) for key in ('candidate', 'signature', 'implementation_sha256', 'runtime', 'source_color_identity')):
                        raise Failure('CONFLICT', 'Resume frame receipt identity differs from saved checkpoint')
                    outputs = receipt.get('outputs', [])
                    if len(outputs) != 2 or {r['format'] for r in outputs} != {'PNG', 'OPEN_EXR'} or any(r['frame'] != receipt['frame'] or r['role'] != receipt['role'] for r in outputs):
                        raise Failure('CONFLICT', 'Resume frame output coverage differs')
                    if any(r not in old.get('outputs', []) + old.get('baselines', []) for r in outputs):
                        raise Failure('CONFLICT', 'Resume frame outputs not in original verified report')
                    for row in outputs:
                        decode_image(row)
                    key = (receipt['role'], receipt['frame'])
                    if key in params['_resume_outputs']:
                        raise Failure('CONFLICT', 'Duplicate resume frame receipt')
                    params['_resume_outputs'][key] = receipt
                from filesystem import FileGuard
                old_guard = stack.enter_context(FileGuard(old['candidate']['file']))
                if old_guard.sha256() != old['candidate']['sha256']:
                    raise Failure('CONFLICT', 'Resume candidate changed')
                destination = job / 'compositing-candidate.blend'
                if destination.exists():
                    raise Failure('CONFLICT', 'New attempt candidate already exists')
                shutil.copyfile(old['candidate']['file'], destination)
                candidate = _descriptor(destination)
                if candidate['sha256'] != old['candidate']['sha256']:
                    raise Failure('CONFLICT', 'New attempt checkpoint copy differs')
                bpy.ops.wm.open_mainfile(filepath=candidate['file'], load_ui=False, use_scripts=False)
                persisted = media.persisted_documents(stack=stack, checked=checked)
                media.guard_rows(persisted, stack, checked)
                params['_persisted_documents'] = persisted
                scene, layer = _activate(params['manifest'])
                resource_snapshot(params['resources'], require_canonical=True)
                report['execution_gate'] = _static_execution_gate(params['resources'])
                report['operation'] = old['operation']
                report['protection'] = old['protection']
                report['saved_expectation'] = copy.deepcopy(old['saved_expectation'])
                report['resumed_from'] = params['resume']
                report['checkpoint_parent'] = old['candidate']
            else:
                progress('apply')
                resolved = None
                if params['manifest']['workflow_version'] in ('1.2', '1.3'):
                    resolved, evidence, tracking = media.prepare(params['manifest'], job, stack, checked, progress)
                    report['media_receipt'] = {'resolved_media': resolved, 'sequence_decode': evidence, 'tracking': tracking}
                    report['staged_resources'] = [*inherited, *media.staged_resources(resolved)]
                    params['_staged_resources'] = report['staged_resources']
                operation = _call(core.apply, params['manifest'], **({'resolved_media': resolved} if resolved is not None else {}))
                params['_persisted_documents'] = media.persisted_documents(stack=stack, checked=checked)
                media.guard_rows(params['_persisted_documents'], stack, checked)
                report['operation'] = operation
                report['protection'] = operation.get('protection')
                if not report['protection'] or report['protection'].get('status') != 'pass':
                    raise Failure('VALIDATION_FAILED', 'Core did not prove scoped non-target protection')
                resource_snapshot(params['resources'], require_canonical=True)
                scene, layer = _activate(params['manifest'])
                report['inspection'] = _call(core.inspect, scene.name, params['manifest']['workflow_id'])
                saved = {'inspection': copy.deepcopy(report['inspection']),
                         'protection': _call(core.snapshot_protection, scene.name, params['manifest']['workflow_id']),
                         'source_config': scene_configuration(scene), 'color_identity': color_identity(scene),
                         'input_bindings': binding_snapshot(report['inspection'])}
                if near(report['source_color_identity'], saved['color_identity']):
                    raise Failure('VALIDATION_FAILED', 'Core unexpectedly changed original color/OCIO identity')
                report['input_bindings'] = saved['input_bindings']
                expected_config = copy.deepcopy(report['source_configuration'])
                # Enabling/restoring the targeted compositor is the declared
                # management change. It is not a preview-quality override.
                expected_config['render']['use_compositing'] = saved['source_config']['render']['use_compositing']
                if params['manifest']['workflow_version'] in ('1.2', '1.3'):
                    expected_config['frame'] = saved['inspection']['reference_frame']
                    expected_config['subframe'] = 0.0
                if near(expected_config, saved['source_config']):
                    raise Failure('VALIDATION_FAILED', 'Core unexpectedly changed render/color/time configuration')
                report['saved_expectation'] = saved
                progress('save_checkpoint')
                destination = job / 'compositing-candidate.blend'
                if destination.exists():
                    raise Failure('CONFLICT', 'Owned compositor candidate already exists')
                bpy.ops.wm.save_as_mainfile(filepath=str(destination), check_existing=False, relative_remap=False)
                candidate = _descriptor(destination)
            from filesystem import FileGuard
            candidate_guard = stack.enter_context(FileGuard(candidate['file']))
            if candidate_guard.sha256() != candidate['sha256']:
                raise Failure('CONFLICT', 'Candidate differs before verification')
            report['candidate'] = candidate
            saved = report['saved_expectation']
            report['inspection'] = saved['inspection']
            report['input_bindings'] = saved.get('input_bindings')
            progress('independent_reopen')
            report['verification'] = _fresh(candidate, params, saved, job, progress, phase='saved')
            report['recovery'] = {'available': True, 'checkpoint': candidate,
                                  'resume_report': str(job / 'compositing-report.json'),
                                  'binding': ['original_request', 'signature', 'runtime', 'implementation_sha256', 'source/resources/candidate SHA', 'saved_expectation'],
                                  'instruction': 'Hash this report and submit identical original normalized request plus resume; creates a new owned attempt'}
            atomic_json(job / 'compositing-report.json', report)
            management = params['manifest']['operation'] in ('adopt', 'detach')
            if management and not explicit_preview:
                report['checks']['render'] = 'not_applicable_management_operation'
            else:
                progress('preview')
                preview_spec = _previews(params, report['inspection'], scene, layer, job, progress, report)
                if report['checks']['preview_settings_restored'] == 'pending_owned_candidate_reopen':
                    scene, layer = _reload_preview_candidate(candidate, params['manifest'], report)
                expected_inspection = _call(core.inspect, scene.name, params['manifest']['workflow_id'])
                expected_protection = _call(core.snapshot_protection, scene.name, params['manifest']['workflow_id'])
                for label, actual in [('inspection', expected_inspection), ('protection', expected_protection), ('source_config', scene_configuration(scene)),
                                      ('color_identity', color_identity(scene)), ('input_bindings', binding_snapshot(expected_inspection))]:
                    mismatch = near(saved[label], actual)
                    if mismatch:
                        raise Failure('VALIDATION_FAILED', 'Temporary render changed ' + label + ': ' + mismatch)
                report['output_verification'] = _fresh(candidate, params, saved, job, progress, phase='outputs', outputs=[*report['baselines'], *report['outputs']])
                report['checks']['render'] = 'passed'
                report['checks']['independent_png_exr_decode'] = 'passed'
                report['video'] = media.encode_video(preview_spec, report, job, progress)
            if candidate_guard.sha256() != candidate['sha256']:
                raise Failure('CONFLICT', 'Candidate bytes changed during preview')
            # Recheck namespace membership after video subprocesses as well;
            # file guards protect existing bytes, not new directory entries.
            media.persisted_media()
            _recheck(checked, params)
            if provider:
                provider.close()
            report['checks'].update(source_resources_implementation_sha='passed', candidate_bytes_unchanged='passed', fresh_saved_graph_protection='passed')
            report['status'] = 'passed'
            report['metrics'] = {'wall_seconds': time.monotonic() - started, 'static_frames': len({r['frame'] for r in report['outputs']}),
                                 'result_files': len(report['outputs']), 'baseline_files': len(report['baselines'])}
            atomic_json(job / 'compositing-manifest.json', params['manifest'])
            atomic_json(job / 'compositing-report.json', report)
            atomic_json(job / 'compositing-progress.json', {'stage': 'complete', 'status': 'passed', 'seconds': time.monotonic() - started})
            return report
    except Exception as exc:
        report['status'] = 'cancelled' if getattr(exc, 'code', None) == 'CANCELLED' else 'failed'
        report['error'] = {'code': getattr(exc, 'code', 'WORKER_FAILED'), 'message': str(exc), 'details': getattr(exc, 'details', None)}
        report['metrics'] = {'wall_seconds': time.monotonic() - started, 'result_files': len(report['outputs']), 'baseline_files': len(report['baselines'])}
        atomic_json(job / 'compositing-report.json', report)
        atomic_json(job / 'compositing-progress.json', {'stage': 'stopped', 'status': report['status'], 'error': report['error']})
        if isinstance(exc, Failure):
            raise
        if isinstance(exc, WorkflowError):
            raise Failure(exc.code, str(exc)) from exc
        raise


def inspect(params, job):
    params = normalize_inspect_params(params)
    job = Path(job).resolve()
    with ExitStack() as stack:
        runtime = runtime_identity()
        checked = _inputs(params, stack)
        bpy.ops.wm.open_mainfile(filepath=params['file'], load_ui=False, use_scripts=False)
        media.guard_rows(media.persisted_documents(stack=stack, checked=checked), stack, checked)
        provider = None
        if any(json.loads(scene.get(core.STATE, '{}')).get('workflow_version') in ('1.2', '1.3') for scene in bpy.data.scenes):
            provider = media.guarded_digest_scope(stack, checked)
        _, inherited = media.persisted_media()
        media.guard_rows(inherited, stack, checked)
        closure = resource_snapshot(params['resources'])
        # Inspect reads static dependency/core state. It never canonicalizes,
        # evaluates frames, creates fake users or saves the supplied source.
        scene = bpy.data.scenes.get(params['scene'])
        if scene is None:
            raise Failure('NOT_FOUND', 'Target compositor Scene missing')
        preflight = _inspection_file_preflight(scene, closure)
        color = color_identity(scene)
        color_inputs = _guard_color_inputs(color, stack, checked)
        inspection = _call(core.inspect, params['scene'], params.get('workflow_id'))
        report = {'compositing_report_version': '1.0', 'status': 'passed',
                  'source': {'file': params['file'], 'sha256': params['expected_sha256']}, 'runtime': runtime,
                  'implementation_sha256': params['implementation_sha256'], 'resources': closure,
                  'inspection': inspection, 'source_saved': False,
                  'input_file_preflight': preflight,
                  'source_color_identity': color,
                  'color_immutable_inputs': color_inputs,
                  'input_bindings': binding_snapshot(inspection),
                  'checks': {'render': 'not_run', 'visual_quality': 'not_received', 'HDR10': 'not_run'}}
        if near(color, color_identity(scene)):
            raise Failure('CONFLICT', 'Color proof identity changed during inspection')
        _recheck(checked, params)
        if provider:
            provider.close()
            report['immutable_media_digest_scope'] = provider.stats
        atomic_json(job / 'compositing-report.json', report)
        return report
