# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure selected public declarations; no IO, jobs, producer or host timeout.

All successful returns are detached, deeply immutable snapshots. Schema is
structural only: normalize additionally checks path/subset/reduced dash/bits.
"""
from types import MappingProxyType
from toon_image_contract import ImageContractError, _validate
from toon_selected_export_contract import (normalize_request as _export,
    request_schema as _export_schema, SelectedExportContractError)
from toon_image_backend import selected_declarations as _declarations
from toon_image_backend import chain_phase as _phase, phase_support as _support
from toon_image_backend.image_cold_primitives import ImageReceiptError
from toon_image_backend.metric_arithmetic import (ChainScreenMetricInputError,
    ChainScreenMetricBudgetExceeded)

PROFILE='selected-mesh-image.v1'
RECIPE_SCHEMA='toon-image-recipe.v1'
NUMERIC_KEYS=('selector','visible_enabled','hidden_enabled','chaining','style','budgets')
CONTEXT_KEYS=('scene','view_layer','frame','camera_name','occluder_object_names','member_object_names')

def _freeze(value):
    if type(value) is dict: return MappingProxyType({k:_freeze(v) for k,v in value.items()})
    if type(value) is list: return tuple(_freeze(v) for v in value)
    return value

def _plain(value):
    if type(value) in (dict,MappingProxyType): return {k:_plain(v) for k,v in value.items()}
    if type(value) in (list,tuple): return [_plain(v) for v in value]
    return value

def _obj(properties,required=None):
    return dict(type='object',additionalProperties=False,properties=properties,
        required=list(properties) if required is None else required)

def _integer(low,high): return dict(type='integer',minimum=low,maximum=high)
def _array(items,low,high): return dict(type='array',items=items,minItems=low,maxItems=high)

def _limits(hard,zero):
    return _obj({key:(dict(type='number',minimum=0,maximum=cap) if key=='max_seconds'
        else _integer(0 if key in zero else 1,cap)) for key,cap in hard.items()},[])

def _build_schema():
    export=_plain(_export_schema())['properties']
    export['export_limits']['required']=[]
    contexts=export['context']['properties']
    # Native integer pairs with a strict 4096-bit scalar domain. Reduction,
    # denominator sign, duty range and effective max_bits remain semantic.
    pair=_array(_integer(-(2**4096-1),2**4096-1),2,2)
    dash=_obj(dict(enabled=dict(type='boolean'),units=dict(const='screen_pixels'),
        length_model=dict(const='sum_of_dyadic_chords.v1'),period_px=pair,phase_px=pair,duty=pair))
    color=_array(dict(type='number',minimum=0,maximum=1),4,4)
    style=_obj(dict(width_px=dict(type='number',exclusiveMinimum=0,maximum=256),
        feather_px=dict(type='number',minimum=0,maximum=256),
        samples_per_axis=dict(type='integer',enum=[1,2,4]),visible_color=color,hidden_color=color,
        chain_dash=dash,seam_policy=dict(const='explicit_phase_cut.v1'),coverage_model=dict(const='round_capsule_union.v1')))
    identity=_limits(dict(_declarations.IDENTITY_HARD),{'max_sources','max_nodes','max_chains','max_incidences','max_json_nodes'})
    phase=_limits(dict(_declarations.PHASE_HARD),{'max_fragments_per_source','max_fragments_total','max_extra_json_nodes'})
    phase['properties']['metric_limits']=_obj(dict(max_bits=_integer(1,4096),max_metric_sources=_integer(0,10000),identity_limits=identity),[])
    budgets=_obj(dict(
        binding=_limits(dict(_declarations.BIND_HARD),{'max_objects','max_sources','max_vertices','max_edges','max_triangles','max_candidate_tests','max_cells','max_npz_members'}),
        phase=phase,render=_limits(dict(_declarations.RENDER_HARD),{'max_pixels','max_datums','max_output_bytes'}),
        total=_obj(dict(max_sources=_integer(0,100000)),[]),planner=identity),[])
    numeric=dict(selector=_obj(dict(model=dict(const='triangle_geometric_winding_open_silhouette.v1'),include_open=dict(type='boolean'))),
        visible_enabled=dict(type='boolean'),hidden_enabled=dict(type='boolean'),
        chaining=_obj(dict(policies=_obj({k:dict(const=v) for k,v in _declarations.POLICIES.items()}),max_batch_sources=_integer(1,10000))),
        style=style,budgets=budgets)
    recipe=_obj(dict(schema=dict(const=RECIPE_SCHEMA),profile=dict(const=PROFILE),
        **contexts,export_limits=export['export_limits'],**numeric))
    params=_obj(dict(file=export['file'],expected_sha256=export['expected_sha256'],resources=export['resources'],
        implementation_sha256=dict(type='string',pattern='^[0-9a-f]{64}$'),source_mode=dict(const='SAVED_DISK_V1'),recipe=recipe))
    params.update({'$schema':'https://json-schema.org/draft/2020-12/schema',
        'title':'toon-image.run selected-mesh-image.v1 params; unregistered contract candidate',
        'description':'Structural schema only; normalize_params lexical/native/reduced rational/effective-bit checks mandatory. No IO or authority.',
        'x-semantic-checks':['Native containers/scalars only; bool is not int and finite float is distinct from int.',
            'Exact local Windows path grammar, lexical aliases, strict UTF8 and member subset use the saved export contract.',
            'Dash pairs must be reduced and denominator positive; period>0, duty in [0,1], effective phase/metric bit cap applies.',
            'All budget declarations remain unfilled and numerically unchanged; max_batch_sources cannot exceed tightened binding.max_sources.',
            'Host timeout admission and describe aggregation are outside this module.']})
    return _freeze(params)

_PARAMS=_build_schema()
_REQUEST=_freeze(dict(**_obj(dict(schema_version=dict(const='1.0'),command=dict(const='toon-image.run'),params=_plain(_PARAMS))),
    **{'$schema':'https://json-schema.org/draft/2020-12/schema','title':'toon-image.run selected-mesh-image.v1 request; unregistered contract candidate'}))

def _normalize_params(value,path):
    _validate(value,_PARAMS,path)
    recipe=value['recipe']
    declaration=dict(schema='toon-selected-saved-export-request.v1',file=value['file'],
        expected_sha256=value['expected_sha256'],resources=value['resources'],source_mode=value['source_mode'],
        context={key:recipe[key] for key in CONTEXT_KEYS},export_limits=recipe['export_limits'])
    try:
        exported=_plain(_export(declaration))
    except SelectedExportContractError as error:
        where=error.path
        if where.startswith('$.context'): where=path+'.recipe'+where[len('$.context'):]
        elif where.startswith('$.export_limits'): where=path+'.recipe'+where[1:]
        else: where=path+where[1:]
        raise ImageContractError(where,str(error)) from error
    # Only pure declaration functions: no mesh scene, catalog, phase, metric or
    # raster production. Effective values are transient validation, not output.
    policy=dict(model='selected_mesh_sources_all_nondegenerate_two_sided.v1',
        member_object_ids=exported['context']['member_object_names'],
        visible_enabled=recipe['visible_enabled'],hidden_enabled=recipe['hidden_enabled'])
    try:
        _declarations.policy_style(policy,recipe['style'])
        effective=_declarations.effective_request_budgets(dict(budgets=recipe['budgets'],chaining=recipe['chaining']))
        phase,_=_phase.normalize_limits(effective['phase'])
        arithmetic=_support.Arithmetic(phase['max_bits'])
        for key in ('period_px','phase_px','duty'):
            arithmetic.parse(recipe['style']['chain_dash'][key],path+'.recipe.style.chain_dash.'+key)
    except (ImageReceiptError,_support.ChainPhaseInputError,_support.ChainPhaseBudgetExceeded,
            ChainScreenMetricInputError,ChainScreenMetricBudgetExceeded) as error:
        raise ImageContractError(path+'.recipe','Invalid selected declaration: '+str(error)) from error
    normalized_recipe=dict(schema=RECIPE_SCHEMA,profile=PROFILE,**exported['context'],
        export_limits=exported['export_limits'],**{key:_plain(recipe[key]) for key in NUMERIC_KEYS})
    return dict(file=exported['file'],expected_sha256=exported['expected_sha256'],resources=exported['resources'],
        implementation_sha256=value['implementation_sha256'],source_mode=value['source_mode'],recipe=normalized_recipe)

def normalize_params(value):
    """Pure validation and saved-export lexical normalization; fresh deep freeze."""
    return _freeze(_normalize_params(value,'$.params'))

def normalize_request(value):
    _validate(value,_REQUEST,'$')
    return _freeze(dict(schema_version=value['schema_version'],command=value['command'],
        params=_normalize_params(value['params'],'$.params')))

def run_params_schema(): return _freeze(_plain(_PARAMS))
def run_request_schema(): return _freeze(_plain(_REQUEST))
