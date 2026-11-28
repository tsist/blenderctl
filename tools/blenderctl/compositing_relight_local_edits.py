"""Bounded full-resolution local recipes. Stdlib contract, explicit NumPy math.

Relative scalars retain near-one meaning; axial scalars retain scene units.
No percentile renormalization, clamping, file, model, or GPU operations.
"""
import copy
import hashlib
import json
import math

LEGACY_VERSION = 'relight-local-edits-v1'
VERSION = 'relight-local-edits-v2'
FIELDS = {'id', 'enabled', 'target', 'operation', 'shape', 'center', 'size',
          'rotation', 'feather', 'strength', 'scale', 'offset'}


def _need(condition, text):
    if not condition:
        raise ValueError(text)


def _number(value, low, high, name):
    try:
        good = type(value) in (int, float) and math.isfinite(value) and low <= value <= high
    except OverflowError:
        good = False
    _need(good, 'Invalid finite number/range: ' + name)
    return float(value)


def empty(version=VERSION):
    _need(type(version) is str and version in (LEGACY_VERSION, VERSION), 'Recipe version differs')
    return {'version': version, 'regions': []}


def is_empty(recipe):
    """Recognize either validated version's exact empty recipe."""
    return not normalize(recipe)['regions']


def normalize(recipe):
    _need(type(recipe) is dict and set(recipe) == {'version', 'regions'}, 'Recipe fields differ')
    _need(type(recipe['version']) is str and recipe['version'] in (LEGACY_VERSION, VERSION) and type(recipe['regions']) is list
          and len(recipe['regions']) <= 8, 'Recipe version/count differs')
    result = copy.deepcopy(recipe)
    ids = set()
    for region in result['regions']:
        _need(type(region) is dict, 'Region must be object')
        fields = FIELDS | {'value'} if result['version'] == VERSION and region.get('target') == 'roughness' else FIELDS
        _need(set(region) == fields, 'Region fields differ')
        ident = region['id']
        _need(type(ident) is str and 0 < len(ident) <= 64 and ident not in ids, 'Unique region id required')
        ids.add(ident)
        _need(type(region['enabled']) is bool, 'Enabled must be bool')
        allowed = {'light': ('restrict', 'attenuate'), 'highlight': ('restrict', 'attenuate'),
                   'line': ('add', 'attenuate'), 'depth': ('affine',)}
        if result['version'] == VERSION:
            allowed.update(roughness=('set',), receiver=('add', 'attenuate'), caster=('restrict', 'attenuate'))
        _need(type(region['target']) is str and region['target'] in allowed
              and type(region['operation']) is str and region['operation'] in allowed[region['target']], 'Target/operation mismatch')
        _need(type(region['shape']) is str and region['shape'] in ('ellipse', 'box'), 'Unknown region shape')
        for name, low, high in (('center', -2, 3), ('size', .001, 4)):
            value = region[name]
            _need(type(value) is list and len(value) == 2, 'Expected vector2: ' + name)
            region[name] = [_number(v, low, high, name) for v in value]
        for name, low, high in (('rotation', -2*math.pi, 2*math.pi), ('feather', 0, 1),
                                ('strength', 0, 1), ('scale', .01, 10), ('offset', -10, 10)):
            region[name] = _number(region[name], low, high, name)
        if region['target'] == 'roughness':
            region['value'] = _number(region['value'], .02, 1, 'value')
    return result


def signature(recipe):
    payload = json.dumps(normalize(recipe), sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, allow_nan=False).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def derived_id(base_cache, recipe):
    _need(type(base_cache) is str and 0 < len(base_cache) <= 256, 'Base cache identity required')
    payload = json.dumps({'base_cache': base_cache, 'recipe_sha256': signature(recipe)},
                         sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _mask(shape, region, np):
    h, w = shape
    y, x = np.indices(shape, dtype=np.float64)
    dx = x + .5 - region['center'][0] * w
    dy = y + .5 - region['center'][1] * h
    c, s = math.cos(region['rotation']), math.sin(region['rotation'])
    u = (c*dx + s*dy) / (region['size'][0]*w*.5)
    v = (-s*dx + c*dy) / (region['size'][1]*h*.5)
    radius = np.sqrt(u*u + v*v) if region['shape'] == 'ellipse' else np.maximum(np.abs(u), np.abs(v))
    if region['feather'] == 0:
        return (radius <= 1).astype(np.float64)
    t = np.clip((1-radius)/region['feather'], 0, 1)
    return t*t*(3-2*t)


def _normal_update(scalar, valid, old_normal_valid, changed, frame, options, normal, normal_valid, np):
    h, w = scalar.shape
    if options.get('method') == 'import_geometry':
        _need(options.get('depth_edit_policy') == 'rederive_affected_from_axial_z'
              and frame['kind'] == 'metric_perspective', 'Imported Normal requires explicit axial edit policy')
        _need(all(type(options.get(k)) in (int, float) and not isinstance(options[k], bool)
                  and options[k] == frame[k] for k in ('fx', 'fy', 'cx', 'cy')), 'Imported Normal K differs from metric frame')
    update = changed.copy()
    update[1:, :] |= changed[:-1, :]
    update[:-1, :] |= changed[1:, :]
    update[:, 1:] |= changed[:, :-1]
    update[:, :-1] |= changed[:, 1:]
    if not update.any():
        return 0
    normal[update] = 0
    normal_valid[update] = False
    center = scalar[1:-1, 1:-1].astype(np.float64)
    neighbors = [scalar[1:-1, :-2].astype(np.float64), scalar[1:-1, 2:].astype(np.float64),
                 scalar[:-2, 1:-1].astype(np.float64), scalar[2:, 1:-1].astype(np.float64)]
    core = old_normal_valid[1:-1, 1:-1] & valid[1:-1, 1:-1] & update[1:-1, 1:-1]
    for v in (valid[1:-1, :-2], valid[1:-1, 2:], valid[:-2, 1:-1], valid[2:, 1:-1]):
        core &= v
    if frame['kind'] == 'screen_heightfield':
        strength = _number(frame['height_scale'], .0001, 10, 'height_scale')
        jump = _number(options['max_height_jump'], 0.000000000001, 1, 'max_height_jump')
        for neighbor in neighbors:
            core &= np.abs(strength*(neighbor-center)) <= jump
        hx = strength*(neighbors[1]-neighbors[0])*h*.5
        hy = -strength*(neighbors[3]-neighbors[2])*h*.5
        vectors = np.stack((-hx, -hy, np.ones_like(hx)), axis=-1)
    else:
        jump = _number(options['max_relative_jump'], .000000000001, 1, 'max_relative_jump')
        for neighbor in neighbors:
            relative = np.full(center.shape, np.inf)
            denominator = np.maximum(np.abs(neighbor), np.abs(center))
            np.divide(np.abs(neighbor-center), denominator, out=relative, where=denominator > 0)
            core &= relative <= jump
        fx = _number(frame['fx'], .001, 100000, 'fx')
        fy = _number(frame['fy'], .001, 100000, 'fy')
        cx = _number(frame['cx'], -100000, 100000, 'cx')
        cy = _number(frame['cy'], -100000, 100000, 'cy')
        y, x = np.indices(scalar.shape, dtype=np.float64)
        if options.get('method') == 'import_geometry':
            # Match preparation's operation order, preserving legacy numerics below.
            points = np.stack(((x+.5-cx)/fx*scalar, (cy-y-.5)/fy*scalar, -scalar), axis=-1)
        else:
            points = np.stack(((x+.5-cx)*scalar/fx, (cy-y-.5)*scalar/fy, -scalar), axis=-1)
        tu = (points[1:-1, 2:] - points[1:-1, :-2])*.5
        tv = (points[2:, 1:-1] - points[:-2, 1:-1])*.5
        vectors = np.cross(tv, tu)
        facing = np.sum(vectors * -points[1:-1, 1:-1], axis=-1)
        vectors[facing < 0] *= -1
        core &= np.isfinite(facing) & (facing != 0)
    length = np.linalg.norm(vectors, axis=-1)
    core &= np.isfinite(vectors).all(axis=-1) & np.isfinite(length) & (length > 0)
    computed = np.zeros(vectors.shape, dtype=np.float64)
    np.divide(vectors, length[..., None], out=computed, where=core[..., None])
    normal[1:-1, 1:-1][core] = computed[core].astype(np.float32)
    normal_valid[1:-1, 1:-1][core] = True
    return int(update.sum())


def apply(arrays, recipe, frame, normal_options, np):
    """Return eight (v1) or twelve (v2) new contiguous arrays and diagnostics.

    Rotation uses the actual pixel plane before ellipse/box axis normalization.
    Rebuild only actual changed scalar's center/four-neighbor footprint, and
    never promote a previously invalid normal to valid.
    """
    recipe = normalize(recipe)
    _need(type(arrays) in (list, tuple) and len(arrays) == 8, 'Exactly eight prepared arrays required')
    rgba, scalar, normal, valid, normal_valid, light, highlight, line = arrays
    _need(isinstance(scalar, np.ndarray) and scalar.ndim == 2, 'Scalar HW required')
    shape = scalar.shape
    _need(4 <= min(shape) and max(shape) <= 2048, 'Bounded domain required')
    for a, expected, dtype in zip(arrays, (shape+(4,), shape, shape+(3,), shape, shape, shape, shape, shape),
                                  (np.float32, np.float32, np.float32, np.bool_, np.bool_, np.float32, np.float32, np.float32)):
        _need(isinstance(a, np.ndarray) and a.shape == expected and a.dtype == dtype
              and np.isfinite(a).all(), 'Array shape/dtype/finite mismatch')
    _need((~normal_valid | valid).all(), 'Normal-valid must be subset of depth-valid')
    for a in (light, highlight, line):
        _need(((a >= 0) & (a <= 1)).all(), 'Mask must be 0..1')
    _need(type(frame) is dict and frame.get('kind') in ('screen_heightfield', 'metric_perspective'), 'Frame required')
    _need(type(normal_options) is dict, 'Normal options required')
    imported = normal_options.get('method') == 'import_geometry'
    if imported:
        _need(normal_options.get('depth_edit_policy') == 'rederive_affected_from_axial_z'
              and frame['kind'] == 'metric_perspective', 'Imported Normal requires metric rederive policy')
        _need(all(type(normal_options.get(k)) in (int, float) and not isinstance(normal_options[k], bool)
                  and normal_options[k] == frame[k] for k in ('fx', 'fy', 'cx', 'cy')), 'Imported Normal K differs from metric frame')
    working = scalar.astype(np.float64)
    out = [np.array(a, copy=True, order='C') for a in arrays]
    summary = {'version': recipe['version'], 'recipe_sha256': signature(recipe), 'regions': [],
               'source_arrays_modified': False, 'clamped': False}
    targets = {'light': 5, 'highlight': 6, 'line': 7}
    if recipe['version'] == VERSION:
        out.extend((np.ones(shape, dtype=np.float32), np.zeros(shape, dtype=np.float32),
                    np.zeros(shape, dtype=np.float32), valid.astype(np.float32, copy=True)))
        targets.update(receiver=10, caster=11)
    for r in recipe['regions']:
        info = {'id': r['id'], 'target': r['target'], 'enabled': r['enabled']}
        if not r['enabled'] or r['strength'] == 0:
            info['support_count'] = 0
            summary['regions'].append(info)
            continue
        mask = _mask(shape, r, np)
        weight = r['strength']*mask
        info.update(support_count=int((mask > 0).sum()), full_count=int((mask == 1).sum()),
                    mask_min=float(mask.min()), mask_max=float(mask.max()))
        if r['target'] == 'depth':
            values = working[valid]
            w = weight[valid]
            with np.errstate(over='ignore', invalid='ignore'):
                working[valid] = values*(1-w) + w*(values*r['scale']+r['offset'])
            _need(np.isfinite(working[valid]).all(), 'Depth affine overflow')
            if frame['kind'] == 'screen_heightfield':
                _need(((working[valid] >= 0) & (working[valid] <= 1)).all(), 'Edited near-one outside0..1; no clamp')
            else:
                _need((working[valid] > 0).all(), 'Edited metric depth must remain positive')
        elif r['target'] == 'roughness':
            # Ordered convex assignments are independent of global roughness.
            # Reapplying always starts from base, never the previous generation.
            out[8] = np.ascontiguousarray(out[8].astype(np.float64)*(1-weight), dtype=np.float32)
            out[9] = np.ascontiguousarray(out[9].astype(np.float64)*(1-weight)+r['value']*weight, dtype=np.float32)
        else:
            idx = targets[r['target']]
            old = out[idx].astype(np.float64)
            if r['operation'] == 'restrict':
                edited = old*(1-r['strength']+weight)
            elif r['operation'] == 'attenuate':
                edited = old*(1-weight)
            else:
                edited = np.maximum(old, weight)
            out[idx] = np.ascontiguousarray(edited, dtype=np.float32)
        summary['regions'].append(info)
    with np.errstate(over='ignore', invalid='ignore'):
        out[1] = np.ascontiguousarray(working, dtype=np.float32)
    _need(np.isfinite(out[1]).all(), 'Prepared scalar float32 overflow')
    if frame['kind'] == 'metric_perspective':
        _need((out[1][valid] > 0).all(), 'Prepared metric underflow/nonpositive')
    else:
        _need(((out[1][valid] >= 0) & (out[1][valid] <= 1)).all(), 'Prepared near-one outside0..1')
    changed = out[1] != scalar
    if imported:
        changed &= valid
    summary['changed_scalar_count'] = int(changed.sum())
    summary['normal_update_count'] = _normal_update(out[1], valid, normal_valid, changed, frame,
                                                   normal_options, out[2], out[4], np)
    summary['changed_mask_counts'] = {name: int((out[index] != arrays[index]).sum()) for name, index in targets.items() if index < 8}
    if recipe['version'] == VERSION:
        summary['changed_mask_counts'].update(receiver=int((out[10] != 0).sum()),
                                               caster=int((out[11] != valid.astype(np.float32)).sum()))
        summary['roughness_support_count'] = int(((out[8] != 1) | (out[9] != 0)).sum())
    summary['normal_valid_count'] = int(out[4].sum())
    if imported:
        affected = changed.copy()
        affected[1:, :] |= changed[:-1, :]
        affected[:-1, :] |= changed[1:, :]
        affected[:, 1:] |= changed[:, :-1]
        affected[:, :-1] |= changed[:, 1:]
        summary['normal_import'] = {
            'frame': 'camera_Xright_Yup_Zbackward', 'source_space': normal_options['space'],
            'surface_relation': normal_options['surface_relation'], 'producer_verified': False,
            'depth_edit_policy': normal_options['depth_edit_policy'],
            'changed_count': int(changed.sum()), 'affected_count': int(affected.sum()),
            'rederived_count': int((affected & out[4]).sum()),
            'invalidated_count': int((affected & normal_valid & ~out[4]).sum()),
            'origin': 'hybrid_imported_and_axial_derived' if affected.any() else 'unchanged_bound_normal',
            'shader_micro_normal_preserved_in_affected_domain': False if affected.any() else None,
        }
    _need((~out[4] | normal_valid).all(), 'Normal-valid promotion forbidden')
    return out, summary
