# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded stage-7 declarative operations. Shared by runtime and JSON Schema export."""
import math,re
from protocol import Failure

def obj(properties,required=None):
    return {'type':'object','additionalProperties':False,'properties':properties,'required':list(properties) if required is None else required}
def enum(*values):return {'enum':list(values)}
def array(item,minimum=0,maximum=1000):return {'type':'array','items':item,'minItems':minimum,'maxItems':maximum}
def number(low=-1e9,high=1e9):return {'type':'number','minimum':low,'maximum':high}
def integer(low,high):return {'type':'integer','minimum':low,'maximum':high}
NAME={'type':'string','minLength':1,'maxLength':63,'pattern':r'^[^\x00-\x1f\x7f]+$'}
NULL_NAME={'oneOf':[NAME,{'type':'null'}]}
BOOL={'type':'boolean'};VEC=array(number(),3,3);POS=number(1e-6,1e6)
AXIS=enum('X','Y','Z','-X','-Y','-Z')
PARENT={'oneOf':[obj({'scene':NAME}),obj({'collection':NAME})]}
TRANSFORM=obj({'location':VEC,'rotation_deg':VEC,'scale':VEC},[])
OPS={}
def operation(name,properties,required=None):
    OPS[name]=obj({'op':{'const':name},**properties},['op',*(list(properties) if required is None else required)])
operation('scene.create',{'name':NAME})
operation('scene.configure',{'scene':NAME,'units':obj({'system':enum('NONE','METRIC','IMPERIAL'),'scale_length':POS,'length_unit':enum('ADAPTIVE','METERS','CENTIMETERS','MILLIMETERS','KILOMETERS','MILES','FEET','INCHES')}),
 'timeline':obj({'start':integer(-1048574,1048574),'end':integer(-1048574,1048574),'fps':integer(1,240),'fps_base':number(.001,1000)}),'camera':NULL_NAME},['scene'])
operation('scene.remove',{'scene':NAME})
operation('view_layer.create',{'scene':NAME,'name':NAME})
operation('view_layer.remove',{'scene':NAME,'view_layer':NAME})
operation('view_layer.configure',{'scene':NAME,'view_layer':NAME,'use':BOOL,'collection_path':array(NAME,1,64),'exclude':BOOL,'holdout':BOOL,'indirect_only':BOOL},['scene','view_layer'])
operation('collection.create',{'name':NAME,'parent':PARENT})
operation('collection.attach',{'collection':NAME,'parent':PARENT,'linked':BOOL})
operation('collection.member',{'collection':NAME,'object':NAME,'linked':BOOL})
operation('collection.rename',{'collection':NAME,'name':NAME})
operation('object.create',{'name':NAME,'collection':NAME,'kind':enum('EMPTY','CUBE','PLANE','UV_SPHERE','CYLINDER','CONE','CAMERA','LIGHT','INSTANCE'),
 'size':POS,'radius':POS,'depth':POS,'segments':integer(3,128),'rings':integer(3,128),'light_type':enum('POINT','SUN','SPOT','AREA'),'instance_collection':NAME},['name','collection','kind'])
operation('object.duplicate',{'object':NAME,'name':NAME,'collection':NAME,'data':enum('linked','copy')})
operation('object.transform',{'object':NAME,'space':enum('local','world'),**TRANSFORM['properties']},['object','space'])
operation('object.parent',{'object':NAME,'parent':NULL_NAME,'keep_world':BOOL})
operation('object.visibility',{'object':NAME,'hide_render':BOOL,'hide_viewport':BOOL},['object'])
operation('object.convert_axes',{'objects':{**array(NAME,1,1000),'uniqueItems':True},'from_forward':AXIS,'from_up':AXIS,'to_forward':AXIS,'to_up':AXIS,'scale_factor':POS})
operation('constraint.add',{'object':NAME,'name':NAME,'type':enum('COPY_LOCATION','COPY_ROTATION','COPY_SCALE','COPY_TRANSFORMS','TRACK_TO'),'target':NAME,
 'influence':number(0,1),'owner_space':enum('WORLD','LOCAL'),'target_space':enum('WORLD','LOCAL'),'track_axis':enum('TRACK_X','TRACK_Y','TRACK_Z','TRACK_NEGATIVE_X','TRACK_NEGATIVE_Y','TRACK_NEGATIVE_Z'),'up_axis':enum('UP_X','UP_Y','UP_Z')},['object','name','type','target'])
operation('constraint.remove',{'object':NAME,'name':NAME})
operation('camera.configure',{'object':NAME,'data_scope':enum('single_user','shared'),'type':enum('PERSP','ORTHO'),'lens':number(1,2500),'ortho_scale':number(.001,1e6),'clip_start':number(.001,1e5),'clip_end':number(.002,1e8)},['object','data_scope'])
operation('light.configure',{'object':NAME,'data_scope':enum('single_user','shared'),'energy':number(0,1e9),'color':array(number(0,1),3,3),'size':number(.001,1e6),'radius':number(0,1e6),'spot_size_deg':number(1,180),'spot_blend':number(0,1),'angle_deg':number(0,180)},['object','data_scope'])
operation('world.create',{'scene':NAME,'name':NAME,'color':array(number(0,1),3,3),'strength':number(0,10000)})
CONTEXT=obj({'scene':NAME,'view_layer':NAME,'frame':integer(-1048574,1048574),'mode':{'const':'OBJECT'},'active_object':NULL_NAME,'selected_objects':{**array(NAME,maximum=1000),'uniqueItems':True}})
MANIFEST=obj({'initial_scene':NAME,'context':CONTEXT,'operations':array({'oneOf':list(OPS.values())},1,1000)},['context','operations'])

def validate(value,schema,path='$'):
    def fail(message):raise Failure('INVALID_REQUEST',path+': '+message)
    if 'oneOf' in schema:
        matches=0
        for s in schema['oneOf']:
            try:validate(value,s,path);matches+=1
            except Failure:pass
        if matches!=1:fail('does not match exactly one allowed shape')
        return
    types={'object':lambda v:isinstance(v,dict),'array':lambda v:isinstance(v,list),'string':lambda v:isinstance(v,str),'number':lambda v:type(v) is int or type(v) is float and math.isfinite(v),'integer':lambda v:type(v) is int,'boolean':lambda v:type(v) is bool,'null':lambda v:v is None}
    if 'type' in schema and not types[schema['type']](value):fail('invalid type')
    if 'const' in schema and value!=schema['const']:fail('invalid constant')
    if 'enum' in schema and value not in schema['enum']:fail('invalid enum')
    if type(value) in (int,float):
        if not schema.get('minimum',-math.inf)<=value<=schema.get('maximum',math.inf):fail('number outside range')
    if isinstance(value,str):
        if not schema.get('minLength',0)<=len(value)<=schema.get('maxLength',math.inf) or ('pattern' in schema and not re.search(schema['pattern'],value)):fail('invalid string')
        if schema==NAME and len(value.encode('utf-8'))>63:fail('name exceeds 63 UTF-8 bytes')
    if isinstance(value,list):
        if not schema.get('minItems',0)<=len(value)<=schema.get('maxItems',math.inf):fail('array outside bounds')
        for i,v in enumerate(value):validate(v,schema.get('items',{}),f'{path}/{i}')
        if schema.get('uniqueItems') and len(set(value))!=len(value):fail('duplicate items')
    if isinstance(value,dict):
        props=schema.get('properties',{})
        if set(schema.get('required',[]))-set(value) or schema.get('additionalProperties') is False and set(value)-set(props):fail('missing or unknown fields')
        for k,v in value.items():validate(v,props.get(k,{}),path+'/'+k)

def normalize(manifest):
    validate(manifest,MANIFEST)
    vertices=0
    for op in manifest['operations']:
        kind=op['op']
        if kind=='object.transform' and not set(op)&{'location','rotation_deg','scale'}:raise Failure('INVALID_REQUEST','Empty transform')
        if 'scale' in op and any(abs(x)<1e-8 for x in op['scale']):raise Failure('INVALID_REQUEST','Singular scales are not supported')
        if kind=='object.create':
            allowed={'CUBE':{'size'},'PLANE':{'size'},'UV_SPHERE':{'radius','segments','rings'},'CYLINDER':{'radius','depth','segments'},'CONE':{'radius','depth','segments'},'LIGHT':{'light_type'},'INSTANCE':{'instance_collection'}}.get(op['kind'],set())
            if set(op)-{'op','name','collection','kind'}-allowed:raise Failure('INVALID_REQUEST','Properties do not belong to primitive kind')
            if op['kind']=='INSTANCE' and 'instance_collection' not in op:raise Failure('INVALID_REQUEST','Instance collection required')
            vertices+={'CUBE':8,'PLANE':4,'UV_SPHERE':op.get('segments',32)*op.get('rings',16),'CYLINDER':2*op.get('segments',32)+2,'CONE':2*op.get('segments',32)+2}.get(op['kind'],0)
        if kind=='object.convert_axes' and (op['from_forward'][-1]==op['from_up'][-1] or op['to_forward'][-1]==op['to_up'][-1]):raise Failure('INVALID_REQUEST','Forward and up axes must differ')
        if kind=='view_layer.configure' and set(op)&{'exclude','holdout','indirect_only'} and 'collection_path' not in op:raise Failure('INVALID_REQUEST','Layer collection flags require an explicit collection path')
        if kind=='constraint.add':
            if op['type']!='TRACK_TO' and set(op)&{'track_axis','up_axis'}:raise Failure('INVALID_REQUEST','Tracking axes require TRACK_TO')
            if op['type']=='TRACK_TO' and op.get('track_axis','TRACK_NEGATIVE_Z')[-1]==op.get('up_axis','UP_Y')[-1]:raise Failure('INVALID_REQUEST','Track and up axes must differ')
    if vertices>1000000:raise Failure('INVALID_REQUEST','Primitive batch exceeds one million estimated vertices')
    return manifest
