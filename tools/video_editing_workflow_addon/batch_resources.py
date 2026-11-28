"""Exact VSE resource closure and decoded movie timing for the saved-file worker."""
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import subprocess

import bpy

from . import core
from .batch_contract import normalize_batch_edit
from .contract import FILE, FRAME_MIN, WorkflowError, _clip_descriptors, _validate, arr, validate_clip
from .protection import resource_paths


FFPROBE_PROJECT_PATH = Path('tools/ffmpeg/ffmpeg-9.0.2-essentials_build/bin/ffprobe.exe')
# Matches the existing local Blender 5.3 migration's short-output-settings.json.
FFPROBE_APPROVED_SHA256 = 'f0d36ecbbdd3bcfac3efa078c96c7271c2e68b3810595552ac3b7f17e9a65c52'


def _fail(code, message):
    raise WorkflowError(code, message)


def _path(value):
    path = Path(value)
    if not path.is_absolute():
        _fail('INVALID_REQUEST', 'Video resource needs an absolute path: ' + value)
    return str(path.resolve())


class ResourceClosure:
    def __init__(self, resources):
        _validate(resources, arr(FILE), 'resources')
        self.declared = {}
        self.checked = {}
        for resource in resources:
            path = _path(resource['file'])
            key = path.casefold()
            if key in self.declared:
                _fail('INVALID_REQUEST', 'Duplicate canonical video resource: ' + path)
            self.declared[key] = {'file': path, 'expected_sha256': resource['expected_sha256']}

    def check(self, path, expected=None):
        path = _path(path)
        key = path.casefold()
        row = self.declared.get(key)
        if row is None:
            _fail('CONFLICT', 'Video resource was not declared: ' + path)
        if expected is not None and expected != row['expected_sha256']:
            _fail('CONFLICT', 'Video resource declaration SHA differs: ' + path)
        if key not in self.checked:
            try:
                actual = core._sha(path)
            except FileNotFoundError:
                _fail('RESOURCE_MISSING', 'Video resource is missing: ' + path)
            except OSError as error:
                _fail('RESOURCE_MISSING', f'Video resource cannot be read: {path} ({error.strerror})')
            if actual != row['expected_sha256']:
                _fail('CONFLICT', 'Video resource bytes differ from declared SHA: ' + path)
            self.checked[key] = row
        return self.checked[key]


def _approved_probe(descriptor, closure):
    project = next((parent for parent in Path(__file__).resolve().parents
                    if (parent / 'blenderctl.ps1').is_file() and (parent / 'tools').is_dir()), None)
    if project is None:
        _fail('UNSUPPORTED', 'This VSE build has no approved FFprobe deployment location')
    approved = _path(str(project / FFPROBE_PROJECT_PATH))
    if (_path(descriptor['file']).casefold() != approved.casefold() or
            descriptor['expected_sha256'] != FFPROBE_APPROVED_SHA256):
        _fail('UNSUPPORTED', 'Use the approved project FFprobe path and version fingerprint: ' + approved)
    return closure.check(approved, FFPROBE_APPROVED_SHA256)


def _decode(movie, probe):
    # File-only demuxing excludes network sources and local editing playlists,
    # whose subordinate media is not represented by the given file descriptor.
    cmd = [probe['file'], '-v', 'error', '-protocol_whitelist', 'file',
           '-format_whitelist', 'mov,matroska,avi', '-select_streams', 'v:0', '-show_streams', '-show_frames',
           '-show_entries', 'stream=index,codec_name,avg_frame_rate,r_frame_rate,time_base,start_time,'
           'width,height,sample_aspect_ratio,duration_ts:frame=best_effort_timestamp,duration,width,height',
           '-of', 'json', movie['file']]
    try:
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, encoding='utf-8',
                                errors='replace', timeout=60, check=False,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        _fail('TIMEOUT', 'FFprobe exceeded its 60-second movie timing budget: ' + movie['file'])
    except OSError:
        _fail('VALIDATION_FAILED', 'Could not execute the declared FFprobe: ' + probe['file'])
    if result.returncode or result.stderr.strip():
        _fail('VALIDATION_FAILED', 'FFprobe reported a decode error: ' + movie['file'])
    try:
        data = json.loads(result.stdout)
    except ValueError:
        _fail('VALIDATION_FAILED', 'FFprobe did not return valid JSON: ' + movie['file'])
    streams = data.get('streams', []) if isinstance(data, dict) else []
    frames = data.get('frames', []) if isinstance(data, dict) else []
    if len(streams) != 1 or not frames:
        _fail('VALIDATION_FAILED', 'FFprobe did not decode one video stream: ' + movie['file'])
    return streams[0], frames


def _timing(movie, probe, fps, cache):
    key = (movie['file'].casefold(), movie['expected_sha256'], probe['file'].casefold(),
           probe['expected_sha256'], fps)
    if key in cache:
        return cache[key]
    stream, frames = _decode(movie, probe)
    path = movie['file']
    try:
        avg_rate, frame_rate = Fraction(stream['avg_frame_rate']), Fraction(stream['r_frame_rate'])
        base = Fraction(stream['time_base'])
        origin = Fraction(stream['start_time'])
        count = len(frames)
        pts = [Fraction(frame['best_effort_timestamp']) * base for frame in frames]
        durations = [Fraction(frame['duration']) * base for frame in frames]
        stream_extent = Fraction(stream['duration_ts']) * base
        size = (int(stream['width']), int(stream['height']))
    except (KeyError, ZeroDivisionError, TypeError, ValueError):
        _fail('UNSUPPORTED', 'MOVIE stream lacks complete timing or size evidence: ' + path)
    if avg_rate != fps or frame_rate != fps:
        _fail('UNSUPPORTED', 'MOVIE must be CFR at project FPS; explicitly conform to PNG+PCM: ' + path)
    if stream.get('sample_aspect_ratio') not in (None, 'N/A', '0:1', '1:1'):
        _fail('UNSUPPORTED', 'Anamorphic MOVIE needs square-pixel normalization: ' + path)
    tolerance = max(base, Fraction(1, 1_000_000))
    if base <= 0 or tolerance >= Fraction(1, 2) / fps:
        _fail('UNSUPPORTED', 'MOVIE time base cannot resolve project frames: ' + path)
    if abs(origin) > tolerance or abs(pts[0]) > tolerance:
        _fail('UNSUPPORTED', 'Nonzero MOVIE origin needs timestamp/AV normalization: ' + path)
    max_error = max(abs(value - pts[0] - Fraction(i, 1) / fps) for i, value in enumerate(pts))
    if max_error > tolerance or any(b <= a for a, b in zip(pts, pts[1:])):
        _fail('UNSUPPORTED', 'MOVIE decoded timestamps are not CFR; explicitly conform to PNG+PCM: ' + path)
    if min(size) <= 0 or any((row.get('width'), row.get('height')) != size for row in frames):
        _fail('UNSUPPORTED', 'MOVIE decoded frame size differs within the stream: ' + path)
    if any(abs(t - 1 / fps) > tolerance for t in durations) or abs(stream_extent - count / fps) > tolerance:
        _fail('UNSUPPORTED', 'MOVIE frame durations or decoded stream extent disagree: ' + path)
    evidence = {'frames': count, 'fps': [fps.numerator, fps.denominator], 'dimensions': list(size),
                'codec': stream.get('codec_name', ''), 'frame_origin_seconds': float(pts[0]),
                'decoded_pts_max_error_seconds': float(max_error),
                'decoded_timing_sha256': hashlib.sha256(json.dumps(frames, sort_keys=True).encode()).hexdigest()}
    cache[key] = evidence
    return evidence


def _require_probe(probe, stream):
    if stream != 0:
        _fail('UNSUPPORTED', 'MOVIE stream selection is qualified only for video stream 0')
    if probe is None:
        _fail('INVALID_REQUEST', 'MOVIE needs SHA-bound request.media_probe and a matching resources entry')


def _native_movie(movie, scene, fps, closure, probe, cache):
    _require_probe(probe, movie.stream_index)
    file = closure.check(bpy.path.abspath(movie.filepath, library=scene.library))
    timing = _timing(file, probe, fps, cache)
    native_count = int(movie.content_duration)
    if native_count != timing['frames']:
        _fail('UNSUPPORTED', 'Native MOVIE duration differs from decoded sample count: ' + movie.name)
    start, end = int(movie.left_handle), int(movie.right_handle)
    source_in = movie.left_handle - movie.content_start
    parent = movie.parent_meta()
    if source_in != int(source_in):
        _fail('UNSUPPORTED', 'Native MOVIE has a fractional source-frame offset: ' + movie.name)
    if parent is None and (source_in < 0 or source_in + end - start > native_count):
        _fail('UNSUPPORTED', 'Native MOVIE visible interval exceeds decoded source samples: ' + movie.name)
    return {'strip': movie.name, 'parent': parent.name if parent else None, **file, **timing,
            'stream': movie.stream_index, 'channel': movie.channel, 'start': start, 'end': end,
            'source_in': int(source_in), 'native_content_duration': native_count}


def verify(request, resources, *, expected=None, cache=None):
    """Hash native and requested dependencies, verify MOVIE timing, compare cold state.

    The production worker holds read locks on all declared files and runs this
    before edits, before save, and after reopening the staged candidate. A cache
    only reuses FFprobe evidence keyed by verified source/tool bytes and FPS.
    """
    request = normalize_batch_edit(request)
    closure = ResourceClosure(resources)
    cache = {} if cache is None else cache
    scene = bpy.data.scenes.get(request['scene'])
    if scene is None or scene.get('_vwe_workflow') != request['workflow_id']:
        _fail('CONFLICT', 'Batch resource target is not this workflow Scene')
    missing_linked = sorted(item.name_full for item in bpy.data.user_map() if getattr(item, 'is_missing', False))
    if missing_linked:
        _fail('RESOURCE_MISSING', f'Linked Blender data is missing: {missing_linked}')
    actual = [closure.check(path) for path in resource_paths()]
    actual.sort(key=lambda row: row['file'].casefold())
    probe = None
    if 'media_probe' in request:
        probe = _approved_probe(request['media_probe'], closure)
    fps = Fraction(*core._timeline(scene)['fps'])
    planned = {}
    for operation in request['operations']:
        for raw in operation.get('clips', ()):
            clip = validate_clip({**raw, 'start': FRAME_MIN})
            for descriptor in _clip_descriptors(clip):
                bound = closure.check(descriptor['file'], descriptor['expected_sha256'])
                planned[bound['file'].casefold()] = bound
            if clip['kind'] == 'MOVIE':
                source = clip['source']
                _require_probe(probe, source['stream'])
                file = source['files'][0]
                bound = closure.check(file['file'], file['expected_sha256'])
                _timing(bound, probe, fps, cache)
    strips = scene.sequence_editor.strips_all if scene.sequence_editor else ()
    movies = [_native_movie(strip, scene, fps, closure, probe, cache) for strip in strips if strip.type == 'MOVIE']
    movies.sort(key=lambda row: row['strip'])
    result = {'status': 'passed', 'resources': actual, 'planned_resources': [planned[k] for k in sorted(planned)],
              'media_probe': probe, 'movie_timing': {'status': 'passed' if movies else 'not_applicable',
                                                    'movies': movies}}
    if expected is not None and result != expected:
        fields = sorted(key for key in result.keys() | expected.keys() if result.get(key) != expected.get(key))
        _fail('VALIDATION_FAILED', 'Saved VSE resource expectation changed on reopen: ' + ', '.join(fields))
    return result
