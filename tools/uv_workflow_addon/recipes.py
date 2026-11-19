"""Purpose rules travel separately from their exact model/tile binding."""
import copy
import math
import re
from .contract import fail


def normalize_policy(policy):
    fields = {'name', 'purpose', 'reference', 'density_basis', 'stretch_p95_max', 'stretch_max',
              'density_relative_max', 'border_px_min', 'gap_px_min'}
    if not isinstance(policy, dict) or set(policy) != fields:
        fail('Unexpected or missing recipe policy fields')
    for key in ('name', 'purpose', 'reference'):
        if not isinstance(policy[key], str) or not policy[key].strip() or len(policy[key]) > 1024:
            fail('Recipe '+key+' must be nonempty text up to 1024 characters')
    if policy['density_basis'] not in ('ISLAND_AREA_REFERENCE','CONFIGURED_TARGETS'):
        fail('Purpose density_basis must be ISLAND_AREA_REFERENCE or CONFIGURED_TARGETS')
    for key in fields - {'name', 'purpose', 'reference', 'density_basis'}:
        if type(policy[key]) not in (int, float) or not math.isfinite(policy[key]) or policy[key] < 0:
            fail('Recipe '+key+' must be finite and nonnegative')
    if not 1 <= policy['stretch_p95_max'] <= policy['stretch_max']:
        fail('Recipe stretch limits must satisfy 1 <= p95 <= max')
    if policy['density_relative_max'] > 1:
        fail('Recipe density relative deviation must be between 0 and 1')
    return copy.deepcopy(policy)


def normalize_recipe(value, names):
    if not isinstance(value, dict) or set(value)-{'version','policy','binding','usage_binding'} or not {'version','policy','binding'}<=set(value) or value['version'] != '1.0':
        fail('recipe requires version=1.0, policy and binding')
    if 'usage_binding' in value and (not isinstance(value['usage_binding'],str) or re.fullmatch('[0-9a-f]{64}',value['usage_binding']) is None):fail('Recipe usage_binding must be SHA-256')
    policy = normalize_policy(value['policy'])
    binding = value['binding']
    if not isinstance(binding, dict) or set(binding) != {'geometry', 'geometry_basis', 'unit_scale', 'width', 'height'}:
        fail('Recipe binding requires geometry, geometry_basis, unit_scale, width and height')
    if binding['geometry_basis'] != 'BASE_MESH':fail('Recipe geometry_basis must be BASE_MESH')
    hashes = binding['geometry']
    if not isinstance(hashes, dict) or set(hashes) != set(names) or any(
            not isinstance(h, str) or re.fullmatch('[0-9a-f]{64}', h) is None for h in hashes.values()):
        fail('Recipe geometry binding must cover the exact target objects')
    if type(binding['unit_scale']) not in (int, float) or not math.isfinite(binding['unit_scale']) or binding['unit_scale'] <= 0:
        fail('Recipe unit_scale must be finite and positive')
    for key in ('width', 'height'):
        if type(binding[key]) is not int or not 1 <= binding[key] <= 16384:
            fail('Recipe tile dimensions must be integers from 1 to 16384')
    if 2 * policy['border_px_min'] >= min(binding['width'], binding['height']):
        fail('Recipe border leaves no usable tile')
    return copy.deepcopy(value)


def binding(objects, manifest, unit):
    from . import core
    return {'geometry': {o.name: core.geometry(o) for o in objects}, 'geometry_basis':'BASE_MESH', 'unit_scale': unit,
            'width': manifest['width'], 'height': manifest['height']}


def require_binding(recipe, current):
    if recipe['binding'] != current:
        fail('Purpose recipe does not match this model, world transform, unit scale or tile; explicitly review and rebind its policy', 'CONFLICT')


def usage_binding(settings,basis):
    from .core import _hash
    value={'mode':settings['mode'] if settings else 'EXCLUSIVE','reuse_groups':settings['reuse_groups'] if settings else []}
    if basis=='CONFIGURED_TARGETS':value['density']=settings['density'] if settings else None
    return _hash(value)


def evaluate(q, recipe, current, settings=None):
    """Only numeric evidence; never carries forward a visual acceptance."""
    q['purpose_checks'] = {'status': 'not_assigned', 'visual': 'not_reviewed'}
    if recipe is None:
        return
    policy = recipe['policy']
    checks = {'status': 'unknown', 'name': policy['name'], 'purpose': policy['purpose'],
              'reference': policy['reference'], 'binding': 'match' if recipe['binding'] == current else 'mismatch',
              'violations': [], 'unknown': [], 'visual': 'not_reviewed',
              'reference_semantics': 'caller-supplied basis, not an authenticated approval', 'values': {},
              'measurement_surface':'base_mesh','evaluated_surface':'not_checked','density_basis':policy['density_basis']}
    if checks['binding'] != 'match':
        checks['unknown'].append('binding')
    expected=recipe.get('usage_binding',usage_binding(None,policy['density_basis']))
    checks['usage_binding']='match' if expected==usage_binding(settings,policy['density_basis']) else 'mismatch'
    if checks['usage_binding']!='match':checks['unknown'].append('usage_binding')
    from .policies import density_value
    density,density_complete=density_value(q,policy['density_basis'])
    specs = [('stretch_p95_max', q['anisotropy']['area_p95'], q['anisotropy']['complete'], 'max', 0),
             ('stretch_max', q['anisotropy']['max'], q['anisotropy']['complete'], 'max', 0),
             ('density_relative_max', density, density_complete, 'max', 0),
             ('border_px_min', q['boundary_margin_px'], q['boundary_margin_px'] is not None, 'min', .001),
             ('gap_px_min', q['gap']['min_px'], q['gap']['complete'], 'min', .001)]
    for key, actual, complete, comparison, tolerance in specs:
        not_applicable = (key == 'gap_px_min' and complete and q['gap'].get('reason') in ('no_distinct_islands','no_distinct_layout_units')) or (key in ('gap_px_min','border_px_min') and q.get('usage_policy',{}).get('mode')=='TILED')
        checks['values'][key] = {'actual': actual, 'limit': policy[key], 'status': 'not_applicable' if not_applicable else 'checked'}
        if not_applicable:
            continue
        if not complete or actual is None or not math.isfinite(actual):
            checks['unknown'].append(key)
        elif (comparison == 'max' and actual > policy[key]) or (comparison == 'min' and actual + tolerance < policy[key]):
            checks['violations'].append(key)
    checks['status'] = 'fail' if checks['violations'] else 'unknown' if checks['unknown'] else 'pass'
    q['purpose_checks'] = checks
    q['purpose_stretch_threshold'] = {'area_p95_max': policy['stretch_p95_max'], 'max': policy['stretch_max']}
    if checks['status'] == 'fail':
        q['status'] = 'fail'
    elif checks['status'] == 'unknown' and q['status'] != 'fail':
        q['status'] = 'unknown'
