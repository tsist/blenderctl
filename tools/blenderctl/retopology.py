"""Bounded QuadriFlow adapters with world-space verification."""
import math
from collections import Counter
import bpy
import bmesh
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from protocol import Failure
from scene_contract import validate
from retopology_contract import OP


def fail(message):
    raise Failure('UNSUPPORTED', message)


def geometry(item):
    mesh = item.data
    mesh.calc_loop_triangles()
    vertices = [item.matrix_world @ v.co for v in mesh.vertices]
    triangles = [tuple(t.vertices) for t in mesh.loop_triangles]
    normals = []
    for ids in triangles:
        a, b, c = [vertices[i] for i in ids]
        cross = (b-a).cross(c-a)
        if not math.isfinite(cross.length) or cross.length <= 2e-12:
            fail('Retopology requires finite nondegenerate world-space triangles')
        normals.append(cross.normalized())
    return vertices, triangles, normals


def topology_evidence(item, source=False):
    mesh = item.data
    if not mesh.polygons or len(mesh.vertices) > 30000 or len(mesh.polygons) > 30000 or len(mesh.loops) > 120000:
        fail('Retopology requires 1..30000 input/result faces and vertices and at most 120000 loops')
    if any(not math.isfinite(x) for v in mesh.vertices for x in v.co):
        fail('Nonfinite mesh coordinates')
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.transform(item.matrix_world)
        bm.normal_update()
        if any(not e.is_manifold or not e.is_contiguous for e in bm.edges) or any(not v.is_manifold for v in bm.verts):
            fail('Retopology requires a closed consistently oriented manifold mesh')
        remaining = set(bm.verts)
        components = 0
        while remaining:
            components += 1
            queue = [remaining.pop()]
            while queue:
                vertex = queue.pop()
                for edge in vertex.link_edges:
                    other = edge.other_vert(vertex)
                    if other in remaining:
                        remaining.remove(other)
                        queue.append(other)
        if components != 1:
            fail('Retopology requires exactly one connected component')
        if any(f.calc_area() <= 1e-12 or not math.isfinite(f.calc_area()) for f in bm.faces):
            fail('Degenerate or nonfinite source/result faces')
        angle = max((e.calc_face_angle() for e in bm.edges), default=0)
        if source and angle > math.radians(45) + 1e-6:
            fail('Geometric hard edges over 45 degrees are unsupported')
        return {'vertices': len(bm.verts), 'edges': len(bm.edges), 'faces': len(bm.faces),
                'euler_characteristic': len(bm.verts)-len(bm.edges)+len(bm.faces),
                'components': components, 'boundary_edges': 0, 'nonmanifold_edges': 0,
                'maximum_dihedral_degrees': math.degrees(angle),
                'quad_faces': sum(len(f.verts) == 4 for f in bm.faces)}
    finally:
        bm.free()


def distance(source, target):
    sv, st, sn = source
    tv, tt, tn = target
    tree = BVHTree.FromPolygons(tv, tt, all_triangles=True)
    count = 0
    sum_distance = sum_angle = maximum = max_angle = 0.0
    for ids, normal in zip(st, sn):
        a, b, c = [sv[i] for i in ids]
        for sample in (a, b, c, (a+b)/2, (b+c)/2, (c+a)/2, (a+b+c)/3):
            location, unused_normal, triangle, separation = tree.find_nearest(sample)
            if location is None or not math.isfinite(separation):
                raise Failure('VALIDATION_FAILED', 'Invalid nearest-surface sample')
            angle = math.degrees(math.acos(max(-1.0, min(1.0, normal.dot(tn[triangle])))))
            count += 1
            sum_distance += separation*separation
            sum_angle += angle*angle
            maximum = max(maximum, separation)
            max_angle = max(max_angle, angle)
    return {'samples': count, 'maximum_distance': maximum, 'rms_distance': math.sqrt(sum_distance/count),
            'maximum_normal_angle_degrees': max_angle, 'rms_normal_angle_degrees': math.sqrt(sum_angle/count)}


def inspect_attributes(item):
    mesh = item.data
    if item.parent or item.modifiers or item.constraints or mesh.uv_layers or mesh.color_attributes or mesh.shape_keys or item.vertex_groups:
        fail('Retopology rejects parenting, modifiers, constraints, UVs, colors, shape keys and vertex groups')
    if any(v.groups for v in mesh.vertices):
        fail('Retopology does not transfer weights')
    if mesh.has_custom_normals:
        fail('Custom normals require explicit normal transfer')
    if len(mesh.materials) > 1 or any(m is None for m in mesh.materials) or any(f.material_index != 0 for f in mesh.polygons):
        fail('Retopology supports at most one material with index zero')
    if len({f.use_smooth for f in mesh.polygons}) != 1:
        fail('Retopology requires uniform smooth shading')
    if any(e.use_edge_sharp or e.use_seam for e in mesh.edges):
        fail('Retopology rejects sharp and seam edge attributes')
    if mesh.asset_data or mesh.keys():
        fail('Retopology does not transfer identified assets or mesh custom properties')
    allowed = {'position', '.edge_verts', '.corner_vert', '.corner_edge',
               '.select_vert', '.select_edge', '.select_poly', '.uv_select_vert', '.uv_select_edge',
               '.uv_select_face', 'sharp_face', 'sharp_edge', 'material_index'}
    if any(a.name not in allowed for a in mesh.attributes):
        fail('Retopology rejects nontrivial named mesh attributes')


def _connected_components(bm):
    remaining = set(bm.verts)
    components = 0
    while remaining:
        components += 1
        queue = [remaining.pop()]
        while queue:
            vertex = queue.pop()
            for edge in vertex.link_edges:
                other = edge.other_vert(vertex)
                if other in remaining:
                    remaining.remove(other)
                    queue.append(other)
    return components


def _geometry_for_bvh(item, warnings=None, material_indices=None):
    mesh = item.data
    mesh.calc_loop_triangles()
    vertices = [item.matrix_world @ v.co for v in mesh.vertices]
    triangles = []
    normals = []
    degenerate = 0
    for t in mesh.loop_triangles:
        ids = tuple(t.vertices)
        a, b, c = [vertices[i] for i in ids]
        cross = (b - a).cross(c - a)
        if not math.isfinite(cross.length) or cross.length <= 2e-12:
            degenerate += 1
            continue
        triangles.append(ids)
        normals.append(cross.normalized())
        if material_indices is not None:
            material_indices.append(mesh.polygons[t.polygon_index].material_index)
    if not triangles:
        fail('Retopology requires at least one nondegenerate world-space triangle for BVH reference')
    if degenerate and warnings is not None:
        warnings.append(f'source_degenerate_triangles_skipped:{degenerate}')
    return vertices, triangles, normals


def _transfer_material_regions(item, source, materials, triangle_slots):
    item.data.materials.clear()
    for material in materials:
        item.data.materials.append(material)
    if not materials:
        return None

    vertices, triangles, _ = source
    tree = BVHTree.FromPolygons(vertices, triangles, all_triangles=True)
    counts = Counter()
    for face in item.data.polygons:
        _, _, triangle, distance = tree.find_nearest(item.matrix_world @ face.center)
        if triangle is None or not math.isfinite(distance):
            raise Failure('VALIDATION_FAILED', 'Cannot locate source material at the retopologized face')
        face.material_index = triangle_slots[triangle]
        counts[face.material_index] += 1

    missing = set(triangle_slots) - counts.keys()
    if missing:
        names = [materials[index].name for index in sorted(missing)]
        raise Failure('VALIDATION_FAILED', f'Retopology lost source material regions {names}; increase the target face count')
    return {'method': 'WORLD_BVH_FACE_CENTER', 'source_triangle_counts': dict(Counter(triangle_slots)),
            'candidate_face_counts': dict(counts), 'all_source_slots_retained': True}


def _dirty_topology_evidence(item, source=False):
    mesh = item.data
    max_v, max_l = (500000, 2000000) if source else (200000, 1000000)
    if not mesh.polygons or len(mesh.vertices) > max_v or len(mesh.polygons) > max_v or len(mesh.loops) > max_l:
        fail(f'Mesh exceeds {max_v} vertices/faces or {max_l} loops budget')
    if any(not math.isfinite(x) for v in mesh.vertices for x in v.co):
        fail('Nonfinite mesh coordinates')
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.transform(item.matrix_world)
        bm.normal_update()
        boundary_edges = sum(1 for e in bm.edges if len(e.link_faces) == 1)
        nonmanifold_edges = sum(
            1 for e in bm.edges
            if len(e.link_faces) > 2 or len(e.link_faces) == 0 or (len(e.link_faces) == 2 and not e.is_contiguous)
        )
        degenerate_faces = sum(1 for f in bm.faces if f.calc_area() <= 1e-12 or not math.isfinite(f.calc_area()))
        components = _connected_components(bm)
        angle = max((e.calc_face_angle(0.0) for e in bm.edges if len(e.link_faces) == 2), default=0.0)
        if not source and (nonmanifold_edges > 0 or degenerate_faces > 0):
            raise Failure('VALIDATION_FAILED', f'Remesh result contains nonmanifold edges ({nonmanifold_edges}) or degenerate faces ({degenerate_faces})')
        return {
            'vertices': len(bm.verts),
            'edges': len(bm.edges),
            'faces': len(bm.faces),
            'euler_characteristic': len(bm.verts) - len(bm.edges) + len(bm.faces),
            'components': components,
            'boundary_edges': boundary_edges,
            'nonmanifold_edges': nonmanifold_edges,
            'degenerate_faces': degenerate_faces,
            'maximum_dihedral_degrees': math.degrees(angle),
            'quad_faces': sum(1 for f in bm.faces if len(f.verts) == 4),
        }
    finally:
        bm.free()


def _run_quadriflow(item, op, smooth, context):
    from modeling import operator_context, finished
    with operator_context(item, context):
        finished(bpy.ops.object.quadriflow_remesh(
            mode='FACES',
            target_faces=op['target_faces'],
            seed=op.get('seed', 0),
            use_mesh_symmetry=bool(op.get('use_mesh_symmetry', False)),
            use_preserve_sharp=bool(op.get('use_preserve_sharp', True)),
            use_preserve_boundary=bool(op.get('use_preserve_boundary', True)),
            preserve_attributes=False,
            smooth_normals=smooth,
        ))


def _execute_shrinkwrap(op, context):
    from modeling import local_static, operator_context, finished, topology, bounded
    import scenes

    item = scenes.find(bpy.data.objects, op['object'], True)
    local_static(item)
    if item.type != 'MESH':
        raise Failure('INVALID_REQUEST', 'Object type incompatible with retopology')
    local_static(item.data)
    if topology(item.data) != op['topology_sha256']:
        raise Failure('CONFLICT', 'Stale retopology topology hash')
    if any(not math.isfinite(x) for row in item.matrix_world for x in row) or abs(item.matrix_world.determinant()) < 1e-12:
        fail('Retopology requires a finite nonsingular world transform')
    apply_modifiers = bool(op.get('apply_modifiers', True))
    if not apply_modifiers and item.modifiers:
        raise Failure('CONFLICT', 'Object has modifiers; set apply_modifiers=True or apply/remove modifiers explicitly')
    if item.data.users > 1:
        if op['data_scope'] == 'reject_shared':
            raise Failure('CONFLICT', 'Shared data requires explicit single_user policy')
        if item.data.asset_data or item.data.get('asset_id'):
            fail('Cannot duplicate asset data identity')

    source_mesh = item.data
    builtin_attrs = {
        'position', '.edge_verts', '.corner_vert', '.corner_edge',
        '.select_vert', '.select_edge', '.select_poly', '.uv_select_vert',
        '.uv_select_edge', '.uv_select_face', 'sharp_face', 'sharp_edge', 'material_index',
    }
    uv_names = [u.name for u in source_mesh.uv_layers]
    color_names = [c.name for c in source_mesh.color_attributes]
    dropped = {
        'uv_layers': uv_names,
        'vertex_groups': [g.name for g in item.vertex_groups],
        'shape_keys': [k.name for k in source_mesh.shape_keys.key_blocks] if source_mesh.shape_keys else [],
        'color_attributes': color_names,
        'custom_attributes': [
            a.name for a in source_mesh.attributes
            if a.name not in builtin_attrs and a.name not in uv_names and a.name not in color_names
        ],
        'applied_modifiers': [m.name for m in item.modifiers] if apply_modifiers else [],
        'had_custom_normals': bool(source_mesh.has_custom_normals),
        'sharp_edges': sum(1 for e in source_mesh.edges if e.use_edge_sharp),
        'seam_edges': sum(1 for e in source_mesh.edges if e.use_seam),
    }

    source_warnings = []
    if item.parent or item.constraints:
        source_warnings.append('parent_or_constraints_present_world_transform_preserved')
    if any(dropped[k] for k in ('uv_layers', 'vertex_groups', 'shape_keys', 'color_attributes', 'custom_attributes')) or dropped['had_custom_normals']:
        source_warnings.append('attributes_dropped_per_DROP_WITH_WARNING')

    scenes.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    eval_source = item.evaluated_get(depsgraph) if apply_modifiers else item
    if apply_modifiers:
        eval_mesh = bpy.data.meshes.new_from_object(eval_source, preserve_all_data_layers=False, depsgraph=depsgraph)
    else:
        eval_mesh = source_mesh.copy()

    if len(eval_mesh.vertices) > 500000 or len(eval_mesh.loops) > 2000000 or len(eval_mesh.polygons) < 4:
        bpy.data.meshes.remove(eval_mesh)
        fail('Evaluated source mesh must contain 4..500000 faces/vertices and at most 2000000 loops')

    ref_mesh = eval_mesh.copy()
    ref_obj = bpy.data.objects.new(item.name + '__RetopoRef', ref_mesh)
    bpy.context.scene.collection.objects.link(ref_obj)
    ref_obj.matrix_world = item.matrix_world.copy()
    pre_voxel_mesh = None
    voxel_used = False
    voxel_fallback = False
    shrinkwrap_applied = False

    try:
        before = _dirty_topology_evidence(ref_obj, source=True)
        if before['boundary_edges'] > 0:
            source_warnings.append('open_boundary_input')
        if before['nonmanifold_edges'] > 0:
            source_warnings.append('nonmanifold_input')
        if before['components'] > 1:
            source_warnings.append('multi_component_input')
        triangle_slots = []
        original_geom = _geometry_for_bvh(ref_obj, source_warnings, triangle_slots)
        materials = [slot.material.original if slot.material else None for slot in eval_source.material_slots]
        if len(materials) > 64:
            fail('Retopology supports up to 64 material slots')
        if len(materials) != len(ref_mesh.materials) or materials and max(triangle_slots) >= len(materials):
            fail('Evaluated material slot layout cannot be transferred')
        if materials and any(materials[index] is None for index in set(triangle_slots)):
            fail('Source geometry uses an empty material slot')
        smooth = sum(1 for f in eval_mesh.polygons if f.use_smooth) * 2 >= len(eval_mesh.polygons)
        matrices = ([list(r) for r in item.matrix_world], [list(r) for r in item.matrix_basis])

        # Isolate working mesh on item and drop legacy attributes/modifiers explicitly
        old_data = item.data
        work_name = item.name + '.RetopoWork'
        scenes.fresh(bpy.data.meshes, work_name)
        eval_mesh.name = work_name
        item.modifiers.clear()
        item.vertex_groups.clear()
        item.data = eval_mesh
        while len(item.data.uv_layers):
            item.data.uv_layers.remove(item.data.uv_layers[0])
        while len(item.data.color_attributes):
            item.data.color_attributes.remove(item.data.color_attributes[0])
        for attr in list(item.data.attributes):
            if attr.name not in builtin_attrs:
                item.data.attributes.remove(attr)
        if item.data.shape_keys:
            with operator_context(item, context):
                item.shape_key_clear()
        if old_data.users == 0:
            bpy.data.meshes.remove(old_data)

        pre_voxel_mesh = item.data.copy()

        if op.get('voxel_remesh', True) and before['boundary_edges'] > 0:
            source_warnings.append('voxel_remesh_skipped_open_boundary_input')
        elif op.get('voxel_remesh', True):
            coords = [v.co for v in item.data.vertices]
            lo = [min(c[i] for c in coords) for i in range(3)]
            hi = [max(c[i] for c in coords) for i in range(3)]
            diag = max(math.sqrt(sum((hi[i] - lo[i]) ** 2 for i in range(3))), 1e-3)
            voxel_size = op.get('voxel_size')
            if voxel_size is None:
                voxel_size = max(1e-4, diag / 100.0)
            elif diag / voxel_size > 400.0:
                voxel_size = diag / 400.0
                source_warnings.append('voxel_size_clamped_for_memory_safety')
            item.data.remesh_voxel_size = voxel_size
            try:
                with operator_context(item, context):
                    res = bpy.ops.object.voxel_remesh()
                if res == {'FINISHED'} and 4 <= len(item.data.polygons) <= 500000 and len(item.data.vertices) <= 500000:
                    voxel_used = True
                else:
                    raise Failure('VALIDATION_FAILED', 'Voxel remesh produced empty or oversized geometry')
            except Exception:
                source_warnings.append('voxel_remesh_failed_fallback_to_base')
                failed_mesh = item.data
                item.data = pre_voxel_mesh.copy()
                bpy.data.meshes.remove(failed_mesh)
                voxel_used = False
                voxel_fallback = True

        try:
            _run_quadriflow(item, op, smooth, context)
        except Exception:
            if voxel_used:
                source_warnings.append('quadriflow_failed_after_voxel_fallback_to_base')
                failed_mesh = item.data
                item.data = pre_voxel_mesh.copy()
                bpy.data.meshes.remove(failed_mesh)
                voxel_used = False
                voxel_fallback = True
                _run_quadriflow(item, op, smooth, context)
            else:
                raise

        needs_qflow_fallback = len(item.data.polygons) < 4
        if not needs_qflow_fallback and voxel_used:
            try:
                post_q = _dirty_topology_evidence(item, source=True)
                if post_q['nonmanifold_edges'] > 0 or post_q['degenerate_faces'] > 0:
                    needs_qflow_fallback = True
            except Exception:
                needs_qflow_fallback = True
        if needs_qflow_fallback:
            if voxel_used:
                source_warnings.append(
                    'quadriflow_empty_after_voxel_fallback_to_base'
                    if len(item.data.polygons) < 4
                    else 'quadriflow_nonmanifold_after_voxel_fallback_to_base'
                )
                failed_mesh = item.data
                item.data = pre_voxel_mesh.copy()
                bpy.data.meshes.remove(failed_mesh)
                item.data.name = work_name
                voxel_used = False
                voxel_fallback = True
                _run_quadriflow(item, op, smooth, context)
            if len(item.data.polygons) < 4:
                raise Failure('VALIDATION_FAILED', 'QuadriFlow produced empty or invalid mesh')

        if op.get('shrinkwrap', True):
            mod = item.modifiers.new(name='RetopoShrinkwrap', type='SHRINKWRAP')
            mod.target = ref_obj
            mod.wrap_method = op.get('shrinkwrap_method', 'NEAREST_SURFACEPOINT')
            if mod.wrap_method == 'PROJECT':
                mod.use_negative_direction = True
                mod.use_positive_direction = True
            with operator_context(item, context):
                finished(bpy.ops.object.modifier_apply(modifier=mod.name))
            shrinkwrap_applied = True

        material_transfer = _transfer_material_regions(item, original_geom, materials, triangle_slots)
        for face in item.data.polygons:
            face.use_smooth = smooth
        item.data.update()

        if matrices != ([list(r) for r in item.matrix_world], [list(r) for r in item.matrix_basis]):
            raise Failure('VALIDATION_FAILED', 'Retopology changed object transforms')

        bounded(item.data)
        after = _dirty_topology_evidence(item, source=False)
        count_error = abs(after['faces'] - op['target_faces']) / op['target_faces']
        if count_error > op['face_count_tolerance']:
            raise Failure('VALIDATION_FAILED', 'Actual face count exceeds declared relative tolerance')
        candidate_geom = geometry(item)
        forward, reverse = distance(original_geom, candidate_geom), distance(candidate_geom, original_geom)
        if max(forward['maximum_distance'], reverse['maximum_distance']) > op['max_surface_distance']:
            raise Failure('VALIDATION_FAILED', 'Retopology exceeds declared sampled world-space surface-distance budget')
        if max(forward['maximum_normal_angle_degrees'], reverse['maximum_normal_angle_degrees']) > op['max_normal_angle_degrees']:
            raise Failure('VALIDATION_FAILED', 'Retopology exceeds declared sampled triangle-normal angle budget')

        return {
            'op': op['op'],
            'object': item.name,
            'adapter': 'QUADRIFLOW_SHRINKWRAP_V1',
            'before': before,
            'after': after,
            'target_faces': op['target_faces'],
            'relative_face_count_error': count_error,
            'topology_sha256': topology(item.data),
            'seed': op.get('seed', 0),
            'source_to_candidate': forward,
            'candidate_to_source': reverse,
            'material_names': [m.name if m else None for m in item.data.materials],
            'material_transfer': material_transfer,
            'smooth': smooth,
            'sampling': 'World-space loop triangles: vertices, edge midpoints and centroid; finite samples against evaluated source reference.',
            'attribute_transfer': 'DROP_WITH_WARNING: modifiers baked if enabled, legacy UV/weights/shape keys/colors/custom attributes dropped.',
            'limitations': 'Evaluates sampled BVH surface distance/normal after optional voxel manifold pass and shrinkwrap; visually inspect silhouette and re-author UV/weights.',
            'attribute_policy': 'DROP_WITH_WARNING',
            'dropped_attributes': dropped,
            'voxel_remesh_used': voxel_used,
            'voxel_fallback_used': voxel_fallback,
            'shrinkwrap_applied': shrinkwrap_applied,
            'source_warnings': source_warnings,
        }
    finally:
        if pre_voxel_mesh is not None and pre_voxel_mesh.name in bpy.data.meshes and pre_voxel_mesh.users == 0:
            bpy.data.meshes.remove(pre_voxel_mesh)
        if ref_obj.name in bpy.data.objects:
            bpy.data.objects.remove(ref_obj, do_unlink=True)
        if ref_mesh.name in bpy.data.meshes and ref_mesh.users == 0:
            bpy.data.meshes.remove(ref_mesh)
        if eval_mesh.name in bpy.data.meshes and eval_mesh.users == 0:
            bpy.data.meshes.remove(eval_mesh)


def execute(op, context):
    validate(op, OP)
    if op.get('adapter') == 'QUADRIFLOW_SHRINKWRAP_V1':
        return _execute_shrinkwrap(op, context)
    from modeling import editable, operator_context, finished, topology
    item = editable(op, copy_data=False)
    if topology(item.data) != op['topology_sha256']:
        raise Failure('CONFLICT', 'Stale retopology topology hash')
    if any(not math.isfinite(x) for row in item.matrix_world for x in row) or abs(item.matrix_world.determinant()) < 1e-12:
        fail('Retopology requires a finite nonsingular world transform')
    inspect_attributes(item)
    before = topology_evidence(item, source=True)
    original = geometry(item)
    matrices = ([list(r) for r in item.matrix_world], [list(r) for r in item.matrix_basis])
    materials = list(item.data.materials)
    smooth = item.data.polygons[0].use_smooth
    # Copy only after all read-only preflight checks. shared is excluded by OP.
    item = editable(op)
    with operator_context(item, context):
        finished(bpy.ops.object.quadriflow_remesh(mode='FACES', target_faces=op['target_faces'], seed=op['seed'],
                   use_mesh_symmetry=False, use_preserve_sharp=False, use_preserve_boundary=False,
                   preserve_attributes=False, smooth_normals=smooth))
    item.data.materials.clear()
    for material in materials:
        if material is None:
            raise Failure('VALIDATION_FAILED', 'Empty material slot is not transferable')
        item.data.materials.append(material)
    for face in item.data.polygons:
        face.material_index = 0
        face.use_smooth = smooth
    item.data.update()
    if matrices != ([list(r) for r in item.matrix_world], [list(r) for r in item.matrix_basis]):
        raise Failure('VALIDATION_FAILED', 'Retopology changed object transforms')
    after = topology_evidence(item)
    if after['euler_characteristic'] != before['euler_characteristic'] or after['quad_faces'] != after['faces']:
        raise Failure('VALIDATION_FAILED', 'Retopology changed Euler topology or produced non-quads')
    count_error = abs(after['faces']-op['target_faces'])/op['target_faces']
    if count_error > op['face_count_tolerance']:
        raise Failure('VALIDATION_FAILED', 'Actual face count exceeds declared relative tolerance')
    candidate = geometry(item)
    forward, reverse = distance(original, candidate), distance(candidate, original)
    if max(forward['maximum_distance'], reverse['maximum_distance']) > op['max_surface_distance']:
        raise Failure('VALIDATION_FAILED', 'Retopology exceeds declared sampled world-space surface-distance budget')
    if max(forward['maximum_normal_angle_degrees'], reverse['maximum_normal_angle_degrees']) > op['max_normal_angle_degrees']:
        raise Failure('VALIDATION_FAILED', 'Retopology exceeds declared sampled triangle-normal angle budget')
    return {'op': op['op'], 'object': item.name, 'adapter': op['adapter'], 'before': before, 'after': after,
            'target_faces': op['target_faces'], 'relative_face_count_error': count_error,
            'topology_sha256': topology(item.data), 'seed': op['seed'],
            'source_to_candidate': forward, 'candidate_to_source': reverse,
            'material_names': [m.name for m in item.data.materials], 'smooth': smooth,
            'sampling': 'World-space loop triangles: vertices, edge midpoints and centroid; finite samples, not a Hausdorff bound.',
            'attribute_transfer': 'None: UV, weights, shape keys, colors and nontrivial attributes are rejected.',
            'limitations': 'Sampled distances and topology checks do not certify absence of self-intersections or globally optimal topology.',
            'boundary_policy': 'REJECT_OPEN', 'sharp_policy': 'REJECT', 'attribute_policy': 'REJECT_NONTRIVIAL'}
