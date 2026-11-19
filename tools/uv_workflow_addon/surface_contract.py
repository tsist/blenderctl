"""Explicit saved-disk evaluation sampling; independent of base-mesh approval."""
import copy
import re
from .contract import fail


def normalize(value, names):
    required={'layers','frames','modes','triangle_limit','usage'}
    if not isinstance(value,dict) or set(value)-required-{'policy','candidate'} or not required<=set(value):
        fail('surface requires layers, frames, modes, triangle_limit and usage')
    v=copy.deepcopy(value)
    if not isinstance(v['layers'],dict) or set(v['layers'])!=set(names) or any(not isinstance(x,str) or not x for x in v['layers'].values()):
        fail('surface.layers must name one existing UV layer for every target')
    frames=v['frames'];modes=v['modes']
    if not isinstance(frames,list) or not 1<=len(frames)<=8 or any(type(f) is not int or not -1048574<=f<=1048574 for f in frames) or len(set(frames))!=len(frames):
        fail('surface.frames requires 1 to 8 distinct integer frames')
    if not isinstance(modes,list) or not 1<=len(modes)<=2 or any(m not in ('VIEWPORT','RENDER') for m in modes) or len(set(modes))!=len(modes):
        fail('surface.modes must explicitly select VIEWPORT and/or RENDER')
    if type(v['triangle_limit']) is not int or not 1<=v['triangle_limit']<=2000000:
        fail('surface.triangle_limit must be from 1 to 2000000', 'RESOURCE_LIMIT')
    if v['usage'] not in ('EXCLUSIVE','CANDIDATE_EXACT'):
        fail('surface.usage must explicitly select EXCLUSIVE or CANDIDATE_EXACT','UNSUPPORTED')
    if v['usage']=='CANDIDATE_EXACT':
        if not isinstance(v.get('candidate'),str) or re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,47}',v['candidate']) is None:
            fail('CANDIDATE_EXACT requires a candidate short ASCII identifier')
    elif 'candidate' in v:
        fail('surface.candidate requires explicit CANDIDATE_EXACT usage')
    if 'policy' in v:
        from .recipes import normalize_policy
        v['policy']=normalize_policy(v['policy'])
        if v['policy']['density_basis']!='ISLAND_AREA_REFERENCE':
            fail('Evaluated surface cannot infer configured base-island density correspondence','UNSUPPORTED')
    return v
