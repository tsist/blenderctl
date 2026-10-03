# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit surface guides; root IDs survive deterministic density selection."""
from scene_contract import obj,array,number,integer,NAME,BOOL
from model_contract import SHA
V=array(number(-10000,10000),3,3)
OP=obj({'op':{'const':'hair.surface'},'adapter':{'const':'SURFACE_GUIDES_V1'},
    'name':NAME,'collection':NAME,'surface':NAME,'uv_map':NAME,'topology_sha256':SHA,'enable_rest_position':BOOL,
    'roots':array(obj({'id':integer(0,2147483647),'triangle':integer(0,1000000),'barycentric':array(number(0,1),3,3)}),1,2048),
    'points_per_curve':integer(2,64),'length':number(.00001,100),'radius':number(.000001,1),
    'seed':integer(0,2147483647),'density':number(.000001,1),'length_variation':number(0,.9),
    'direction':V,'bend':V,'controls':array(obj({'id':integer(0,2147483647),'points':array(V,2,64)}),0,2048)})
OPS={'hair.surface':OP}
FRAMES={**array(integer(1,10000),1,32),'uniqueItems':True}
COLLISION=obj({'object':NAME,'distance_mode':{'enum':['ORIENTED_SURFACE','UNSIGNED_SURFACE']},
    'vertices':integer(1,100000),'topology_sha256':SHA,'world_positions_sha256':SHA,
    'minimum_proxy_vertex_distance':number(),'minimum_sampled_curve_clearance':number(),
    'negative_curve_samples':integer(0,100000),'curve_samples':integer(1,100000)})
COLLISION['properties'].update(clearance_threshold=number(0,.1),clearance_pass=BOOL)
SAMPLES=array(obj({'frame':integer(1,10000),'objects':array(obj({'object':NAME,'surface':NAME,'coordinate_space':{'const':'WORLD'},'curves':array(obj({'id':integer(0,2147483647),'root':V,'points':array(V,2,64),'radii':array(number(0,10000),2,64)}),1,2048),'collisions':array(COLLISION,1,8)},['object','surface','coordinate_space','curves']),0,128)}),1,32)
REPORT=obj({'hair_report_version':{'const':'1.0'},'bindings':array({'type':'object'},0,128),'dynamics':array({'type':'object'},0,128),'samples':SAMPLES,'operations':array(obj({'index':integer(0,199),'op':NAME,'detail':{'type':'object'}}),0,200),'scope':{'type':'string'}},['hair_report_version','bindings','dynamics','samples','scope'])
VOLUME_SAMPLE=obj({'object':NAME,'file':{'type':'string','minLength':1},'file_sha256':SHA,'source_sha256':SHA,'active_voxels':integer(1,1000000),'index_min':array(integer(-100000000,100000000),3,3),'index_max':array(integer(-100000000,100000000),3,3),'sum':number(0,1e9),'maximum':number(0,100.00001),'values_sha256':SHA,'voxel_size':number(.0001,1)})
SAMPLES['items']['properties']['volumes']=array(VOLUME_SAMPLE,0,8)
REPORT['properties']['volumes']=array({'type':'object'},0,8)
REPORT['properties']['backend_status']={'type':'object'}
