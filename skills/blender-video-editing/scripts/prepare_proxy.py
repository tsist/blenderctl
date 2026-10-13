"""Prepare owned lower-resolution PNG proxies from a SHA-bound IMAGE sequence.

No installation, movie/VFR conversion, source overwrite or directory cleanup.
Example: python prepare_proxy.py --source-manifest image-source.json
  --ffmpeg ffmpeg-descriptor.json --output-dir ABSOLUTE_NEW_DIR --percent 50
source manifest: {"clip_id":"wide", "source_files":[FILE,...]}.
ffmpeg descriptor: {"file":"ABSOLUTE_FFMPEG", "expected_sha256":"64lowerhex"}.
The result JSON supplies one output.preview.proxies entry and resource rows.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time

ROOT = Path(os.environ.get('BLENDERCTL_ROOT', str(Path(__file__).resolve().parents[3]))).resolve()
sys.path.insert(0, str(ROOT / 'tools/blenderctl'))
from filesystem import FileGuard


def descriptor(row):
    if not isinstance(row, dict) or set(row) != {'file', 'expected_sha256'}:
        raise ValueError('Expected exact file/expected_sha256 descriptor')
    if not isinstance(row['file'], str) or not Path(row['file']).is_absolute():
        raise ValueError('Descriptor path must be absolute')
    if not isinstance(row['expected_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', row['expected_sha256']):
        raise ValueError('Descriptor SHA256 must be 64 lower-case hex')
    return {**row, 'file': str(Path(row['file']).resolve())}


def png_size(path):
    with Path(path).open('rb') as stream:
        header = stream.read(33)
    if len(header) != 33 or header[:8] != b'\x89PNG\r\n\x1a\n' or header[12:16] != b'IHDR':
        raise ValueError('Only actual PNG IMAGE members are qualified')
    width, height = struct.unpack('>II', header[16:24])
    if not width or not height:
        raise ValueError('Invalid PNG dimensions')
    return width, height


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-manifest', type=Path, required=True)
    parser.add_argument('--ffmpeg', type=Path, required=True, help='JSON FILE descriptor, not an unpinned executable name')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--percent', type=int, choices=(25, 50, 75), default=50)
    parser.add_argument('--timeout-seconds', type=float, default=60)
    args = parser.parse_args(argv)
    if not args.output_dir.is_absolute() or not 0 < args.timeout_seconds <= 300:
        raise ValueError('Absolute output directory and timeout in (0,300] required')
    output = args.output_dir.resolve()
    if output.exists():
        raise ValueError('Output directory already exists; choose a new version')
    source = json.loads(args.source_manifest.read_text(encoding='utf-8-sig'))
    if not isinstance(source, dict) or set(source) != {'clip_id', 'source_files'} or not re.fullmatch('[A-Za-z][A-Za-z0-9_-]{0,63}', str(source['clip_id'])):
        raise ValueError('Source manifest requires clip_id and source_files')
    if not isinstance(source['source_files'], list) or not 1 <= len(source['source_files']) <= 100000:
        raise ValueError('source_files requires 1..100000 ordered IMAGE members')
    sources = [descriptor(row) for row in source['source_files']]
    tool = descriptor(json.loads(args.ffmpeg.read_text(encoding='utf-8-sig')))
    started = time.monotonic()
    with ExitStack() as stack:
        tool_guard = stack.enter_context(FileGuard(tool['file']))
        if tool_guard.sha256() != tool['expected_sha256']:
            raise ValueError('FFmpeg SHA mismatch')
        guards = {}
        dimensions = []
        for row in sources:
            key = row['file'].casefold()
            if key not in guards:
                guards[key] = stack.enter_context(FileGuard(row['file']))
            if guards[key].sha256() != row['expected_sha256']:
                raise ValueError('Source SHA mismatch: ' + row['file'])
            dimensions.append(png_size(row['file']))
        if len(set(dimensions)) != 1:
            raise ValueError('Mixed sequence dimensions are not qualified')
        width, height = dimensions[0]
        if width * args.percent % 100 or height * args.percent % 100:
            raise ValueError('Proxy percentage must preserve exact integer dimensions/aspect')
        small = (width * args.percent // 100, height * args.percent // 100)
        if min(small) < 1:
            raise ValueError('Proxy dimensions would be empty')
        output.mkdir(parents=True, exist_ok=False)
        results = []
        report = {'status': 'running', 'kind': 'IMAGE_PNG', 'clip_id': source['clip_id'],
                  'source_files': sources, 'files': results, 'ffmpeg': tool,
                  'source_dimensions': [width, height], 'proxy_dimensions': list(small),
                  'percent': args.percent, 'quality': 'lower-resolution preview only; not original quality'}
        report_path = output / 'proxy-manifest.json'
        try:
            for index, row in enumerate(sources):
                if (output / 'cancel.request').exists():
                    raise InterruptedError('Proxy preparation cancelled; partial files retained')
                temporary = output / ('frame-' + str(index).zfill(6) + '.partial.png')
                final = output / ('frame-' + str(index).zfill(6) + '.png')
                log = output / ('frame-' + str(index).zfill(6) + '.log')
                command = [tool['file'], '-hide_banner', '-nostdin', '-v', 'error', '-n',
                           '-i', row['file'], '-frames:v', '1', '-vf',
                           'scale=' + str(small[0]) + ':' + str(small[1]) + ':flags=lanczos',
                           '-pix_fmt', 'rgba', '-threads', '1', str(temporary)]
                with log.open('wb') as stream:
                    result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT,
                                            timeout=args.timeout_seconds, check=False)
                    if result.returncode:
                        raise RuntimeError('FFmpeg failed; see ' + str(log))
                    decode = subprocess.run([tool['file'], '-hide_banner', '-nostdin', '-v', 'error', '-xerror',
                        '-i', str(temporary), '-frames:v', '1', '-f', 'null', '-'],
                        stdout=stream, stderr=subprocess.STDOUT, timeout=args.timeout_seconds, check=False)
                    if decode.returncode or png_size(temporary) != small:
                        raise RuntimeError('Proxy full decode/dimensions failed; see ' + str(log))
                os.replace(temporary, final)
                results.append({'file': str(final), 'expected_sha256': sha(final)})
            for row in sources:
                if guards[row['file'].casefold()].sha256() != row['expected_sha256']:
                    raise ValueError('Source changed during preparation')
            if tool_guard.sha256() != tool['expected_sha256']:
                raise ValueError('FFmpeg changed during preparation')
            report.update(status='passed', seconds=time.monotonic() - started,
                resources=list({row['file'].casefold(): row for row in [*sources, *results, tool]}.values()),
                preview_entry={'clip_id': source['clip_id'], 'source_files': sources, 'files': results})
        except BaseException as exc:
            report.update(status='cancelled' if isinstance(exc, (KeyboardInterrupt, InterruptedError)) else 'failed', error=str(exc))
            raise
        finally:
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'status': 'passed', 'report': str(report_path),
                          'preview_entry': report['preview_entry'], 'resources': report['resources']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
