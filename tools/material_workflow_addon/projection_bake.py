"""Cycles geometry projection with an explicit target, contributors and UV."""
import json
import math
import uuid
from pathlib import Path

from .contract import WorkflowError
from .procedural_bake import _pixels, _save_verified, _uv_check
from .procedural_mesh_maps import _digest_numbers, _mesh_fingerprint


CHANNELS = {
    'normal': 'Tangent-space normal, +X +Y +Z',
    'curvature': 'Normalized convex mean curvature',
    'ao': 'Ambient visibility, white is unoccluded',
}
CURVATURE = 'MW_ConvexCurvature'


def _fail(message):
    raise WorkflowError('projection_bake', message)


def _positive(value, label, zero=False):
    if not math.isfinite(value) or value < 0 or (not zero and value == 0):
        _fail(f'{label} must be finite and {"nonnegative" if zero else "positive"}')


def _curvature(mesh, matrix, radius):
    import bmesh
    from mathutils import Vector

    # Integrate signed hinge curvature around each vertex, then divide by
    # its barycentric surface area. Do this in world units, before encoding.
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.transform(matrix)
        bm.normal_update()
        bm.verts.index_update()
        accum = [0.0] * len(bm.verts)
        areas = [0.0] * len(bm.verts)
        for face in bm.faces:
            area = face.calc_area() / len(face.verts)
            for vertex in face.verts:
                areas[vertex.index] += area
        for edge in bm.edges:
            if edge.is_manifold:
                weight = edge.calc_face_angle_signed(0) * edge.calc_length() * 0.25
                for vertex in edge.verts:
                    accum[vertex.index] += weight
        values = [min(1.0, max(0.0, radius * total / area)) if area > 0 else 0.0
                  for total, area in zip(accum, areas)]
    finally:
        bm.free()
    attribute = mesh.attributes.new(CURVATURE, 'FLOAT', 'POINT')
    attribute.data.foreach_set('value', values)
    return attribute.name


def _snapshot(obj, scene, depsgraph, meshes, objects):
    import bpy

    evaluated = obj.evaluated_get(depsgraph)
    matrix = evaluated.matrix_world.copy()
    if not all(math.isfinite(item) for row in matrix for item in row) or matrix.determinant() <= 0:
        _fail(f'{obj.name}: apply reflected or singular transforms before baking')
    axes = [axis.normalized() for axis in matrix.to_3x3().col]
    if any(abs(axes[a].dot(axes[b])) > 1e-5 for a, b in ((0, 1), (0, 2), (1, 2))):
        _fail(f'{obj.name}: apply the sheared world transform before baking')
    mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=depsgraph)
    meshes.append(mesh)
    if not mesh.polygons:
        _fail(f'{obj.name}: evaluated mesh is empty')
    clone = bpy.data.objects.new(obj.name + ' / projection', mesh)
    objects.append(clone)
    scene.collection.objects.link(clone)
    clone.matrix_world = matrix
    mesh.materials.clear()
    return clone


def _geometry_record(mesh, matrix):
    return {
        'vertices': len(mesh.vertices),
        'faces': len(mesh.polygons),
        'positions_sha256': _digest_numbers((vertex.co for vertex in mesh.vertices), 'd'),
        'corners_sha256': _digest_numbers((tuple(face.vertices) for face in mesh.polygons), 'q'),
        'normals_sha256': _digest_numbers((corner.vector for corner in mesh.corner_normals), 'd'),
        'matrix_world': [list(row) for row in matrix],
    }


def _emission(material):
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new('ShaderNodeOutputMaterial')
    emission = tree.nodes.new('ShaderNodeEmission')
    emission.inputs['Color'].default_value = (1, 1, 1, 1)
    tree.links.new(emission.outputs[0], output.inputs['Surface'])
    return tree, emission.inputs['Color']


def bake_projection(context, target, sources, uv_layer, output_dir, *,
                    size=1024, extrusion=0.01, distance=0.02, cage=None,
                    ao_distance=0.02, curvature_radius=0.005, samples=16, margin=8,
                    channels=None, device='CPU', progress=None):
    """Return the path of a complete manifest. Failed runs remain unpublished."""
    import bpy
    import numpy as np

    requested = tuple(CHANNELS) if channels is None else tuple(channels)
    if not requested or len(set(requested)) != len(requested) or set(requested) - set(CHANNELS):
        _fail('Select at least one distinct projection channel')
    complete = set(requested) == set(CHANNELS)
    if device not in {'CPU', 'GPU'}:
        _fail('Projection device must be CPU or GPU')
    if progress is None:
        progress = lambda *_: None
    sources = tuple(sources)
    inputs = (target, *sources, *((cage,) if cage else ()))
    if context.mode != 'OBJECT' or not sources or len(set(inputs)) != len(inputs):
        _fail('Choose distinct sources, target and optional cage in Object Mode')
    for obj in inputs:
        if obj.type != 'MESH' or context.view_layer.objects.get(obj.name) != obj:
            _fail('Every projection input must be a mesh in the current view layer')
    if not isinstance(size, int) or not 64 <= size <= 2048:
        _fail('Resolution must be an integer from 64 to 2048')
    if not isinstance(samples, int) or not 1 <= samples <= 128:
        _fail('Samples must be an integer from 1 to 128')
    if not isinstance(margin, int) or not 0 <= margin <= size // 8:
        _fail('Margin must be between 0 and one eighth of the resolution')
    _positive(extrusion, 'Extrusion', zero=True)
    _positive(distance, 'Ray distance')
    _positive(ao_distance, 'AO distance')
    _positive(curvature_radius, 'Curvature radius')
    base = Path(output_dir)
    if not base.is_absolute():
        _fail('The bake directory must be absolute')

    source_scene = context.scene
    depsgraph = context.evaluated_depsgraph_get()
    window = context.window
    original_layer = context.view_layer
    scene = bpy.data.scenes.new('MW Projection')
    layer = scene.view_layers[0]
    meshes, objects, materials = [], [], []
    live_image = None
    try:
        receiver = _snapshot(target, scene, depsgraph, meshes, objects)
        contributors = [_snapshot(obj, scene, depsgraph, meshes, objects) for obj in sources]
        shell = _snapshot(cage, scene, depsgraph, meshes, objects) if cage else None
        uv_check = _uv_check(receiver.data, uv_layer, {face.material_index for face in receiver.data.polygons})
        uv_check['scope'] = 'entire evaluated target'
        identity = {
            'target': _mesh_fingerprint(receiver.data, uv_layer),
            'target_transform': [list(row) for row in receiver.matrix_world],
            'sources': {obj.name: _geometry_record(clone.data, clone.matrix_world)
                        for obj, clone in zip(sources, contributors)},
            'frame': source_scene.frame_current,
            'evaluation': 'viewport snapshot',
        }
        if shell:
            target_faces = [tuple(face.vertices) for face in receiver.data.polygons]
            cage_faces = [tuple(face.vertices) for face in shell.data.polygons]
            if len(shell.data.vertices) != len(receiver.data.vertices) or cage_faces != target_faces:
                _fail('The evaluated cage must use the target vertex order and face topology')
            shell.hide_render = True
            identity['cage'] = {'name': cage.name, **_geometry_record(shell.data, shell.matrix_world)}
        receiver.data.uv_layers.active_index = receiver.data.uv_layers.find(uv_layer)
        receiver.data.uv_layers[uv_layer].active_render = True

        destination = base / ('projection-' + uuid.uuid4().hex[:12])
        destination.mkdir(parents=True)
        scene.render.engine = 'CYCLES'
        scene.cycles.device = device
        scene.cycles.samples = samples
        scene.cycles.use_denoising = False
        scene.render.bake.margin_type = 'EXTEND'
        scene.render.bake.normal_r = 'POS_X'
        scene.render.bake.normal_g = 'POS_Y'
        scene.render.bake.normal_b = 'POS_Z'
        receiver_mat = bpy.data.materials.new('MW Projection Receiver')
        materials.append(receiver_mat)
        receiver.data.materials.append(receiver_mat)
        tree, _ = _emission(receiver_mat)
        tex = tree.nodes.new('ShaderNodeTexImage')
        tree.nodes.active = tex

        source_inputs = []
        for obj in contributors:
            material = bpy.data.materials.new('MW Projection Source')
            materials.append(material)
            obj.data.materials.append(material)
            source_tree, emission_color = _emission(material)
            channel_outputs = {}
            if 'curvature' in requested:
                curve = source_tree.nodes.new('ShaderNodeAttribute')
                curve.attribute_name = _curvature(obj.data, obj.matrix_world, curvature_radius)
                channel_outputs['curvature'] = curve.outputs['Fac']
            if 'ao' in requested:
                ao = source_tree.nodes.new('ShaderNodeAmbientOcclusion')
                ao.inputs['Distance'].default_value = ao_distance
                ao.samples = samples
                # AO samples the hit source surface independently of the low mesh.
                ao.only_local = True
                channel_outputs['ao'] = ao.outputs['AO']
            source_inputs.append((source_tree.links, emission_color, channel_outputs))

        if window:
            window.scene = scene
            window.view_layer = layer
        layer.objects.active = receiver
        receiver.select_set(True, view_layer=layer)
        layer.update()

        def bake(name, mode, selected_to_active, padding):
            image = bpy.data.images.new(name, width=size, height=size, alpha=True, float_buffer=True)
            image.colorspace_settings.name = 'Non-Color'
            tex.image = image
            selected = [receiver]
            for contributor in contributors:
                contributor.select_set(selected_to_active, view_layer=layer)
            if selected_to_active:
                selected.extend(contributors)
            try:
                with context.temp_override(scene=scene, view_layer=layer, object=receiver,
                                           active_object=receiver, selected_objects=selected,
                                           selected_editable_objects=selected):
                    result = bpy.ops.object.bake(
                        type=mode, target='IMAGE_TEXTURES', use_clear=True, uv_layer=uv_layer,
                        use_selected_to_active=selected_to_active, normal_space='TANGENT',
                        use_cage=selected_to_active and bool(shell or extrusion),
                        cage_object=shell.name if shell and selected_to_active else '',
                        cage_extrusion=extrusion, max_ray_distance=distance,
                        margin=padding, margin_type='EXTEND')
                if result != {'FINISHED'}:
                    _fail(f'{name}: Cycles bake did not finish')
                return image
            except Exception:
                bpy.data.images.remove(image)
                raise

        def witness(name, project):
            image = bake(name, 'EMIT', project, 0)
            try:
                return _pixels(image)[:, 0] > 0.5
            finally:
                bpy.data.images.remove(image)

        progress(0.2, '正在检查目标 UV 覆盖')
        covered = witness('Target UV coverage', False)
        progress(0.35, '正在检查高模投射命中')
        hits = witness('Named source hit witness', True)
        covered_count = int(np.count_nonzero(covered))
        unhit = covered & ~hits
        missed = int(np.count_nonzero(unhit))
        if not covered_count or missed == covered_count:
            _fail(f'{target.name}: no confirmed ray hits on the target UV')

        maps = {}
        names = {'normal': '切线法线', 'curvature': '曲率', 'ao': '环境遮蔽'}
        for index, name in enumerate(requested):
            progress(0.48 + 0.4 * index / len(requested), '正在烘焙' + names[name])
            if name != 'normal':
                for links, socket, outputs in source_inputs:
                    links.new(outputs[name], socket)
            live_image = bake(name, 'NORMAL' if name == 'normal' else 'EMIT', True, margin)
            if missed:
                # Keep missed texels neutral in the material. The independent
                # source hit witness continues to report them as missing.
                pixels = _pixels(live_image)
                value = {'normal': (0.5, 0.5, 1, 1), 'curvature': (0, 0, 0, 1), 'ao': (1, 1, 1, 1)}
                pixels[unhit] = value[name]
                live_image.pixels.foreach_set(pixels.ravel())
            path = destination / (name + '.png')
            entry = _save_verified(live_image, path, 'Non-Color', '16')
            live_image = None
            maps[name] = {**entry, 'file': path.name, 'meaning': CHANNELS[name]}

        report = {
            'schema': 'mw-projection-1.0' if complete else 'mw-projection-maps-1.0',
            'status': 'complete', 'blender': bpy.app.version_string,
            'target': target.name, 'sources': [obj.name for obj in sources], 'uv_layer': uv_layer,
            'identity': identity, 'uv_check': uv_check, 'maps': maps,
            'witness': {'target_texels': covered_count, 'missed_texels': missed,
                        'coverage_fraction': 1 - missed / covered_count},
            'settings': {'size': size, 'extrusion': extrusion, 'distance': distance,
                         'ao_distance': ao_distance, 'curvature_radius': curvature_radius,
                         'ao_scope': 'hit source object', 'samples': samples, 'margin': margin},
        }
        progress(0.92, '正在核验贴图并写入报告')
        manifest = destination / ('projection.json' if complete else 'projection-maps.json')
        temporary = destination / 'projection.tmp'
        temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
        temporary.replace(manifest)
        return manifest
    finally:
        if live_image is not None:
            # The reused encoder may have released the generated buffer already.
            try:
                bpy.data.images.remove(live_image)
            except ReferenceError:
                pass
        if window:
            window.scene = source_scene
            window.view_layer = original_layer
        for obj in objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.scenes.remove(scene)
        for mesh in meshes:
            bpy.data.meshes.remove(mesh)
        for material in materials:
            bpy.data.materials.remove(material)
