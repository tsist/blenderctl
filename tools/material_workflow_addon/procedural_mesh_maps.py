"""Isolated Cycles mesh-map pre-bakes. No source data, materials or files are saved.

These are renderer-derived edge/cavity masks, not geometric curvature estimates.
M0 deliberately has no cache. Each invocation owns a fresh output subdirectory.
"""
import hashlib
import json
import math
from array import array
from pathlib import Path
import re
import struct
import uuid

from .contract import WorkflowError

_STATIC_MODIFIERS = {
    'BEVEL', 'SUBSURF', 'SOLIDIFY', 'MIRROR', 'ARRAY', 'TRIANGULATE',
    'WEIGHTED_NORMAL', 'NORMAL_EDIT', 'EDGE_SPLIT', 'BOOLEAN', 'DECIMATE',
    'WELD', 'DISPLACE', 'SMOOTH', 'LAPLACIANSMOOTH', 'CAST', 'SHRINKWRAP',
}


def _fail(message):
    raise WorkflowError('invalid_mesh_map', message)


def _number(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(name + ' must be a number')
    if not math.isfinite(value) or not minimum <= value <= maximum:
        _fail(name + ' is outside the supported finite range')
    return float(value)


def _requests(requests, size):
    if isinstance(size, bool) or not isinstance(size, int) or not 16 <= size <= 2048:
        _fail('size must be an integer from 16 to 2048')
    if not isinstance(requests, list) or not 1 <= len(requests) <= 32:
        _fail('requests must contain 1 to 32 maps')
    result, ids = [], set()
    for item in requests:
        if not isinstance(item, dict) or set(item) - {'id', 'type', 'parameters'}:
            _fail('request must only contain id/type/parameters')
        ident, kind = item.get('id'), item.get('type')
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,47}', ident):
            _fail('map id must be a safe identifier of at most 48 characters')
        if ident.upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM' + str(i) for i in range(1, 10)),
                             *('LPT' + str(i) for i in range(1, 10))}:
            _fail('map id is a Windows reserved filename')
        if ident.casefold() in ids:
            _fail('duplicate map id: ' + ident)
        ids.add(ident.casefold())
        if not isinstance(kind, str) or kind not in {'edge', 'cavity'}:
            _fail('map type must be edge or cavity')
        params = item.get('parameters', {})
        allowed = {'width', 'samples', 'gain'} if kind == 'edge' else {'distance', 'samples', 'only_local'}
        if not isinstance(params, dict) or set(params) - allowed:
            _fail('unsupported map parameters: ' + ident)
        count = params.get('samples', 16)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 128:
            _fail('samples must be an integer from 1 to 128')
        normalized = {'samples': count}
        if kind == 'edge':
            normalized['width'] = _number(params.get('width', 0.01), 'width', 1e-6, 10000)
            normalized['gain'] = _number(params.get('gain', 32), 'gain', 1e-6, 1e6)
        else:
            normalized['distance'] = _number(params.get('distance', 0.1), 'distance', 1e-6, 10000)
            local = params.get('only_local', True)
            if not isinstance(local, bool):
                _fail('only_local must be a boolean')
            if not local:
                raise WorkflowError('unsupported_mesh_map_scope',
                    'M0 supports only_local=true; scene-contact AO requires an environment fingerprint')
            normalized['only_local'] = local
        result.append({'id': ident, 'type': kind, 'parameters': normalized})
    return result


def _digest_numbers(rows, code):
    digest = hashlib.sha256()
    for row in rows:
        values = tuple(row)
        digest.update(struct.pack('<I', len(values)))
        digest.update(struct.pack('<' + code * len(values), *values))
    return digest.hexdigest()


def _mesh_fingerprint(mesh, uv_layer):
    """Read the actual geometry, corner UVs, split normals and shading state."""
    uv = mesh.uv_layers.get(uv_layer)
    if uv is None or len(uv.data) != len(mesh.loops):
        _fail('UV layer missing or incomplete: ' + uv_layer)
    rows = [tuple(vertex.co) for vertex in mesh.vertices]
    if any(not math.isfinite(v) for row in rows for v in row):
        _fail('mesh contains non-finite coordinates')
    uv_rows = [tuple(corner.uv) for corner in uv.data]
    if any(not math.isfinite(v) for row in uv_rows for v in row):
        _fail('UV layer contains non-finite coordinates')
    # One PNG is the declared scope; do not silently discard UDIM polygons.
    if any(v < -1e-6 or v > 1.000001 for row in uv_rows for v in row):
        _fail('M0 requires UV coordinates in the 0..1 tile')
    uv_area = 0.0
    for polygon in mesh.polygons:
        corners = [uv_rows[i] for i in polygon.loop_indices]
        if len(corners) < 3: continue
        a = corners[0]
        for b, c in zip(corners[1:-1], corners[2:]):
            uv_area += abs((b[0] - a[0]) * (c[1] - a[1]) -
                           (b[1] - a[1]) * (c[0] - a[0])) * 0.5
    if uv_area < 1e-12:
        _fail('UV layer has no non-degenerate polygon area')
    geometry = _digest_numbers(rows, 'd')
    topology = _digest_numbers(
        [tuple(edge.vertices) for edge in mesh.edges] +
        [(poly.loop_start, poly.loop_total, poly.material_index, int(poly.use_smooth)) for poly in mesh.polygons] +
        [(loop.vertex_index,) for loop in mesh.loops], 'q')
    normal_rows = [tuple(n.vector) for n in mesh.corner_normals]
    if any(not math.isfinite(v) for row in normal_rows for v in row):
        _fail('mesh contains non-finite split normals')
    normals = _digest_numbers(normal_rows, 'd')
    fields = {'geometry_sha256': geometry, 'topology_sha256': topology,
              'uv_sha256': _digest_numbers(uv_rows, 'd'), 'normal_sha256': normals,
              'vertices': len(mesh.vertices), 'polygons': len(mesh.polygons),
              'loops': len(mesh.loops), 'uv_layer': uv_layer}
    fields['sha256'] = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()
    return fields


def _build_material(kind, parameters, image, token, owned):
    import bpy
    mat = bpy.data.materials.new('MWMeshMap/' + token)
    owned.append(mat)
    mat.use_nodes = True
    tree = mat.node_tree
    tree.nodes.clear()
    nodes, links = tree.nodes, tree.links
    output = nodes.new('ShaderNodeOutputMaterial')
    emission = nodes.new('ShaderNodeEmission')
    links.new(emission.outputs['Emission'], output.inputs['Surface'])
    if kind == 'edge':
        bevel = nodes.new('ShaderNodeBevel')
        bevel.inputs['Radius'].default_value = parameters['width']
        bevel.samples = parameters['samples']
        geometry = nodes.new('ShaderNodeNewGeometry')
        dot = nodes.new('ShaderNodeVectorMath'); dot.operation = 'DOT_PRODUCT'
        links.new(bevel.outputs['Normal'], dot.inputs[0])
        links.new(geometry.outputs['Normal'], dot.inputs[1])
        inverse = nodes.new('ShaderNodeMath'); inverse.operation = 'SUBTRACT'
        inverse.inputs[0].default_value = 1
        links.new(dot.outputs['Value'], inverse.inputs[1])
        gain = nodes.new('ShaderNodeMath'); gain.operation = 'MULTIPLY'; gain.use_clamp = True
        gain.inputs[1].default_value = parameters['gain']
        links.new(inverse.outputs[0], gain.inputs[0])
        value = gain.outputs[0]
    else:
        ao = nodes.new('ShaderNodeAmbientOcclusion')
        ao.inputs['Color'].default_value = (1, 1, 1, 1)
        ao.inputs['Distance'].default_value = parameters['distance']
        ao.samples = parameters['samples']; ao.only_local = parameters['only_local']
        inverse = nodes.new('ShaderNodeMath'); inverse.operation = 'SUBTRACT'; inverse.use_clamp = True
        inverse.inputs[0].default_value = 1
        # Intentionally invert AO Color, rather than baking lit surface shading.
        links.new(ao.outputs['Color'], inverse.inputs[1])
        value = inverse.outputs[0]
    links.new(value, emission.inputs['Color'])
    tex = nodes.new('ShaderNodeTexImage'); tex.image = image
    for node in nodes: node.select = False
    tex.select = True; nodes.active = tex
    return mat


def bake_mesh_maps(obj, uv_layer, requests, output_dir, size=512):
    """Bake target-only masks; returns ``maps[id]`` metadata and input fingerprints.

    Width/distance are Blender scene distances with matrix_world preserved.
    Static viewport evaluation is frozen into a temporary mesh. Original meshes,
    modifiers, slots, active UVs, materials and render settings are never edited.
    The result directory is unique, even for identical inputs. No cache lookup.
    """
    import bpy
    normalized = _requests(requests, size)
    if not isinstance(obj, bpy.types.Object) or obj.type != 'MESH' or obj.name not in bpy.context.view_layer.objects:
        _fail('target must be a mesh in the current view layer')
    if obj.mode != 'OBJECT' or bpy.context.mode != 'OBJECT':
        _fail('Object mode is required; edit data is not silently synchronized')
    if not isinstance(uv_layer, str) or not uv_layer:
        _fail('uv_layer must be a non-empty name')
    if not obj.data.polygons:
        _fail('target has no polygons')
    if not isinstance(output_dir, (str, Path)) or not Path(output_dir).is_absolute():
        _fail('output_dir must be an absolute path')
    base = Path(output_dir)
    if base.exists() and not base.is_dir(): _fail('output_dir is not a directory')
    for mod in obj.modifiers:
        if mod.show_viewport != mod.show_render:
            _fail('viewport/render modifier visibility differs: ' + mod.name)
        if mod.show_viewport and mod.type not in _STATIC_MODIFIERS:
            _fail('unsupported M0 modifier: ' + mod.type)
    transform = [float(x) for row in obj.matrix_world for x in row]
    if any(not math.isfinite(x) for x in transform) or abs(obj.matrix_world.determinant()) < 1e-12:
        _fail('target transform must be finite and invertible')
    source = _mesh_fingerprint(obj.data, uv_layer)
    # One shared auxiliary map covers every slot. Slot-isolated UV checks would
    # miss cross-slot aliases and let unrelated faces overwrite the same texel.
    from .procedural_bake import _uv_check
    source_uv = _uv_check(obj.data, uv_layer, {p.material_index for p in obj.data.polygons})
    source_uv['scope'] = 'whole-object UVs, including positive-area overlaps across material slots'
    scene = mesh = duplicate = None
    materials, images = [], []
    window = bpy.context.window
    previous_scene = window.scene if window else None
    previous_layer = window.view_layer if window else None
    run_dir = None
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        mesh = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph),
                    preserve_all_data_layers=True, depsgraph=depsgraph)
        evaluated = _mesh_fingerprint(mesh, uv_layer)
        if not mesh.polygons: _fail('evaluated target has no polygons')
        modifiers = [m for m in obj.modifiers if m.show_viewport]
        bevel_only = bool(modifiers) and all(m.type == 'BEVEL' for m in modifiers)
        evaluated_uv = _uv_check(mesh, uv_layer, {p.material_index for p in mesh.polygons},
                                 allow_degenerate=bevel_only)
        evaluated_uv['scope'] = 'whole-object UVs, including positive-area overlaps across material slots'
        source_inputs = {'source': source, 'evaluated': evaluated,
                         'matrix_world': transform,
                         'transform_sha256': _digest_numbers([transform], 'd'),
                         'evaluation_mode': 'VIEWPORT_STATIC_SNAPSHOT',
                         'frame': bpy.context.scene.frame_current,
                         'blender_version': bpy.app.version_string,
                         'blender_build_hash': bpy.app.build_hash.decode(),
                         'scene_scope': 'target_only'}
        source_inputs['uv_validation'] = {'source': source_uv, 'evaluated': evaluated_uv}
        source_inputs['sha256'] = hashlib.sha256(json.dumps(source_inputs, sort_keys=True).encode()).hexdigest()
        token = uuid.uuid4().hex
        base.mkdir(parents=True, exist_ok=True)
        run_dir = base / ('mesh-maps-' + token)
        run_dir.mkdir(exist_ok=False)
        scene = bpy.data.scenes.new('MWMeshMaps/' + token)
        scene.render.engine = 'CYCLES'
        scene.cycles.device = 'CPU'
        scene.cycles.samples = 1
        scene.cycles.seed = 0
        scene.cycles.use_animated_seed = False
        scene.cycles.use_denoising = False
        scene.render.bake.use_selected_to_active = False
        scene.render.bake.margin = min(8, size // 8)
        scene.render.bake.margin_type = 'EXTEND'
        duplicate = bpy.data.objects.new('MWMeshMapTarget/' + token, mesh)
        scene.collection.objects.link(duplicate)
        duplicate.matrix_world = obj.matrix_world.copy()
        mesh.uv_layers.active_index = mesh.uv_layers.find(uv_layer)
        for uv in mesh.uv_layers: uv.active_render = uv.name == uv_layer
        # Discard source slot references from the private copy, never the original.
        slot_count = max(1, len(mesh.materials), max(p.material_index for p in mesh.polygons) + 1)
        mesh.materials.clear()
        layer = scene.view_layers[0]
        if window:
            window.scene = scene
            window.view_layer = layer
        duplicate.select_set(True, view_layer=layer)
        layer.objects.active = duplicate
        layer.update()
        maps = {}
        for request in normalized:
            ident, kind, params = request['id'], request['type'], request['parameters']
            image = bpy.data.images.new('MWMeshMap/' + ident + '/' + token,
                        width=size, height=size, alpha=False, float_buffer=False)
            images.append(image)
            image.colorspace_settings.name = 'Non-Color'
            image.generated_color = (0, 0, 0, 1)
            material = _build_material(kind, params, image, token + '/' + ident, materials)
            mesh.materials.clear()
            for _ in range(slot_count): mesh.materials.append(material)
            with bpy.context.temp_override(scene=scene, view_layer=layer, object=duplicate,
                    active_object=duplicate, selected_objects=[duplicate],
                    selected_editable_objects=[duplicate]):
                status = bpy.ops.object.bake(type='EMIT', target='IMAGE_TEXTURES',
                    use_clear=True, uv_layer=uv_layer, margin=scene.render.bake.margin)
            if status != {'FINISHED'}:
                raise WorkflowError('mesh_map_bake_failed', 'Cycles bake did not finish: ' + ident)
            path = run_dir / (ident + '.png')
            if path.exists(): raise WorkflowError('mesh_map_output_exists', str(path))
            image.file_format = 'PNG'; image.filepath_raw = str(path)
            image.save()
            data = path.read_bytes()
            if data[:8] != b'\x89PNG\r\n\x1a\n' or struct.unpack('>II', data[16:24]) != (size, size):
                raise WorkflowError('mesh_map_output_invalid', 'PNG dimensions/signature differ')
            pixels = array('f', [0.0]) * (size * size * 4)
            image.pixels.foreach_get(pixels)
            decoded = None
            tolerance = 1.0 / 255.0 + 1e-6
            try:
                decoded = bpy.data.images.load(str(path), check_existing=False)
                decoded.colorspace_settings.name = 'Non-Color'
                if tuple(decoded.size) != (size, size) or len(decoded.pixels) != len(pixels):
                    raise WorkflowError('mesh_map_decode_failed', 'Decoded image dimensions differ: ' + ident)
                disk_pixels = array('f', [0.0]) * len(decoded.pixels)
                decoded.pixels.foreach_get(disk_pixels)
                if any(not math.isfinite(v) for v in disk_pixels):
                    raise WorkflowError('mesh_map_decode_failed', 'Decoded image contains non-finite data')
                maximum_error = max(abs(a - b) for a, b in zip(pixels, disk_pixels))
                if maximum_error > tolerance:
                    raise WorkflowError('mesh_map_decode_failed', 'Saved PNG differs from baked buffer: ' + ident)
                values = disk_pixels[0::4]
                if min(values) < 0 or max(values) > 1:
                    raise WorkflowError('mesh_map_decode_failed', 'Decoded mask exceeds 0..1: ' + ident)
            finally:
                if decoded is not None: bpy.data.images.remove(decoded)
            maps[ident] = {'file': str(path), 'sha256': hashlib.sha256(data).hexdigest(),
                'width': size, 'height': size, 'bytes': len(data), 'color_space': 'Non-Color',
                'type': kind, 'method': 'cycles_bevel_normal_1_minus_dot_clamped' if kind == 'edge'
                    else 'cycles_ao_color_inverted_target_only',
                'parameters': params, 'minimum': min(values), 'maximum': max(values),
                'mean': sum(values) / len(values), 'input_sha256': source_inputs['sha256'],
                'scene_scope': 'target_only', 'uv_margin': scene.render.bake.margin,
                'statistics_source': 'independently_reloaded_Non-Color_disk_pixels',
                'buffer_disk_max_abs_error': maximum_error, 'decode_tolerance': tolerance,
                'disk_decode_verified': True}
        result = {'maps': maps, 'inputs': source_inputs, 'output_dir': str(run_dir),
                  'cache': 'disabled', 'status': 'complete'}
        (run_dir / 'mesh-maps.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        return result
    except WorkflowError:
        raise
    except Exception as error:
        raise WorkflowError('mesh_map_bake_failed', str(error)) from error
    finally:
        if window and previous_scene:
            window.scene = previous_scene
            if previous_layer: window.view_layer = previous_layer
        if scene: bpy.data.scenes.remove(scene)
        if duplicate and duplicate.name in bpy.data.objects:
            bpy.data.objects.remove(duplicate, do_unlink=True)
        if mesh and mesh.name in bpy.data.meshes: bpy.data.meshes.remove(mesh)
        for material in materials:
            if material.name in bpy.data.materials: bpy.data.materials.remove(material)
        for image in images:
            if image.name in bpy.data.images: bpy.data.images.remove(image)
