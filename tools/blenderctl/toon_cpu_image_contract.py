# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure opt-in saved-source contract for the relocated CPU line provider."""
import copy,json,sys
from toon_image_contract import ImageContractError,_obj,_array,_integer
from toon_painted_image_contract import plain,freeze,_checked,need,_limits,BIND_HARD,RENDER_HARD
from toon_selected_export_contract import request_schema,normalize_request,CONTEXT_KEYS

PROFILE='cpu-boundary-image.v1'
RECIPE_SCHEMA='toon-cpu-image-recipe.v1'
PRODUCER='toon-painted-boundary-units-request.v1'
MAX_REQUEST_BYTES=1048576

def build_schema():
    scalar=lambda cap:dict(type='number',minimum=0,maximum=cap)
    text=dict(type='string',minLength=1,maxLength=1024)
    mapping={'anyOf':[dict(type='null'),_obj(dict(selection={'oneOf':[_obj(dict(index=_integer(0,15))),_obj(dict(name=text))]},
        channel=dict(type='string',enum=['mean_rgb','red','max_rgb','alpha']),encoding=dict(type='string',enum=['linear_rgba','srgb_rgba']),
        missing=dict(type='string',enum=['error','zero','one'])))]}
    curve={'anyOf':[dict(type='null'),_array(scalar(1),2,1024)]}
    brush=_obj(dict(native_size=scalar(sys.float_info.max),blend_amount=scalar(1),color=_array(scalar(1),3,3),width_map=mapping,alpha_map=mapping,size_curve=curve,alpha_curve=curve))
    required=['id','priority','object_ids','material_names','visible_enabled','hidden_enabled','brush','size_mode','initial_scale_ex','coverage_extent_factor']
    group=_obj(dict(id=dict(type='string',minLength=1,maxLength=256),priority=_integer(-100000,100000),
        object_ids=_array(text,0,1024),material_names=_array(text,0,1024),visible_enabled=dict(type='boolean'),hidden_enabled=dict(type='boolean'),
        brush=brush,size_mode=dict(type='string',enum=['RELATIVE','ABSOLUTE']),initial_scale_ex=scalar(sys.float_info.max),
        coverage_extent_factor=dict(type='number',exclusiveMinimum=0,maximum=8),
        **{key:brush for key in ('silhouette_brush','smoothing_brush','marked_brush')}),required)
    target={'anyOf':[dict(type='null'),_integer(0,2147483647)]}
    units=_obj(dict(sensor_fit=dict(type='string',enum=['AUTO','HORIZONTAL','VERTICAL']),absolute_scale=scalar(sys.float_info.max),
        relative_target_width=target,relative_target_height=target))
    settings=_obj(dict(include_open=dict(type='boolean'),feather_px=scalar(256),samples_per_axis=dict(type='integer',enum=[1,2,4]),
        coverage_profile=dict(type='string',enum=['round_capsule.v1','extent_falloff.v1']),post_filter_sigma=scalar(4),
        source_categories=_array(dict(type='string',enum=['selected','smoothing','marked']),1,3,True),force_marked_visible=dict(type='boolean')))
    producer=_obj(dict(schema=dict(const=PRODUCER),groups=_array(group,1,32),settings=settings,units=units,
        budgets=_obj(dict(binding=_limits(BIND_HARD),render=_limits(RENDER_HARD)),[])),['schema','groups','settings','units'])
    export=plain(request_schema())['properties'];context=export['context']['properties']
    recipe=_obj(dict(schema=dict(const=RECIPE_SCHEMA),profile=dict(const=PROFILE),**context,export_limits=export['export_limits'],producer=producer))
    return _obj(dict(file=export['file'],expected_sha256=export['expected_sha256'],resources=export['resources'],
        source_mode=dict(const='SAVED_DISK_V1'),implementation_sha256=dict(type='string',pattern='^[0-9a-f]{64}$'),recipe=recipe))

_SCHEMA=freeze(build_schema())
def normalize_params(value):
    _checked(value,_SCHEMA,'$.params')
    recipe=value['recipe'];producer=recipe['producer']
    export=dict(schema='toon-selected-saved-export-request.v1',file=value['file'],expected_sha256=value['expected_sha256'],
        resources=value['resources'],source_mode=value['source_mode'],context={key:recipe[key] for key in CONTEXT_KEYS},export_limits=recipe['export_limits'])
    try:
        clean=plain(normalize_request(export))
    except ValueError as error:raise ImageContractError('$.params.recipe',str(error)) from error
    need(recipe['member_object_names']==recipe['occluder_object_names'],'$.params.recipe','CPU profile preserves the complete captured universe')
    groups=producer['groups'];ids=[g['id'] for g in groups];priorities=[g['priority'] for g in groups]
    need(len(set(ids))==len(ids) and len(set(priorities))==len(priorities),'$.params.recipe.producer.groups','Unique group IDs/priorities required')
    known=set(clean['context']['occluder_object_names'])
    for group in groups:
        need(bool(group['object_ids'] or group['material_names']),'$.params.recipe.producer.groups','Nonempty selector required')
        for key in ('object_ids','material_names'):need(len(set(group[key]))==len(group[key]),'$.params.recipe.producer.groups','Unique selectors required')
        need(set(group['object_ids'])<=known,'$.params.recipe.producer.groups','Object outside captured universe')
        for key in ('brush','silhouette_brush','smoothing_brush','marked_brush'):
            if key in group:
                for mapping in ('width_map','alpha_map'):
                    m=group[key][mapping]
                    if m is not None and 'name' in m['selection']:need(bool(m['selection']['name'].strip()),'$.params.recipe.producer.groups','Nonblank attribute name')
    # Actual camera resolution comes from the guarded saved export. Resolution-
    # dependent widths remain unmodified here and are validated by the provider.
    raw=json.dumps(producer,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    need(len(raw)<=MAX_REQUEST_BYTES-2048,'$.params.recipe.producer','Bounded space for host input binding required')
    return freeze(dict(file=clean['file'],expected_sha256=clean['expected_sha256'],resources=clean['resources'],
        source_mode=clean['source_mode'],implementation_sha256=value['implementation_sha256'],
        recipe=dict(schema=RECIPE_SCHEMA,profile=PROFILE,**clean['context'],export_limits=clean['export_limits'],producer=copy.deepcopy(producer))))

def run_params_schema():return _SCHEMA
def split_params(params):
    r=params['recipe']
    return dict(schema='toon-selected-saved-export-request.v1',file=params['file'],expected_sha256=params['expected_sha256'],resources=plain(params['resources']),
        source_mode='SAVED_DISK_V1',context={key:plain(r[key]) for key in CONTEXT_KEYS},export_limits=plain(r['export_limits'])),plain(r['producer'])
def minimum_timeout(params):
    budget=params['recipe']['producer'].get('budgets',{})
    return budget.get('binding',{}).get('max_seconds',300)+budget.get('render',{}).get('max_seconds',300)+90
