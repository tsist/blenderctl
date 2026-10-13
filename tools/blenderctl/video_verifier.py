"""Independent saved-candidate VSE reader; no save or output mutation."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from protocol import atomic_json, digest
from video_identity import implementation_sha256


def mismatch(expected, actual, path='$'):
    """Compare complete JSON evidence, with bounded numeric round-trip tolerance."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return None if expected is actual else path
    if isinstance(expected, int) and isinstance(actual, int):
        return None if expected == actual else path
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return None if abs(expected - actual) <= 1e-6 * max(1.0, abs(expected), abs(actual)) else path
    if type(expected) is not type(actual):
        return path
    if isinstance(expected, dict):
        if set(expected) != set(actual):
            return path + ':keys'
        for key in sorted(expected):
            result = mismatch(expected[key], actual[key], path + '.' + key)
            if result:
                return result
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return path + ':length'
        for index, pair in enumerate(zip(expected, actual)):
            result = mismatch(*pair, path + '[' + str(index) + ']')
            if result:
                return result
        return None
    return None if expected == actual else path


def runtime_identity(bpy):
    build = bpy.app.build_hash
    return {'version': bpy.app.version_string, 'build': build.decode() if isinstance(build, bytes) else str(build),
            'binary_sha256': digest(bpy.app.binary_path)}


def verify(path):
    import bpy
    from video_editing_workflow_addon import core
    config = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    report = {'status': 'failed', 'blender': bpy.app.version_string,
              'build': runtime_identity(bpy)['build']}
    try:
        if implementation_sha256() != config['implementation_sha256']:
            raise ValueError('Video implementation changed')
        if runtime_identity(bpy) != config['runtime']:
            raise ValueError('Blender runtime differs')
        if digest(config['candidate']['file']) != config['candidate']['sha256']:
            raise ValueError('Candidate bytes changed')
        bpy.ops.wm.open_mainfile(filepath=config['candidate']['file'], load_ui=False, use_scripts=False)
        scene = bpy.data.scenes[config['scene']]
        bpy.context.window.scene = scene
        actual = json.loads(json.dumps(core.inspect(config['scene'], config['workflow_id']), allow_nan=False))
        difference = mismatch(config['saved_expectation']['inspection'], actual)
        if difference:
            raise ValueError('Saved VSE inspection differs: ' + difference)
        protection = json.loads(json.dumps(core.snapshot_protection(config['scene']), allow_nan=False))
        difference = mismatch(config['saved_expectation']['protection'], protection)
        if difference:
            atomic_json(Path(config['output']).with_name('video-reopen-observed.json'), protection)
            raise ValueError('Saved project protection differs: ' + difference)
        declared = {str(Path(row['file']).resolve()).casefold(): row for row in config['resources']}
        actual_paths = sorted(set(str(Path(row if isinstance(row, str) else row.get('file', row.get('path'))).resolve()) for row in core.resource_paths()), key=str.casefold)
        if [row.casefold() for row in actual_paths] != [row.casefold() for row in config['saved_expectation']['resource_paths']]:
            raise ValueError('Actual saved resource paths differ')
        for value in actual_paths:
            row = declared.get(value.casefold())
            if row is None or digest(value) != row['expected_sha256']:
                raise ValueError('Saved resource undeclared or changed: ' + value)
        if digest(config['candidate']['file']) != config['candidate']['sha256']:
            raise ValueError('Candidate changed during independent read')
        report.update(status='passed', non_target_protection='passed', resources='passed', inspection=actual)
    except Exception as exc:
        report['error'] = str(exc)
    atomic_json(config['output'], report)
    return 0 if report['status'] == 'passed' else 8


if __name__ == '__main__':
    sys.exit(verify(sys.argv[sys.argv.index('--') + 1]))
