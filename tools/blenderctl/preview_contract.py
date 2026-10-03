# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit asset preview recipes and input-bound reusable receipts."""
from pathlib import Path
import copy
from protocol import Failure
from scene_contract import obj,array,enum,number,integer,NAME,BOOL,validate
from node_contract import FILE,SHA
from render_contract import DEVICE,COLOR,file_input
from driver_contract import PROFILE
from pipeline_contract import POLICY,LIMITS,ID
KINDS=('Object','Collection','Material','GeometryNodeTree','ShaderNodeTree')
TARGET=obj({'type':enum(*KINDS),'name':NAME,'library':{'type':'null'}})
VIEWS=enum('FRONT','RIGHT','BACK','THREE_QUARTER')
MANIFEST=obj({'preview_version':{'const':'1.0'},'target':TARGET,'scene':NAME,'view_layer':NAME,'frame':integer(1,10000),'fixture':enum('NONE','CUBE','UV_SPHERE'),'appearance':enum('CLAY','SOURCE'),'visibility':enum('REQUIRE_VISIBLE','INCLUDE_HIDDEN'),'views':{**array(VIEWS,1,4),'uniqueItems':True},'projection':enum('ORTHO','PERSP'),'margin':number(.03,.35),'width':integer(64,1024),'height':integer(64,1024),'engine':enum('CYCLES','BLENDER_EEVEE','BLENDER_WORKBENCH'),'device':DEVICE,'samples':integer(1,128),'transparent':BOOL,'background':array(number(0,1),3,3),'color':COLOR,'embed':BOOL,'embed_view':VIEWS})
PARAMS=obj({'file':{'type':'string','minLength':1},'expected_sha256':SHA,'manifest':MANIFEST,'resources':array(FILE,0,100),'driver_profile':PROFILE,'simulation_receipt':FILE,'reuse_receipt':FILE},['file','expected_sha256','manifest'])
BATCH=obj({'preview_batch_version':{'const':'1.0'},'policy':POLICY,'items':array(obj({'id':ID,'params':PARAMS,'limits':LIMITS,'timeout_seconds':number(.05,86400),'reuse':BOOL}),1,32)})
OUTPUT=obj({'view':VIEWS,'file':{'type':'string'},'sha256':SHA,'bytes':integer(1,134217728)})
RECEIPT=obj({'preview_receipt_version':{'const':'1.0'},'recipe':{'type':'object'},'recipe_sha256':SHA,'implementation_sha256':SHA,'blender_build':{'type':'string'},'outputs':array(OUTPUT,1,4),'candidate':{'oneOf':[FILE,{'type':'null'}]},'embedded_preview':{'oneOf':[{'type':'object'},{'type':'null'}]},'framing':array({'type':'object'},1,4),'source_saved':{'const':False}})
def normalize(v):
    validate(v,MANIFEST)
    kind=v['target']['type']
    if (kind in ('Object','Collection'))!=(v['fixture']=='NONE'):raise Failure('INVALID_REQUEST','Objects/collections require NONE fixture; materials/node groups require CUBE or UV_SPHERE')
    if kind in ('Material','ShaderNodeTree') and v['appearance']!='SOURCE':raise Failure('INVALID_REQUEST','Material/shader previews require SOURCE appearance')
    if v['embed_view'] not in v['views']:raise Failure('INVALID_REQUEST','embed_view must be in views')
    if v['engine']=='BLENDER_WORKBENCH' and (v['samples']!=8 or v['appearance']!='CLAY'):raise Failure('UNSUPPORTED','Workbench preview requires CLAY and eight samples')
    from render_contract import normalize_run
    normalize_run(render_spec(v,v['scene'],v['view_layer'],'PreviewCamera'))
    return v
def render_spec(v,scene,layer,camera):
    return {'scene':scene,'view_layer':layer,'camera':camera,'engine':v['engine'],'device':v['device'],'frames':[v['frame']],'width':v['width'],'height':v['height'],'samples':v['samples'],'format':'PNG','depth':'8','transparent':v['transparent'],'color':v['color'],'compositor':False,'passes':[],'denoise':False,'motion_blur':0}
def normalize_params(p):
    validate(p,PARAMS);normalize(p['manifest']);file_input({'file':p['file']})
    if Path(p['file']).suffix.lower()!='.blend':raise Failure('INVALID_REQUEST','Preview source must be a blend file')
    if p.get('driver_profile') and p['driver_profile']['source_sha256']!=p['expected_sha256']:raise Failure('CONFLICT','Preview driver profile does not match source SHA')
    for d in input_documents(p):file_input(d)
    return p
def input_documents(p):
    return [*p.get('resources',[]),*[p[k] for k in ('simulation_receipt','reuse_receipt') if k in p]]
def receipt_inputs(r):
    validate(r,RECEIPT)
    return [{'file':x['file'],'expected_sha256':x['sha256']} for x in r['outputs']]+([r['candidate']] if r['candidate'] else [])
def expand_batch(v):
    validate(v,BATCH)
    # Validate recipe shape now; file existence/hash checks remain per-item so
    # continue mode can preserve successes alongside missing/stale inputs.
    for row in v['items']:normalize(row['params']['manifest'])
    from pipeline_contract import normalize as pipeline_normalize
    return pipeline_normalize({'pipeline_version':'1.0','policy':v['policy'],'steps':[{'id':x['id'],'command':'asset.preview.prepare','params':copy.deepcopy(x['params']),'depends_on':[],'bindings':[],'limits':x['limits'],'timeout_seconds':x['timeout_seconds'],'reuse':x['reuse']} for x in v['items']]})
