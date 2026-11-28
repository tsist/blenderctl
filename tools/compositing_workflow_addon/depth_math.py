"""Pure NumPy CPU preparation; geometry and artistic heightfields stay distinct."""
import copy
import math

import numpy as np


class MathError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _need(condition, code, message):
    if not condition:
        raise MathError(code, message)


def _number(value, name, low=None, high=None, positive=False):
    try:
        finite = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        finite = False
    _need(finite, 'parameter_type', name + ' must be a finite JSON number')
    _need((not positive or value > 0) and (low is None or value >= low) and
          (high is None or value <= high), 'parameter_range', name + ' outside domain')
    return float(value)


def _object(value, keys, name):
    _need(type(value) is dict and set(value) == set(keys), 'parameter_fields', name + ' fields differ')


def _binary(array, shape, name):
    _need(isinstance(array, np.ndarray) and array.shape == shape and
          array.dtype in (np.dtype('bool'), np.dtype('float32')), 'mask_type', name + ' must be bool or float32 HW')
    _need(bool(np.isfinite(array).all()) and bool(np.isin(array, (0, 1)).all()), 'mask_values', name + ' must be finite binary')
    return np.array(array, dtype=bool, order='C', copy=True)


def _stats(array, valid):
    values = array[valid].astype(np.float64)
    return {'valid_count': int(values.size), 'invalid_count': int(array.size - values.size),
            'min': float(values.min()), 'max': float(values.max()), 'mean': float(values.mean())}


def prepare_arrays(raw, valid, params, edit_masks, boundary_exclusion=None, *,
                   imported_normal=None, imported_normal_validity=None):
    """Return new arrays; never modify raw, masks, or the request dictionaries.

    params is the complete depth-prepare request. Host file/provenance validation
    is separate; this function checks the numerical subcontracts it consumes.
    """
    _need(type(params) is dict, 'parameter_type', 'params must be an object')
    _need(isinstance(raw, np.ndarray) and raw.ndim == 2 and raw.dtype == np.dtype('float32')
          and raw.flags.c_contiguous, 'raw_type', 'raw must be float32 C-contiguous HW')
    shape = raw.shape
    domain = params.get('domain')
    _object(domain, ('width', 'height', 'pixel_aspect', 'origin', 'pixel_center', 'row_direction', 'column_direction'), 'domain')
    for field, actual in (('height', shape[0]), ('width', shape[1])):
        _need(type(domain[field]) is int and 4 <= domain[field] <= 2048 and domain[field] == actual,
              'domain', 'Declared dimensions differ from raw')
    _need(type(domain['pixel_aspect']) is list and type(domain['pixel_center']) is list
          and domain['pixel_aspect'] == [1, 1] and domain['pixel_center'] == [.5, .5]
          and all(type(v) in (int, float) and not isinstance(v, bool) for v in domain['pixel_aspect'] + domain['pixel_center'])
          and domain['origin'] == 'top-left' and domain['row_direction'] == 'down'
          and domain['column_direction'] == 'right', 'domain', 'Unsupported pixel domain')
    budget = params.get('budget')
    _need(type(budget) is dict and type(budget.get('max_pixels')) is int
          and 0 < budget['max_pixels'] <= 4194304 and raw.size <= budget['max_pixels'], 'budget', 'Pixel budget exceeded')
    validity = _binary(valid, shape, 'validity')
    _need(bool(validity.any()), 'empty_validity', 'No valid samples')
    _need(bool(np.isfinite(raw[validity]).all()), 'nonfinite_valid', 'Valid raw samples must be finite')
    semantics = params.get('semantics')
    _object(semantics, ('measure', 'near_order', 'units'), 'semantics')
    measure = semantics['measure']
    norm = params.get('normalization')
    normal = params.get('normal')
    _need(type(norm) is dict and type(normal) is dict, 'parameter_type', 'normalization/normal objects required')
    method = normal.get('method')
    if method != 'import_geometry':
        _need(imported_normal is None and imported_normal_validity is None, 'normal_input', 'Unused imported Normal arrays supplied')
    if measure == 'axial_z':
        _need(semantics['near_order'] == 'smaller_nearer' and semantics['units'] == 'scene_unit', 'semantics', 'Axial Z requires positive scene units, smaller nearer')
        _object(norm, ('method',), 'normalization')
        _need(norm['method'] == 'none' and method in ('none', 'central_perspective', 'import_geometry'), 'semantics', 'Axial Z cannot use heightfield normalization')
        _need(bool((raw[validity] > 0).all()), 'nonpositive_depth', 'Valid axial depth must be positive')
    elif measure == 'relative_score':
        _need(semantics['near_order'] in ('smaller_nearer', 'larger_nearer') and semantics['units'] == 'unitless', 'semantics', 'Relative score requires declared direction and unitless units')
        _object(norm, ('method', 'low', 'high'), 'normalization')
        _need(norm['method'] == 'percentile_near_one' and method in ('none', 'central_heightfield'), 'semantics', 'Relative score requires percentile near-height normalization')
        low = _number(norm['low'], 'low', 0, 100)
        high = _number(norm['high'], 'high', 0, 100)
        _need(low < high, 'parameter_range', 'low must be less than high')
    else:
        raise MathError('semantics', 'Unknown depth measure')
    stage = {'domain': copy.deepcopy(domain), 'semantics': copy.deepcopy(semantics),
             'normalization': copy.deepcopy(norm), 'edits': [], 'normal': copy.deepcopy(normal),
             'invalid_working_scalar': 0, 'source_arrays_modified': False,
             'edited_scalar_clamped': False, 'geometric_accuracy_or_confidence_assigned': False}
    working = np.zeros(shape, dtype=np.float64)
    working[validity] = raw[validity]
    statistics = {'raw_valid': _stats(raw, validity)}
    if measure == 'relative_score':
        lo, hi = np.percentile(working[validity], [low, high], method='linear')
        _need(math.isfinite(float(lo)) and math.isfinite(float(hi)) and hi > lo, 'degenerate_percentile', 'Percentile range must be finite and nonzero')
        near = np.clip((working[validity] - lo) / (hi - lo), 0, 1)
        working[validity] = near if semantics['near_order'] == 'larger_nearer' else 1 - near
        stage['normalization'].update(percentile_method='linear', statistics_domain='explicit_validity',
                                      lower_value=float(lo), upper_value=float(hi), clip=[0, 1], near_value=1)
    edits = params.get('edits')
    _need(type(edits) is list and len(edits) <= 8 and type(edit_masks) in (list, tuple)
          and len(edits) == len(edit_masks), 'edits', 'Ordered edits/masks mismatch or too many edits')
    for index, (edit, mask) in enumerate(zip(edits, edit_masks)):
        _object(edit, ('mask', 'scale', 'offset'), 'edit')
        scale = _number(edit['scale'], 'edit.scale', positive=True)
        offset = _number(edit['offset'], 'edit.offset')
        _need(isinstance(mask, np.ndarray) and mask.shape == shape and mask.dtype == np.dtype('float32')
              and bool(np.isfinite(mask).all()) and bool(((mask >= 0) & (mask <= 1)).all()), 'edit_mask', 'Edit weight must be float32 finite HW in [0,1]')
        weights = mask[validity].astype(np.float64)
        before = working[validity]
        with np.errstate(over='ignore', invalid='ignore'):
            working[validity] = before * (1 - weights) + weights * (before * scale + offset)
        _need(bool(np.isfinite(working[validity]).all()), 'nonfinite_edit', 'Edit overflow')
        if measure == 'axial_z':
            _need(bool((working[validity] > 0).all()), 'nonpositive_depth', 'Edited axial depth must remain positive')
        stage['edits'].append({'order': index, **copy.deepcopy(edit)})
    with np.errstate(over='ignore', invalid='ignore'):
        scalar = np.asarray(working, dtype=np.float32, order='C')
    _need(bool(np.isfinite(scalar[validity]).all()), 'float32_overflow', 'Prepared scalar cannot be represented as finite float32')
    if measure == 'axial_z':
        _need(bool((scalar[validity] > 0).all()), 'float32_underflow', 'Prepared float32 axial depth must remain positive')
    statistics['prepared_valid'] = _stats(scalar, validity)
    normal_validity = np.zeros(shape, dtype=bool)
    if method == 'import_geometry':
        _need(params.get('depth_prepare_version') == '1.1', 'normal_version', 'Imported Normal requires v1.1')
        _need(params.get('validity', {}).get('kind') == 'explicit_mask', 'normal_validity', 'Imported Normal requires explicit depth validity')
        _need(boundary_exclusion is None, 'boundary_mask', 'Imported Normal does not consume a derivative boundary mask')
        _object(normal, ('method', 'data', 'normal_validity', 'encoding', 'space', 'world_to_camera',
                         'surface_relation', 'source', 'fx', 'fy', 'cx', 'cy', 'max_relative_jump', 'depth_edit_policy'), 'normal')
        _need(normal['encoding'] == 'signed-float32-XYZ' and normal['space'] in
              ('world_Xright_Yforward_Zup', 'camera_Xright_Yup_Zbackward'), 'normal_encoding', 'Explicit signed Normal frame required')
        _need(normal['surface_relation'] in ('geometry_surface', 'shader_surface') and
              normal['depth_edit_policy'] == 'rederive_affected_from_axial_z', 'normal_policy', 'Normal relation/edit policy differs')
        _object(normal['source'], ('kind', 'statement'), 'normal.source')
        _need(normal['source']['kind'] == 'declared_external_normal' and type(normal['source']['statement']) is str
              and bool(normal['source']['statement'].strip()), 'normal_source', 'Explicit external Normal declaration required')
        rotation = normal['world_to_camera']
        _need(type(rotation) is list and len(rotation) == 3 and all(type(row) is list and len(row) == 3 for row in rotation),
              'normal_rotation', 'Rotation must be typed 3x3')
        rotation = np.array([[_number(v, 'world_to_camera') for v in row] for row in rotation], dtype=np.float64)
        with np.errstate(over='ignore', invalid='ignore'):
            orthogonal = bool(np.isfinite(rotation).all()) and bool(np.max(np.abs(rotation @ rotation.T - np.eye(3))) <= 1e-6)
            orthogonal = orthogonal and bool(np.max(np.abs(rotation.T @ rotation - np.eye(3))) <= 1e-6)
        _need(orthogonal and abs(float(np.linalg.det(rotation)) - 1) <= 1e-6,
              'normal_rotation', 'Proper orthogonal rotation with determinant +1 required')
        if normal['space'] == 'camera_Xright_Yup_Zbackward':
            _need(bool(np.array_equal(rotation, np.eye(3))), 'normal_rotation', 'Camera Normal must not be rotated twice')
        for k in ('fx', 'fy'):
            _number(normal[k], k, positive=True)
        for k in ('cx', 'cy'):
            _number(normal[k], k)
        _number(normal['max_relative_jump'], 'max_relative_jump', positive=True, high=1)
        _need(isinstance(imported_normal, np.ndarray) and imported_normal.shape == shape + (3,)
              and imported_normal.dtype == np.dtype('float32') and imported_normal.flags.c_contiguous
              and bool(np.isfinite(imported_normal).all()), 'normal_type', 'Imported Normal must be finite float32 C/HW3')
        source_nv = _binary(imported_normal_validity, shape, 'imported_normal_validity')
        _need(bool(source_nv.any()) and not bool((source_nv & ~validity).any()), 'normal_validity', 'Imported Normal valid domain must be nonempty depth subset')
        _need(bool((imported_normal[~source_nv] == 0).all()), 'normal_invalid', 'Imported invalid Normal must be zero')
        lengths = np.linalg.norm(imported_normal[source_nv].astype(np.float64), axis=1)
        _need(bool((np.abs(lengths - 1) <= 1e-5).all()), 'normal_unit', 'Imported Normal must already be unit length; no implicit normalization')
        normals = np.array(imported_normal, dtype=np.float32, order='C', copy=True)
        if normal['space'] == 'world_Xright_Yforward_Zup':
            normals[source_nv] = imported_normal[source_nv].astype(np.float64) @ rotation.T
        _need(bool(np.isfinite(normals).all()) and
              bool((np.abs(np.linalg.norm(normals[source_nv].astype(np.float64), axis=1) - 1) <= 1e-5).all()),
              'normal_transform', 'Transformed Normal exceeds finite/unit budget')
        normal_validity = source_nv.copy()
        changed = validity & (scalar != raw)
        affected = changed.copy()
        affected[1:, :] |= changed[:-1, :]
        affected[:-1, :] |= changed[1:, :]
        affected[:, 1:] |= changed[:, :-1]
        affected[:, :-1] |= changed[:, 1:]
        rederived = np.zeros(shape, dtype=bool)
        if bool(affected.any()):
            derivative_params = copy.deepcopy(params)
            derivative_params['depth_prepare_version'] = '1.0'
            derivative_params['edits'] = []
            derivative_params['normal'] = {'method': 'central_perspective',
                **{k: normal[k] for k in ('fx', 'fy', 'cx', 'cy', 'max_relative_jump')}, 'boundary_exclusion': None}
            candidate = prepare_arrays(scalar, validity, derivative_params, [])
            rederived = affected & source_nv & candidate['normal_validity']
            normals[affected] = 0
            normal_validity[affected] = False
            normals[rederived] = candidate['normal'][rederived]
            normal_validity[rederived] = True
        stage['normal_import'] = {
            'frame': 'camera_Xright_Yup_Zbackward', 'source_space': normal['space'],
            'surface_relation': normal['surface_relation'], 'producer_verified': False,
            'source': copy.deepcopy(normal['source']), 'depth_edit_policy': normal['depth_edit_policy'],
            'changed_count': int(changed.sum()), 'affected_count': int(affected.sum()),
            'rederived_count': int(rederived.sum()), 'invalidated_count': int((affected & source_nv & ~normal_validity).sum()),
            'origin': 'hybrid_imported_and_axial_derived' if bool(affected.any()) else 'imported_declared_normal',
            'shader_micro_normal_preserved_in_affected_domain': False if bool(affected.any()) else None,
            'world_axes': 'Blender world XYZ; Yforward is a label, not camera forward',
            'intrinsics_and_rotation_authority': 'caller declaration; no producer or geometry certification',
            'implicit_normalization': False,
        }
        statistics.update(normal_valid_count=int(normal_validity.sum()), normal_invalid_count=int(raw.size - normal_validity.sum()),
                          normal_unit_length_max_error=float(np.max(np.abs(np.linalg.norm(normals[normal_validity].astype(np.float64), axis=1) - 1)))
                          if bool(normal_validity.any()) else None)
        return {'scalar': scalar, 'validity': validity, 'normal': normals, 'normal_validity': normal_validity,
                'statistics': statistics, 'stage_parameters': stage}
    if method == 'none':
        _object(normal, ('method',), 'normal')
        _need(boundary_exclusion is None, 'boundary_mask', 'Unused boundary mask supplied')
        statistics['normal_valid_count'] = 0
        return {'scalar': scalar, 'validity': validity, 'normal': None, 'normal_validity': normal_validity,
                'statistics': statistics, 'stage_parameters': stage}
    excluded = np.zeros(shape, dtype=bool) if boundary_exclusion is None else _binary(boundary_exclusion, shape, 'boundary_exclusion')
    _need((normal.get('boundary_exclusion') is None) == (boundary_exclusion is None), 'boundary_mask', 'Boundary descriptor/array presence differs')
    usable = validity & ~excluded
    core = usable[1:-1, 1:-1].copy()
    neighbors = (scalar[1:-1, :-2].astype(np.float64), scalar[1:-1, 2:].astype(np.float64),
                 scalar[:-2, 1:-1].astype(np.float64), scalar[2:, 1:-1].astype(np.float64))
    for neighbor_valid in (usable[1:-1, :-2], usable[1:-1, 2:], usable[:-2, 1:-1], usable[2:, 1:-1]):
        core &= neighbor_valid
    center = scalar[1:-1, 1:-1].astype(np.float64)
    stage['normal'].update(stencil='center+left+right+up+down; central differences; border invalid',
                           boundary_policy='exclude center and all four neighbors',
                           boundary_mask_supplied=boundary_exclusion is not None,
                           contact_detection='explicit mask only; no automatic complete contact identification')
    if method == 'central_perspective':
        _object(normal, ('method', 'fx', 'fy', 'cx', 'cy', 'max_relative_jump', 'boundary_exclusion'), 'normal')
        fx = _number(normal['fx'], 'fx', positive=True)
        fy = _number(normal['fy'], 'fy', positive=True)
        cx = _number(normal['cx'], 'cx')
        cy = _number(normal['cy'], 'cy')
        jump = _number(normal['max_relative_jump'], 'max_relative_jump', positive=True, high=1)
        for neighbor in neighbors:
            difference = np.abs(neighbor - center)
            denominator = np.maximum(np.abs(neighbor), np.abs(center))
            relative = np.full(center.shape, np.inf)
            np.divide(difference, denominator, out=relative, where=denominator > 0)
            core &= relative <= jump
        row, col = np.indices(shape, dtype=np.float64)
        with np.errstate(over='ignore', invalid='ignore', divide='ignore'):
            points = np.stack(((col + .5 - cx) / fx * scalar, (cy - row - .5) / fy * scalar, -scalar), axis=2)
        _need(bool(np.isfinite(points[validity]).all()), 'projection_overflow', 'Valid projected positions must remain finite')
        tu = (points[1:-1, 2:] - points[1:-1, :-2]) / 2
        tv = (points[2:, 1:-1] - points[:-2, 1:-1]) / 2
        vectors = np.cross(tv, tu)
        facing = np.sum(vectors * -points[1:-1, 1:-1], axis=2)
        vectors[facing < 0] *= -1
        core &= np.isfinite(facing) & (facing != 0)
        stage['normal'].update(space='camera_Xright_Yup_Zbackward', projection='calibrated_perspective',
                               origin_kind='derived_axial_depth', formula='P=d*((u-cx)/fx,(cy-v)/fy,-1); normalize(Tv cross Tu)',
                               orientation='dot(N,-P)>0', jump_domain='edited float32 axial scalar',
                               jump_formula='abs(neighbor-center)/max(abs(neighbor),abs(center))')
    else:
        _object(normal, ('method', 'height_scale', 'max_height_jump', 'boundary_exclusion'), 'normal')
        strength = _number(normal['height_scale'], 'height_scale', positive=True, high=10)
        jump = _number(normal['max_height_jump'], 'max_height_jump', positive=True, high=1)
        for neighbor in neighbors:
            core &= np.abs(strength * (neighbor - center)) <= jump
        height = strength * scalar.astype(np.float64)
        h_x = (height[1:-1, 2:] - height[1:-1, :-2]) * shape[0] / 2
        h_y = -(height[2:, 1:-1] - height[:-2, 1:-1]) * shape[0] / 2
        vectors = np.stack((-h_x, -h_y, np.ones_like(h_x)), axis=2)
        stage['normal'].update(space='image_height_Xright_Yup_Ztoward', projection='artistic_screen_proxy',
                               origin_kind='derived_relative_heightfield', units='unitless', formula='normalize(-h_x,-h_y,1)',
                               coordinates='X=(u-W/2)/H,Y=(H/2-v)/H,Z=height_scale*edited_near1',
                               pixel_step=1 / shape[0], jump_domain='scaled unitless height', jump_formula='abs(h_neighbor-h_center)',
                               metric_calibration=False, geometric_normal_qualification=False)
    lengths = np.linalg.norm(vectors, axis=2)
    core &= np.isfinite(vectors).all(axis=2) & np.isfinite(lengths) & (lengths > 0)
    derived = np.zeros(vectors.shape, dtype=np.float64)
    np.divide(vectors, lengths[:, :, None], out=derived, where=core[:, :, None])
    normals = np.zeros(shape + (3,), dtype=np.float32)
    normals[1:-1, 1:-1] = derived
    normal_validity[1:-1, 1:-1] = core
    statistics.update(normal_valid_count=int(normal_validity.sum()), normal_invalid_count=int(raw.size - normal_validity.sum()),
                      boundary_exclusion_pixels=int(excluded.sum()))
    if bool(normal_validity.any()):
        error = np.abs(np.linalg.norm(normals[normal_validity].astype(np.float64), axis=1) - 1)
        statistics['normal_unit_length_max_error'] = float(error.max())
    else:
        statistics['normal_unit_length_max_error'] = None
    return {'scalar': scalar, 'validity': validity, 'normal': normals, 'normal_validity': normal_validity,
            'statistics': statistics, 'stage_parameters': stage}
