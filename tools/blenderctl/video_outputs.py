"""Qualified SDR outputs from the current candidate, without reconstructing it.

Host owns resource locks and candidate verification. Every output is new and job
local. Audio is decoded synchronously in bounded chunks; native sound.mixdown is
never used. HDR, retimed audio and unmodelled audio animation reject explicitly.
"""
from pathlib import Path
from fractions import Fraction
import contextlib
import json
import math
import os
import subprocess
import time
import uuid
import wave

import bpy
from protocol import Failure, atomic_json, digest
from video_editing_workflow_addon import contract

RATE = 48000
CHUNK = RATE * 2


def _emit(progress, stage, **details):
    if progress:
        progress(stage, **details)


def _file(path, **extra):
    path = Path(path)
    return {'file': str(path.resolve()), 'sha256': digest(path),
            'bytes': path.stat().st_size, **extra}


def _inspection(params, report):
    data = report.get('saved_expectation', {}).get('inspection') or report.get('inspection')
    if not data:
        from video_editing_workflow_addon import core
        data = core.inspect(params['manifest']['scene'], params['manifest'].get('workflow_id'))
    if not isinstance(data, dict) or not data.get('timeline'):
        raise Failure('VALIDATION_FAILED', 'Output requires actual saved timeline inspection')
    return data


def _actual_clips(data):
    rows = []
    for value in data.get('clips', []):
        row = dict(value)
        actual = row.get('actual')
        if isinstance(actual, dict):
            row.update(actual)
        if not {'kind', 'start', 'duration'} <= row.keys():
            raise Failure('VALIDATION_FAILED', 'Inspection clip lacks actual kind/start/duration')
        rows.append(row)
    return rows


def _tool(params, key):
    descriptor = params['output'].get(key)
    if not isinstance(descriptor, dict) or not {'file', 'expected_sha256'} <= descriptor.keys():
        raise Failure('INVALID_REQUEST', 'Explicit output.' + key + ' descriptor required')
    path = Path(descriptor['file'])
    if not path.is_absolute() or not path.is_file():
        raise Failure('NOT_FOUND', 'Declared output tool does not exist')
    declared = {str(Path(r['file']).resolve()).casefold(): r['expected_sha256'] for r in params['resources']}
    if declared.get(str(path.resolve()).casefold()) != descriptor['expected_sha256']:
        raise Failure('INVALID_REQUEST', 'Output tool must be declared in resources with matching SHA')
    if digest(path) != descriptor['expected_sha256']:
        raise Failure('CONFLICT', 'Declared output tool SHA changed')
    return str(path.resolve())


def _external(command, root, label, progress):
    """No shell; bounded log memory and cancellation while the owned child runs."""
    stdout = root / (label + '.stdout.txt')
    stderr = root / (label + '.stderr.txt')
    with stdout.open('xb') as out, stderr.open('xb') as err:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
        try:
            while True:
                _emit(progress, label, child_pid=process.pid)
                try:
                    code = process.wait(timeout=.25)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            raise
    if code:
        tail = stderr.read_bytes()[-3000:].decode('utf-8', errors='replace')
        raise Failure('VALIDATION_FAILED', label + ' failed (' + str(code) + '): ' + tail)
    return stdout


def _capture(scene):
    targets = [
        (scene, ['frame_start', 'frame_end', 'frame_current', 'frame_subframe']),
        (scene.render, ['resolution_x', 'resolution_y', 'resolution_percentage',
                       'threads_mode', 'threads',
                       'filepath', 'use_file_extension', 'use_sequencer',
                       'use_border', 'use_multiview', 'use_stamp', 'use_render_cache',
                       'use_texture_cache', 'use_auto_generate_texture_cache']),
        (scene.render.image_settings, ['media_type', 'file_format', 'color_mode',
                                      'color_depth', 'compression']),
    ]
    return [(owner, {k: getattr(owner, k) for k in keys if hasattr(owner, k)})
            for owner, keys in targets]


@contextlib.contextmanager
def _temporary(scene):
    saved = _capture(scene)
    window = bpy.context.window
    original_scene = window.scene if window else None
    try:
        if window:
            window.scene = scene
        scene.render.threads_mode = 'FIXED'
        scene.render.threads = int(os.environ.get('VWE_RENDER_THREADS', '2'))
        yield
    finally:
        # media_type must precede the dependent format; capture preserves order.
        frame = saved[0][1]
        for owner, values in saved[1:]:
            for key, value in values.items():
                setattr(owner, key, value)
        scene.frame_start = frame['frame_start']
        scene.frame_end = frame['frame_end']
        scene.frame_set(frame['frame_current'], subframe=frame['frame_subframe'])
        if window and original_scene:
            window.scene = original_scene
        after = _capture(scene)
        if any(before != now for (_, before), (_, now) in zip(saved, after)):
            raise Failure('VALIDATION_FAILED', 'Temporary output settings were not restored')


def _no_external_compositor(scene):
    if not math.isclose(scene.render.pixel_aspect_x, scene.render.pixel_aspect_y, rel_tol=1e-6):
        raise Failure('UNSUPPORTED', 'Non-square-pixel video output needs an explicit aspect conversion')
    if not scene.render.use_compositing:
        return
    tree = getattr(scene, 'node_tree', None)
    visited = set()
    def visit(group):
        if not group or group.as_pointer() in visited:
            return
        visited.add(group.as_pointer())
        for node in group.nodes:
            if node.bl_idname == 'CompositorNodeOutputFile' and not node.mute:
                raise Failure('UNSUPPORTED', 'Scene compositor has external File Output; no implicit external writes')
            visit(getattr(node, 'node_tree', None))
    visit(tree)


def _png_check(path, width, height):
    import numpy as np
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        if tuple(image.size) != (width, height):
            raise Failure('VALIDATION_FAILED', 'PNG dimensions differ from requested output')
        pixels = np.empty(len(image.pixels), dtype=np.float32)
        image.pixels.foreach_get(pixels)
        if not np.isfinite(pixels).all():
            raise Failure('VALIDATION_FAILED', 'PNG contains nonfinite decoded pixels')
        return {'width': width, 'height': height, 'decoded_values': len(pixels),
                'finite': True, 'min': float(pixels.min()), 'max': float(pixels.max())}
    finally:
        bpy.data.images.remove(image)


def _render_frames(scene, interval, width, height, root, progress):
    frames = root / 'frames'
    frames.mkdir()
    # Keep the authored canvas: changing resolution_x/y changes VSE geometry,
    # while render percentage correctly scales transforms, crops and text.
    scene.render.resolution_percentage = int(width * 100 / scene.render.resolution_x)
    scene.render.use_sequencer = True
    scene.render.use_file_extension = True
    scene.render.use_border = False
    scene.render.use_multiview = False
    scene.render.use_stamp = False
    for key in ('use_render_cache', 'use_texture_cache', 'use_auto_generate_texture_cache'):
        if hasattr(scene.render, key):
            setattr(scene.render, key, False)
    settings = scene.render.image_settings
    settings.media_type = 'IMAGE'
    settings.file_format = 'PNG'
    settings.color_mode = 'RGBA'
    settings.color_depth = '8'
    settings.compression = 15
    result = []
    for index, frame in enumerate(range(*interval)):
        _emit(progress, 'output_frame', frame=frame, completed=index, total=interval[1] - interval[0])
        path = frames / ('frame-' + str(index).zfill(8) + '.png')
        if path.exists():
            raise Failure('CONFLICT', 'Frame output already exists')
        scene.frame_set(frame)
        # Blender 5.2 SPEED can reuse stale integer-frame cache at fractional
        # sample times. Invalidate before every requested output frame.
        if scene.sequence_editor:
            for strip in scene.sequence_editor.strips_all:
                strip.invalidate_cache('RAW')
                strip.invalidate_cache('COMPOSITE')
        scene.render.filepath = str(path)
        completed = bpy.ops.render.render(write_still=True, scene=scene.name)
        if 'FINISHED' not in completed or not path.is_file():
            raise Failure('VALIDATION_FAILED', 'Frame render did not complete')
        result.append(_file(path, frame=frame, sequence_index=index,
                            image=_png_check(path, width, height)))
        atomic_json(root / 'frames-checkpoint.json', {'status': 'partial', 'last_complete': result[-1],
                    'completed': len(result), 'total': interval[1] - interval[0],
                    'resume_supported': False, 'reason': 'Candidate resume supported by Host; frame reuse not yet qualified'})
    atomic_json(root / 'frames-checkpoint.json', {'status': 'passed', 'outputs': result,
                                                'resume_supported': False})
    return result


def _action_curves(scene):
    animation = scene.animation_data
    if not animation or not animation.action:
        return []
    action = animation.action
    curves = list(getattr(action, 'fcurves', []))
    slot = getattr(animation, 'action_slot', None)
    for layer in getattr(action, 'layers', []):
        for action_strip in layer.strips:
            for bag in getattr(action_strip, 'channelbags', []):
                handle = getattr(bag, 'slot_handle', None)
                if slot and handle is not None and handle != slot.handle:
                    continue
                curves.extend(bag.fcurves)
    return curves


def _envelope(scene, strip, row, curves):
    volume_path = strip.path_from_id('volume')
    pan_path = strip.path_from_id('pan')
    matched = [curve for curve in curves if curve.data_path == volume_path]
    if any(curve.data_path == pan_path for curve in curves):
        raise Failure('UNSUPPORTED', 'Animated audio pan is not qualified')
    animation = scene.animation_data
    if animation:
        if any(curve.data_path in (volume_path, pan_path) for curve in animation.drivers):
            raise Failure('UNSUPPORTED', 'Driven audio gain/pan is not qualified')
        if any(not track.mute for track in animation.nla_tracks):
            raise Failure('UNSUPPORTED', 'Audio scene with active NLA needs evaluated mix qualification')
    fade_in, fade_out = row.get('fade_in', 0), row.get('fade_out', 0)
    if not fade_in and not fade_out:
        if matched:
            raise Failure('UNSUPPORTED', 'Unmodelled volume animation is not qualified')
        return None, float(strip.volume)
    if len(matched) != 1:
        raise Failure('UNSUPPORTED', 'Managed audio fades require one verified editable volume curve')
    curve = matched[0]
    if curve.modifiers or curve.extrapolation != 'CONSTANT':
        raise Failure('UNSUPPORTED', 'Audio curve modifiers/extrapolation are not qualified')
    base = row['volume']
    start, end = strip.left_handle, strip.right_handle
    expected = {}
    if fade_in:
        expected[start] = 0.0
        expected[start + fade_in] = base
    if fade_out:
        expected[end - fade_out] = base
        expected[end] = 0.0
    points = sorted((float(key.co[0]), float(key.co[1])) for key in curve.keyframe_points)
    wanted = sorted(expected.items())
    if len(points) != len(wanted) or any(abs(x-a) > 1e-5 or abs(y-b) > 1e-5 for (x,y),(a,b) in zip(points,wanted)):
        raise Failure('UNSUPPORTED', 'Actual volume curve differs from declared fade envelope')
    if any(key.interpolation != 'LINEAR' for key in curve.keyframe_points):
        raise Failure('UNSUPPORTED', 'Only verified LINEAR audio fades are qualified')
    if abs(strip.volume - curve.evaluate(scene.frame_current + scene.frame_subframe)) > 1e-5:
        raise Failure('UNSUPPORTED', 'Actual animated volume differs from evaluated managed curve')
    return points, base


def _audio_tracks(scene, clips, params):
    rows = []
    editor = scene.sequence_editor
    if not editor:
        return rows
    declared = {str(Path(r['file']).resolve()).casefold(): r['expected_sha256'] for r in params['resources']}
    named = {row.get('name'): row for row in clips if row['kind'] == 'SOUND'}
    ids = {row.get('id'): row for row in clips if row['kind'] == 'SOUND'}
    curves = _action_curves(scene)
    for strip in editor.strips_all:
        if strip.type != 'SOUND' or strip.mute:
            continue
        parent = strip.parent_meta()
        if parent:
            raise Failure('UNSUPPORTED', 'Nested SOUND needs qualified Meta timing/mute mapping')
        if editor.channels[strip.channel - 1].mute:
            continue
        row = ids.get(strip.get('_vwe_clip_id')) or named.get(strip.name)
        if not row:
            # Ordinary manual strips are preserved, but unknown retiming/fades
            # cannot be represented truthfully by a manifest-based decoder.
            raise Failure('UNSUPPORTED', 'Audio requires inspected metadata for SOUND: ' + strip.name)
        source = row.get('source', {})
        if source.get('speed', 1) != 1 or source.get('freeze', False) or source.get('stream', 0) != 0:
            raise Failure('UNSUPPORTED', 'Audio speed/freeze is not yet qualified')
        if len(strip.retiming_keys):
            raise Failure('UNSUPPORTED', 'Native retimed audio is not yet qualified')
        if strip.modifiers:
            raise Failure('UNSUPPORTED', 'Audio modifiers need an evaluated decoder')
        path = Path(bpy.path.abspath(strip.sound.filepath)).resolve()
        if str(path).casefold() not in declared:
            raise Failure('INVALID_REQUEST', 'Actual SOUND source is not declared in resources')
        if digest(path) != declared[str(path).casefold()]:
            raise Failure('CONFLICT', 'Actual SOUND resource SHA differs')
        if not math.isfinite(strip.volume) or not math.isfinite(strip.pan):
            raise Failure('VALIDATION_FAILED', 'Nonfinite actual audio gain/pan')
        if abs(strip.pan) > 1e-12:
            raise Failure('UNSUPPORTED', 'Nonzero VSE pan needs native-law parity qualification; no custom balance substitution')
        points, volume = _envelope(scene, strip, row, curves)
        rows.append({'strip': strip, 'file': str(path), 'start': int(strip.left_handle),
                     'end': int(strip.right_handle), 'content_start': float(strip.content_start),
                     'animation_offset': int(strip.animation_offset_start),
                     'sound_offset': float(strip.sound_offset), 'volume': volume, 'envelope': points,
                     'pan': float(strip.pan), 'fade_in': row.get('fade_in', 0),
                     'fade_out': row.get('fade_out', 0)})
    return rows


def _audio_source_cache(tracks, params, root, progress):
    """Decode each source once from sample zero; never seek by float seconds.

    Resampling before integer slicing avoids decoder seek rounding and changing
    the resampler phase at each mix chunk. Float PCM preserves over-range values
    for the existing clipping check instead of clipping during normalization.
    """
    if not tracks:
        return {}
    ffmpeg, ffprobe = _tool(params, 'ffmpeg'), _tool(params, 'ffprobe')
    directory = root / 'audio-sources'
    directory.mkdir()
    sources = {}
    for track in tracks:
        filename = track['file']
        if filename in sources:
            continue
        label = 'source-' + str(len(sources)).zfill(4)
        probe = _external([ffprobe, '-v', 'error', '-select_streams', 'a:0',
            '-show_entries', 'stream=sample_rate,channels,codec_name', '-of', 'json', filename],
            directory, label + '-probe', progress)
        streams = json.loads(probe.read_text(encoding='utf-8'))['streams']
        if len(streams) != 1 or int(streams[0].get('channels', 0)) not in (1, 2):
            raise Failure('UNSUPPORTED', 'Audio source requires qualified mono or stereo input')
        channels = int(streams[0]['channels'])
        filters = ['aresample=48000:resampler=swr:dither_method=none']
        if channels == 1:
            filters.append('pan=stereo|c0=c0|c1=c0')
        partial = directory / (label + '.partial.f32le')
        path = directory / (label + '.f32le')
        command = [ffmpeg, '-nostdin', '-v', 'error', '-xerror', '-n', '-i', filename,
            '-map', '0:a:0', '-vn', '-sn', '-dn', '-af', ','.join(filters),
            '-ac', '2', '-ar', str(RATE), '-c:a', 'pcm_f32le', '-f', 'f32le', str(partial)]
        _external(command, directory, label + '-decode', progress)
        byte_count = partial.stat().st_size
        if not byte_count or byte_count % 8:
            raise Failure('VALIDATION_FAILED', 'Origin-zero float audio cache has invalid sample count')
        partial.rename(path)
        sources[filename] = {'file': str(path), 'sample_frames': byte_count // 8,
            'source': {'file': filename, 'expected_sha256': digest(filename)},
            'decoded': _file(path), 'input': streams[0],
            'rate': RATE, 'channels': 2, 'format': 'f32le', 'command': command,
            'origin': 'first decoded sample is source sample zero',
            'mono_policy': 'unity duplication', 'resampling': 'origin-zero swr, no dither'}
    atomic_json(directory / 'sources.json', list(sources.values()))
    return sources


def _mix_audio(scene, clips, params, interval, fps, root, progress):
    import numpy as np
    tracks = _audio_tracks(scene, clips, params)
    master = float(scene.audio_volume)
    if not math.isfinite(master) or master < 0:
        raise Failure('VALIDATION_FAILED', 'Invalid Scene audio master gain')
    animation = scene.animation_data
    if any(c.data_path == 'audio_volume' for c in _action_curves(scene)) or (animation and any(c.data_path == 'audio_volume' for c in animation.drivers)):
        raise Failure('UNSUPPORTED', 'Animated Scene master gain requires evaluated audio qualification')
    sources = _audio_source_cache(tracks, params, root, progress)
    count = round(Fraction((interval[1] - interval[0]) * RATE, 1) / fps)
    path = root / 'audio.wav'
    partial = root / 'audio.partial.wav'
    peak = 0.0
    clips_above_range = 0
    qa = {'status': 'running', 'rate': RATE, 'channels': 2, 'sample_frames': count,
          'chunk_sample_frames': CHUNK, 'tracks': len(tracks), 'finite': True,
          'peak': 0.0, 'clipped_samples': 0, 'master_gain': master,
          'sample_count_rounding': 'nearest, ties to even',
          'source_sample_rounding': 'nearest, ties to even; integer seek after origin-zero decode',
          'decoder': 'SHA-bound FFmpeg origin-zero float32 PCM, integer sample windows',
          'decoded_sources': list(sources.values())}
    with wave.open(str(partial), 'wb') as wav:
        wav.setparams((2, 2, RATE, 0, 'NONE', 'not compressed'))
        for offset in range(0, count, CHUNK):
            _emit(progress, 'output_audio', completed=offset, total=count)
            size = min(CHUNK, count - offset)
            mixed = np.zeros((size, 2), dtype=np.float64)
            # Sample zero is the left edge of the requested half-open range.
            absolute_start = Fraction(interval[0], 1) / fps + Fraction(offset, RATE)
            absolute_end = absolute_start + Fraction(size, RATE)
            for track in tracks:
                left = max(absolute_start, Fraction(track['start'], 1) / fps)
                right = min(absolute_end, Fraction(track['end'], 1) / fps)
                if left >= right:
                    continue
                dest = round((left - absolute_start) * RATE)
                length = min(size - dest, round((right - left) * RATE))
                begin = (left - Fraction(str(track['content_start'])) / fps
                         + Fraction(track['animation_offset'], 1) / fps
                         + Fraction(str(track['sound_offset'])))
                if begin < -Fraction(1, RATE):
                    raise Failure('UNSUPPORTED', 'Audio begins before source content')
                source_start = max(0, round(begin * RATE))
                source = sources[track['file']]
                if source_start + length > source['sample_frames']:
                    raise Failure('VALIDATION_FAILED', 'Decoded SOUND is shorter than the exact requested sample window')
                with open(source['file'], 'rb') as decoded:
                    decoded.seek(source_start * 8)
                    raw = decoded.read(length * 8)
                if len(raw) != length * 8:
                    raise Failure('VALIDATION_FAILED', 'Incomplete integer audio sample window')
                samples = np.frombuffer(raw, dtype='<f4').reshape(length, 2).astype(np.float64)
                if not np.isfinite(samples).all():
                    raise Failure('VALIDATION_FAILED', 'Nonfinite decoded audio')
                positions = float(left * fps) - track['start'] + np.arange(length) * float(fps) / RATE
                gain = np.full(length, track['volume'], dtype=np.float64)
                if track['envelope']:
                    coordinates, values = zip(*track['envelope'])
                    gain = np.interp(positions + track['start'], coordinates, values)
                # Balance pan leaves centre untouched; -1/+1 mute the opposite
                # channel. Declared explicitly, not an undocumented pan law.
                pan = track['pan']
                samples[:, 0] *= gain * (1 - max(0, pan))
                samples[:, 1] *= gain * (1 + min(0, pan))
                mixed[dest:dest + length] += samples
                del raw, samples, gain, positions
            mixed *= master
            if not np.isfinite(mixed).all():
                raise Failure('VALIDATION_FAILED', 'Nonfinite mixed audio')
            peak = max(peak, float(np.abs(mixed).max(initial=0)))
            clips_above_range += int(np.count_nonzero((mixed < -1) | (mixed > 32767 / 32768)))
            if clips_above_range:
                qa.update(status='failed', peak=peak, clipped_samples=clips_above_range,
                          reason='Mix exceeds PCM16 representable range; no silent clipping or successful final WAV')
                atomic_json(root / 'audio-qa.json', qa)
                return None, qa
            wav.writeframes(np.rint(mixed * 32768).astype('<i2').tobytes())
    partial.rename(path)
    decoded_count = 0
    with wave.open(str(path), 'rb') as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (2, 2, RATE):
            raise Failure('VALIDATION_FAILED', 'Completed WAV PCM format differs')
        while raw := wav.readframes(CHUNK):
            values = np.frombuffer(raw, dtype='<i2')
            decoded_count += len(values) // 2
        if decoded_count != count or wav.getnframes() != count:
            raise Failure('VALIDATION_FAILED', 'Completed WAV sample count differs')
    qa.update(status='passed', peak=peak, clipped_samples=0, decoded_sample_frames=decoded_count,
              pan_law='native data rechannel at centre; nonzero pan rejected until parity qualification',
              speed_support='unity only; arbitrary audio retiming rejected')
    atomic_json(root / 'audio-qa.json', qa)
    return _file(path, kind='WAV', audio=qa), qa


def _stamp(frame, fps):
    milliseconds = round(Fraction(frame * 1000, 1) / fps)
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds, ms = divmod(remainder, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}'


def _subtitles(clips, interval, fps, root):
    cues = []
    for row in clips:
        if row['kind'] != 'TEXT' or row.get('mute', False):
            continue
        left = max(interval[0], row['start'])
        right = min(interval[1], row['start'] + row['duration'])
        if left < right:
            text = row.get('text')
            if not isinstance(text, str):
                raise Failure('VALIDATION_FAILED', 'Inspected subtitle has no actual text')
            cues.append((left, right, text.replace('\r\n', '\n').replace('\r', '\n')))
    cues.sort(key=lambda x: (x[0], x[1]))
    path = root / 'subtitles.srt'
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        for i, (left, right, text) in enumerate(cues, 1):
            stream.write(f'{i}\n{_stamp(left - interval[0], fps)} --> {_stamp(right - interval[0], fps)}\n{text}\n\n')
    return _file(path, kind='SRT', cues=len(cues), timing='output-relative rational fps')


def _encode(params, frames, audio, width, height, fps, root, progress):
    ffmpeg, ffprobe = _tool(params, 'ffmpeg'), _tool(params, 'ffprobe')
    if width % 2 or height % 2:
        raise Failure('INVALID_REQUEST', 'H264 yuv420p requires even dimensions')
    path = root / 'video.mp4'
    command = [ffmpeg, '-nostdin', '-n', '-v', 'error', '-framerate', str(fps),
               '-start_number', '0', '-i', str(root / 'frames' / 'frame-%08d.png')]
    if audio:
        command += ['-i', audio['file']]
    command += ['-map', '0:v:0']
    if audio:
        command += ['-map', '1:a:0', '-c:a', 'aac', '-b:a', '192k', '-ar', str(RATE)]
    else:
        command += ['-an']
    # Preserve rendered SDR sRGB transfer, explicitly convert RGB to BT709 YUV;
    # signal transfer truthfully as sRGB rather than mislabelling it BT709.
    command += ['-vf', 'scale=in_range=pc:out_range=tv:out_color_matrix=bt709',
                '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-pix_fmt', 'yuv420p',
                '-colorspace', 'bt709', '-color_primaries', 'bt709',
                '-color_trc', 'iec61966-2-1', '-color_range', 'tv',
                '-frames:v', str(len(frames)), '-movflags', '+faststart', str(path)]
    _external(command, root, 'output_encode', progress)
    streamfile = _external([ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)], root, 'output_probe_streams', progress)
    info = json.loads(streamfile.read_text(encoding='utf-8'))
    videos = [stream for stream in info['streams'] if stream['codec_type'] == 'video']
    audios = [stream for stream in info['streams'] if stream['codec_type'] == 'audio']
    if len(videos) != 1 or (len(audios) != (1 if audio else 0)):
        raise Failure('VALIDATION_FAILED', 'Encoded video/audio stream count differs')
    video = videos[0]
    if (int(video['width']), int(video['height'])) != (width, height) or Fraction(video['avg_frame_rate']) != fps:
        raise Failure('VALIDATION_FAILED', 'MP4 dimensions/rational frame rate differ')
    ptsfile = _external([ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_frames',
        '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'csv=p=0', str(path)], root, 'output_probe_frames', progress)
    count = 0
    max_error = 0.0
    previous = None
    tolerance = max(float(Fraction(video['time_base'])) * 1.1, 1.1e-6)
    with ptsfile.open(encoding='utf-8') as stream:
        for line in stream:
            token = line.strip().split(',')[0]
            if not token:
                continue
            try:
                timestamp = float(token)
            except ValueError as exc:
                raise Failure('VALIDATION_FAILED', 'Non-numeric video frame PTS') from exc
            error = abs(timestamp - float(Fraction(count, 1) / fps))
            max_error = max(error, max_error)
            if not math.isfinite(timestamp) or (previous is not None and timestamp <= previous) or error > tolerance:
                raise Failure('VALIDATION_FAILED', 'Full video PTS check failed')
            previous = timestamp
            count += 1
    if count != len(frames):
        raise Failure('VALIDATION_FAILED', 'Full MP4 frame count differs')
    _external([ffmpeg, '-nostdin', '-v', 'error', '-xerror', '-i', str(path), '-map', '0', '-f', 'null', '-'], root, 'output_decode_full', progress)
    qa = {'status': 'passed', 'frames': count, 'width': width, 'height': height,
          'fps': [fps.numerator, fps.denominator], 'full_pts': 'passed',
          'pts_max_error_seconds': max_error, 'full_decode': 'passed',
          'color_mode': 'SDR', 'color_transfer': video.get('color_transfer'),
          'color_primaries': video.get('color_primaries'), 'color_space': video.get('color_space'),
          'audio_streams': len(audios), 'source_wav_sample_count_checked': bool(audio)}
    if audio and (int(audios[0].get('sample_rate', 0)) != RATE or int(audios[0].get('channels', 0)) != 2):
        raise Failure('VALIDATION_FAILED', 'MP4 audio rate/channel count differs')
    return _file(path, kind='MP4', video=qa), qa


def render_outputs(params, job, report, progress):
    """Return verified output descriptors; restore all temporary candidate state."""
    output = params['output']
    if output.get('color_mode', 'SDR') != 'SDR':
        raise Failure('UNSUPPORTED', 'HDR10_PQ output has not been qualified; no SDR substitution')
    inspected = _inspection(params, report)
    timeline = inspected['timeline']
    clips = _actual_clips(inspected)
    try:
        budget = contract.validate_output_budget(output, timeline, clips)
    except contract.WorkflowError as exc:
        raise Failure(exc.code, str(exc)) from exc
    scene_name = inspected.get('scene') or params['manifest']['scene']
    scene = bpy.data.scenes.get(scene_name)
    if scene is None:
        raise Failure('NOT_FOUND', 'Inspected candidate scene not loaded')
    if output['format'] == 'MP4':
        _tool(params, 'ffmpeg')
        _tool(params, 'ffprobe')
        if budget['width'] % 2 or budget['height'] % 2:
            raise Failure('INVALID_REQUEST', 'H264 output requires even dimensions')
    _no_external_compositor(scene)
    job = Path(job).resolve()
    root = job / ('video-output-' + uuid.uuid4().hex)
    root.mkdir()
    fps = Fraction(*timeline['fps'])
    ranges = [('main', budget['frames'], None)]
    for index, row in enumerate(contract.output_shots(output, timeline, clips)):
        ranges.append(('shot-' + str(index).zfill(6), row['frames'],
                       {'clip_ids': row['clip_ids'], 'shot_id': row['shot_id']}))
    result = {'status': 'running', 'outputs': [], 'audio_qa': [], 'video_qa': [],
              'budget': budget, 'color_mode': 'SDR', 'timeline_authority': 'current saved candidate, no manifest reconstruction',
              'resume': {'frame_reuse': False, 'candidate_checkpoint': 'Host-owned; output restarts into new job directory'},
              'checks': {'visual_quality': 'not_run', 'audio_listening': 'not_run'}}
    with _temporary(scene):
        for label, interval, shot in ranges:
            range_root = root / label
            range_root.mkdir()
            audio = None
            if output.get('audio', True):
                audio, qa = _mix_audio(scene, clips, params, interval, fps, range_root, progress)
                result['audio_qa'].append({'range': label, **qa})
                if qa['status'] != 'passed':
                    result['status'] = 'failed'
                    atomic_json(root / 'output-report.json', result)
                    return result
                result['outputs'].append({**audio, 'range': label, 'frames': interval, 'shot': shot})
            frames = _render_frames(scene, interval, budget['width'], budget['height'], range_root, progress)
            result['outputs'].extend({**row, 'range': label, 'shot': shot} for row in frames)
            if output.get('subtitles'):
                result['outputs'].append({**_subtitles(clips, interval, fps, range_root), 'range': label, 'shot': shot})
            if output['format'] == 'MP4':
                movie, qa = _encode(params, frames, audio, budget['width'], budget['height'], fps, range_root, progress)
                result['outputs'].append({**movie, 'range': label, 'frames': interval, 'shot': shot})
                result['video_qa'].append({'range': label, **qa})
            else:
                result['video_qa'].append({'range': label, 'status': 'passed', 'frames': len(frames),
                                           'full_png_decode': 'passed', 'width': budget['width'], 'height': budget['height']})
            atomic_json(root / 'output-report.json', result)
    result['status'] = 'passed'
    result['checks']['settings_restored'] = 'passed'
    atomic_json(root / 'output-report.json', result)
    result['report'] = _file(root / 'output-report.json', kind='OUTPUT_REPORT')
    return result
