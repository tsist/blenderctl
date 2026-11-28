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
from . import finishing
from .contract import WorkflowError, normalize_manifest, validate_clip
from .protection import (snapshot_protection, resource_paths, native_strip_record,
                         compare_protection)
from . import retiming


RIPPLE_MAX_SECONDS = 600
RIPPLE_MAX_WIDTH = 1920
RIPPLE_MAX_HEIGHT = 1080
CAPTION_STYLE = {'wrap_width', 'use_outline', 'outline_color', 'outline_width',
                 'use_shadow', 'shadow_color', 'use_box', 'box_color', 'alignment_x'}


def _error(code, message):
    raise WorkflowError(code, message)


def batch_edit(request, *, checkpoint_dir):
    """Run the candidate's batch operation in an isolated Blender worker."""
    from .batch_edit import run
    return run(request, checkpoint_dir=checkpoint_dir)


def verify_batch_resources(request, resources, *, expected=None, cache=None):
    """Validate a loaded VSE project's media and compare a saved expectation."""
    from .batch_resources import verify
    return verify(request, resources, expected=expected, cache=cache)


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
        for key in CAPTION_STYLE:
            if key in spec:
                value = getattr(media, key)
                result[key] = list(value) if key.endswith('_color') else value
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


def _scope(timeline, action):
    rate = timeline['fps'][0] / timeline['fps'][1]
    seconds = (timeline['frame_end_exclusive'] - timeline['frame_start']) / rate
    if (max(timeline['width'], timeline['height']) > max(RIPPLE_MAX_WIDTH, RIPPLE_MAX_HEIGHT) or
            timeline['width'] * timeline['height'] > RIPPLE_MAX_WIDTH * RIPPLE_MAX_HEIGHT or
            seconds > RIPPLE_MAX_SECONDS):
        _error('UNSUPPORTED', action + ' is limited to short SDR timelines up to '
               + str(RIPPLE_MAX_WIDTH) + 'x' + str(RIPPLE_MAX_HEIGHT) + ' and '
               + str(RIPPLE_MAX_SECONDS) + ' seconds')
    return {'name': 'short_SDR', 'max_width': RIPPLE_MAX_WIDTH,
            'max_height': RIPPLE_MAX_HEIGHT, 'max_seconds': RIPPLE_MAX_SECONDS,
            'width': timeline['width'], 'height': timeline['height'],
            'seconds': seconds, 'fps': timeline['fps']}


def _source_limit(scene, strip, spec):
    media = _media(strip)
    if spec['kind'] not in {'IMAGE', 'MOVIE', 'SOUND'}:
        return
    source = spec['source']
    if spec['kind'] == 'IMAGE' and len(source['files']) == 1:
        return
    count = int(media.content_duration)
    last = source['source_in'] if source['freeze'] else (
        source['source_in'] + math.floor((spec['duration'] - 1) * source['speed']))
    if last >= count:
        _error('INVALID_REQUEST', 'Updated duration exceeds actual source samples: ' + spec['id'])


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
        for key in CAPTION_STYLE.intersection(fields):
            setattr(strip, key, spec[key])
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
            # A pure unlock can release a manual strip lock without widening the edit.
            unlocking = strip.lock and op == 'update' and edit['changes'] == {'lock': False}
            if (strip.lock and not unlocking) or scene.sequence_editor.channels[strip.channel - 1].lock:
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
                if not unlocking and not _equal(current.get(field), baseline.get(field)) and not _equal(current.get(field), value):
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
                        # Compatibility with earlier saved strip records.
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


def export_cut_specs(scene_name, workflow_id):
    """Return a platform-neutral cut-only description of one managed timeline.

    The exchange remains lossless only for neutral Movie/Sound cuts. Anything
    OTIO cannot carry (generators, effects, retiming, grades, transforms, fades,
    subtitles, unmanaged strips or manual animation) is rejected here instead
    of being silently omitted by an exporter.
    """
    scene = bpy.data.scenes.get(scene_name)
    if scene is None or scene.get('_vwe_workflow') != workflow_id:
        _error('CONFLICT', 'Export target is not this workflow Scene')
    state = inspect(scene_name, workflow_id)
    timeline = state['timeline']
    scope = _scope(timeline, 'Cut-only OTIO')
    if state['diagnostics']['unmanaged_strips']:
        _error('UNSUPPORTED', 'Cut-only OTIO requires a fully managed Scene')
    if state['diagnostics']['manual_animation']:
        _error('UNSUPPORTED', 'Cut-only OTIO cannot represent manual animation')
    owned = _owned(scene, workflow_id)
    result = []
    names = set()
    for clip in state['clips']:
        cid, kind = clip['id'], clip['kind']
        if kind not in {'MOVIE', 'SOUND'}:
            _error('UNSUPPORTED', 'Cut-only OTIO supports only MOVIE/SOUND clips: ' + cid)
        if clip.get('alternatives'):
            _error('UNSUPPORTED', 'Cut-only OTIO cannot carry alternative candidates: ' + cid)
        if clip.get('fade_in') or clip.get('fade_out'):
            _error('UNSUPPORTED', 'Cut-only OTIO cannot carry fades: ' + cid)
        source = clip['source']
        if source.get('speed', 1) != 1 or source.get('freeze', False):
            _error('UNSUPPORTED', 'Cut-only OTIO cannot carry retiming: ' + cid)
        if source.get('stream', 0) != 0:
            _error('UNSUPPORTED', 'Cut-only OTIO cannot carry non-default streams: ' + cid)
        if len(source['files']) != 1:
            _error('UNSUPPORTED', 'Cut-only OTIO requires one source file per clip: ' + cid)
        strip = owned[cid]
        if kind == 'MOVIE':
            if source.get('color_space') != 'sRGB':
                _error('UNSUPPORTED', 'Cut-only OTIO requires sRGB movie sources: ' + cid)
            transform = clip.get('transform', {})
            crop = clip.get('crop', {})
            grade = clip.get('grade', {})
            if (clip.get('opacity', 1) != 1 or
                    any(transform.get(key, default) != default for key, default in
                        (('x', 0), ('y', 0), ('scale_x', 1), ('scale_y', 1), ('rotation_degrees', 0))) or
                    any(crop.get(key, 0) != 0 for key in ('left', 'right', 'top', 'bottom')) or
                    grade.get('multiply', 1) != 1 or grade.get('saturation', 1) != 1):
                _error('UNSUPPORTED', 'Cut-only OTIO cannot carry transforms, crops, grades or opacity: ' + cid)
        else:
            if clip.get('volume', 1) != 1 or clip.get('pan', 0) != 0:
                _error('UNSUPPORTED', 'Cut-only OTIO cannot carry volume/pan: ' + cid)
        _source_limit(scene, strip, clip)
        media = _media(strip)
        row = copy.deepcopy(clip)
        row['full_length'] = int(media.content_duration)
        if row['name'] in names:
            _error('UNSUPPORTED', 'Cut-only OTIO requires unique clip names: ' + row['name'])
        names.add(row['name'])
        result.append(row)
    if not result:
        _error('UNSUPPORTED', 'Cut-only OTIO requires at least one managed clip')
    result.sort(key=lambda row: (row['channel'], row['start'], row['id']))
    return {'scene': scene.name, 'workflow_id': workflow_id, 'timeline': timeline,
            'channels': max(row['channel'] for row in result), 'clips': result,
            'scope': scope}


def ripple(scene_name, workflow_id, clip_id, duration, edge='END'):
    """Extend/shorten one managed clip's end and move later managed material.

    Only clips that begin at or after the old half-open end move. Material that
    crosses the boundary stays anchored; the output frame range is at least the
    remaining material and the shifted timeline end. All changed strips are
    excluded from the non-target witness and are independently reopened.
    """
    scene = bpy.data.scenes.get(scene_name)
    if scene is None or scene.get('_vwe_workflow') != workflow_id:
        _error('CONFLICT', 'Ripple target is not this workflow Scene')
    if edge != 'END':
        _error('UNSUPPORTED', 'Only right-edge duration ripple is supported')
    if not isinstance(duration, int) or isinstance(duration, bool) or duration < 1:
        _error('INVALID_REQUEST', 'Ripple duration must be a positive integer')
    state = inspect(scene_name, workflow_id)
    old_timeline = _timeline(scene)
    _scope(old_timeline, 'Ripple editing')
    owned = _owned(scene, workflow_id)
    specs = {row['id']: copy.deepcopy(row) for row in state['clips']}
    if clip_id not in specs:
        _error('NOT_FOUND', 'Clip missing: ' + clip_id)
    current = specs[clip_id]
    strip = owned[clip_id]
    if strip.lock or scene.sequence_editor.channels[strip.channel - 1].lock:
        _error('CONFLICT', 'Clip is locked: ' + clip_id)
    baseline = _read(strip, '_vwe_baseline', {})
    if (not _equal(current.get('duration'), baseline.get('duration')) and
            not _equal(current.get('duration'), duration)):
        _error('CONFLICT', 'Manual edit conflicts at ' + clip_id + '.duration')
    if clip_id in state['diagnostics']['manual_animation']:
        _error('CONFLICT', 'Manual animation conflicts: ' + clip_id)
    old_end = current['start'] + current['duration']
    delta = duration - current['duration']
    target = copy.deepcopy(current)
    target['duration'] = duration
    shifted = []
    if delta:
        shifted = sorted((cid for cid, spec in specs.items()
                          if cid != clip_id and spec['start'] >= old_end),
                         key=lambda cid: (specs[cid]['start'], specs[cid]['channel'], cid))
        for cid in shifted:
            spec, current_strip = specs[cid], owned[cid]
            if current_strip.lock or scene.sequence_editor.channels[current_strip.channel - 1].lock:
                _error('CONFLICT', 'Ripple would move a locked clip: ' + cid)
            if cid in state['diagnostics']['manual_animation']:
                _error('CONFLICT', 'Ripple would move manually animated clip: ' + cid)
            base = _read(current_strip, '_vwe_baseline', {})
            if not _equal(spec.get('start'), base.get('start')) or not _equal(spec.get('duration'), base.get('duration')):
                _error('CONFLICT', 'Ripple would compound manual timing: ' + cid)
            moved = copy.deepcopy(spec)
            moved['start'] += delta
            specs[cid] = moved
        for unknown in state['diagnostics']['unmanaged_strips']:
            if unknown['start'] >= old_end:
                _error('UNSUPPORTED', 'Ripple would leave unmanaged strip behind: ' + unknown['name'])
    _source_limit(scene, strip, target)
    new_end = old_timeline['frame_end_exclusive'] + delta
    for spec in specs.values():
        new_end = max(new_end, spec['start'] + spec['duration'])
    for unknown in state['diagnostics']['unmanaged_strips']:
        new_end = max(new_end, unknown['start'] + unknown['duration'])
    new_timeline = copy.deepcopy(old_timeline)
    new_timeline['frame_end_exclusive'] = max(new_end, new_timeline['frame_start'] + 1)
    _scope(new_timeline, 'Ripple editing')
    specs[clip_id] = validate_clip(target, new_timeline)
    for cid, spec in list(specs.items()):
        specs[cid] = validate_clip(spec, new_timeline)
    _preflight_collisions(specs, state['diagnostics']['unmanaged_strips'])

    touched = [clip_id] + shifted
    excluded, excluded_paths = [], []
    for cid in touched:
        changed_strip = owned[cid]
        excluded.append(changed_strip.name)
        excluded_paths.append(changed_strip.path_from_id())
        if changed_strip.type == 'META':
            excluded_paths.extend(child.path_from_id() for child in changed_strip.strips)
    before = snapshot_protection(scene_name, excluded, excluded_paths)
    _set_timeline(scene, new_timeline)
    # Native handle setters resolve an overlap by moving the edited strip to
    # another channel. Vacate the destination first: positive ripple from the
    # right, negative ripple after the target has made room on the left.
    order = (list(reversed(shifted)) + [clip_id]) if delta > 0 else ([clip_id] + shifted)
    for cid in order:
        changed_strip = owned[cid]
        prior = {key: {'baseline': _read(changed_strip, key, {} if key != '_vwe_curves' else []),
                       'observed': observed}
                 for key, observed in (('_vwe_baseline', _actual(scene, changed_strip)),
                                       ('_vwe_curves', _curve_snapshot(scene, changed_strip)),
                                       ('_vwe_native', native_strip_record(changed_strip)))}
        if cid == clip_id:
            if changed_strip.type == 'META':
                retiming.update_mapping(scene, changed_strip, specs[cid]['start'], specs[cid]['duration'])
            else:
                changed_strip.right_handle = specs[cid]['start'] + specs[cid]['duration']
            fields = {'start', 'duration', 'channel'}
        else:
            if changed_strip.type == 'META':
                retiming.update_mapping(scene, changed_strip, specs[cid]['start'], specs[cid]['duration'])
            else:
                changed_strip.content_start += delta
            fields = {'start', 'duration', 'channel'}
        _style(scene, changed_strip, specs[cid], fields)
        changed_strip['_vwe_spec'] = _json(specs[cid])
        _assert_fields(scene, changed_strip, specs[cid], fields)
        _store(scene, changed_strip, specs[cid], prior)
    after = snapshot_protection(scene_name, excluded, excluded_paths)
    protection = compare_protection(before, after)
    if protection['status'] != 'pass':
        _error('VALIDATION_FAILED', 'Non-target project contents changed during ripple edit')
    changed = [clip_id] + shifted
    operation_log = [{'op': 'update', 'id': clip_id, 'fields': ['duration']}]
    operation_log.extend({'op': 'ripple_shift', 'id': cid, 'fields': ['start'], 'delta': delta}
                         for cid in shifted)
    return {'workflow_id': workflow_id, 'scene': scene_name, 'changed_ids': changed,
            'operation_log': operation_log,
            'ripple': {'id': clip_id, 'edge': 'END', 'boundary': old_end,
                       'old_duration': current['duration'], 'new_duration': duration,
                       'delta': delta, 'shifted_ids': shifted,
                       'timeline_before': old_timeline, 'timeline_after': new_timeline,
                       'scope': 'short_SDR'},
            'inspection': inspect(scene_name, workflow_id),
            'protection': {**protection, 'status': 'passed',
                           'scope': 'Existing non-target content'}}
