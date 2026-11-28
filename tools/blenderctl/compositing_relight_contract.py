"""Strict single-batch supplemental light contract; no numeric/model/scene imports."""
import copy
import ntpath
import re
from compositing_depth_contract import DepthError, loads_strict, signature, _module as base
from compositing_relight_kernel import normalize as normalize_config

obj = base._obj
const = base._const
FILE = base.FILE
NUMBER = {'type': 'number'}
V3 = {'type': 'array', 'items': NUMBER, 'minItems': 3, 'maxItems': 3}
NORMALIZED = {'type': 'number', 'minimum': 0, 'maximum': 1}
common = {'id': {'type': 'string', 'minLength': 1, 'maxLength': 64},
          'type': {'enum': ['POINT', 'SPOT', 'AREA', 'SUN']},
          'intensity': NUMBER, 'color': V3, 'diffuse': NUMBER, 'specular': NUMBER,
          'samples': {'type': 'integer', 'enum': [1, 4, 8, 16, 32]}}
point = {'position': V3, 'radius': NUMBER, 'softening': NUMBER, 'reference_distance': NUMBER}
light = {'oneOf': [
    obj(common | point | {'type': const('POINT')}),
    obj(common | point | {'type': const('SPOT'), 'aim': V3, 'cone_deg': NUMBER, 'blend': NORMALIZED}),
    obj(common | {k:v for k,v in point.items() if k!='radius'} |
        {'type': const('AREA'), 'aim': V3, 'shape': {'enum': ['SQUARE','RECTANGLE','DISK','ELLIPSE']}, 'size': NUMBER, 'size_y': NUMBER}),
    obj(common | {'type': const('SUN'), 'direction': V3, 'anchor': V3, 'angle_deg': NUMBER}),
]}
_CONFIG_V1 = obj({'version': const('relight-kernel-draft-0.1'),
    'frame': {'oneOf': [obj({'kind': const('screen_heightfield'), 'height_scale': NUMBER}),
                        obj({'kind': const('metric_perspective'), 'fx': NUMBER, 'fy': NUMBER, 'cx': NUMBER, 'cy': NUMBER})]},
    'lights': {'type': 'array', 'items': light, 'maxItems': 8}, 'mix': NORMALIZED,
    'bypass': {'type': 'boolean'}, 'material': obj({'roughness': NUMBER, 'f0': V3}),
    'line_protect': NORMALIZED,
    'quality': obj({'shadow_steps': {'type': 'integer','minimum':0,'maximum':64},
                    'shadow_bias': NUMBER, 'sun_reach': NUMBER,
                    'max_work': {'type':'integer','minimum':1,'maximum':2147483647}})})
PROJECTION = obj({'enabled': {'type': 'boolean'}, 'point': V3, 'normal': V3,
    'strength': NORMALIZED,
    'transmission': {'type':'array','items':NORMALIZED,'minItems':3,'maxItems':3},
    'light_ids': {'type':'array','items':{'type':'string','minLength':1,'maxLength':64},
                  'maxItems':8,'uniqueItems':True}})
_CONFIG_V2 = copy.deepcopy(_CONFIG_V1)
_CONFIG_V2['properties']['version'] = const('relight-kernel-draft-0.2')
_CONFIG_V2['properties']['projection'] = PROJECTION
_CONFIG_V2['required'].append('projection')
BASE = obj({'exposure': NUMBER, 'contrast': NUMBER, 'saturation': NUMBER,
            'mask': {'enum': ['ALL', 'LIGHT']}})
_CONFIG_V3 = copy.deepcopy(_CONFIG_V2)
_CONFIG_V3['properties']['version'] = const('relight-kernel-draft-0.3')
_CONFIG_V3['properties']['base'] = BASE
_CONFIG_V3['required'].append('base')
ANIME = obj({
    'form': NORMALIZED, 'wrap': NUMBER, 'para_spread': NUMBER, 'back_dark': NORMALIZED,
    'back_span': NUMBER, 'dark_side_reach': NUMBER, 'shadow_lo': NORMALIZED, 'shadow_hi': NORMALIZED,
    'shadow_tint': NORMALIZED, 'shadow_color': V3, 'cel': NORMALIZED,
    'cel_steps': {'type':'integer','minimum':2,'maximum':8}, 'cel_soft': NORMALIZED,
    'rim_width': NUMBER, 'rim_edge0': NORMALIZED, 'rim_edge1': NORMALIZED,
    'rim_gain': NUMBER, 'rim_white_mix': NORMALIZED,
    'rim_light_ids': {'type':'array','items':{'type':'string','minLength':1,'maxLength':64},'maxItems':8,'uniqueItems':True},
    'line_core_lo': NORMALIZED, 'line_core_hi': NORMALIZED,
    'solo_light_id': {'type':'string','maxLength':64},
    'subject_source': {'enum':['LIGHT_MASK','DEPTH_VALID','ALPHA']},
    'view': {'enum':['RESULT','PARA','FORM','DARK_SIDE','RIM','LINE','DELTA','DEPTH','NORMAL','VALIDITY']},
    'delta_gain': NUMBER})
ENVIRONMENT = obj({'subject_tint': V3, 'subject_exposure': NUMBER,
                   'background_tint': V3, 'background_exposure': NUMBER})
_CONFIG_V4 = copy.deepcopy(_CONFIG_V3)
_CONFIG_V4['properties']['version'] = const('relight-kernel-draft-0.4')
_CONFIG_V4['properties']['anime'] = ANIME
_CONFIG_V4['properties']['environment'] = ENVIRONMENT
_CONFIG_V4['required'].append('anime')
CONFIG = {'oneOf': [_CONFIG_V1, _CONFIG_V2, _CONFIG_V3, _CONFIG_V4]}
SOURCE = {'oneOf': [obj({'kind': const('bundle_RGB')}),
    obj({'kind': const('scene_linear_straight_override'), 'image': FILE,
         'colorspace': const('scene_linear_Rec709'),
         'format': {'enum': ['NPY','EXR']}, 'parent_RGB': FILE,
         'statement': base.STATEMENT})]}
MASK = {'anyOf': [{'type': 'null'}, FILE]}
PARAMS_SCHEMA = obj({'depth_relight_version': const('1.0'), 'bundle': FILE,
    'source': SOURCE, 'masks': obj({'light': MASK, 'highlight': MASK, 'line': MASK}),
    'config': CONFIG, 'compute': {'oneOf': [obj({'device':const('CPU_NUMPY')}),
                                         obj({'device':const('CUDA0_FP32'), 'runtime_profile':FILE})]},
    'budget': obj({'max_pixels':{'type':'integer','minimum':1,'maximum':4194304},
                   'timeout_seconds':{'type':'integer','minimum':1,'maximum':1200}}),
    'resources': base.RESOURCES})
PARAMS_SCHEMA.update({'$schema':'http://json-schema.org/draft-07/schema#',
    'title':'compositing.depth-relight params v1.0',
    '$comment':'normalize also validates exact kernel ranges, Windows path/SHA coverage, work budget. Runtime checks successful parent, pixel domain and frame equality. No implicit resizing, fallback or geometric certification.'})

def need(condition, message, code='INVALID_REQUEST'):
    if not condition: raise DepthError(code, message)

def pathkey(path): return ntpath.normcase(ntpath.normpath(path))

def validate(value, schema, path='$'):
    # The old depth validator has no boolean type because its old schemas did
    # not need it. Keep that numeric identity unchanged; this consumer owns
    # the small exact schema subset it uses, including strict boolean fields.
    if 'const' in schema: need(base._equal(value,schema['const']), path+': const differs')
    if 'enum' in schema: need(any(base._equal(value,v) for v in schema['enum']), path+': enum differs')
    for kind in ('oneOf','anyOf'):
        if kind in schema:
            count=0
            for option in schema[kind]:
                try:validate(value,option,path);count+=1
                except DepthError:pass
            need(count==1 if kind=='oneOf' else count>=1,path+': '+kind+' differs')
    kind=schema.get('type')
    types={'object':lambda:type(value) is dict,'array':lambda:type(value) is list,
           'string':lambda:type(value) is str,'boolean':lambda:type(value) is bool,
           'number':lambda:type(value) in (float,int),'integer':lambda:type(value) is int,'null':lambda:value is None}
    if kind is not None:need(kind in types and types[kind](),path+': '+str(kind)+' required')
    if kind=='object':
        props=schema['properties'];need(all(k in value for k in schema.get('required',[])),path+': missing fields')
        if schema.get('additionalProperties') is False:need(set(value)<=set(props),path+': unknown fields')
        for k,v in value.items():
            if k in props:validate(v,props[k],path+'/'+k)
    if kind=='array':
        need(schema.get('minItems',0)<=len(value)<=schema.get('maxItems',100000),path+': array size')
        if schema.get('uniqueItems'):need(len({signature(v) for v in value})==len(value),path+': duplicate values')
        for i,v in enumerate(value):validate(v,schema.get('items',{}),path+'/'+str(i))
    if kind=='string':
        need(schema.get('minLength',0)<=len(value)<=schema.get('maxLength',16777216),path+': string size')
        if 'pattern' in schema:need(re.search(schema['pattern'],value) is not None,path+': pattern')
    if kind in ('number','integer'):
        for key,compare in [('minimum',lambda v,b:v>=b),('maximum',lambda v,b:v<=b),('exclusiveMinimum',lambda v,b:v>b)]:
            if key in schema:need(compare(value,schema[key]),path+': '+key)

def input_descriptors(value):
    if type(value) is dict:
        if set(value)=={'file','expected_sha256'}: return [value]
        return [r for k,v in value.items() if k!='resources' for r in input_descriptors(v)]
    if type(value) is list: return [r for v in value for r in input_descriptors(v)]
    return []

def normalize_params(value):
    base._finite_tree(value)
    validate(value, PARAMS_SCHEMA)
    result=copy.deepcopy(value)
    try: result['config']=normalize_config(result['config'])
    except (TypeError, ValueError, KeyError) as exc:
        raise DepthError('INVALID_REQUEST', 'Relight config: '+str(exc)) from exc
    descriptors=input_descriptors(result)
    expected={}
    for r in descriptors+result['resources']:
        k=pathkey(r['file'])
        need(ntpath.isabs(r['file']), 'Absolute path required')
        need(k not in expected or expected[k]==r['expected_sha256'], 'Conflicting path/SHA')
        expected[k]=r['expected_sha256']
    resources={pathkey(r['file']):r['expected_sha256'] for r in result['resources']}
    for r in descriptors: need(resources.get(pathkey(r['file']))==r['expected_sha256'], 'Direct input missing resource coverage')
    need(len(resources)==len(result['resources']), 'Duplicate resource path')
    return result

def params_schema(): return copy.deepcopy(PARAMS_SCHEMA)

def frame_for(bundle):
    original=bundle['request']['params'];normal=original['normal']
    if original['semantics']['measure']=='axial_z':
        need(normal['method'] in ('central_perspective', 'import_geometry'),
             'Axial bundle needs perspective derived/imported Normal', 'UNSUPPORTED')
        if normal['method'] == 'import_geometry':
            need(original['depth_prepare_version'] == '1.1' and
                 bundle.get('normal_frame') == 'camera_Xright_Yup_Zbackward',
                 'Imported Normal needs the explicit qualified camera frame', 'UNSUPPORTED')
        return {'kind':'metric_perspective',**{k:normal[k] for k in ('fx','fy','cx','cy')}}
    need(normal['method']=='central_heightfield', 'Relative bundle needs derived heightfield Normal', 'UNSUPPORTED')
    return {'kind':'screen_heightfield','height_scale':normal['height_scale']}

def admit_bundle(params, bundle):
    original=bundle['request']['params'];domain=original['domain'];pixels=domain['width']*domain['height']
    need(params['config']['frame']==frame_for(bundle), 'Light coordinate frame differs from bound Depth/Normal', 'CONFLICT')
    need(pixels<=params['budget']['max_pixels'], 'Original pixel domain exceeds request budget', 'RESOURCE_LIMIT')
    samples=sum(l['samples'] for l in params['config']['lights'])
    steps=params['config']['quality']['shadow_steps']
    projection=params['config'].get('projection',{})
    extra=sum(l['samples'] for l in params['config']['lights'] if l['intensity']>0 and
              l['id'] in projection.get('light_ids',[])) if projection.get('enabled') and not params['config']['bypass'] and params['config']['mix']>0 else 0
    need(pixels*(max(1,samples)+extra)*max(1,steps)<=params['config']['quality']['max_work'], 'Declared light work budget exceeded', 'RESOURCE_LIMIT')
    if params['source']['kind']!='bundle_RGB':
        need({pathkey(params['source']['parent_RGB']['file']):params['source']['parent_RGB']['expected_sha256']}==
             {pathkey(original['rgb']['file']):original['rgb']['expected_sha256']}, 'Override is bound to a different source RGB', 'CONFLICT')
    return domain
