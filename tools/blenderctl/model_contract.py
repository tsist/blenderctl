# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded modeling operations; shared runtime/JSON Schema contract."""
from scene_contract import (obj, enum, array, number, integer, NAME, BOOL, VEC,
                            CONTEXT, OPS as SCENE_OPS, validate, normalize as scene_normalize)
from protocol import Failure

SHA={'type':'string','pattern':'^[0-9a-f]{64}$'}
INDICES={**array(integer(0,999999),1,100000),'uniqueItems':True}
SELECTION={'oneOf':[{'const':'ALL'},obj({'indices':INDICES,'topology_sha256':SHA})]}
SCOPE=enum('reject_shared','single_user','shared')
TARGET={'object':NAME,'data_scope':SCOPE}
OPS={}
def operation(name,properties,required=None):
    OPS[name]=obj({'op':{'const':name},**properties},['op',*(properties if required is None else required)])

operation('mesh.create',{'name':NAME,'collection':NAME,'vertices':array(VEC,3,100000),
    'edges':array(array(integer(0,99999),2,2),0,200000),'faces':array(array(integer(0,99999),3,1000),1,100000)},['name','collection','vertices','faces'])
operation('mesh.positions',{**TARGET,'selection':SELECTION,'positions':array(VEC,1,100000)})
operation('mesh.extrude',{**TARGET,'selection':SELECTION,'offset':VEC})
operation('mesh.inset',{**TARGET,'selection':SELECTION,'thickness':number(.000001,10000),'depth':number(-10000,10000)})
operation('mesh.bevel',{**TARGET,'selection':SELECTION,'offset':number(.000001,10000),'segments':integer(1,8)})
operation('mesh.subdivide',{**TARGET,'selection':SELECTION,'cuts':integer(1,8)})
operation('mesh.delete_faces',{**TARGET,'selection':SELECTION})
operation('mesh.triangulate',{**TARGET,'selection':SELECTION})
operation('mesh.merge_distance',{**TARGET,'distance':number(.00000001,1000)})
operation('mesh.normals',{**TARGET,'recalculate':BOOL,'smooth':BOOL})
operation('uv.layer',{**TARGET,'action':enum('create','activate','remove'),'name':NAME})
operation('uv.set',{**TARGET,'layer':NAME,'topology_sha256':SHA,'coordinates':array(array(number(-1e6,1e6),2,2),1,1000000)})
operation('uv.seams',{**TARGET,'selection':SELECTION,'marked':BOOL})
operation('uv.unwrap',{**TARGET,'layer':NAME,'method':enum('ANGLE_BASED','CONFORMAL','SMART'),'margin':number(0,.1)},list(TARGET)+['layer','method','margin'])
operation('uv.pack',{**TARGET,'layer':NAME,'margin':number(0,.1),'rotate':BOOL})
from uv_contract import LAYOUT
OPS['uv.layout']=LAYOUT
from retopology_contract import OP as RETOPOLOGY
OPS['mesh.retopology']=RETOPOLOGY

MODIFIERS={
 'BEVEL':obj({'width':number(.000001,1000),'segments':integer(1,8),'angle_limit_deg':number(0,180)}),
 'BOOLEAN':obj({'operation':enum('DIFFERENCE','UNION','INTERSECT'),'object':NAME,'solver':{'const':'EXACT'}}),
 'SOLIDIFY':obj({'thickness':number(-1000,1000),'offset':number(-1,1)}),
 'SUBSURF':obj({'levels':integer(1,3)}),
 'MIRROR':obj({'axes':{**array(enum('X','Y','Z'),1,3),'uniqueItems':True},'merge_threshold':number(0,1),'clip':BOOL}),
 'DECIMATE':obj({'ratio':number(.001,1)}),
 'TRIANGULATE':obj({}),
 'WELD':obj({'merge_threshold':number(.00000001,1)}),
}
operation('modifier.add',{'object':NAME,'name':NAME,'type':enum(*MODIFIERS),'settings':{'type':'object'}})
OPS['modifier.add']={'oneOf':[obj({'op':{'const':'modifier.add'},'object':NAME,'name':NAME,'type':{'const':kind},'settings':settings}) for kind,settings in MODIFIERS.items()]}
operation('modifier.remove',{'object':NAME,'name':NAME})
operation('modifier.apply',{**TARGET,'name':NAME})
operation('mesh.lod',{'object':NAME,'name':NAME,'collection':NAME,'ratio':number(.001,.999)})
operation('curve.create',{'name':NAME,'collection':NAME,'type':enum('POLY','BEZIER'),'points':array(VEC,2,10000),'cyclic':BOOL,'bevel_depth':number(0,1000),'resolution':integer(1,32)})
from curve_contract import FILE,TEXT_LAYOUT,VERIFY,CURVE_SETTINGS
OPS['curve.create']['properties']['settings']=CURVE_SETTINGS
TEXT={'type':'string','minLength':1,'maxLength':1000,'pattern':r'^[^\x00-\x08\x0b-\x1f\x7f\ud800-\udfff]+$'}
operation('text.create',{'name':NAME,'collection':NAME,'body':TEXT,'size':number(.0001,1000),'extrude':number(0,1000),'font':FILE,'layout':TEXT_LAYOUT},['name','collection','body','size','extrude'])
import copy
_font_text=copy.deepcopy(OPS['text.create']);_font_text['required'].append('font')
_ascii_text=copy.deepcopy(OPS['text.create']);_ascii_text['properties'].pop('font')
_ascii_text['properties']['body']={**TEXT,'pattern':r'^[\x20-\x7e\n\t]+$'}
OPS['text.create']={'oneOf':[_ascii_text,_font_text]}
operation('object.to_mesh',{**TARGET,'verify':VERIFY},list(TARGET))
MANIFEST=obj({'initial_scene':NAME,'context':CONTEXT,'operations':array({'oneOf':[*SCENE_OPS.values(),*OPS.values()]},1,500)},['context','operations'])

def normalize(manifest):
    validate(manifest,MANIFEST)
    scene_ops=[op for op in manifest['operations'] if op['op'] in SCENE_OPS]
    if scene_ops:scene_normalize({**manifest,'operations':scene_ops})
    total=0
    for op in manifest['operations']:
        if op['op']=='text.create' and 'font' not in op and any(ord(c)>127 for c in op['body']):raise Failure('INVALID_REQUEST','Unicode text requires an explicit font file/hash')
        if op['op']=='modifier.add':validate(op['settings'],MODIFIERS[op['type']])
        if op['op']=='mesh.create':
            n=len(op['vertices']);total+=n
            faces=op['faces'];edges=op.get('edges',[])
            if sum(len(f) for f in faces)>1000000:raise Failure('INVALID_REQUEST','Mesh exceeds one million face corners')
            if any(max(f)>=n or len(set(f))!=len(f) for f in [*faces,*edges]):
                raise Failure('INVALID_REQUEST','Mesh indices must be in range and unique within each face/edge')
            if len({tuple(sorted(f)) for f in faces})!=len(faces) or len({tuple(sorted(e)) for e in edges})!=len(edges):
                raise Failure('INVALID_REQUEST','Duplicate mesh faces/edges')
    if total>200000:raise Failure('INVALID_REQUEST','Mesh batch exceeds 200000 input vertices')
    return manifest
