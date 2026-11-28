"""Bounded owned hot session and revision transport contract. No Torch/bpy."""
import copy, re
from compositing_relight_contract import (PARAMS_SCHEMA as RELIGHT, normalize_params as relight_normalize,
    CONFIG, obj, const, validate, need, signature, DepthError, base)
from compositing_relight_kernel import normalize as kernel_normalize
import compositing_relight_local_edits as local_edits

PARAMS_SCHEMA=obj({'depth_session_version':const('1.0'),'relight':RELIGHT,
    'preview_divisor':{'type':'integer','enum':[1,2,4]},
    'lifetime_seconds':{'type':'integer','minimum':10,'maximum':1800},
    'idle_seconds':{'type':'integer','minimum':5,'maximum':600},
    'max_revisions':{'type':'integer','minimum':1,'maximum':512},
    'revision_timeout_seconds':{'type':'integer','minimum':1,'maximum':120}})
PARAMS_SCHEMA.update({'$schema':'http://json-schema.org/draft-07/schema#','title':'compositing.depth-session params v1.0'})
PARAMS_SCHEMA['properties']['max_data_generations']={'type':'integer','minimum':1,'maximum':64}
PARAMETERS=obj({'config':CONFIG,'kind':{'enum':['preview','final']}})
_N=lambda lo,hi:{'type':'number','minimum':lo,'maximum':hi}
_V2=lambda lo,hi:{'type':'array','items':_N(lo,hi),'minItems':2,'maxItems':2}
_REGION_V1=obj({
    'id':{'type':'string','minLength':1,'maxLength':64},'enabled':{'type':'boolean'},
    'target':{'enum':['light','highlight','line','depth']},'operation':{'enum':['restrict','attenuate','add','affine']},
    'shape':{'enum':['ellipse','box']},'center':_V2(-2,3),'size':_V2(.001,4),'rotation':_N(-6.283185307179586,6.283185307179586),
    'feather':_N(0,1),'strength':_N(0,1),'scale':_N(.01,10),'offset':_N(-10,10)})
_REGION_V2=copy.deepcopy(_REGION_V1)
_REGION_V2['properties']['target']['enum'] += ['receiver','caster']
_ROUGH_REGION=copy.deepcopy(_REGION_V1)
_ROUGH_REGION['properties']['target']=const('roughness')
_ROUGH_REGION['properties']['operation']=const('set')
_ROUGH_REGION['properties']['value']=_N(.02,1)
_ROUGH_REGION['required'].append('value')
LOCAL_EDITS_SCHEMA={'oneOf': [
    obj({'version':const('relight-local-edits-v1'),'regions':{'type':'array','maxItems':8,'items':_REGION_V1}}),
    obj({'version':const('relight-local-edits-v2'),'regions':{'type':'array','maxItems':8,'items':{'oneOf':[_REGION_V2,_ROUGH_REGION]}}})]}
LOCAL_EDITS_SCHEMA['$comment']='normalize enforces unique IDs and target/operation pairs. Full scalar domain and retained normal validity are runtime checks. Rotation uses full image pixel coordinates.'
PARAMETERS['properties']['local_edits']=LOCAL_EDITS_SCHEMA
IDENTITY_FIELDS=('session_id','incarnation_id','cache_id')
MESSAGE_FIELDS=set(('schema',*IDENTITY_FIELDS,'revision','parameters','parameters_sha256','reason'))

def normalize_params(value):
    base._finite_tree(value);validate(value,PARAMS_SCHEMA);p=copy.deepcopy(value);p['relight']=relight_normalize(p['relight'])
    need(p['relight']['compute']['device']=='CUDA0_FP32','Hot session requires explicit fixed CUDA0_FP32; no fallback')
    need(p['idle_seconds']<=p['lifetime_seconds'],'Idle exceeds lifetime')
    return p

def params_schema():return copy.deepcopy(PARAMS_SCHEMA)

def normalized_parameters(value,initial_config):
    base._finite_tree(value);validate(value,PARAMETERS)
    try:c=kernel_normalize(value['config'])
    except (TypeError,ValueError,KeyError) as exc:raise DepthError('INVALID_REQUEST',str(exc)) from exc
    need(c['frame']==initial_config['frame'],'Session frame cannot change','CONFLICT')
    need(c['quality']['max_work']<=initial_config['quality']['max_work'],'Work cap cannot increase','RESOURCE_LIMIT')
    result={'config':c,'kind':value['kind']}
    if 'local_edits' in value:
        try:result['local_edits']=local_edits.normalize(value['local_edits'])
        except (ValueError,TypeError,KeyError,OverflowError) as exc:raise DepthError('INVALID_REQUEST',str(exc)) from exc
    return result

def check_message(value,ready,initial_config):
    base._finite_tree(value)
    need(type(value) is dict and set(value)==MESSAGE_FIELDS and value['schema']=='relight-session-message-v1','Invalid session message shape')
    need(all(type(value[k]) is str and value[k]==ready[k] for k in IDENTITY_FIELDS),'Alien session/cache/incarnation','CONFLICT')
    need(type(value['revision']) is int and 1<=value['revision']<=2147483647,'Exact positive revision required')
    need(type(value['reason']) is str and 0<len(value['reason'])<=64,'Reason required')
    p=normalized_parameters(value['parameters'],initial_config)
    need(type(value['parameters_sha256']) is str and re.fullmatch('[0-9a-f]{64}',value['parameters_sha256']) and
         signature(p)==value['parameters_sha256'],'Immutable parameter SHA differs','CONFLICT')
    return p

def preview_domain(domain,divisor):
    need(domain['width']%divisor==domain['height']%divisor==0,'Explicit preview divisor must preserve integral domain','UNSUPPORTED')
    result=copy.deepcopy(domain);result['width']//=divisor;result['height']//=divisor
    need(result['width']>=4 and result['height']>=4,'Preview domain too small')
    return result

def preview_config(config,divisor):
    out=copy.deepcopy(config)
    if divisor>1 and out['frame']['kind']=='metric_perspective':
        for key in ('fx','fy'):out['frame'][key]/=divisor
        # nearest tie samples d*i+d/2+0.5, not d*(i+0.5).
        for key in ('cx','cy'):out['frame'][key]=(out['frame'][key]-.5)/divisor
    if divisor>1 and 'anime' in out:
        out['anime']['rim_width']/=divisor
    return out
