"""Native VSE core shared by UI and host. All paths are explicit local resources.

The host owns file transactions: apply edits a disposable open copy and never
saves. A failed apply must not be saved. Unknown strips are kept, not rebuilt.
"""
import copy
import hashlib
import json
import math
from fractions import Fraction
from pathlib import Path

import bpy
from .contract import WorkflowError, normalize_manifest, validate_clip
from .protection import (snapshot_protection, resource_paths, native_strip_record,
                         compare_protection)
from . import retiming


def _error(code, message):
    raise WorkflowError(code, message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _read(owner, key, default=None):
    raw = owner.get(key)
    if raw is None:
        return copy.deepcopy(default)
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        _error('CONFLICT', 'Invalid workflow metadata: ' + key)


def _equal(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6)
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    return a == b


def _sha(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _path(path, library=None):
    return str(Path(bpy.path.abspath(path, library=library)).resolve())


def _file(path):
    path = _path(path)
    if not Path(path).is_file():
        _error('RESOURCE_MISSING', 'Missing VSE media: ' + path)
    return {'file': path, 'expected_sha256': _sha(path)}


def _curves(scene):
    ad = scene.animation_data
    if not ad or not ad.action:
        return []
    result = []
    for layer in ad.action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                if bag.slot_handle == ad.action_slot_handle:
                    result.extend(bag.fcurves)
    return result


def _curve_snapshot(scene, strip):
    prefix = strip.path_from_id() + '.'
    return sorted([{'path': c.data_path[len(prefix):], 'index': c.array_index,
                    'extrapolation': c.extrapolation,
                    'points': [[float(p.co.x), float(p.co.y), p.interpolation] for p in c.keyframe_points],
                    'modifiers': [m.type for m in c.modifiers], 'mute': c.mute}
                   for c in _curves(scene) if c.data_path.startswith(prefix)],
                  key=lambda x: (x['path'], x['index']))


def _roots(scene):
    return list(scene.sequence_editor.strips) if scene.sequence_editor else []


def _owned(scene, workflow):
    result = {}
    for strip in _roots(scene):
        if strip.get('_vwe_workflow') != workflow:
            continue
        cid = strip.get('_vwe_clip_id')
        if not cid or cid in result:
            _error('CONFLICT', 'Missing or duplicate managed clip identity')
        result[cid] = strip
    return result


def _media(strip):
    if strip.type != 'META':
        return strip
    inputs = [s for s in strip.strips if s.type in {'IMAGE', 'MOVIE'}]
    effects = [s for s in strip.strips if s.type == 'SPEED']
    if len(inputs) != 1 or len(effects) != 1 or effects[0].input_1 != inputs[0]:
        _error('UNSUPPORTED', 'Managed retime structure has been changed')
    return inputs[0]


def _timeline(scene):
    stored = _read(scene, '_vwe_timeline', {})
    fps = stored.get('fps', [scene.render.fps, 1])
    actual = scene.render.fps / scene.render.fps_base
    if not math.isclose(fps[0] / fps[1], actual, rel_tol=1e-6):
        rate = Fraction(actual).limit_denominator(1000000)
        fps = [rate.numerator, rate.denominator]
    return {'fps': fps, 'width': scene.render.resolution_x,
            'height': scene.render.resolution_y, 'frame_start': scene.frame_start,
            'frame_end_exclusive': scene.frame_end + 1}


def _set_timeline(scene, spec):
    numerator, denominator = spec['fps']
    base_fps = max(1, round(numerator / denominator))
    scene.render.fps = base_fps
    scene.render.fps_base = base_fps * denominator / numerator
    scene.render.resolution_x = spec['width']
    scene.render.resolution_y = spec['height']
    scene.render.resolution_percentage = 100
    scene.frame_start = spec['frame_start']
    scene.frame_end = spec['frame_end_exclusive'] - 1
    scene['_vwe_timeline'] = _json(spec)
    if not _equal(_timeline(scene), spec):
        _error('UNSUPPORTED', 'Native timeline limits cannot represent requested values')


def _actual(scene, strip):
    spec = _read(strip, '_vwe_spec')
    if not spec:
        _error('CONFLICT', 'Managed clip has no author specification')
    result = copy.deepcopy(spec)
    result.update(name=strip.name, channel=strip.channel, start=int(strip.left_handle),
                  duration=int(strip.duration), lock=strip.lock, mute=strip.mute)
    media = _media(strip)
    if spec['kind'] != 'SOUND':
        result['crop'] = {'left': strip.crop.min_x, 'right': strip.crop.max_x,
                          'top': strip.crop.max_y, 'bottom': strip.crop.min_y}
        result['grade'] = {'multiply': strip.color_multiply, 'saturation': strip.color_saturation}
        if not spec.get('fade_in') and not spec.get('fade_out'):
            result['opacity'] = strip.blend_alpha
        if 'transform' in spec or strip.type != 'SOUND':
            transform = strip.transform
            result['transform'] = {'x': transform.offset_x, 'y': transform.offset_y,
                                   'scale_x': transform.scale_x, 'scale_y': transform.scale_y,
                                   'rotation_degrees': math.degrees(transform.rotation)}
    else:
        if not spec.get('fade_in') and not spec.get('fade_out'):
            result['volume'] = strip.volume
        result['pan'] = strip.pan
    if spec['kind'] in {'IMAGE', 'MOVIE', 'SOUND'}:
        src = result['source']
        if media.type == 'IMAGE':
            paths = [_path(str(Path(bpy.path.abspath(media.directory)) / e.filename)) for e in media.elements]
        elif media.type == 'MOVIE':
            paths = [_path(media.filepath)]
        else:
            paths = [_path(media.sound.filepath, media.sound.library)]
        # Reuse declared SHA only for the exact path; host independently hashes all
        # resources before and after work. New manual paths are hashed on capture.
        known = {r['file'].casefold(): r for r in src['files']}
        src['files'] = [copy.deepcopy(known.get(p.casefold())) or _file(p) for p in paths]
        src['source_in'] = int(media.left_handle - media.content_start)
        if media.type in {'MOVIE', 'IMAGE'}:
            src['color_space'] = media.colorspace_settings.name
        if media.type == 'MOVIE':
            src['stream'] = media.stream_index
        if strip.type == 'META':
            mapping = retiming.inspect_mapping(scene, strip)
            src.update({key: mapping[key] for key in ('source_in', 'speed', 'freeze')})
        else:
            src.update(speed=1, freeze=False)
    if spec['kind'] == 'TEXT':
        result.update(text=media.text, font_size=int(media.font_size), color=list(media.color))
        if media.font and 'font' in spec:
            if _path(media.font.filepath) != spec['font']['file']:
                result['font'] = _file(media.font.filepath)
    if spec['kind'] == 'COLOR':
        result['color'] = list(media.color) + [spec['color'][3]]
    return result


def inspect(scene_name, workflow_id=None):
    scene = bpy.data.scenes.get(scene_name)
    if scene is None:
        _error('NOT_FOUND', 'Scene not found: ' + scene_name)
    owner = scene.get('_vwe_workflow')
    if workflow_id is not None and owner != workflow_id:
        _error('CONFLICT', 'Scene workflow identity differs')
    managed = _owned(scene, owner) if owner else {}
    clips, changes, animation_changes = [], {}, []
    for cid, strip in sorted(managed.items()):
        actual = _actual(scene, strip)
        clips.append(actual)
        base = _read(strip, '_vwe_baseline', {})
        changed = [key for key in actual if not _equal(actual[key], base.get(key))]
        if changed:
            changes[cid] = changed
        if not _equal(_curve_snapshot(scene, strip), _read(strip, '_vwe_curves', [])):
            animation_changes.append(cid)
    known = {s.as_pointer() for s in managed.values()}
    unknown = [{'name': s.name, 'type': s.type, 'channel': s.channel,
                'start': int(s.left_handle), 'duration': int(s.duration),
                'lock': s.lock, 'mute': s.mute} for s in _roots(scene) if s.as_pointer() not in known]
    return {'scene': scene.name, 'workflow_id': owner, 'timeline': _timeline(scene),
            'clips': clips, 'diagnostics': {'manual_fields': changes,
                'manual_animation': animation_changes, 'unmanaged_strips': unknown,
                'scope': 'Native supported fields; unsupported contents are retained and protected'}}


def _fade(scene, strip, spec):
    field = 'volume' if spec['kind'] == 'SOUND' else 'blend_alpha'
    path = strip.path_from_id(field)
    # Remove only this clip's old supported envelope.
    ad = scene.animation_data
    if ad and ad.action:
        for layer in ad.action.layers:
            for action_strip in layer.strips:
                for bag in action_strip.channelbags:
                    if bag.slot_handle == ad.action_slot_handle:
                        for curve in list(bag.fcurves):
                            if curve.data_path == path:
                                bag.fcurves.remove(curve)
    base = spec['volume'] if spec['kind'] == 'SOUND' else spec['opacity']
    setattr(strip, field, base)
    start, end = spec['start'], spec['start'] + spec['duration']
    points = {}
    if spec['fade_in']:
        points.update({start: 0.0, start + spec['fade_in']: base})
    if spec['fade_out']:
        points.update({end - spec['fade_out']: base, end: 0.0})
    for frame, value in sorted(points.items()):
        setattr(strip, field, value)
        strip.keyframe_insert(data_path=field, frame=frame)
    evaluated = base
    for curve in _curves(scene):
        if curve.data_path == path:
            curve.extrapolation = 'CONSTANT'
            for point in curve.keyframe_points:
                point.interpolation = 'LINEAR'
            evaluated = curve.evaluate(scene.frame_current + scene.frame_subframe)
    # frame_set on an inactive Scene need not evaluate its animation in the
    # current depsgraph. Set this known envelope's evaluated value explicitly.
    setattr(strip, field, evaluated)


_MISSING = object()


def _rebase_edited(baseline, before, after):
    """Advance only authored deltas; disjoint edits must not bless manual data."""
    if _equal(before, after):
        return copy.deepcopy(baseline) if baseline is not _MISSING else _MISSING
    if isinstance(before, dict) and isinstance(after, dict) and (isinstance(baseline, dict) or baseline is _MISSING):
        base = {} if baseline is _MISSING else baseline
        result = {}
        for key in set(base) | set(before) | set(after):
            value = _rebase_edited(base.get(key, _MISSING), before.get(key, _MISSING), after.get(key, _MISSING))
            if value is not _MISSING:
                result[key] = value
        return result
    if isinstance(before, list) and isinstance(after, list):
        if isinstance(baseline, list) and len(baseline) == len(before) == len(after):
            return [_rebase_edited(base, old, new) for base, old, new in zip(baseline, before, after)]
        if baseline is not _MISSING and not _equal(baseline, before):
            _error('CONFLICT', 'An edited collection overlaps manual structural changes')
    return copy.deepcopy(after) if after is not _MISSING else _MISSING


def _store(scene, strip, spec, prior=None):
    strip['_vwe_spec'] = _json(spec)
    for key, actual in (('_vwe_baseline', _actual(scene, strip)),
                        ('_vwe_curves', _curve_snapshot(scene, strip)),
                        ('_vwe_native', native_strip_record(strip))):
        if prior is not None:
            actual = _rebase_edited(prior[key]['baseline'], prior[key]['observed'], actual)
        strip[key] = _json(actual)


def _assert_fields(scene, strip, spec, fields):
    actual = _actual(scene, strip)
    for key in set(fields) - {'fit'}:
        if key not in spec:
            continue
        if key == 'transform':
            for axis in ('scale_x', 'scale_y'):
                value = actual['transform'][axis]
                if value <= 0 or not math.isclose(value, spec['transform'][axis], rel_tol=1e-6, abs_tol=0):
                    _error('UNSUPPORTED', 'Native transform cannot represent positive scale: ' + spec['id'])
        if not _equal(spec[key], actual.get(key)):
            _error('VALIDATION_FAILED', 'Native clip differs at ' + spec['id'] + '.' + key)


def _style(scene, strip, spec, fields=None, font_cache=None):
    fields = set(spec) if fields is None else set(fields)
    if 'name' in fields:
        strip.name = spec['name']
    if 'channel' in fields:
        strip.channel = spec['channel']
    if 'mute' in fields:
        strip.mute = spec['mute']
    if 'lock' in fields:
        strip.lock = spec['lock']
    if spec['kind'] == 'SOUND':
        if 'pan' in fields:
            strip.pan = spec['pan']
        if 'volume' in fields:
            strip.volume = spec['volume']
    else:
        if 'crop' in fields:
            values = spec['crop']
            if strip.type in {'IMAGE', 'MOVIE'}:
                element = strip.elements[0]
                width, height = element.orig_width, element.orig_height
            else:
                width, height = scene.render.resolution_x, scene.render.resolution_y
            if values['left'] + values['right'] >= width or values['top'] + values['bottom'] >= height:
                _error('INVALID_REQUEST', 'Crop removes the whole input: ' + spec['id'])
            for key, native in [('left', 'min_x'), ('right', 'max_x'), ('top', 'max_y'), ('bottom', 'min_y')]:
                setattr(strip.crop, native, values[key])
        if 'grade' in fields:
            strip.color_multiply = spec['grade']['multiply']
            strip.color_saturation = spec['grade']['saturation']
        if 'transform' in fields:
            values = spec.get('transform', {})
            for field, key, default in [('offset_x', 'x', 0), ('offset_y', 'y', 0),
                                        ('scale_x', 'scale_x', 1), ('scale_y', 'scale_y', 1)]:
                setattr(strip.transform, field, values.get(key, default))
            strip.transform.rotation = math.radians(values.get('rotation_degrees', 0))
        if 'opacity' in fields:
            strip.blend_alpha = spec['opacity']
    if spec['kind'] == 'TEXT':
        if 'text' in fields:
            strip.text = spec['text']
        if 'font_size' in fields:
            strip.font_size = spec['font_size']
        if 'color' in fields:
            strip.color = spec['color']
        if 'font' in fields and 'font' in spec:
            # Isolate existing IDs, but share a newly loaded font within this
            # atomic apply. One packed font per caption exhausts Blender's BLF
            # slots and silently renders later captions without glyphs.
            key = (spec['font']['file'], spec['font']['expected_sha256'])
            font = font_cache.get(key) if font_cache is not None else None
            if font is None:
                font = bpy.data.fonts.load(spec['font']['file'], check_existing=False)
                font.pack()
                if font_cache is not None:
                    font_cache[key] = font
            strip.font = font
    if spec['kind'] == 'COLOR' and 'color' in fields:
        strip.color = spec['color'][:3]
    if fields & {'fade_in', 'fade_out', 'volume', 'opacity', 'start', 'duration'}:
        _fade(scene, strip, spec)


def _make(scene, spec, workflow, relative_scale=None, font_cache=None):
    editor = scene.sequence_editor_create()
    kind, src = spec['kind'], spec.get('source', {})
    start, duration, channel = spec['start'], spec['duration'], spec['channel']
    speed, freeze, source_in = src.get('speed', 1), src.get('freeze', False), src.get('source_in', 0)
    if kind == 'SOUND' and (speed != 1 or freeze):
        _error('UNSUPPORTED', 'Audio speed requires a separately verified resampling route')
    if kind == 'COLOR' and spec['color'][3] != 1:
        _error('UNSUPPORTED', 'Use COLOR opacity for native alpha; color alpha must be 1')
    kwargs = {'name': spec['name'], 'channel': channel, 'frame_start': start}
    if kind in {'COLOR', 'TEXT'}:
        strip = editor.strips.new_effect(type=kind, length=duration, **kwargs)
    elif kind == 'IMAGE':
        files = src['files']
        parents = {str(Path(r['file']).parent).casefold() for r in files}
        if len(parents) != 1:
            _error('UNSUPPORTED', 'An image sequence must have a common directory')
        strip = editor.strips.new_image(filepath=files[0]['file'], fit_method={'CROP': 'FILL'}.get(spec.get('fit'), spec.get('fit', 'FIT')), **kwargs)
        for row in files[1:]:
            strip.elements.append(Path(row['file']).name)
    elif kind == 'MOVIE':
        strip = editor.strips.new_movie(filepath=src['files'][0]['file'],
                    fit_method={'CROP': 'FILL'}.get(spec.get('fit'), spec.get('fit', 'FIT')), stream=src['stream'], **kwargs)
    else:
        strip = editor.strips.new_sound(filepath=src['files'][0]['file'], stream=src['stream'], **kwargs)
    if kind in {'IMAGE', 'MOVIE', 'SOUND'}:
        if kind != 'SOUND':
            strip.colorspace_settings.name = src.get('color_space', 'sRGB')
        full_length = int(strip.content_duration)
        last = source_in if freeze else source_in + math.floor((duration - 1) * speed)
        if (kind != 'IMAGE' or len(src['files']) > 1) and last >= full_length:
            _error('INVALID_REQUEST', 'Source is shorter than requested samples: ' + spec['id'])
        if (speed != 1 or freeze) and kind != 'SOUND':
            media = strip
            media.name = spec['id'] + '_input'
            strip, mapping = retiming.create_mapping(scene, media, clip_id=spec['id'],
                name=spec['name'], channel=channel, start=start, duration=duration,
                source_in=source_in, speed=speed, freeze=freeze)
        else:
            strip.content_start = start - source_in
            strip.left_handle = start
            strip.right_handle = start + duration
    strip['_vwe_workflow'] = workflow
    strip['_vwe_clip_id'] = spec['id']
    strip['_vwe_spec'] = _json(spec)
    if kind != 'SOUND':
        fit_scale = [strip.transform.scale_x, strip.transform.scale_y]
        strip['_vwe_fit_scale'] = _json(fit_scale)
        if relative_scale is not None:
            spec['transform']['scale_x'] = fit_scale[0] * relative_scale[0]
            spec['transform']['scale_y'] = fit_scale[1] * relative_scale[1]
    if kind != 'SOUND':
        strip.blend_type = 'ALPHA_OVER'
    _style(scene, strip, spec, font_cache=font_cache)
    _store(scene, strip, spec)
    if int(strip.left_handle) != start or int(strip.duration) != duration or strip.channel != channel:
        _error('CONFLICT', 'Native strip placement differs: ' + spec['id'])
    _assert_fields(scene, strip, spec, spec)
    return strip


def _preflight_collisions(specs, unknown):
    by_channel = {}
    for clip in list(specs.values()) + unknown:
        by_channel.setdefault(clip['channel'], []).append(clip)
    for channel, clips in by_channel.items():
        clips = sorted(clips, key=lambda c: c['start'])
        for left, right in zip(clips, clips[1:]):
            if left['start'] + left['duration'] > right['start']:
                _error('CONFLICT', 'Overlapping strips on channel ' + str(channel))


def apply(manifest):
    manifest = normalize_manifest(manifest)
    name, workflow = manifest['scene'], manifest['workflow_id']
    scene = bpy.data.scenes.get(name)
    operation_log, changed = [], []
    # Invocation-local only: no mutation/reuse of preexisting user fonts and
    # no stale bpy references retained across file loads or future patches.
    font_cache = {}
    if manifest['action'] == 'create':
        if scene is not None:
            _error('CONFLICT', 'Create requires a new Scene name')
        before = snapshot_protection(name, ['*'])
        specs = {c['id']: c for c in manifest['clips']}
        _preflight_collisions(specs, [])
        scene = bpy.data.scenes.new(name)
        scene['_vwe_workflow'] = workflow
        scene['_vwe_version'] = '1.0'
        _set_timeline(scene, manifest['timeline'])
        scene.render.use_sequencer = True
        scene.render.use_compositing = False
        # New Scene defaults to //, which save_as does not remap as an input
        # dependency. Bind that authored setting before capturing expectations.
        scene.render.filepath = _path(scene.render.filepath)
        scene.view_settings.view_transform = 'Standard'
        scene.view_settings.look = 'None'
        scene.view_settings.exposure = 0
        scene.view_settings.gamma = 1
        scene.sequencer_colorspace_settings.name = 'sRGB'
        for spec in specs.values():
            _make(scene, spec, workflow, font_cache=font_cache)
            changed.append(spec['id'])
            operation_log.append({'op': 'add', 'id': spec['id']})
        after = snapshot_protection(name, ['*'])
    else:
        if scene is None or scene.get('_vwe_workflow') != workflow:
            _error('CONFLICT', 'Patch target is not this workflow Scene')
        state = inspect(name, workflow)
        owned = _owned(scene, workflow)
        specs = {c['id']: copy.deepcopy(c) for c in state['clips']}
        old_timeline = _timeline(scene)
        timeline = manifest.get('timeline', old_timeline)
        if 'timeline' in manifest and not _equal(old_timeline, _read(scene, '_vwe_timeline')) and not _equal(timeline, old_timeline):
            _error('CONFLICT', 'Timeline was manually changed')
        actions = []
        touched = set()
        for edit in manifest['edits']:
            op = edit['op']
            cid = edit['clip']['id'] if op == 'add' else edit['id']
            if cid in touched:
                _error('INVALID_REQUEST', 'One edit per clip per atomic work unit: ' + cid)
            touched.add(cid)
            if op == 'add':
                if cid in specs:
                    _error('CONFLICT', 'Clip already exists: ' + cid)
                specs[cid] = validate_clip(edit['clip'], timeline)
                destination_channel = specs[cid]['channel']
                if destination_channel <= len(scene.sequence_editor.channels) and scene.sequence_editor.channels[destination_channel - 1].lock:
                    _error('CONFLICT', 'Destination channel is locked')
                actions.append((op, cid, None, None))
                continue
            if cid not in specs:
                _error('NOT_FOUND', 'Clip missing: ' + cid)
            strip = owned[cid]
            if strip.lock or scene.sequence_editor.channels[strip.channel - 1].lock:
                _error('CONFLICT', 'Clip is locked: ' + cid)
            current, baseline = specs[cid], _read(strip, '_vwe_baseline', {})
            if op == 'remove':
                if (not _equal(current, baseline) or cid in state['diagnostics']['manual_animation'] or
                        not _equal(native_strip_record(strip), _read(strip, '_vwe_native'))):
                    _error('CONFLICT', 'Removal conflicts with manual changes: ' + cid)
                del specs[cid]
                actions.append((op, cid, None, None))
                continue
            if op == 'select_candidate':
                target = next((a for a in current.get('alternatives', []) if a['candidate_id'] == edit['candidate_id']), None)
                if target is None:
                    _error('NOT_FOUND', 'Candidate missing: ' + edit['candidate_id'])
                alternatives = [a for a in current['alternatives'] if a['candidate_id'] != target['candidate_id']]
                alternatives.append({'candidate_id': current['candidate_id'], 'source': current['source']})
                fields = {'source': target['source'], 'candidate_id': target['candidate_id'], 'alternatives': alternatives}
            else:
                fields = edit['changes']
            for field, value in fields.items():
                if not _equal(current.get(field), baseline.get(field)) and not _equal(current.get(field), value):
                    _error('CONFLICT', 'Manual edit conflicts at ' + cid + '.' + field)
            if cid in state['diagnostics']['manual_animation'] and set(fields) - {'name', 'channel', 'lock', 'shot_id', 'candidate_id', 'alternatives'}:
                _error('CONFLICT', 'Manual animation conflicts: ' + cid)
            merged = {**current, **copy.deepcopy(fields)}
            specs[cid] = validate_clip(merged, timeline)
            destination_channel = specs[cid]['channel']
            if destination_channel <= len(scene.sequence_editor.channels) and scene.sequence_editor.channels[destination_channel - 1].lock:
                _error('CONFLICT', 'Destination channel is locked')
            rebuild = bool(set(fields) & {'source', 'fit'})
            if not rebuild and current['kind'] in {'IMAGE', 'MOVIE', 'SOUND'}:
                source = specs[cid]['source']
                count = int(_media(strip).content_duration)
                if current['kind'] != 'IMAGE' or len(source['files']) > 1:
                    last = source['source_in'] + (0 if source['freeze'] else math.floor((specs[cid]['duration'] - 1) * source['speed']))
                    if last >= count:
                        _error('INVALID_REQUEST', 'Updated duration exceeds actual source samples: ' + cid)
            if rebuild and (len(strip.modifiers) or _curve_snapshot(scene, strip) != _read(strip, '_vwe_curves', [])):
                _error('UNSUPPORTED', 'Media replacement would discard unsupported manual content: ' + cid)
            if rebuild and not _equal(native_strip_record(strip), _read(strip, '_vwe_native')):
                _error('CONFLICT', 'Media replacement needs review of manually modified native strip: ' + cid)
            actions.append(('replace' if rebuild else 'update', cid, fields, current))
        for spec in specs.values():
            validate_clip(spec, timeline)
        _preflight_collisions(specs, state['diagnostics']['unmanaged_strips'])
        excluded = [owned[c].name for c in touched if c in owned]
        excluded_paths = []
        for cid in touched:
            if cid in owned:
                strip = owned[cid]
                excluded_paths.append(strip.path_from_id())
                if strip.type == 'META':
                    excluded_paths.extend(child.path_from_id() for child in strip.strips)
        excluded.extend(specs[c]['name'] for c in touched if c in specs)
        # Include action targets even for a timeline-only patch.
        before = snapshot_protection(name, excluded or ['__vwe_no_strip__'], excluded_paths)
        if 'timeline' in manifest:
            _set_timeline(scene, timeline)
        for op, cid, fields, current in actions:
            relative_scale = None
            if op in {'remove', 'replace'}:
                strip = owned[cid]
                if op == 'replace' and current['kind'] in {'IMAGE', 'MOVIE'} and 'transform' not in fields:
                    fit_scale = _read(strip, '_vwe_fit_scale')
                    if fit_scale is None:
                        # Compatibility with early development candidates.
                        if strip.type == 'META' or current.get('fit') == 'ORIGINAL':
                            fit_scale = [1.0, 1.0]
                        else:
                            element = strip.elements[0]
                            ratios = [old_timeline['width'] / element.orig_width, old_timeline['height'] / element.orig_height]
                            fit_scale = [max(ratios) if current.get('fit') == 'CROP' else min(ratios)] * 2
                    relative_scale = [current['transform'][key] / base for key, base in zip(('scale_x', 'scale_y'), fit_scale)]
                prefixes = [strip.path_from_id() + '.']
                if strip.type == 'META':
                    prefixes.extend(child.path_from_id() + '.' for child in strip.strips)
                for curve in list(_curves(scene)):
                    if any(curve.data_path.startswith(prefix) for prefix in prefixes):
                        # Remove through owning channelbag, never another clip's curves.
                        for layer in scene.animation_data.action.layers:
                            for action_strip in layer.strips:
                                for bag in action_strip.channelbags:
                                    if bag.slot_handle == scene.animation_data.action_slot_handle and curve in list(bag.fcurves):
                                        bag.fcurves.remove(curve)
                scene.sequence_editor.strips.remove(strip)
            if op in {'add', 'replace'}:
                strip = _make(scene, specs[cid], workflow, relative_scale, font_cache)
                excluded.append(strip.name)
            elif op == 'update':
                strip, spec = owned[cid], specs[cid]
                prior = {key: {'baseline': _read(strip, key, {} if key != '_vwe_curves' else []), 'observed': observed}
                         for key, observed in (('_vwe_baseline', _actual(scene, strip)),
                                               ('_vwe_curves', _curve_snapshot(scene, strip)),
                                               ('_vwe_native', native_strip_record(strip)))}
                if strip.type == 'META' and set(fields) & {'start', 'duration'}:
                    retiming.update_mapping(scene, strip, spec['start'], spec['duration'])
                else:
                    if 'start' in fields:
                        strip.content_start += spec['start'] - current['start']
                    if 'duration' in fields:
                        strip.right_handle = spec['start'] + spec['duration']
                _style(scene, strip, spec, fields, font_cache)
                strip['_vwe_spec'] = _json(spec)
                _assert_fields(scene, strip, spec, fields)
                _store(scene, strip, spec, prior)
            operation_log.append({'op': op, 'id': cid, 'fields': sorted(fields or {})})
            changed.append(cid)
        after = snapshot_protection(name, excluded or ['__vwe_no_strip__'], excluded_paths)
    protection = compare_protection(before, after)
    if protection['status'] != 'pass':
        _error('VALIDATION_FAILED', 'Non-target project contents changed during VSE work')
    return {'workflow_id': workflow, 'scene': name, 'changed_ids': changed,
            'operation_log': operation_log, 'inspection': inspect(name, workflow),
            'protection': {**protection, 'status': 'passed', 'scope': 'Existing non-target content'}}
