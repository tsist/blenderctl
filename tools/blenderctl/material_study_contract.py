# SPDX-License-Identifier: GPL-3.0-or-later
"""Host-safe bounded studies; expansions reuse the canonical batch normalizer."""
from copy import deepcopy
from material_batch_contract import BATCH_PARAMS_SCHEMA, normalize_params as normalize_batch_params
from material_workflow_addon.contract import LAYER_V2, FILE, ID, obj, _validate, _absolute, _fail, WorkflowError
from protocol import Failure

MAX_CASES, MAX_VIEWS, MAX_TOTAL_VIEWS = 8, 6, 32
CASE_VALUES = obj({k:{p:v for p,v in deepcopy(LAYER_V2['properties']['values']['properties'][k]).items() if p!='default'}
    for k in ('normal_strength','roughness','metallic','ior','coat_weight','coat_roughness','coat_ior')})
CASE_SCHEMA = obj({'id':ID,'values':CASE_VALUES,'color_mode':{'enum':['original','white'],'default':'original'}},('id','values'))
STUDY_SCHEMA = obj({'assignment_id':ID,'layer_id':ID,'cases':{'type':'array','items':CASE_SCHEMA,'minItems':1,'maxItems':MAX_CASES},
    'semantic_analysis':{'type':'boolean','default':True}},('assignment_id','layer_id','cases'))
STUDY_PARAMS_SCHEMA = deepcopy(BATCH_PARAMS_SCHEMA)
STUDY_PARAMS_SCHEMA['properties']['study']=STUDY_SCHEMA
STUDY_PARAMS_SCHEMA['required'].append('study')
STUDY_PARAMS_SCHEMA['properties']['manifest']['properties']['preview']['properties']['views']['maxItems']=MAX_VIEWS
RESERVED={'con','prn','aux','nul',*(f'com{i}' for i in range(1,10)),*(f'lpt{i}' for i in range(1,10))}

def _target(params):
    study=params['study']
    assignment=next((a for a in params['manifest']['assignments'] if a['id']==study['assignment_id']),None)
    if assignment is None:_fail('study.assignment_id','unknown assignment')
    layer=next((l for l in assignment['layers'] if l['id']==study['layer_id']),None)
    if layer is None:_fail('study.layer_id','unknown layer')
    return assignment,layer

def normalize_params(value):
    try:
        raw=_validate(deepcopy(value),STUDY_PARAMS_SCHEMA,'params')
        study=raw.pop('study');resume=raw.pop('resume',None)
        result=normalize_batch_params(raw);result['study']=study
        keys=[case['id'].casefold() for case in study['cases']]
        if len(keys)!=len(set(keys)):_fail('study.cases.id','casefold duplicate ID')
        if set(keys)&RESERVED:_fail('study.cases.id','Windows reserved directory name')
        if len(keys)*len(result['manifest']['preview']['views'])>MAX_TOTAL_VIEWS:_fail('study.cases','cases times views exceeds 32')
        _target(result)
        if resume is not None:
            resume['file']=_absolute(resume['file']);result['resume']=resume
        return result
    except WorkflowError as error:
        raise Failure(error.code,str(error)) from error

def expand_cases(normalized):
    """Return [(case_doc, normalized_batch_params)]; study resume is never forwarded."""
    params=normalize_params(normalized);output=[]
    for case in params['study']['cases']:
        batch=deepcopy({k:v for k,v in params.items() if k not in ('study','resume')})
        target={'study':params['study'],'manifest':batch['manifest']}
        _,layer=_target(target)
        layer['values'].update(case['values'])
        if case['color_mode']=='white':
            layer['channels'].pop('base_color',None)
            layer['values']['base_color']=[.65,.65,.65,1]
        output.append((deepcopy(case),normalize_batch_params(batch)))
    return output

def input_documents(params):
    """Source belongs to runner; branch resources stay runtime-isolated."""
    return [params['resume']] if 'resume' in params else []
