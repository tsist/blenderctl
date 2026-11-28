# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit paint-aware saved-source declarations, separate from old profiles.

Validation is pure: source paths, object existence and dependency closure are
verified later by the guarded export/host. Numerical producers are not invoked.
"""
from collections.abc import Mapping
import json
from types import MappingProxyType
from toon_image_contract import ImageContractError,_validate,_obj,_array,_integer
from toon_selected_export_contract import normalize_request as normalize_export,request_schema,CONTEXT_KEYS

PROFILE='painted-mesh-image.v1'
RECIPE_SCHEMA='toon-painted-image-recipe.v1'
AUTHORED='toon-painted-authored-image-request.v1'
INTERSECTION='toon-painted-intersection-image-request.v1'
# Versioned public declaration caps, checked against producer tables in the
# boundary tests. No producer/NumPy import belongs to historical normalization.
BIND_HARD=dict(max_objects=64,max_sources=10000,max_vertices=2097152,max_edges=4194304,max_triangles=1048576,
    max_input_bytes=536870912,max_candidate_tests=5000000,max_cells=500000,max_bits=4096,
    max_result_bytes=134217728,max_seconds=300,max_npz_members=64)
RENDER_HARD=dict(max_pixels=4194304,max_datums=2000000,max_output_bytes=536870912,max_seconds=300)
INTERSECTION_HARD=dict(max_triangles=1048576,max_vertices=2097152,max_objects=64,max_sources=100000,max_cells=2000000,
    max_primitives=2000000,max_style_knots=2000000,max_record_bytes=4194304,max_retained_source_bytes=268435456,
    max_output_bytes=4294967296,max_seconds=3600,max_pixels=4194304)
PIPELINE_HARD=dict(max_pairs=20000000,max_eligible=100000,max_narrow_pairs=30000000,max_cells=2000000,
    max_output_bytes=4294967296,max_seconds=3600,max_rational_bits=8192,max_pixels=16777216)
MAX_REQUEST_BYTES=1048576

def plain(v):
    if isinstance(v,Mapping):return {k:plain(x) for k,x in v.items()}
    if type(v) in (tuple,list):return [plain(x) for x in v]
    return v

def freeze(v):
    if type(v) is dict:return MappingProxyType({k:freeze(x) for k,x in v.items()})
    if type(v) is list:return tuple(freeze(x) for x in v)
    return v

def need(v,path,message):
    if not v:raise ImageContractError(path,message)

def _limits(values,integer_seconds=False):
    return _obj({k:(dict(type='number',minimum=0,maximum=v) if k=='max_seconds' and not integer_seconds else _integer(0,v)) for k,v in values.items()},[])

def _checked(value,schema,path):
    branches=schema.get('oneOf',schema.get('anyOf'))
    if branches is not None:
        passed=0
        for branch in branches:
            try:_checked(value,branch,path)
            except ImageContractError:continue
            passed+=1
        need(passed==1 if 'oneOf' in schema else passed>0,path,'No unique supported declaration branch')
        return
    kind=schema.get('type')
    if kind=='null':need(value is None,path,'Literal null required');return
    if kind=='object':
        need(type(value) is dict and all(type(k) is str for k in value),path,'Native exact dictionary required')
        need(set(schema.get('required',()))<=set(value)<=set(schema['properties']),path,'Unknown or missing fields')
        for key,item in value.items():_checked(item,schema['properties'][key],path+'.'+key)
    elif kind=='array':
        need(type(value) is list and schema['minItems']<=len(value)<=schema['maxItems'],path,'Bounded native list required')
        for i,item in enumerate(value):_checked(item,schema['items'],path+'['+str(i)+']')
        if schema.get('uniqueItems'):need(len(set(value))==len(value),path,'Unique names required')
    else:_validate(value,schema,path)

def _schema(kind):
    scalar=lambda hi:dict(type='number',minimum=0,maximum=hi)
    text=dict(type='string',minLength=1,maxLength=1024)
    # Explicit branches also validate null, maps and sampled curves without
    # importing numerical modules or opening historical input declarations.
    map_schema={'anyOf':[dict(type='null'),_obj(dict(selection={'oneOf':[_obj(dict(index=_integer(0,15))),_obj(dict(name=text))]},
        channel=dict(type='string',enum=['mean_rgb','red','max_rgb','alpha']),
        encoding=dict(type='string',enum=['linear_rgba','srgb_rgba']),missing=dict(type='string',enum=['error','zero','one'])))]}
    curve={'anyOf':[dict(type='null'),_array(scalar(1),2,1024)]}
    brush=_obj(dict(width_px=scalar(256),blend_amount=scalar(1),color=_array(scalar(1),3,3),width_map=map_schema,alpha_map=map_schema,size_curve=curve,alpha_curve=curve))
    group=_obj(dict(id=dict(type='string',minLength=1,maxLength=256),priority=_integer(-100000,100000),
        object_ids=_array(text,0,1024),material_names=_array(text,0,1024),visible_enabled=dict(type='boolean'),
        hidden_enabled=dict(type='boolean'),brush=brush,**({k:brush for k in ('silhouette_brush','sharp_brush','marked_brush')} if kind==AUTHORED else {})),
        ['id','priority','object_ids','material_names','visible_enabled','hidden_enabled','brush'])
    settings=dict(feather_px=scalar(256),samples_per_axis=dict(type='integer',enum=[1,2,4]),
        coverage_profile=dict(type='string',enum=['round_capsule.v1','extent_falloff.v1']))
    optional={}
    if kind==AUTHORED:
        settings.update(include_open=dict(type='boolean'),post_filter_sigma=scalar(4),
            source_types=_array(dict(type='string',enum=['selected','sharp','marked']),1,3,True),force_marked_visible=dict(type='boolean'))
        optional=dict(budgets=_obj(dict(binding=_limits(BIND_HARD),render=_limits(RENDER_HARD)),[]))
    else:
        settings.update(postfilter_sigma=scalar(4),point_policy=dict(type='string',enum=['disk','suppress']),self_intersection=dict(type='boolean'))
        optional=dict(limits=_limits(INTERSECTION_HARD,True),pipeline_budgets=_limits(PIPELINE_HARD,True))
    producer=_obj(dict(schema=dict(const=kind),groups=_array(group,1,32),settings=_obj(settings),**optional),['schema','groups','settings'])
    e=plain(request_schema())['properties'];context=e['context']['properties']
    recipe=_obj(dict(schema=dict(const=RECIPE_SCHEMA),profile=dict(const=PROFILE),**context,export_limits=e['export_limits'],producer=producer))
    return _obj(dict(file=e['file'],expected_sha256=e['expected_sha256'],resources=e['resources'],
        source_mode=dict(const='SAVED_DISK_V1'),implementation_sha256=dict(type='string',pattern='^[0-9a-f]{64}$'),recipe=recipe))

_SCHEMAS={kind:freeze(_schema(kind)) for kind in (AUTHORED,INTERSECTION)}

def normalize_params(value):
    need(type(value) is dict,'$.params','Native exact params required')
    r=value.get('recipe');need(type(r) is dict,'$.params.recipe','Native recipe required')
    p=r.get('producer');need(type(p) is dict,'$.params.recipe.producer','Explicit producer required')
    kind=p.get('schema');need(type(kind) is str and kind in _SCHEMAS,'$.params.recipe.producer.schema','Explicit authored or painted intersection producer required')
    _checked(value,_SCHEMAS[kind],'$.params')
    export=dict(schema='toon-selected-saved-export-request.v1',file=value['file'],expected_sha256=value['expected_sha256'],
        resources=value['resources'],source_mode=value['source_mode'],context={k:r[k] for k in CONTEXT_KEYS},export_limits=r['export_limits'])
    try:
        normalized=plain(normalize_export(export))
        ids=[g['id'] for g in p['groups']];priorities=[g['priority'] for g in p['groups']]
        need(len(set(ids))==len(ids) and len(set(priorities))==len(priorities),'$.params.recipe.producer.groups','Unique group IDs and priorities required')
        for g in p['groups']:
            need(bool(g['object_ids'] or g['material_names']),'$.params.recipe.producer.groups','Empty group selection')
            for key in ('object_ids','material_names'):
                need(len(set(g[key]))==len(g[key]),'$.params.recipe.producer.groups','Unique selectors required')
        # Authored/painted source universe is the whole captured universe. Groups
        # own individual object/material regions; no undeclared member filter.
        need(r['member_object_names']==r['occluder_object_names'],'$.params.recipe.member_object_names','Painted profile requires the complete ordered occluder universe; groups select strokes')
        known=set(r['occluder_object_names'])
        need(all(set(g['object_ids'])<=known for g in p['groups']),'$.params.recipe.producer.groups','Unknown object selector outside captured universe')
        if kind==INTERSECTION:need(all(set(g)=={'id','priority','object_ids','material_names','visible_enabled','hidden_enabled','brush'} for g in p['groups']),'$.params.recipe.producer.groups','Intersection uses one explicit intersection brush per group')
        if kind==AUTHORED:
            for key in ('max_bits','max_result_bytes','max_input_bytes'):
                need(p.get('budgets',{}).get('binding',{}).get(key,1)>0,'$.params.recipe.producer.budgets.binding','Positive bound required')
        encoded=(json.dumps(p,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode('utf-8')
        need(len(encoded)<=MAX_REQUEST_BYTES-2048,'$.params.recipe.producer','Producer declaration leaves no bounded space for its host input binding')
    except ImageContractError:raise
    except (ValueError,TypeError,KeyError,UnicodeError) as error:
        raise ImageContractError('$.params.recipe.producer','Invalid painted declaration: '+str(error)) from error
    out=dict(file=normalized['file'],expected_sha256=normalized['expected_sha256'],resources=normalized['resources'],
        source_mode=normalized['source_mode'],implementation_sha256=value['implementation_sha256'],
        recipe=dict(schema=RECIPE_SCHEMA,profile=PROFILE,**normalized['context'],export_limits=normalized['export_limits'],producer=plain(p)))
    return freeze(out)

def run_params_schema():
    return freeze(dict(oneOf=[plain(v) for v in _SCHEMAS.values()],
        description='Paint-aware explicit producer; normalize_params semantic validation mandatory. Complete captured universe, groups select strokes.'))

def minimum_timeout(params):
    p=params['recipe']['producer']
    if p['schema']==AUTHORED:
        b=p.get('budgets',{});binding=b.get('binding',{}).get('max_seconds',300);render=b.get('render',{}).get('max_seconds',300)
        return binding+max(binding,render)+90
    return p.get('limits',{}).get('max_seconds',3600)+90

def split_params(params):
    r=params['recipe']
    return dict(schema='toon-selected-saved-export-request.v1',file=params['file'],expected_sha256=params['expected_sha256'],
        resources=params['resources'],source_mode=params['source_mode'],context={k:r[k] for k in CONTEXT_KEYS},export_limits=r['export_limits']),plain(r['producer'])
