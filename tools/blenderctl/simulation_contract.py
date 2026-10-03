# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded physics authoring and exact-input cache receipts."""
from pathlib import Path
from protocol import Failure,read_json
from scene_contract import obj,enum,array,number,integer,NAME,BOOL,VEC,CONTEXT,validate
from model_contract import MANIFEST as MODEL_MANIFEST,normalize as model_normalize,OPS as MODEL_OPS,SCENE_OPS,SHA
OPS={}
def operation(name,props,required=None):OPS[name]=obj({'op':{'const':name},**props},['op',*(props if required is None else required)])
operation('physics.rigid',{'object':NAME,'type':enum('ACTIVE','PASSIVE'),'mass':number(.001,10000),'shape':enum('BOX','SPHERE','CONVEX_HULL'),'friction':number(0,1),'restitution':number(0,1)})
operation('physics.cloth',{'object':NAME,'name':NAME,'quality':integer(1,20),'mass':number(.001,10),'air_damping':number(0,10)})
operation('physics.soft',{'object':NAME,'name':NAME,'mass':number(.001,10),'friction':number(0,50),'use_edges':BOOL})
operation('physics.collision',{'object':NAME,'name':NAME,'thickness':number(.001,.1)})
operation('physics.world',{'gravity':array(number(-100,100),3,3),'substeps':integer(1,100),'iterations':integer(1,100)})
operation('physics.fluid_domain',{'object':NAME,'name':NAME,'resolution':integer(16,32)})
operation('physics.fluid_flow',{'object':NAME,'name':NAME,'density':number(.01,5)})
operation('physics.liquid_domain',{'object':NAME,'name':NAME,'resolution':integer(16,32)})
operation('physics.liquid_flow',{'object':NAME,'name':NAME})
operation('physics.remove',{'object':NAME,'modifier':NAME})
operation('special.grease_pencil',{'name':NAME,'collection':NAME,'layer':NAME,'frame':integer(1,10000),'strokes':array(array(VEC,2,1000),1,100),'radius':number(.001,1)})
operation('special.hair',{'name':NAME,'collection':NAME,'curves':array(array(VEC,2,1000),1,100),'radius':number(.001,1)})
operation('special.points',{'name':NAME,'collection':NAME,'positions':array(VEC,1,10000),'radius':number(.001,1)})
from hair_contract import OPS as HAIR_OPS,SAMPLES as HAIR_SAMPLES
from hair_dynamics_contract import OPS as HAIR_DYNAMICS_OPS
from hair_shape_contract import OPS as HAIR_SHAPE_OPS
from hair_volume_contract import OPS as HAIR_VOLUME_OPS
HAIR_NAMES=(*HAIR_OPS,*HAIR_DYNAMICS_OPS,*HAIR_SHAPE_OPS,*HAIR_VOLUME_OPS)
OPS.update(HAIR_OPS);OPS.update(HAIR_DYNAMICS_OPS);OPS.update(HAIR_SHAPE_OPS);OPS.update(HAIR_VOLUME_OPS)
MANIFEST=obj({'initial_scene':NAME,'context':CONTEXT,'operations':array({'oneOf':[*MODEL_MANIFEST['properties']['operations']['items']['oneOf'],*OPS.values()]},1,200)},['context','operations'])
HAIR_ONLY={'properties':{'operations':{'items':{'properties':{'op':enum(*HAIR_NAMES)}}}}}
HAIR_PRESENT={'required':['operations'],'properties':{'operations':{'contains':{'required':['op'],'properties':{'op':enum(*HAIR_NAMES)}}}}}
MANIFEST['allOf']=[{'if':HAIR_PRESENT,'then':HAIR_ONLY}]
FILE=obj({'file':{'type':'string','minLength':1},'expected_sha256':SHA})
def report_array(item):return {'type':'array','items':item}
SAMPLES=report_array(obj({'frame':integer(1,10000),'objects':report_array(obj({'name':NAME,'vertices':report_array(VEC)})),'density':report_array(obj({'name':NAME,'count':integer(1,1000000),'sum':number(0,1e12),'max':number(0,1e9)}))}))
SAMPLES['items']['properties']['liquid']=report_array(obj({'name':NAME,'vertices':array(VEC,0,100000),'faces':integer(0,200000),'topology_sha256':SHA}))
RECEIPT=obj({'simulation_cache_version':{'const':'1.0'},'candidate':{'type':'string','minLength':1},'candidate_sha256':SHA,'blender_build':{'type':'string','minLength':1},'request':obj({'scene':NAME,'view_layer':NAME,'frame_start':integer(1,10000),'frame_end':integer(2,10000)}),'files':array(obj({**FILE['properties'],'bytes':integer(1,134217728)}),0,1000),'samples':SAMPLES,'reopen':{'const':'pass'},'seconds':number(0,1e9),'resource_policy':obj({'threads':{'const':2},'input_vertices':integer(0,20000),'max_frames':{'const':120},'gas_max_resolution':{'const':32},'gas_max_frames':{'const':24},'cache_bytes':integer(0,134217728),'peak_working_set_bytes':integer(1,2**60),'memory_hard_limit':{'const':False},'disk_limit':{'type':'string'}}),'scope':{'type':'string'}})
from copy import deepcopy
from driver_contract import PROFILE
RIG_CLOTH_RECEIPT=deepcopy(RECEIPT)
RIG_CLOTH_RECEIPT['properties']['simulation_cache_version']={'const':'1.1'}
RIG_CLOTH_RECEIPT['properties']['request']['properties']['adapter']={'const':'RIG_CLOTH_V1'}
RIG_CLOTH_RECEIPT['properties']['request']['required'].append('adapter')
RIG_CLOTH_RECEIPT['properties']['driver_profile']=PROFILE
RIG_CLOTH_RECEIPT['required'].append('driver_profile')
RIG_CLOTH_RECEIPT['properties']['resource_policy']['properties']['input_vertices']=integer(0,100000)
RIG_CLOTH_RECEIPT['properties']['resource_policy']['properties']['max_frames']={'const':32}
HAIR_RECEIPT=deepcopy(RIG_CLOTH_RECEIPT)
HAIR_RECEIPT['properties']['simulation_cache_version']={'const':'1.3'}
HAIR_RECEIPT['properties']['request']['properties']['adapter']={'const':'HAIR_CLOTH_V1'}
HAIR_SOURCE_CLOTH={**array(NAME,1,8),'uniqueItems':True}
HAIR_RECEIPT['properties']['request']['properties']['source_cloth']=HAIR_SOURCE_CLOTH
HAIR_RECEIPT['required'].remove('driver_profile')
HAIR_RECEIPT['properties']['hair_samples']=HAIR_SAMPLES;HAIR_RECEIPT['required'].append('hair_samples')
LIQUID_RECEIPT=deepcopy(RECEIPT)
LIQUID_RECEIPT['properties']['simulation_cache_version']={'const':'1.4'}
LIQUID_RECEIPT['properties']['request']['properties']['adapter']={'const':'NATIVE_LIQUID_V1'}
LIQUID_RECEIPT['properties']['request']['required'].append('adapter')
LIQUID_RECEIPT['properties']['samples']['items']['required'].append('liquid')
LIQUID_RECEIPT['properties']['resource_policy']['properties'].update(max_frames={'const':24},liquid_max_resolution={'const':32},liquid_max_frames={'const':24})
RECEIPT={'oneOf':[RECEIPT,RIG_CLOTH_RECEIPT,HAIR_RECEIPT,LIQUID_RECEIPT]}
BAKE=obj({'scene':NAME,'view_layer':NAME,'frame_start':integer(1,10000),'frame_end':integer(2,10000),'mode':enum('bake','reuse'),'receipt':FILE,'adapter':enum('RIG_CLOTH_V1','HAIR_CLOTH_V1','NATIVE_LIQUID_V1'),'source_cloth':HAIR_SOURCE_CLOTH},['scene','view_layer','frame_start','frame_end','mode'])
BAKE['allOf']=[{'if':{'required':['source_cloth']},'then':{'required':['adapter'],'properties':{'adapter':{'const':'HAIR_CLOTH_V1'}}}}]
from zone_cache_contract import BAKE as ZONE_BAKE,RECEIPT as ZONE_RECEIPT,normalize as zone_normalize
BAKE={'oneOf':[BAKE,ZONE_BAKE]}
RECEIPT['oneOf'].append(ZONE_RECEIPT)
def normalize(manifest):
    validate(manifest,MANIFEST)
    if any(o['op'].startswith('hair.') for o in manifest['operations']) and any(o['op'] not in HAIR_NAMES for o in manifest['operations']):raise Failure('INVALID_REQUEST','Hair manifests may contain only explicit hair adapters')
    from hair_shape_contract import normalize as shape_normalize
    from hair_volume_contract import normalize as volume_normalize
    for op in manifest['operations']:
        if op['op']=='hair.shape':shape_normalize(op)
        if op['op'] in HAIR_VOLUME_OPS:volume_normalize(op)
    created=sum(len(o.get('positions',[])) if o['op']=='special.points' else sum(map(len,o.get('curves',o.get('strokes',[])))) if o['op'].startswith('special.') else len(o.get('vertices',[])) if o['op']=='mesh.create' else 0 for o in manifest['operations'])
    if created>20000:raise Failure('INVALID_REQUEST','Manifest exceeds 20000 explicit geometry points')
    prior=[o for o in manifest['operations'] if o['op'] in MODEL_OPS or o['op'] in SCENE_OPS]
    if prior:model_normalize({**manifest,'operations':prior})
    return manifest
def bake_contract(spec):
    validate(spec,BAKE)
    if 'source_cloth' in spec and spec.get('adapter')!='HAIR_CLOTH_V1':raise Failure('INVALID_REQUEST','source_cloth requires HAIR_CLOTH_V1')
    if spec.get('adapter')=='GEOMETRY_ZONE_V1':
        zone_normalize(spec)
        if 'receipt' in spec:
            p=Path(spec['receipt']['file'])
            if not p.is_absolute() or not p.is_file():raise Failure('NOT_FOUND','Receipt must be an existing absolute file')
        return spec
    if not 1<=spec['frame_end']-spec['frame_start']<=119:raise Failure('INVALID_REQUEST','Cache range must be 2..120 consecutive frames')
    if spec.get('adapter') and spec['frame_end']-spec['frame_start']>31:raise Failure('UNSUPPORTED','Rig/hair cloth is bounded to 32 consecutive frames')
    if spec.get('adapter')=='NATIVE_LIQUID_V1' and spec['frame_end']-spec['frame_start']>23:raise Failure('UNSUPPORTED','Native liquid is limited to 24 integer frames')
    if (spec['mode']=='reuse')!=('receipt' in spec):raise Failure('INVALID_REQUEST','Only reuse requires a hashed receipt')
    if 'receipt' in spec:
        p=Path(spec['receipt']['file'])
        if not p.is_absolute() or not p.is_file():raise Failure('NOT_FOUND','Receipt must be an existing absolute file')
    return spec
def input_documents(params):
    receipt=params.get('manifest',{}).get('receipt')
    if not receipt:return params.get('resources',[])
    # Only the explicitly hashed receipt is an input. External cache files are
    # validated and guarded separately by the supervisor after locking it.
    return [receipt,*params.get('resources',[])]
def receipt_contract(receipt):
    validate(receipt,RECEIPT)
    seen=set()
    for d in receipt['files']:
        p=Path(d['file'])
        if not p.is_absolute() or not p.is_file():raise Failure('NOT_FOUND','Cache receipt dependency missing')
        key=str(p.resolve()).casefold()
        if key in seen:raise Failure('INVALID_REQUEST','Duplicate cache receipt dependency')
        seen.add(key)
