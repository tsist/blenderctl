# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure export-only declarations; no bpy/NumPy, IO or implementation authority."""
import ntpath
import re
from pathlib import PureWindowsPath
from types import MappingProxyType

SCHEMA='toon-selected-saved-export-request.v1'
EXPORT_HARD_LIMITS=MappingProxyType(dict(max_objects=64,max_vertices=2097152,max_edges=4194304,
    max_triangles=1048576,max_loops=4194304,max_polygons=1048576,max_array_bytes=268435456,
    max_input_bytes=536870912,max_document_bytes=4194304,max_pixels=4194304,max_dimension=2048))
CONTEXT_KEYS=('scene','view_layer','frame','camera_name','occluder_object_names','member_object_names')


class SelectedExportContractError(ValueError):
    def __init__(self,path,message):
        self.code='INVALID_REQUEST';self.path=path
        super().__init__(message)


def _need(ok,path,message):
    if not ok:raise SelectedExportContractError(path,message)


def _fields(value,names,path):
    _need(type(value) is dict,path,'Native exact dictionary required')
    _need(len(value)==len(names) and all(type(key) is str and key in names for key in value),path,'Exact fields required')


def _text(value,path,maximum):
    _need(type(value) is str and 0<len(value)<=maximum,path,'Bounded native string required')
    try:value.encode('utf-8',errors='strict')
    except UnicodeEncodeError as error:raise SelectedExportContractError(path,'Strict UTF-8 required') from error
    _need(not any(ord(c)<32 or ord(c)==127 for c in value),path,'Control character forbidden')
    return value


def _path(value,path):
    value=_text(value,path,1024).replace('/','\\')
    _need(re.match(r'^[A-Za-z]:\\',value) is not None,path,'Local absolute drive path required')
    parts=value[3:].split('\\')
    _need(all(part and part not in ('.','..') and not part.endswith((' ','.')) and
              not any(c in '<>:"|?*' for c in part) and
              not (ntpath.isreserved(part) if hasattr(ntpath,'isreserved') else PureWindowsPath(part).is_reserved())
              for part in parts),path,'Unsafe/ambiguous Windows component')
    return value[0].upper()+value[1:]


def _sha(value,path):
    _need(type(value) is str and re.fullmatch('[0-9a-f]{64}',value) is not None,path,'Lowercase SHA256 required')
    return value


def _names(value,path,minimum):
    _need(type(value) is list and minimum<=len(value)<=64,path,'Native bounded names list required')
    result=[];seen=set()
    for index,name in enumerate(value):
        name=_text(name,path+'['+str(index)+']',63)
        _need(bool(name.strip()) and name not in seen,path,'Unique nonblank names required')
        seen.add(name);result.append(name)
    return result


def _freeze(value):
    if type(value) is dict:return MappingProxyType({key:_freeze(item) for key,item in value.items()})
    if type(value) is list:return tuple(_freeze(item) for item in value)
    return value


def normalize_request(value):
    _fields(value,('schema','file','expected_sha256','resources','source_mode','context','export_limits'),'$')
    _need(type(value['schema']) is str and value['schema']==SCHEMA,'$.schema','Unsupported export schema')
    _need(type(value['source_mode']) is str and value['source_mode']=='SAVED_DISK_V1','$.source_mode','SAVED_DISK_V1 required')
    source=_path(value['file'],'$.file')
    _need(PureWindowsPath(source).suffix.lower()=='.blend','$.file','Saved .blend source required')
    source_sha=_sha(value['expected_sha256'],'$.expected_sha256')
    resources=value['resources'];_need(type(resources) is list and len(resources)<=100,'$.resources','At most 100 native resources required')
    seen={ntpath.normcase(ntpath.normpath(source))};clean=[]
    for index,row in enumerate(resources):
        path='$.resources['+str(index)+']';_fields(row,('file','expected_sha256'),path)
        filename=_path(row['file'],path+'.file');key=ntpath.normcase(ntpath.normpath(filename))
        _need(key not in seen,path+'.file','Lexical input alias forbidden');seen.add(key)
        clean.append(dict(file=filename,expected_sha256=_sha(row['expected_sha256'],path+'.expected_sha256')))
    context=value['context'];_fields(context,CONTEXT_KEYS,'$.context')
    ctx={key:_text(context[key],'$.context.'+key,63) for key in ('scene','view_layer','camera_name')}
    _need(all(v.strip() for v in ctx.values()),'$.context','Nonblank context names required')
    frame=context['frame'];_need(type(frame) is int and -1048574<=frame<=1048574,'$.context.frame','Exact bounded integer frame required');ctx['frame']=frame
    ctx['occluder_object_names']=_names(context['occluder_object_names'],'$.context.occluder_object_names',1)
    ctx['member_object_names']=_names(context['member_object_names'],'$.context.member_object_names',0)
    _need(set(ctx['member_object_names'])<=set(ctx['occluder_object_names']),'$.context.member_object_names','Members must be a lexical occluder subset')
    limits=value['export_limits'];_need(type(limits) is dict and len(limits)<=len(EXPORT_HARD_LIMITS),'$.export_limits','Native export limit dictionary required')
    checked={}
    for key,bound in limits.items():
        _need(type(key) is str and key in EXPORT_HARD_LIMITS,'$.export_limits','Unknown export limit')
        _need(type(bound) is int and 0<=bound<=EXPORT_HARD_LIMITS[key],'$.export_limits.'+key,'Limit may only tighten with exact integer')
        checked[key]=bound
    _need(len(ctx['occluder_object_names'])<=checked.get('max_objects',64),'$.export_limits.max_objects','Object declarations exceed tightened cap')
    return _freeze(dict(schema=SCHEMA,file=source,expected_sha256=source_sha,resources=clean,
        source_mode='SAVED_DISK_V1',context=ctx,export_limits=checked))


def request_schema():
    """Structure snapshot; normalize_request's lexical/semantic checks required."""
    name=dict(type='string',minLength=1,maxLength=63)
    descriptor=dict(type='object',additionalProperties=False,required=['file','expected_sha256'],properties=dict(
        file=dict(type='string',minLength=1,maxLength=1024),expected_sha256=dict(type='string',pattern='^[0-9a-f]{64}$')))
    context=dict(type='object',additionalProperties=False,required=list(CONTEXT_KEYS),properties=dict(
        scene=dict(name),view_layer=dict(name),camera_name=dict(name),frame=dict(type='integer',minimum=-1048574,maximum=1048574),
        occluder_object_names=dict(type='array',minItems=1,maxItems=64,uniqueItems=True,items=dict(name)),
        member_object_names=dict(type='array',minItems=0,maxItems=64,uniqueItems=True,items=dict(name))))
    return _freeze(dict(type='object',additionalProperties=False,
        required=['schema','file','expected_sha256','resources','source_mode','context','export_limits'],
        properties=dict(schema=dict(const=SCHEMA),file=dict(descriptor['properties']['file']),
            expected_sha256=dict(descriptor['properties']['expected_sha256']),
            resources=dict(type='array',minItems=0,maxItems=100,items=descriptor),source_mode=dict(const='SAVED_DISK_V1'),
            context=context,export_limits=dict(type='object',additionalProperties=False,
                properties={key:dict(type='integer',minimum=0,maximum=cap) for key,cap in EXPORT_HARD_LIMITS.items()})),
        description='Pure export-only structure. Local path grammar, exact native types, lexical aliases and member semantics require normalize_request. No IO or authority.'))
