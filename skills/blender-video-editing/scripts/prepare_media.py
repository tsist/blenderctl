"""Explicit SDR Movie -> project-fps PNG IMAGE and aligned PCM SOUND preparation.

Input JSON: {"clip_id":"shot", "source":FILE, "fps":[24,1],
 "color_policy":"SDR_SRGB_ASSERT", "audio":"NONE"|"FIRST"}.
FILE is an absolute local file/expected_sha256 descriptor. FFmpeg and ffprobe
descriptors are mandatory. No installs, source edits, downloads or cleanup.
Uses timestamp sample-and-hold, never optical interpolation or audio speed.
Preserves duration by ceiling to project frames; records the entire tail delta.
Known HDR/wide-gamut, rotation, changing dimensions, unclocked/gapped audio,
non-48k audio and >2 audio channels are explicitly rejected in this first helper.
Unknown SDR color tags require the caller's explicit SDR_SRGB_ASSERT; this is
an interpretation of the decoded SDR bytes, not colorimetric qualification.
"""
import argparse
from bisect import bisect_right
from contextlib import ExitStack
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time
import wave

sys.dont_write_bytecode = True
ROOT = Path(os.environ.get('BLENDERCTL_ROOT', str(Path(__file__).resolve().parents[3]))).resolve()
sys.path.insert(0, str(ROOT / 'tools/blenderctl'))
from filesystem import FileGuard


def descriptor(row):
    if not isinstance(row, dict) or set(row) != {'file', 'expected_sha256'}:
        raise ValueError('Expected exact FILE descriptor')
    if not isinstance(row['file'], str) or not Path(row['file']).is_absolute():
        raise ValueError('FILE path must be absolute')
    if not isinstance(row['expected_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', row['expected_sha256']):
        raise ValueError('Expected lower-case SHA256')
    return {**row, 'file': str(Path(row['file']).absolute())}


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def file_row(path):
    return {'file': str(path), 'expected_sha256': sha(path)}


def rational(value):
    return [value.numerator, value.denominator]


def nearest(value):
    # Ties away from zero; explicit maximum quantization error is 0.5 samples.
    return math.floor(value + Fraction(1, 2)) if value >= 0 else -math.floor(-value + Fraction(1, 2))


def png_size(path):
    with Path(path).open('rb') as stream:
        header = stream.read(33)
    if len(header) != 33 or header[:8] != b'\x89PNG\r\n\x1a\n' or header[12:16] != b'IHDR':
        raise ValueError('Invalid decoded PNG')
    return struct.unpack('>II', header[16:24])


def video_clock(stream, frames):
    if not frames or any(f.get('width') != stream['width'] or f.get('height') != stream['height'] for f in frames):
        raise ValueError('Empty video or changing dimensions are not qualified')
    if stream.get('field_order', 'unknown') not in ('unknown', 'progressive'):
        raise ValueError('Interlaced source is not qualified')
    if stream.get('sample_aspect_ratio', '1:1') not in ('1:1', '0:1', 'N/A'):
        raise ValueError('Non-square pixels require a separately verified geometric policy')
    for item in stream.get('side_data_list', []):
        if item.get('side_data_type') == 'Display Matrix' and item.get('rotation', 0) != 0:
            raise ValueError('Rotated Movie needs an explicit separately verified transform')
    if stream.get('color_transfer') in ('smpte2084', 'arib-std-b67') or stream.get('color_primaries') == 'bt2020' or stream.get('color_space') in ('bt2020nc', 'bt2020c'):
        raise ValueError('HDR/wide-gamut conversion is not qualified by SDR helper')
    if any(f.get('color_transfer') in ('smpte2084', 'arib-std-b67') or f.get('color_primaries') == 'bt2020' for f in frames):
        raise ValueError('Per-frame HDR/wide-gamut metadata is not qualified')
    base = Fraction(stream['time_base'])
    if base <= 0 or any('best_effort_timestamp' not in f for f in frames):
        raise ValueError('Every decoded video frame requires an exact timestamp')
    pts = [int(f['best_effort_timestamp']) * base for f in frames]
    if any(b <= a for a, b in zip(pts, pts[1:])):
        raise ValueError('Non-increasing video timestamps')
    # Every non-final interval is defined by consecutive presentation timestamps.
    # Final duration must be explicit. Do not invent it from nominal/average fps.
    last = frames[-1].get('duration', frames[-1].get('pkt_duration'))
    if last is None or int(last) <= 0:
        raise ValueError('Final frame duration is missing or non-positive')
    end = pts[-1] + int(last) * base
    if 'duration_ts' in stream and 'start_pts' in stream:
        stream_end = (int(stream['start_pts']) + int(stream['duration_ts'])) * base
        if abs(stream_end - end) > base:
            raise ValueError('Container endpoint and decoded final interval disagree')
    return pts, end


def audio_clock(stream, frames):
    if int(stream.get('sample_rate', 0)) != 48000 or int(stream.get('channels', 0)) not in (1, 2):
        raise ValueError('Initial audio qualification requires 48k mono/stereo')
    if not frames or any('best_effort_timestamp' not in f or 'nb_samples' not in f for f in frames):
        raise ValueError('Every decoded audio frame requires timestamp and sample count')
    base = Fraction(stream['time_base'])
    pts = [int(f['best_effort_timestamp']) * base for f in frames]
    counts = [int(f['nb_samples']) for f in frames]
    if any(n <= 0 or f.get('sample_rate', 48000) != 48000 or f.get('channels', stream['channels']) != stream['channels'] for n, f in zip(counts, frames)):
        raise ValueError('Changing or invalid audio format')
    # Container clocks may round each packet to ticks. Reject actual discontinuity,
    # but record the exact bound rather than mistaking coarse ticks for missing PCM.
    errors = [];elapsed = 0
    for p, count in zip(pts, counts):
        errors.append(p - (pts[0] + Fraction(elapsed,48000)));elapsed += count
    if any(abs(delta) > base for delta in errors):
        raise ValueError('Gapped/drifting audio timestamps need a separate qualified policy')
    return pts[0], sum(counts), max((abs(e) for e in errors), default=Fraction(0))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-manifest', type=Path, required=True)
    parser.add_argument('--ffmpeg', type=Path, required=True, help='SHA-bound executable descriptor JSON')
    parser.add_argument('--ffprobe', type=Path, required=True, help='SHA-bound executable descriptor JSON')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--timeout-seconds', type=float, default=60)
    parser.add_argument('--max-frames', type=int, default=100000)
    args = parser.parse_args(argv)
    if not args.output_dir.is_absolute() or not 0 < args.timeout_seconds <= 300 or not 1 <= args.max_frames <= 100000:
        raise ValueError('Absolute output, bounded timeout and 1..100000 max frames required')
    output = args.output_dir.absolute()
    if os.path.lexists(output):
        raise ValueError('Output already exists; choose a new version')
    spec = json.loads(args.source_manifest.read_text(encoding='utf-8-sig'))
    if not isinstance(spec, dict) or set(spec) != {'clip_id', 'source', 'fps', 'color_policy', 'audio'}:
        raise ValueError('Expected clip_id/source/fps/color_policy/audio')
    if not isinstance(spec['clip_id'], str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,57}', spec['clip_id']):
        raise ValueError('clip_id requires 1..58 safe characters')
    if not isinstance(spec['fps'], list) or len(spec['fps']) != 2 or any(type(v) is not int or not 1 <= v <= 1000000 for v in spec['fps']):
        raise ValueError('fps requires two positive bounded integers')
    if spec['color_policy'] != 'SDR_SRGB_ASSERT' or spec['audio'] not in ('NONE', 'FIRST'):
        raise ValueError('Explicit SDR_SRGB_ASSERT and audio NONE/FIRST are required')
    fps = Fraction(*spec['fps'])
    source = descriptor(spec['source'])
    tools = [descriptor(json.loads(p.read_text(encoding='utf-8-sig'))) for p in (args.ffmpeg, args.ffprobe)]
    started = time.monotonic()
    report = {'status': 'running', 'version': '1.0', 'source': source, 'tools': {'ffmpeg': tools[0], 'ffprobe': tools[1]},
              'fps': rational(fps), 'policy': 'TIMESTAMP_SAMPLE_HOLD_CEIL_DURATION',
              'color_policy': spec['color_policy'], 'audio_selection': spec['audio'],
              'png_encoding': '8-bit RGBA SDR decoded bytes; no HDR or source bit-depth preservation claim',
              'limits': 'SDR interpretation; 48k mono/stereo continuous audio only; no interpolation/audio speed',
              'clip_templates': [], 'files': [], 'mapping': []}
    with ExitStack() as stack:
        guards = {}
        for row in [source, *tools]:
            key = row['file'].casefold()
            if key not in guards:
                guards[key] = stack.enter_context(FileGuard(row['file']))
            if guards[key].sha256() != row['expected_sha256']:
                raise ValueError('SHA mismatch: ' + row['file'])
        output.mkdir(parents=True, exist_ok=False)
        report_path = output / 'media-manifest.json'

        def checkpoint():
            if (output / 'cancel.request').exists():
                raise InterruptedError('Cancelled; owned partials retained')

        def command(argv, label, json_output=False):
            checkpoint()
            log = output / (label + '.log')
            if json_output:
                capture = output / (label + '.json')
                with capture.open('wb') as out, log.open('wb') as err:
                    result = subprocess.run(argv, stdout=out, stderr=err, timeout=args.timeout_seconds)
                if result.returncode:
                    raise RuntimeError('Command failed; see ' + str(log))
                return json.loads(capture.read_text(encoding='utf-8'))
            with log.open('wb') as stream:
                result = subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT, timeout=args.timeout_seconds)
            if result.returncode:
                raise RuntimeError('Command failed; see ' + str(log))

        try:
            probe = command([tools[1]['file'], '-v', 'error', '-show_streams', '-show_frames', '-of', 'json', source['file']], 'input-probe', True)
            videos = [s for s in probe.get('streams', []) if s['codec_type'] == 'video' and not s.get('disposition', {}).get('attached_pic')]
            if len(videos) != 1:
                raise ValueError('Exactly one non-attached video stream required')
            stream = videos[0]
            frames = [f for f in probe.get('frames', []) if f.get('stream_index') == stream['index']]
            if len(frames) > args.max_frames:
                raise ValueError('Input decode frame budget exceeded')
            pts, end = video_clock(stream, frames)
            first, duration = pts[0], end - pts[0]
            length = math.ceil(duration * fps)
            if not 1 <= length <= args.max_frames:
                raise ValueError('Output frame budget exceeded')
            output_duration = Fraction(length, 1) / fps
            decoded = output / 'decoded-original';decoded.mkdir()
            pattern = decoded / 'frame-%06d.png'
            command([tools[0]['file'], '-hide_banner', '-nostdin', '-v', 'error', '-xerror', '-n', '-i', source['file'],
                     '-map', '0:' + str(stream['index']), '-an', '-sn', '-dn', '-fps_mode', 'passthrough', '-pix_fmt', 'rgba',
                     '-threads', '1', '-start_number', '0', str(pattern)], 'decode-original')
            originals = sorted(decoded.glob('frame-*.png'))
            if len(originals) != len(frames) or any(png_size(p) != (stream['width'], stream['height']) for p in originals):
                raise ValueError('Full decode count/dimensions disagree with input PTS')
            # Full decode all actual PNG files, independent of source packet probe.
            command([tools[0]['file'], '-hide_banner', '-nostdin', '-v', 'error', '-xerror', '-framerate', '1', '-start_number', '0',
                     '-i', str(pattern), '-f', 'null', '-'], 'verify-decoded-original')
            image_dir = output / 'image';image_dir.mkdir()
            for i in range(length):
                checkpoint()
                relative = Fraction(i, 1) / fps
                selected = bisect_right(pts, first + relative) - 1
                path = image_dir / ('frame-' + str(i).zfill(6) + '.png')
                shutil.copyfile(originals[selected], path)
                row = file_row(path);report['files'].append(row)
                report['mapping'].append({'output_index': i, 'output_time': rational(relative), 'source_index': selected,
                                          'source_pts': rational(pts[selected]), 'source_png_sha256': sha(originals[selected])})
            report.update(input_frame_count=len(frames), frame_count=length, width=stream['width'], height=stream['height'],
                          video_first_pts=rational(first), input_duration=rational(duration), output_duration=rational(output_duration),
                          tail_extension=rational(output_duration - duration),
                          input_pts=[rational(p) for p in pts], input_end=rational(end),
                          video_color_tags={k: stream.get(k) for k in ('color_space', 'color_primaries', 'color_transfer', 'color_range')})
            report['clip_templates'].append({'id': spec['clip_id'], 'kind': 'IMAGE', 'channel': 1, 'start': 1, 'duration': length,
                'source': {'files': report['files'], 'source_in': 0, 'speed': 1, 'color_space': 'sRGB'}})
            audios = [s for s in probe.get('streams', []) if s['codec_type'] == 'audio']
            if spec['audio'] == 'FIRST':
                if not audios:
                    raise ValueError('FIRST audio requested but no audio stream exists')
                astream = audios[0]
                aframes = [f for f in probe.get('frames', []) if f.get('stream_index') == astream['index']]
                astart, sample_sum, jitter = audio_clock(astream, aframes)
                raw = output / 'decoded-original.wav'
                channel_policy = 'MONO_DUPLICATE_UNITY' if astream['channels'] == 1 else 'STEREO_IDENTITY'
                channel_filter = 'pan=stereo|c0=c0|c1=c0' if astream['channels'] == 1 else 'anull'
                command([tools[0]['file'], '-hide_banner', '-nostdin', '-v', 'error', '-xerror', '-n', '-i', source['file'],
                         '-map', '0:' + str(astream['index']), '-vn', '-sn', '-dn', '-af', channel_filter,
                         '-ac', '2', '-ar', '48000', '-c:a', 'pcm_s16le', str(raw)], 'decode-audio')
                with wave.open(str(raw), 'rb') as reader:
                    if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate(), reader.getnframes()) != (2, 2, 48000, sample_sum):
                        raise ValueError('Decoded PCM sample count/format disagrees with complete audio frames')
                    # Do not load whole audio: align and copy bounded 65536-sample blocks.
                    relative_audio = astart - first
                    offset = nearest(relative_audio * 48000)
                    target_samples = nearest(output_duration * 48000)
                    skip = min(sample_sum, max(0, -offset))
                    prefix = min(target_samples, max(0, offset))
                    copy_count = min(sample_sum - skip, target_samples - prefix)
                    suffix = target_samples - prefix - copy_count
                    path = output / 'aligned-audio.wav'
                    with wave.open(str(path), 'wb') as writer:
                        writer.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
                        for count in (prefix,):
                            while count:
                                checkpoint();chunk=min(count,65536);writer.writeframesraw(b'\0' * (chunk * 4));count-=chunk
                        reader.setpos(skip);remaining=copy_count
                        while remaining:
                            checkpoint();chunk=min(remaining,65536);data=reader.readframes(chunk)
                            if len(data) != chunk * 4:raise ValueError('Unexpected PCM EOF')
                            writer.writeframesraw(data);remaining-=chunk
                        while suffix:
                            checkpoint();chunk=min(suffix,65536);writer.writeframesraw(b'\0' * (chunk * 4));suffix-=chunk
                with wave.open(str(path), 'rb') as check:
                    if check.getnframes() != target_samples:raise ValueError('Aligned PCM output sample mismatch')
                command([tools[0]['file'], '-hide_banner', '-nostdin', '-v', 'error', '-xerror', '-i', str(path), '-f', 'null', '-'], 'verify-aligned-audio')
                report['audio'] = {'input_stream_index': astream['index'], 'input_first_pts': rational(astart),
                    'relative_start': rational(relative_audio), 'alignment_offset_samples': offset,
                    'alignment_quantization_seconds': rational(Fraction(offset,48000)-relative_audio),
                    'timestamp_jitter_bound': rational(jitter), 'input_samples': sample_sum, 'output_samples': target_samples,
                    'trim_head_samples': skip, 'trim_tail_samples': sample_sum - skip - copy_count,
                    'pad_head_samples': prefix, 'pad_tail_samples': target_samples-prefix-copy_count,
                    'output_tail_sample_quantization_seconds': rational(Fraction(target_samples,48000)-output_duration),
                    'sample_rate': 48000, 'channels': 2, 'channel_policy': channel_policy,
                    'sample_width': 2, 'speed': 1, 'file': file_row(path)}
                report['clip_templates'].append({'id': spec['clip_id'] + '_audio', 'kind': 'SOUND', 'channel': 2, 'start': 1,
                    'duration': length, 'source': {'files': [file_row(path)], 'source_in': 0, 'speed': 1}, 'volume': 1, 'pan': 0})
            else:
                report['audio'] = {'status': 'not_requested', 'available_streams': len(audios)}
            for row in [source, *tools]:
                if guards[row['file'].casefold()].sha256() != row['expected_sha256']:
                    raise ValueError('Guarded source/tool SHA changed')
            generated = [*report['files'], *([report['audio']['file']] if 'file' in report['audio'] else [])]
            report['resources'] = list({r['file'].casefold(): r for r in [source, *tools, *generated]}.values())
            report.update(status='passed', seconds=time.monotonic()-started,
                          qualification='Complete source and PNG/PCM decode; frame identity/integration checked by caller')
        except BaseException as exc:
            report.update(status='cancelled' if isinstance(exc,(InterruptedError,KeyboardInterrupt)) else 'failed', error=str(exc))
            raise
        finally:
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        print(json.dumps({'status':'passed','report':str(report_path),'frame_count':length,
                          'fps':rational(fps),'audio':report['audio']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:sys.exit(main())
    except Exception as exc:
        print(json.dumps({'status':'failed','error':str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
