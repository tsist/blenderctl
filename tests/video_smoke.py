"""Opt-in, bounded native smoke: eight 160x90 SDR frames, serial run/cold inspect.

Requires an existing Blender 5.2 and Pillow in the host Python for PNG decoding.
Output must be a new short absolute directory; no downloads or GUI interaction.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--blender', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from PIL import Image
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    log = out / 'logs'
    log.mkdir()
    commands = []
    def run(argv, name, json_result=False):
        flags = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS if os.name == 'nt' else 0
        begin = time.monotonic()
        result = subprocess.run([str(x) for x in argv], capture_output=True, timeout=150, creationflags=flags)
        (log / (name + '.stdout')).write_bytes(result.stdout)
        (log / (name + '.stderr')).write_bytes(result.stderr)
        commands.append({'name': name, 'exit_code': result.returncode, 'seconds': round(time.monotonic() - begin, 3)})
        if result.returncode:
            raise RuntimeError('Command failed: ' + name + '; see ' + str(log))
        return json.loads(result.stdout) if json_result else None
    py = [sys.executable, '-B']
    source = out / 'seed.blend'
    run([args.blender, '--background', '--factory-startup', '--disable-autoexec', '--threads', '1',
         '--python', ROOT / 'examples/create_fixture.py', '--', source], 'fixture')
    source_sha = sha(source)
    bound = run(py + [ROOT / 'examples/make_input.py', '--source', source, '--output', out / 'input.json'], 'input', True)
    compiled = out / 'compiled'
    result = run(py + [ROOT / 'templates/video/cli.py', 'compile', '--template', 'cg-clean', '--input', bound['file'],
                     '--expected-input-sha', bound['expected_sha256'], '--output-dir', compiled], 'compile', True)
    if not result['ok']:
        raise RuntimeError('Compiler failure')
    prefix = py + [ROOT / 'tools/blenderctl/cli.py', '--blender', args.blender, '--jobs-dir', out / 'j', '--timeout', '120', '--compact']
    result = run(prefix + ['request', compiled / 'request.json'], 'create', True)
    if not result['ok']:
        raise RuntimeError('Create failure')
    job = out / 'j' / result['job_id']
    status = json.loads((job / 'status.json').read_text())
    report = json.loads((job / 'video-report.json').read_text())
    if status['state'] != 'succeeded' or status['exit_code'] != 0 or report['status'] != 'passed':
        raise RuntimeError('Native job not qualified')
    candidate = report['candidate']
    request = {'schema_version': '1.0', 'command': 'video.inspect', 'params': {
        'file': candidate['file'], 'expected_sha256': candidate['sha256'], 'resources': [],
        'scene': 'PublicVideoExample', 'workflow_id': 'PublicVideoExample'}}
    inspection = out / 'inspect.json'
    inspection.write_text(json.dumps(request), encoding='utf-8')
    reopened = run(prefix + ['request', inspection], 'cold-inspect', True)
    if not reopened['ok'] or reopened['data']['status'] != 'passed':
        raise RuntimeError('Cold inspection failed')
    pngs = [Path(row['file']) for row in report['output_files'] if row['file'].lower().endswith('.png')]
    main_frames = sorted(p for p in pngs if 'main' in p.parts)
    if len(main_frames) != 8 or len(pngs) != 16:
        raise RuntimeError('Unexpected frame count')
    for frame in pngs:
        with Image.open(frame) as image:
            image.load()
            if image.size != (160, 90):
                raise RuntimeError('Wrong frame dimensions')
    for index, frame in enumerate(main_frames):
        with Image.open(frame) as image:
            pixels = image.convert('RGB')
            expected = (255, 0, 0) if index < 4 else (0, 0, 255)
            if pixels.getextrema() != tuple((v, v) for v in expected):
                raise RuntimeError('Unexpected color/cut position: ' + frame.name)
    if sha(source) != source_sha:
        raise RuntimeError('Source was modified')
    evidence = {'status': 'passed', 'source_preserved': True, 'native_create': result['job_id'],
                'cold_inspect': reopened['job_id'], 'main_frames': 8, 'decoded_pngs': len(pngs),
                'pixel_match': 'all pixels exact red/blue at the cut', 'commands': commands,
                'scope': '160x90 SDR COLOR strips; no audio/text/HDR/4K or aesthetic qualification'}
    (out / 'RESULT.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    print(json.dumps(evidence, indent=2))

if __name__ == '__main__':
    main()
