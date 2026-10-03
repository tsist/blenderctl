# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded static closed mesh QuadriFlow adapter; no attribute transfer."""
import math
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


def execute(op, context):
    validate(op, OP)
    if bpy.app.version != (5, 2, 1):
        fail('QUADRIFLOW_CLOSED_V1 is verified only on Blender 5.2.1')
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
