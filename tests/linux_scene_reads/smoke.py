"""Native CLI smoke: Python, official Linux Blender 5.3.0, new private /tmp child."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

blender = Path(sys.argv[1]).resolve()
root = Path(sys.argv[2]).absolute()
root.mkdir(mode=0o700)
cli = Path(__file__).resolve().parents[2] / 'tools/blenderctl/cli.py'
env = {k: v for k, v in os.environ.items() if not k.startswith('BLENDER_') and k != 'OCIO'}
env['BLENDER_USER_RESOURCES'] = str(root / 'profile')
subprocess.run([str(blender), '-b', '-t', '1', '--factory-startup', '--disable-autoexec', '--offline-mode',
                '--python-exit-code', '23', '-P', str(Path(__file__).with_name('fixture.py')),
                '--', str(root)], check=True, env=env)
sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
def inspect(name, source, expected=None, error=None, *extra):
    command = [sys.executable, str(cli), '--blender', str(blender), '--jobs-dir', str(root / 'jobs'),
               'scene', 'inspect', str(source)]
    if expected is not None:
        command += ['--expected-sha256', expected]
    result = subprocess.run([*command, *extra], env=env, capture_output=True, text=True)
    report = json.loads(result.stdout)
    (root / (name + '.json')).write_text(result.stdout)
    if error:
        assert result.returncode and report['error']['code'] == error, report
        return
    assert not result.returncode and report['ok'], report
    assert report['data']['source_unchanged'] and expected == report['data']['source_sha256']
    assert report['data']['input_hashes'] == {str(source): expected}
    assert 'scene-report.json' in report['artifacts']
    return report['data']

source = root / 'clean.blend'
expected = sha(source)
report = inspect('saved', source, expected)
assert report['evaluation_context'] == {'scene':'Fixture', 'view_layer':'Primary', 'frame':3, 'mode':'OBJECT'}
assert report['scenes'][0]['camera'] == 'Fixture Camera'
objects = {row['name']: row for row in report['objects']}
assert [r[3] for r in objects['Triangle']['matrix_world'][:3]] == [2, 3, 4]
assert objects['Instance']['instance_collection']['name'] == 'Instanced Source'
ctx = root / 'ctx.json'
ctx.write_text(json.dumps({'scene':'Fixture', 'view_layer':'Primary', 'frame':7, 'mode':'OBJECT',
                           'selected_objects':['Triangle'], 'active_object':'Triangle'}))
assert inspect('context', source, expected, None, '--context', str(ctx))['evaluation_context']['frame'] == 7
inspect('missing-input', root / 'missing.blend', expected, 'NOT_FOUND')
inspect('missing-sha', source, error='UNSUPPORTED')
inspect('bad-sha', source, '0' * 64, 'CONFLICT')
symlink = root / 'alias.blend'
symlink.symlink_to(source)
inspect('symlink', symlink, expected, 'CONFLICT')
for name in ('external', 'linked', 'modifier'):
    path = root / (name + '.blend')
    inspect(name, path, sha(path), 'UNSUPPORTED')
with source.open('r+b'):
    inspect('writer', source, expected, 'CONFLICT')
assert sha(source) == expected
print('Native scene.inspect passed: 2 positive, 8 refusal cases; source unchanged.')
