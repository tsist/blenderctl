# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact-input Geometry Nodes disk caches; relocation produces a new receipt."""
from scene_contract import obj,enum,array,integer,number,NAME,BOOL,validate
from node_contract import FILE,SHA
from protocol import Failure
ATTR=obj({'name':NAME,'type':enum('FLOAT','FLOAT_VECTOR')})
TARGET=obj({'object':NAME,'modifier':NAME,'attributes':array(ATTR,0,8)},['object','modifier'])
REQUEST=obj({'adapter':{'const':'GEOMETRY_ZONE_V1'},'scene':NAME,'view_layer':NAME,'frame_start':integer(1,10000),'frame_end':integer(2,10000),'targets':array(TARGET,1,8)})
BAKE=obj({**REQUEST['properties'],'mode':enum('bake','reuse','relocate'),'receipt':FILE},[*REQUEST['required'],'mode'])
def rows(item):return {'type':'array','items':item}
VEC=array(number(),3,3)
SAMPLES=rows(obj({'frame':integer(1,10000),'objects':rows(obj({'name':NAME,'vertices':rows(VEC),'topology_sha256':SHA,'attributes':rows(obj({'name':NAME,'type':enum('FLOAT','FLOAT_VECTOR'),'domain':{'const':'POINT'},'data':rows({'oneOf':[number(),VEC]})}))}))}))
RECEIPT=obj({'simulation_cache_version':{'const':'1.2'},'adapter':{'const':'GEOMETRY_ZONE_V1'},'source':FILE,'candidate':{'type':'string','minLength':1},'candidate_sha256':SHA,'blender_build':{'type':'string'},'implementation_sha256':SHA,'request':REQUEST,'resources':array(FILE,0,100),'bindings':rows({'type':'object'}),'files':array(obj({**FILE['properties'],'bytes':integer(1,134217728)}),1,1000),'samples':SAMPLES,'states':rows({'type':'object'}),'reopen':{'const':'pass'},'seconds':number(0,1e9),'resource_policy':obj({'input_vertices':integer(1,20000),'max_frames':{'const':32},'max_bakes':{'const':8},'cache_bytes':integer(1,134217728),'sampling_tolerance':{'type':'string'}}),'scope':{'type':'string'}})
def normalize(spec):
    validate(spec,BAKE)
    if not 1<=spec['frame_end']-spec['frame_start']<=31:raise Failure('INVALID_REQUEST','Geometry zone cache requires 2..32 consecutive frames')
    if (spec['mode']!='bake')!=('receipt' in spec):raise Failure('INVALID_REQUEST','Only reuse/relocate requires a hashed receipt')
    ids=[(t['object'],t['modifier']) for t in spec['targets']]
    if len(ids)!=len(set(ids)) or len({x[0] for x in ids})!=len(ids):raise Failure('INVALID_REQUEST','Each target object/modifier must occur once')
    for t in spec['targets']:
        names=[x['name'] for x in t.get('attributes',[])]
        if len(names)!=len(set(names)):raise Failure('INVALID_REQUEST','Duplicate observed attribute')
    return spec
