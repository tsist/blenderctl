# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise public video.run/inspect against a synthetic native-only Linux cut."""
import argparse
import copy
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'tools/blenderctl/cli.py'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main(blender, proof):
    proof.mkdir(parents=True, exist_ok=False)
    env = {k: v for k, v in os.environ.items() if not k.startswith('BLENDER_') and k != 'OCIO'}
    blend = proof / 'seed.blend'
    subprocess.run([blender, '--background', '--factory-startup', '--disable-autoexec', '--offline-mode',
                    '--python-exit-code', '23', '--python-expr',
                    f'import bpy; bpy.ops.wm.save_as_mainfile(filepath={str(blend)!r})'],
                   env=env, check=True, timeout=30)
    source_sha = sha(blend)
    cli = [sys.executable, str(CLI), '--blender', str(blender), '--jobs-dir', str(proof / 'jobs')]

    def call(name, source, expected, manifest=None, extras=None, error=None):
        params = {'file': str(source), 'expected_sha256': expected, 'resources': []}
        if manifest is None:
            params.update(scene='VideoCut', workflow_id='cut01')
            command = 'video.inspect'
        else:
            params['manifest'] = manifest
            command = 'video.run'
        params.update(extras or {})
        req = proof / (name + '.request.json')
        req.write_text(json.dumps(dict(schema_version='1.0', command=command, params=params)))
        result = subprocess.run([*cli, 'request', str(req)], env=env, capture_output=True, text=True, timeout=60)
        (proof / (name + '.stderr')).write_text(result.stderr)
        (proof / (name + '.json')).write_text(result.stdout)
        response = json.loads(result.stdout)
        job = Path(response['artifacts']['job_directory'])
        state = json.loads((job / 'status.json').read_text())
        if error:
            assert result.returncode and not response['ok'] and response['error']['code'] == error, response
            assert state['state'] == 'failed' and not (job / 'video-candidate.blend').exists()
            return response
        assert result.returncode == 0 and response['ok'] and state['state'] == 'succeeded', response
        assert response['data']['source_unchanged'] and response['data']['source_sha256'] == expected
        if manifest is not None:
            assert response['data']['verification']['status'] == 'passed'
            assert response['data']['checks']['candidate_bytes_unchanged'] == 'passed'
            assert not response['data']['output_files']
            assert response['data']['recovery']['available'] is False
            assert not list(job.glob('.vse-stage-*'))
            assert not job.stat().st_mode & 0o077
        return response['data']

    manifest = {
        'version': '1.0', 'action': 'create', 'workflow_id': 'cut01', 'scene': 'VideoCut',
        'timeline': {'fps': [24, 1], 'width': 320, 'height': 180, 'frame_start': 1, 'frame_end_exclusive': 9},
        'clips': [
            {'id': 'shot001', 'shot_id': 'shot001', 'kind': 'COLOR', 'channel': 1, 'start': 1,
             'duration': 4, 'color': [.15, .3, .6, 1], 'lock': True},
            {'id': 'shot002', 'shot_id': 'shot002', 'kind': 'COLOR', 'channel': 1, 'start': 5,
             'duration': 4, 'color': [.6, .2, .1, 1]},
        ],
    }
    # Independent supervisors may hold read leases on one source at the same time.
    with ThreadPoolExecutor(2) as pool:
        jobs = [pool.submit(call, name, blend, source_sha, manifest) for name in ('create-a', 'create-b')]
        first, second = [f.result() for f in jobs]
    candidate, other = first['candidate'], second['candidate']
    assert candidate['file'] != other['file']
    assert sha(candidate['file']) == candidate['sha256'] and sha(other['file']) == other['sha256']

    patch = {k: manifest[k] for k in ('version', 'workflow_id', 'scene')}
    patch.update(action='patch', edits=[{'op': 'update', 'id': 'shot002', 'changes': {'color': [.2, .4, .6, 1]}}])
    patched = call('patch', candidate['file'], candidate['sha256'], patch)['candidate']
    live = call('cold-inspect', patched['file'], patched['sha256'])['inspection']
    rows = {row['id']: row for row in live['clips']}
    assert rows['shot001']['lock'] and rows['shot001']['duration'] == 4
    assert rows['shot002']['start'] == 5 and abs(rows['shot002']['color'][1] - .4) < 1e-6
    locked = copy.deepcopy(patch)
    locked['edits'][0]['id'] = 'shot001'
    call('locked', candidate['file'], candidate['sha256'], locked, error='CONFLICT')
    call('wrong-sha', blend, '0' * 64, manifest, error='CONFLICT')
    with blend.open('r+b'):  # Existing writable FD conflicts even without a write.
        call('source-writer', blend, source_sha, manifest, error='CONFLICT')
    call('resume-unsupported', blend, source_sha, manifest,
         extras={'resume': {'file': str(blend), 'expected_sha256': source_sha}}, error='UNSUPPORTED')

    # Inject a collision after the normal runner creates its unique owned job.
    # Public CLI users cannot select an existing candidate location.
    sys.path.insert(0, str(CLI.parent))
    import runner
    req = json.loads((proof / 'create-a.request.json').read_text())
    job = runner.prepare(req, blender=blender, root=proof / 'jobs')
    occupied = job / 'video-candidate.blend'
    occupied.symlink_to(blend)  # Existing symlink must not be silently replaced.
    result, status = runner.execute(job)
    (proof / 'occupied.json').write_text(json.dumps(result))
    assert status and not result['ok'] and result['error']['code'] == 'CONFLICT', result
    assert occupied.is_symlink() and occupied.readlink() == blend
    assert sha(blend) == source_sha and sha(candidate['file']) == candidate['sha256']
    print('PASS create concurrently, patch, cold inspect, locked strip, wrong SHA, existing writer, occupied candidate, resume refused, unchanged sources')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--blender', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    if sys.platform != 'linux':
        p.error('Linux required')
    main(a.blender.resolve(), a.output.resolve())
