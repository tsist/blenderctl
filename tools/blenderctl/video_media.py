"""Verify decoded MOVIE timestamps before accepting frame-index editing.

Native VSE duration can silently reinterpret mixed FPS and VFR. Conforming is
an explicit derivative operation; this reader never rewrites source media.
"""
from fractions import Fraction
from pathlib import Path
import json
import uuid

import bpy
from protocol import Failure, atomic_json, digest


def verify_movie_timing(params, job, progress):
    scene = bpy.data.scenes[params['manifest']['scene']]
    movies = [s for s in scene.sequence_editor.strips_all if s.type == 'MOVIE'] if scene.sequence_editor else []
    if not movies:
        return {'status': 'not_applicable', 'movies': 0}
    tool = params.get('media_probe') or params.get('output', {}).get('ffprobe')
    if not tool:
        raise Failure('INVALID_REQUEST', 'MOVIE input requires explicit SHA-bound params.media_probe (FFprobe), or output.ffprobe')
    declared = {r['file'].casefold(): r['expected_sha256'] for r in params['resources']}
    if declared.get(tool['file'].casefold()) != tool['expected_sha256'] or digest(tool['file']) != tool['expected_sha256']:
        raise Failure('CONFLICT', 'Media probe tool is undeclared or changed')
    from video_editing_workflow_addon import core
    fps_spec = core._timeline(scene)['fps']
    fps = Fraction(*fps_spec)
    root = Path(job) / ('media-timing-' + uuid.uuid4().hex)
    root.mkdir()
    from video_outputs import _external
    rows, cached = [], {}
    for movie in movies:
        path = str(Path(bpy.path.abspath(movie.filepath)).resolve())
        if movie.stream_index != 0:
            raise Failure('UNSUPPORTED', 'MOVIE nonzero stream needs separately qualified stream-index semantics')
        if path.casefold() not in declared or digest(path) != declared[path.casefold()]:
            raise Failure('CONFLICT', 'MOVIE bytes differ from declared resources')
        if path not in cached:
            label = 'media_probe_' + str(len(cached))
            output = _external([tool['file'], '-v', 'error', '-select_streams', 'v:0',
                '-show_streams', '-show_frames', '-show_entries',
                'stream=index,codec_type,avg_frame_rate,r_frame_rate,time_base,start_time,width,height,sample_aspect_ratio,duration_ts:frame=best_effort_timestamp,duration,width,height',
                '-of', 'json', path], root, label, progress)
            data = json.loads(output.read_text(encoding='utf-8'))
            streams = data.get('streams', [])
            frames = data.get('frames', [])
            if len(streams) != 1 or not frames:
                raise Failure('VALIDATION_FAILED', 'Media probe did not decode one video stream')
            stream = streams[0]
            if Fraction(stream['avg_frame_rate']) != fps or Fraction(stream['r_frame_rate']) != fps:
                raise Failure('UNSUPPORTED', 'Mixed FPS/VFR MOVIE must be explicitly conformed to project FPS as PNG+PCM before editing: ' + path)
            if stream.get('sample_aspect_ratio') not in (None, 'N/A', '0:1', '1:1'):
                raise Failure('UNSUPPORTED', 'Anamorphic MOVIE needs explicit square-pixel normalization')
            base = Fraction(stream['time_base'])
            pts = [Fraction(frame['best_effort_timestamp']) * base for frame in frames]
            tolerance = max(base, Fraction(1, 1000000))
            if tolerance >= Fraction(1, 2) / fps:
                raise Failure('UNSUPPORTED', 'MOVIE time base cannot resolve project frames reliably')
            if abs(pts[0]) > tolerance:
                raise Failure('UNSUPPORTED', 'Nonzero MOVIE origin needs explicit timestamp/AV normalization')
            errors = [abs(t - pts[0] - Fraction(i, 1) / fps) for i, t in enumerate(pts)]
            if max(errors) > tolerance or any(b <= a for a, b in zip(pts, pts[1:])):
                raise Failure('UNSUPPORTED', 'Decoded MOVIE is not CFR at project FPS; use explicit PNG+PCM conform')
            if any((frame.get('width'), frame.get('height')) != (stream['width'], stream['height']) for frame in frames):
                raise Failure('UNSUPPORTED', 'MOVIE frame dimensions change within the stream')
            if any('duration' not in frame or abs(Fraction(frame['duration']) * base - 1 / fps) > tolerance for frame in frames):
                raise Failure('UNSUPPORTED', 'MOVIE frame durations are not verified CFR; explicitly conform to PNG+PCM')
            if 'duration_ts' not in stream or abs(Fraction(stream['duration_ts']) * base - len(frames) / fps) > tolerance:
                raise Failure('UNSUPPORTED', 'MOVIE stream end differs from decoded frame extent; explicitly conform to PNG+PCM')
            cached[path] = {'file': path, 'sha256': declared[path.casefold()], 'frames': len(frames),
                'fps': fps_spec, 'decoded_pts_max_error_seconds': float(max(errors)),
                'frame_origin_seconds': float(pts[0]), 'probe': {'file': str(output), 'sha256': digest(output)}}
        row = cached[path]
        if int(movie.content_duration) != row['frames']:
            raise Failure('UNSUPPORTED', 'Native MOVIE sample count differs from full decoded frame count')
        rows.append({'strip': movie.name, **row})
    report = {'status': 'passed', 'movies': rows, 'policy': 'decoded CFR, source/project FPS equal, zero origin, square pixels; no implicit speed conversion'}
    atomic_json(root / 'report.json', report)
    return report
