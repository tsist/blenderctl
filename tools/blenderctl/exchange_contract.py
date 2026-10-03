# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded format adapters and explicitly trusted local Python contracts."""
from pathlib import Path
from protocol import Failure,DEFAULT_BLENDER
from scene_contract import obj,array,enum,number,integer,NAME,BOOL,validate
from node_contract import FILE
FORMATS=('GLB','OBJ','STL','PLY','FBX','USD','ABC')
SUFFIX={'GLB':'.glb','OBJ':'.obj','STL':'.stl','PLY':'.ply','FBX':'.fbx','USD':'.usdc','ABC':'.abc'}
IMPORT=obj({'format':enum(*FORMATS),'scene':NAME,'frame':integer(1,100000),'fps':integer(1,240),'scale_to_meters':number(.000001,1000000),'mode':enum('STATIC','ANIMATION')})
IMPORT['properties']['geometry_adapter']=enum('EVALUATED_MESH_CACHE_V1','FBX_NATIVE_CLOCK_V1','GLB_NATIVE_CLOCK_V1')
IMPORT['allOf']=[{'if':{'required':['geometry_adapter']},'then':{'properties':{'mode':{'const':'ANIMATION'},'scale_to_meters':{'const':1}}}},
 {'if':{'required':['geometry_adapter'],'properties':{'geometry_adapter':{'const':'EVALUATED_MESH_CACHE_V1'}}},'then':{'properties':{'format':{'const':'USD'}}}},
 {'if':{'required':['geometry_adapter'],'properties':{'geometry_adapter':{'const':'FBX_NATIVE_CLOCK_V1'}}},'then':{'properties':{'format':{'const':'FBX'}}}}]
IMPORT['allOf'].append({'if':{'required':['geometry_adapter'],'properties':{'geometry_adapter':{'const':'GLB_NATIVE_CLOCK_V1'}}},'then':{'properties':{'format':{'const':'GLB'}}}})
CACHE_SAMPLING=obj({'seed_substeps':{'type':'integer',**enum(1,2,4,8)},'max_depth':integer(0,24),'max_evaluations':integer(5,4096),'max_vertex_samples':integer(1,256000000),'max_position_error':number(1e-7,.001)})
ANALYZE=obj({'scene':NAME,'view_layer':NAME,'objects':{**array(NAME,1,16),'uniqueItems':True},
 'frame_start':number(1,100000),'frame_end':number(1,100000),
 'sampling':obj({'seed_substeps':{'type':'integer',**enum(1,2,4,8)},'max_depth':integer(0,24),
 'max_evaluations':integer(5,4096),'max_vertex_samples':integer(1,16000000),'max_position_error':number(1e-7,.001)})})
EXPORT=obj({'format':enum(*FORMATS),'scene':NAME,'view_layer':NAME,'objects':{**array(NAME,1,100),'uniqueItems':True},'mode':enum('STATIC','ANIMATION'),'frame_start':integer(1,100000),'frame_end':integer(1,100000),'meters_per_unit':number(.000001,1000000),'materials':enum('NONE','GLTF_PBR')})
EXPORT['properties']['rig_adapter']=enum('BBONE_CAGE_V1','BBONE_MULTI_CAGE_V1')
EXPORT['properties']['geometry_adapter']=enum('EVALUATED_MESH_CLIP_V1','EVALUATED_MESH_CACHE_V1')
CLIP_SAMPLING=obj({'substeps':{'type':'integer',**enum(1,2,4,8)},'max_position_error':number(1e-7,.001),'max_vertex_samples':integer(1,16000000)})
EXPORT['properties']['sampling']={'oneOf':[CLIP_SAMPLING,CACHE_SAMPLING]}
EXPORT['allOf']=[{'if':{'required':['rig_adapter']},'then':{'properties':{'format':enum('GLB','FBX'),'mode':{'const':'ANIMATION'},'materials':{'const':'NONE'},'meters_per_unit':{'const':1},'objects':{'minItems':2,'maxItems':17}}}},
 {'if':{'required':['rig_adapter'],'properties':{'rig_adapter':{'const':'BBONE_CAGE_V1'}}},'then':{'properties':{'format':{'const':'GLB'},'objects':{'maxItems':2}}}}]
EXPORT['allOf'].extend([
 {'if':{'required':['geometry_adapter']},'then':{'required':['sampling'],'not':{'required':['rig_adapter']},'properties':{'format':enum('GLB','FBX','USD'),'mode':{'const':'ANIMATION'},'materials':{'const':'NONE'},'meters_per_unit':{'const':1},'objects':{'maxItems':16}}}},
 {'if':{'required':['geometry_adapter'],'properties':{'geometry_adapter':{'const':'EVALUATED_MESH_CACHE_V1'}}},'then':{'properties':{'format':{'const':'USD'},'sampling':CACHE_SAMPLING}}},
 {'if':{'required':['geometry_adapter'],'properties':{'geometry_adapter':{'const':'EVALUATED_MESH_CLIP_V1'}}},'then':{'properties':{'format':enum('GLB','FBX'),'sampling':CLIP_SAMPLING}}},
 {'if':{'required':['sampling']},'then':{'required':['geometry_adapter']}}])
for contract in (IMPORT,EXPORT):
    contract['allOf'].append({'if':{'required':['format','mode'],'properties':{'format':{'const':'USD'},'mode':{'const':'ANIMATION'}}},'then':{'required':['geometry_adapter'],'properties':{'geometry_adapter':{'const':'EVALUATED_MESH_CACHE_V1'}}}})
CONVERT_ITEM=obj({'input':FILE,'import':IMPORT,'output_format':enum(*FORMATS)})
CONVERT_ITEM['properties']['geometry_adapter']=enum('FBX_NATIVE_CLOCK_V1','GLB_NATIVE_CLOCK_V1')
CONVERT_ITEM['allOf']=[{'if':{'required':['geometry_adapter']},'then':{'properties':{'import':{'required':['geometry_adapter'],'properties':{'format':{'const':'USD'},'mode':{'const':'ANIMATION'},'geometry_adapter':{'const':'EVALUATED_MESH_CACHE_V1'}}}}},'else':{'properties':{'import':{'properties':{'mode':{'const':'STATIC'}}}}}}]
for kind in ('FBX','GLB'):
    CONVERT_ITEM['allOf'].append({'if':{'required':['geometry_adapter'],'properties':{'geometry_adapter':{'const':kind+'_NATIVE_CLOCK_V1'}}},'then':{'properties':{'output_format':{'const':kind}}}})
CONVERT=obj({'items':array(CONVERT_ITEM,1,8)})
SCRIPT=obj({'script':FILE,'trust':{'const':'execute_local_python_without_sandbox'},'params':{'type':'object'},'outputs':{**array({'type':'string','minLength':1,'maxLength':128,'pattern':r'^output-[A-Za-z0-9_-]+\.(json|txt|blend|png|glb)$'},1,16),'uniqueItems':True}})
SCRIPT['properties']['trust']=enum('execute_local_python_without_sandbox','execute_local_python_in_appcontainer')
SCRIPT['properties']['isolation']={'const':'WINDOWS_APPCONTAINER_V1'}
SCRIPT['allOf']=[{'if':{'required':['isolation']},'then':{'properties':{'trust':{'const':'execute_local_python_in_appcontainer'}}},'else':{'properties':{'trust':{'const':'execute_local_python_without_sandbox'}}}}]
STAR=obj({'adapter':{'const':'extra_mesh_star_v1'},'name':NAME,'points':integer(3,32),'outer_radius':number(.01,100),'inner_radius':number(.01,100),'height':number(.01,100)})
PINNED={'interface.py':'44710b69bb90ab4491f5c10bd2563e5e6e7f3de15db2ebf104b7e73b23b9d1aa','add_mesh_star.py':'bdaa124caf1f90e302e85a2f7abb24e21f3b46baf132b3f100948642dc9633d4'}
def addon_documents(blender=DEFAULT_BLENDER):
    base=Path(blender).parent/'portable/extensions/blender_org/extra_mesh_objects'
    return [{'file':str(base/name),'expected_sha256':sha} for name,sha in PINNED.items()]
def descriptor(d,max_bytes=256*1024*1024):
    validate(d,FILE);p=Path(d['file'])
    if not p.is_absolute():raise Failure('INVALID_REQUEST','Exchange inputs require absolute paths')
    if not p.is_file():raise Failure('NOT_FOUND','Input missing: '+str(p))
    if p.stat().st_size>max_bytes:raise Failure('UNSUPPORTED','Exchange input exceeds declared byte limit: '+str(max_bytes))
    return d
def normalize(v,kind):
    validate(v,{'analyze':ANALYZE,'export':EXPORT,'import':IMPORT,'convert':CONVERT,'run':SCRIPT,'prepare':STAR}[kind])
    if kind=='analyze' and not 0<v['frame_end']-v['frame_start']<=59:raise Failure('INVALID_REQUEST','Analysis needs a positive range of at most 59 frames')
    if kind=='prepare' and v['inner_radius']>=v['outer_radius']:raise Failure('INVALID_REQUEST','Star requires inner_radius < outer_radius')
    if kind=='export':
        if v.get('geometry_adapter'):
            cache=v['geometry_adapter']=='EVALUATED_MESH_CACHE_V1'
            if 'rig_adapter' in v or 'sampling' not in v:raise Failure('INVALID_REQUEST','Mesh clip requires sampling and excludes rig_adapter')
            validate(v['sampling'],CACHE_SAMPLING if cache else CLIP_SAMPLING)
            if v['format'] not in (('USD',) if cache else ('GLB','FBX')) or v['mode']!='ANIMATION' or v['materials']!='NONE' or v['meters_per_unit']!=1 or len(v['objects'])>16:raise Failure('UNSUPPORTED','Evaluated geometry adapter format/context mismatch')
            if cache and v['frame_end']<=v['frame_start']:raise Failure('INVALID_REQUEST','Native mesh cache needs a positive frame interval')
            if not cache and (v['frame_end']-v['frame_start'])*v['sampling']['substeps']+1>65:raise Failure('UNSUPPORTED','Mesh clip exceeds 65 sampled keys')
        elif 'sampling' in v:raise Failure('INVALID_REQUEST','Sampling requires explicit geometry_adapter')
        if v.get('rig_adapter'):
            multi=v['rig_adapter']=='BBONE_MULTI_CAGE_V1'
            if v['format'] not in (('GLB','FBX') if multi else ('GLB',)) or v['mode']!='ANIMATION' or v['materials']!='NONE' or v['meters_per_unit']!=1 or not 2<=len(v['objects'])<=(17 if multi else 2) or v['frame_end']-v['frame_start']>31:
                raise Failure('UNSUPPORTED','Cage adapter requires bounded mesh/rig selection, GLB (multi also FBX), ANIMATION, NONE materials, meter scale one and at most 32 frames')
        if v['frame_end']<v['frame_start'] or v['frame_end']-v['frame_start']>59:raise Failure('INVALID_REQUEST','Export range must span 1..60 frames')
        if v['mode']=='STATIC' and v['frame_end']!=v['frame_start']:raise Failure('INVALID_REQUEST','Static export has one frame')
        if v['materials']=='GLTF_PBR' and v['format']!='GLB':raise Failure('UNSUPPORTED','PBR materials require GLB')
    if kind in ('export','import') and v['mode']=='ANIMATION':
        if v['format'] not in ('GLB','FBX') and not (v['format']=='USD' and v.get('geometry_adapter')=='EVALUATED_MESH_CACHE_V1'):raise Failure('UNSUPPORTED','USD animation requires explicit native mesh cache adapter')
        if v.get('meters_per_unit',v.get('scale_to_meters'))!=1:raise Failure('UNSUPPORTED','Animated exchanges require meter units at scale one')
    if kind=='import' and v.get('geometry_adapter'):
        expected={'FBX_NATIVE_CLOCK_V1':'FBX','GLB_NATIVE_CLOCK_V1':'GLB','EVALUATED_MESH_CACHE_V1':'USD'}[v['geometry_adapter']]
        if v['format']!=expected or v['mode']!='ANIMATION' or v['scale_to_meters']!=1:raise Failure('INVALID_REQUEST','Explicit geometry import requires matching format, animation and meter scale one')
    if kind=='convert':
        for x in v['items']:
            clock=x.get('geometry_adapter') in ('FBX_NATIVE_CLOCK_V1','GLB_NATIVE_CLOCK_V1')
            descriptor(x['input'],1024**3 if clock else 256*1024**2);normalize(x['import'],'import')
            if clock:
                if x['import'].get('geometry_adapter')!='EVALUATED_MESH_CACHE_V1' or x['output_format']!=x['geometry_adapter'].split('_')[0] or x['import']['mode']!='ANIMATION' or x['import']['format']!='USD':raise Failure('UNSUPPORTED','Native clock converts explicit USD native caches only')
                if Path(x['input']['file']).suffix.lower() not in ('.usd','.usda','.usdc'):raise Failure('INVALID_REQUEST','Native cache conversion requires a USD file')
            elif x['import']['mode']!='STATIC':raise Failure('UNSUPPORTED','Batch animation conversion requires an explicit adapter')
    if kind=='run':descriptor(v['script'])
    if kind=='run':
        expected='execute_local_python_in_appcontainer' if 'isolation' in v else 'execute_local_python_without_sandbox'
        if v['trust']!=expected:raise Failure('INVALID_REQUEST','Trust acknowledgment must match the explicit isolation profile')
    if kind=='run' and any(not x.startswith('output-') for x in v['outputs']):raise Failure('INVALID_REQUEST','Trusted output names must start with output- to avoid job metadata collisions')
    return v
def input_documents(params):
    docs=list(params.get('resources',[]));m=params.get('manifest',{})
    for x in m.get('items',[]):
        descriptor(x['input'],1024**3 if x.get('geometry_adapter') in ('FBX_NATIVE_CLOCK_V1','GLB_NATIVE_CLOCK_V1') else 256*1024**2)
    if 'script' in m:docs.append(m['script'])
    if 'simulation_receipt' in params:docs.append(params['simulation_receipt'])
    for d in docs:descriptor(d)
    return docs+[x['input'] for x in m.get('items',[])]
