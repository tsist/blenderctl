# SPDX-License-Identifier: GPL-3.0-or-later
"""Strict finite static remeshing contract shared by runtime and schema export."""
from scene_contract import obj, enum, number, integer, NAME,BOOL,array

OP = obj({
    'op': {'const': 'mesh.retopology'}, 'object': NAME,
    'data_scope': enum('reject_shared', 'single_user'),
    'topology_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
    'adapter': {'const': 'QUADRIFLOW_CLOSED_V1'},
    'target_faces': integer(16, 10000), 'seed': integer(0, 2147483647),
    'face_count_tolerance': number(0, 1),
    'max_surface_distance': number(0, 1000000),
    'max_normal_angle_degrees': number(0, 180),
    'boundary_policy': {'const': 'REJECT_OPEN'},
    'sharp_policy': {'const': 'REJECT'},
    'attribute_policy': {'const': 'REJECT_NONTRIVIAL'},
})
METRICS=obj({'samples':integer(1,10000000),'maximum_distance':number(0,1e12),'rms_distance':number(0,1e12),
    'maximum_normal_angle_degrees':number(0,180),'rms_normal_angle_degrees':number(0,180)})
TOPOLOGY=obj({'vertices':integer(1,30000),'edges':integer(1,120000),'faces':integer(1,30000),
    'euler_characteristic':integer(-100000,2),'components':{'const':1},'boundary_edges':{'const':0},
    'nonmanifold_edges':{'const':0},'maximum_dihedral_degrees':number(0,180),'quad_faces':integer(0,30000)})
REPORT=obj({'operation_index':integer(0,499),'op':{'const':'mesh.retopology'},'object':NAME,
    'adapter':{'const':'QUADRIFLOW_CLOSED_V1'},'before':TOPOLOGY,'after':TOPOLOGY,
    'target_faces':integer(16,10000),'relative_face_count_error':number(0,1),
    'topology_sha256':OP['properties']['topology_sha256'],'seed':OP['properties']['seed'],
    'source_to_candidate':METRICS,'candidate_to_source':METRICS,
    'material_names':array(NAME,0,1),'smooth':BOOL,'sampling':{'type':'string'},'attribute_transfer':{'type':'string'},'limitations':{'type':'string'},
    'boundary_policy':{'const':'REJECT_OPEN'},'sharp_policy':{'const':'REJECT'},'attribute_policy':{'const':'REJECT_NONTRIVIAL'}})
