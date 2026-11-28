"""Bounded static & dirty-mesh QuadriFlow remeshing contracts shared by runtime and schema export."""
from scene_contract import obj, enum, number, integer, NAME, BOOL, array

SHA = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}

OP_CLOSED = obj({
    'op': {'const': 'mesh.retopology'},
    'object': NAME,
    'data_scope': enum('reject_shared', 'single_user'),
    'topology_sha256': SHA,
    'adapter': {'const': 'QUADRIFLOW_CLOSED_V1'},
    'target_faces': integer(16, 10000),
    'seed': integer(0, 2147483647),
    'face_count_tolerance': number(0, 1),
    'max_surface_distance': number(0, 1000000),
    'max_normal_angle_degrees': number(0, 180),
    'boundary_policy': {'const': 'REJECT_OPEN'},
    'sharp_policy': {'const': 'REJECT'},
    'attribute_policy': {'const': 'REJECT_NONTRIVIAL'},
})

OP_SHRINKWRAP = obj({
    'op': {'const': 'mesh.retopology'},
    'object': NAME,
    'data_scope': enum('reject_shared', 'single_user'),
    'topology_sha256': SHA,
    'adapter': {'const': 'QUADRIFLOW_SHRINKWRAP_V1'},
    'target_faces': integer(16, 50000),
    'seed': integer(0, 2147483647),
    'face_count_tolerance': number(0, 1),
    'max_surface_distance': number(0, 1000000),
    'max_normal_angle_degrees': number(0, 180),
    'apply_modifiers': BOOL,
    'voxel_remesh': BOOL,
    'voxel_size': number(1e-4, 100.0),
    'use_preserve_sharp': BOOL,
    'use_preserve_boundary': BOOL,
    'use_mesh_symmetry': BOOL,
    'shrinkwrap': BOOL,
    'shrinkwrap_method': enum('NEAREST_SURFACEPOINT', 'PROJECT', 'NEAREST_VERTEX', 'TARGET_PROJECT'),
    'attribute_policy': {'const': 'DROP_WITH_WARNING'},
}, required=[
    'op', 'object', 'data_scope', 'topology_sha256', 'adapter',
    'target_faces', 'face_count_tolerance', 'max_surface_distance', 'max_normal_angle_degrees',
])

OP = {'oneOf': [OP_CLOSED, OP_SHRINKWRAP]}

METRICS = obj({
    'samples': integer(1, 10000000),
    'maximum_distance': number(0, 1e12),
    'rms_distance': number(0, 1e12),
    'maximum_normal_angle_degrees': number(0, 180),
    'rms_normal_angle_degrees': number(0, 180),
})

TOPOLOGY = obj({
    'vertices': integer(1, 30000),
    'edges': integer(1, 120000),
    'faces': integer(1, 30000),
    'euler_characteristic': integer(-100000, 2),
    'components': {'const': 1},
    'boundary_edges': {'const': 0},
    'nonmanifold_edges': {'const': 0},
    'maximum_dihedral_degrees': number(0, 180),
    'quad_faces': integer(0, 30000),
})

DIRTY_TOPOLOGY = obj({
    'vertices': integer(1, 500000),
    'edges': integer(1, 2000000),
    'faces': integer(1, 500000),
    'euler_characteristic': integer(-2000000, 200000),
    'components': integer(1, 50000),
    'boundary_edges': integer(0, 2000000),
    'nonmanifold_edges': integer(0, 2000000),
    'degenerate_faces': integer(0, 500000),
    'maximum_dihedral_degrees': number(0, 180),
    'quad_faces': integer(0, 500000),
})

RESULT_TOPOLOGY = obj({
    'vertices': integer(1, 200000),
    'edges': integer(1, 1000000),
    'faces': integer(1, 200000),
    'euler_characteristic': integer(-1000000, 100000),
    'components': integer(1, 10000),
    'boundary_edges': integer(0, 200000),
    'nonmanifold_edges': {'const': 0},
    'degenerate_faces': {'const': 0},
    'maximum_dihedral_degrees': number(0, 180),
    'quad_faces': integer(0, 200000),
})

DROPPED_ATTRIBUTES = obj({
    'uv_layers': array(NAME, 0, 100),
    'vertex_groups': array(NAME, 0, 4096),
    'shape_keys': array(NAME, 0, 4096),
    'color_attributes': array(NAME, 0, 100),
    'custom_attributes': array(NAME, 0, 512),
    'applied_modifiers': array(NAME, 0, 256),
    'had_custom_normals': BOOL,
    'sharp_edges': integer(0, 2000000),
    'seam_edges': integer(0, 2000000),
})

REPORT_CLOSED = obj({
    'operation_index': integer(0, 499),
    'op': {'const': 'mesh.retopology'},
    'object': NAME,
    'adapter': {'const': 'QUADRIFLOW_CLOSED_V1'},
    'before': TOPOLOGY,
    'after': TOPOLOGY,
    'target_faces': integer(16, 10000),
    'relative_face_count_error': number(0, 1),
    'topology_sha256': SHA,
    'seed': OP_CLOSED['properties']['seed'],
    'source_to_candidate': METRICS,
    'candidate_to_source': METRICS,
    'material_names': array(NAME, 0, 1),
    'smooth': BOOL,
    'sampling': {'type': 'string'},
    'attribute_transfer': {'type': 'string'},
    'limitations': {'type': 'string'},
    'boundary_policy': {'const': 'REJECT_OPEN'},
    'sharp_policy': {'const': 'REJECT'},
    'attribute_policy': {'const': 'REJECT_NONTRIVIAL'},
})

REPORT_SHRINKWRAP = obj({
    'operation_index': integer(0, 499),
    'op': {'const': 'mesh.retopology'},
    'object': NAME,
    'adapter': {'const': 'QUADRIFLOW_SHRINKWRAP_V1'},
    'before': DIRTY_TOPOLOGY,
    'after': RESULT_TOPOLOGY,
    'target_faces': integer(16, 50000),
    'relative_face_count_error': number(0, 1),
    'topology_sha256': SHA,
    'seed': OP_CLOSED['properties']['seed'],
    'source_to_candidate': METRICS,
    'candidate_to_source': METRICS,
    'material_names': array({'oneOf': [NAME, {'type': 'null'}]}, 0, 64),
    'smooth': BOOL,
    'sampling': {'type': 'string'},
    'attribute_transfer': {'type': 'string'},
    'limitations': {'type': 'string'},
    'attribute_policy': {'const': 'DROP_WITH_WARNING'},
    'dropped_attributes': DROPPED_ATTRIBUTES,
    'voxel_remesh_used': BOOL,
    'voxel_fallback_used': BOOL,
    'shrinkwrap_applied': BOOL,
    'source_warnings': array({'type': 'string'}, 0, 64),
})
REPORT_SHRINKWRAP['properties']['material_transfer'] = {'oneOf': [{'type': 'null'}, obj({
    'method': {'const': 'WORLD_BVH_FACE_CENTER'},
    'source_triangle_counts': {'type':'object','additionalProperties':integer(1, 2000000)},
    'candidate_face_counts': {'type':'object','additionalProperties':integer(1, 200000)},
    'all_source_slots_retained': {'const': True},
})]}

REPORT = {'oneOf': [REPORT_CLOSED, REPORT_SHRINKWRAP]}
