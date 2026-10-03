# SPDX-License-Identifier: GPL-3.0-or-later
"""Run one CPU material preview in owned background Blender processes.

python tests/run_blender_smoke.py --blender ABSOLUTE_BINARY --output NEW_DIRECTORY
64x64, four samples, one view. No GUI, GPU, external assets or network.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blender', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if not args.blender.is_absolute():
        parser.error('--blender must be an absolute path')
    blender = args.blender.resolve(strict=True)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    summary = {'ok': False, 'started_at': time.time(), 'blender': str(blender), 'calls': [],
               'scope': 'Original Cube/UV/texture; one static CPU material.run, independent reopen and image decoding, addon registration.',
               'not_run': ['GUI interaction', 'GPU/Eevee', 'animation', 'batch/resume/cancel', 'artistic quality']}
    def persist():
        (out / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    def run(label, command, timeout):
        process = subprocess.run(command, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        (out / (label + '-stdout.log')).write_text(process.stdout, encoding='utf-8')
        (out / (label + '-stderr.log')).write_text(process.stderr, encoding='utf-8')
        summary['calls'].append({'label': label, 'exit_code': process.returncode})
        persist()
        if process.returncode: raise RuntimeError(label + ' failed; inspect captured logs')
        return process
    def native(label, mode):
        return run(label, [str(blender), '--background', '--factory-startup', '--disable-autoexec', '--python-exit-code', '1',
                           '--python', str(ROOT / 'tests/blender_smoke.py'), '--', mode, str(out), str(ROOT)], 180)
    try:
        persist()
        native('fixture', 'fixture')
        original = hashlib.sha256((out / 'source.blend').read_bytes()).hexdigest()
        process = run('material-run', [sys.executable, str(ROOT / 'tools/blenderctl/cli.py'), '--blender', str(blender),
                                      '--jobs-dir', str(out / 'jobs'), '--timeout', '180', 'request', str(out / 'request.json')], 225)
        result = json.loads(process.stdout)
        (out / 'cli-result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        if not result['ok'] or result['data']['status'] != 'pass': raise AssertionError(result)
        if not result['data']['source_unchanged']: raise AssertionError('CLI reported source change')
        if hashlib.sha256((out / 'source.blend').read_bytes()).hexdigest() != original: raise AssertionError('Source bytes changed')
        native('independent-reopen', 'oracle')
        summary.update(ok=True, source_sha256=original, source_unchanged=True,
                       result=str(out / 'cli-result.json'), oracle=str(out / 'oracle.json'),
                       evidence=json.loads((out / 'oracle.json').read_text(encoding='utf-8')))
    except Exception as exc:
        summary.update(error=str(exc), traceback=traceback.format_exc())
    finally:
        summary['elapsed_seconds'] = time.time() - summary['started_at']
        persist()
    print(json.dumps(summary, indent=2))
    return 0 if summary['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
