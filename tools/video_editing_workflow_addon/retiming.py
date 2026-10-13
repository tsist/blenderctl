"""Native complete-input SPEED FRAME_NUMBER mappings, without RetimingKeys.

Source index = source_in + floor((project_frame - start) * speed).
Indices and animation keys use integer project frames, independently of the
project FPS ratio or media timestamp metadata. VFR interpretation is separate.
"""
import array
import hashlib
import json
import math

from .contract import WorkflowError

MODEL = 'FRAME_NUMBER_SPARSE_V1'
TAG = '_vwe_mapping'


def _fail(code, message):
    raise WorkflowError(code, message)


def _parameters(start, duration, source_in, speed, freeze):
    for name, value in (('start', start), ('duration', duration), ('source_in', source_in)):
        if isinstance(value, bool) or not isinstance(value, int):
            _fail('INVALID_REQUEST', name + ' must be an integer')
    if duration < 1 or source_in < 0:
        _fail('INVALID_REQUEST', 'duration must be positive and source_in nonnegative')
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed) or speed <= 0:
        _fail('INVALID_REQUEST', 'speed must be positive and finite')
    if not isinstance(freeze, bool):
        _fail('INVALID_REQUEST', 'freeze must be boolean')
    return {'start': start, 'duration': duration, 'source_in': source_in, 'speed': speed, 'freeze': freeze}


def mapping_points(start, duration, source_in, speed, freeze=False):
    """Yield only actual source-index change points; freeze uses a constant."""
    _parameters(start, duration, source_in, speed, freeze)
    if freeze:
        return
    previous = None
    for offset in range(duration):
        value = source_in + math.floor(offset * speed)
        if value != previous:
            yield start + offset, value
            previous = value


def _curves(scene, data_path):
    ad = scene.animation_data
    if ad is None or ad.action is None:
        return []
    curves = []
    for layer in ad.action.layers:
        for action_strip in layer.strips:
            for bag in action_strip.channelbags:
                if bag.slot_handle == ad.action_slot_handle:
                    curves.extend(curve for curve in bag.fcurves if curve.data_path == data_path)
    return curves


def _signature(points):
    digest = hashlib.sha256()
    for point in points:
        digest.update((str(int(point[0])) + ':' + str(int(point[1])) + ';').encode('ascii'))
    return digest.hexdigest()


def _assign_curve(scene, effect, params):
    effect.speed_frame_number = params['source_in']
    if params['freeze']:
        return 0
    curves = _curves(scene, effect.path_from_id('speed_frame_number'))
    if not curves:
        effect.keyframe_insert(data_path='speed_frame_number', frame=params['start'])
        curves = _curves(scene, effect.path_from_id('speed_frame_number'))
    if len(curves) != 1:
        _fail('CONFLICT', 'Mapping must own exactly one animation curve')
    curve = curves[0]
    coords = array.array('f', (coordinate for point in mapping_points(**params) for coordinate in point))
    key_count = len(coords) // 2
    curve.keyframe_points.clear()
    curve.keyframe_points.add(key_count)
    curve.keyframe_points.foreach_set('co', coords)
    for point in curve.keyframe_points:
        point.interpolation = 'CONSTANT'
    curve.extrapolation = 'CONSTANT'
    curve.update()
    return key_count


def _store(meta, params, full_length, clip_id, key_count):
    stored = {'model': MODEL, **params, 'full_length': full_length,
              'clip_id': clip_id, 'project_frame_domain': 'integer project timeline frames; source media indices are zero-based',
              'keyframe_count': key_count, 'curve_signature': _signature(mapping_points(**params))}
    meta[TAG] = json.dumps(stored, sort_keys=True, separators=(',', ':'))
    for child in meta.strips:
        child.invalidate_cache('RAW')
        child.invalidate_cache('COMPOSITE')
    meta.invalidate_cache('RAW')
    meta.invalidate_cache('COMPOSITE')


def create_mapping(scene, media, *, clip_id, name, channel, start, duration,
                   source_in, speed, freeze=False):
    """Wrap one new Image/Movie in a native editable mapping and output Meta.

    Return (Meta strip, bounded evidence). Caller owns source/hash/protection,
    top-level identity tags, save/output and Host cancellation checkpoints.
    This function never modifies existing mappings or unrelated curves.
    """
    params = _parameters(start, duration, source_in, speed, freeze)
    if media.type not in ('IMAGE', 'MOVIE') or media.parent_meta() is not None:
        _fail('INVALID_REQUEST', 'Mapping requires a new top-level IMAGE or MOVIE strip')
    if isinstance(channel, bool) or not isinstance(channel, int) or not 1 <= channel <= 128:
        _fail('INVALID_REQUEST', 'Output channel must be 1..128')
    full_length = int(media.content_duration)
    last_index = source_in if freeze else source_in + math.floor((duration - 1) * speed)
    if full_length < 1 or last_index >= full_length:
        _fail('INVALID_REQUEST', 'Requested mapping exceeds complete source media')
    editor = scene.sequence_editor
    # Move before changing channel/bounds: top-level setters can silently
    # shuffle overlapping media around unrelated background/neighbor strips.
    meta = editor.strips.new_meta(name=name + '_meta', channel=channel, frame_start=start)
    media.move_to_meta(meta)
    # source_in is applied exactly once by FRAME_NUMBER, never by source trim.
    media.channel = 1
    media.content_start = start
    media.left_handle = start
    media.right_handle = start + max(full_length, duration)
    effect = meta.strips.new_effect(name=clip_id + '_speed', type='SPEED',
        channel=2, frame_start=start, length=max(full_length, duration), input1=media)
    effect.speed_control = 'FRAME_NUMBER'
    effect.use_frame_interpolate = False
    effect.speed_frame_number = source_in
    meta.left_handle = start
    meta.right_handle = start + duration
    for child in meta.strips:
        child['_vwe_internal'] = clip_id
    key_count = _assign_curve(scene, effect, params)
    _store(meta, params, full_length, clip_id, key_count)
    evidence = inspect_mapping(scene, meta)
    return meta, evidence


def update_mapping(scene, meta, start, duration):
    """Move/trim/extend one verified mapping, preserving relative source time.

    Validate the complete existing mapping and source range before writing.
    Only this Meta, its input bounds and its one owned curve are changed.
    Unsupported/hand-modified content raises CONFLICT rather than drifting.
    """
    previous = inspect_mapping(scene, meta)['mapping']
    params = _parameters(start, duration, previous['source_in'], previous['speed'], previous['freeze'])
    last = params['source_in'] if params['freeze'] else params['source_in'] + math.floor((duration - 1) * params['speed'])
    if last >= previous['full_length']:
        _fail('INVALID_REQUEST', 'Updated mapping exceeds complete source media')
    effect = next(child for child in meta.strips if child.type == 'SPEED')
    media = effect.input_1
    media.content_start = start
    media.left_handle = start
    media.right_handle = start + max(previous['full_length'], duration)
    meta.left_handle = start
    meta.right_handle = start + duration
    key_count = _assign_curve(scene, effect, params)
    _store(meta, params, previous['full_length'], previous['clip_id'], key_count)
    return inspect_mapping(scene, meta)


def inspect_mapping(scene, meta, *, strict=True):
    """Return mapping parameters/evidence; strict=False reports hand changes.

    No frame evaluation or mutation. A caller inspecting a hand-edited source
    may retain errors as manual-change evidence; a producer validates strictly.
    """
    try:
        stored = json.loads(meta[TAG])
    except (KeyError, TypeError, ValueError):
        _fail('CONFLICT', 'Missing or malformed native frame mapping identity')
    if stored.get('model') != MODEL:
        _fail('UNSUPPORTED', 'Unknown native frame mapping model')
    params = {key: stored[key] for key in ('start', 'duration', 'source_in', 'speed', 'freeze')}
    _parameters(**params)
    effects = [child for child in meta.strips if child.type == 'SPEED']
    errors = []
    if len(effects) != 1:
        _fail('CONFLICT', 'Mapping requires one native SPEED child')
    effect = effects[0]
    media = effect.input_1
    if media is None or media.parent_meta() != meta:
        errors.append('native input binding changed')
    if effect.speed_control != 'FRAME_NUMBER' or effect.use_frame_interpolate:
        errors.append('native mapping mode changed')
    if int(meta.left_handle) != params['start'] or int(meta.duration) != params['duration']:
        errors.append('output timing differs from mapping identity')
    if media is not None and (int(media.content_start) != params['start'] or int(media.left_handle) != params['start']):
        errors.append('source trim differs from complete-input mapping')
    if media is not None and (int(media.content_duration) != stored['full_length'] or int(media.right_handle) != params['start'] + max(stored['full_length'], params['duration'])):
        errors.append('complete source length/bounds changed')
    curves = _curves(scene, effect.path_from_id('speed_frame_number'))
    actual_count = 0
    actual_signature = _signature(())
    if params['freeze']:
        if curves or abs(effect.speed_frame_number - params['source_in']) > 1e-6:
            errors.append('freeze constant changed')
    elif len(curves) != 1:
        errors.append('mapping curve count changed')
    else:
        curve = curves[0]
        actual_count = len(curve.keyframe_points)
        actual_signature = _signature((point.co.x, point.co.y) for point in curve.keyframe_points)
        if curve.mute or curve.modifiers or curve.extrapolation != 'CONSTANT':
            errors.append('mapping curve behavior changed')
        expected_points = iter(mapping_points(**params))
        for point in curve.keyframe_points:
            pair = (float(point.co.x), float(point.co.y))
            expected = next(expected_points, None)
            if expected is None or pair != expected or point.interpolation != 'CONSTANT':
                errors.append('mapping keyframes differ from floor sampling')
                break
        if next(expected_points, None) is not None:
            errors.append('mapping keyframes are incomplete')
    if actual_count != stored['keyframe_count'] or actual_signature != stored['curve_signature']:
        errors.append('mapping key identity changed')
    if errors and strict:
        _fail('CONFLICT', '; '.join(dict.fromkeys(errors)))
    return {'source_in': params['source_in'], 'speed': params['speed'], 'freeze': params['freeze'],
            'mapping': {**stored, 'status': 'passed' if not errors else 'manual_changes',
                        'actual_keyframe_count': actual_count, 'actual_curve_signature': actual_signature,
                        'errors': list(dict.fromkeys(errors))}}
