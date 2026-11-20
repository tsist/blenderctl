# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline saved-fixture smoke; no media rendering, previews or export.

Place the Blender 5.3 install and this checkout on a qualified local filesystem,
then run python tests/run_linux_workflow_inspect_smoke.py /path/to/blender.
The scratch directory defaults to /tmp, itself required to be qualified.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'tools/blenderctl/cli.py'
BUILD_FIXTURE = """
import bpy
import sys
from pathlib import Path
tools, destination = sys.argv[-2:]
sys.path.insert(0, tools)
from compositing_workflow_addon import core
scene = bpy.context.scene
strip = scene.sequence_editor_create().strips.new_effect(
    name='Linux fixture color', type='COLOR', channel=1, frame_start=1, length=12)
strip.color = (.22, .33, .44)
core.apply({
    'workflow_version': '1.0', 'workflow_id': 'fixture', 'scene': scene.name,
    'view_layer': scene.view_layers[0].name, 'operation': 'create',
    'source': {'kind': 'render_layers'},
    'components': [{'id': 'exposure', 'kind': 'exposure', 'params': {'ev': .5}}],
})
bpy.ops.wm.save_as_mainfile(filepath=destination)
"""


def main(blender):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('BLENDER_') and key != 'OCIO'}
    with tempfile.TemporaryDirectory(dir=env.get('GUARD_TEST_ROOT', '/tmp')) as temporary:
        job = Path(temporary)
        source, resources = job / 'fixture.blend', job / 'resources.json'
        fixture = job / 'fixture.py'
        fixture.write_text(BUILD_FIXTURE, encoding='utf-8')
        resources.write_text('[]', encoding='utf-8')
        env['BLENDER_USER_RESOURCES'] = str(job / 'user')
        subprocess.run([blender, '--background', '--factory-startup', '--disable-autoexec', '--offline-mode',
                        '--python-exit-code', '23', '--python', str(fixture), '--',
                        str(ROOT / 'tools'), str(source)], env=env, check=True)
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        for unit in ('compositing', 'video'):
            command = [sys.executable, str(CLI), '--blender', blender, '--jobs-dir', str(job / 'jobs'),
                       unit, 'inspect', str(source), '--resources', str(resources), '--scene', 'Scene']
            if unit == 'compositing':
                command.extend(('--workflow-id', 'fixture'))
            result = subprocess.run([*command, '--expected-sha256', source_sha],
                                    env=env, capture_output=True, text=True, check=False)
            report = json.loads(result.stdout)
            assert result.returncode == 0 and report['ok'], report.get('error')
            data = report['data']
            assert data['source_unchanged'] and data['source_sha256'] == source_sha
            assert len(data['input_hashes']) > 128
            assert not data['source_saved'] and data['checks']['render'] == 'not_run'
            inspection = data['inspection']
            if unit == 'compositing':
                assert inspection['status'] == 'managed' and not inspection['conflicts']
                assert data['color_immutable_inputs']['count'] > 0
            else:
                assert any(row['type'] == 'COLOR' and row['duration'] == 12
                           for row in inspection['diagnostics']['unmanaged_strips'])
                assert data['checks']['audio_listening'] == 'not_run'
            result = subprocess.run([*command, '--expected-sha256', '0' * 64],
                                    env=env, capture_output=True, text=True, check=False)
            report = json.loads(result.stdout)
            assert result.returncode and not report['ok'] and report['error']['code'] == 'CONFLICT', report
            print(f'{unit}.inspect: saved fixture verified; wrong SHA rejected')
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_sha


if __name__ == '__main__':
    if sys.platform != 'linux' or len(sys.argv) != 2:
        raise SystemExit('Usage (Linux): python tests/run_linux_workflow_inspect_smoke.py /path/to/blender')
    main(str(Path(sys.argv[1]).resolve()))
