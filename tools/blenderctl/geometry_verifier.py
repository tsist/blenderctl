"""Independent Blender reader for geometry candidates. Never saves the input."""
import json
import math
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from protocol import atomic_json, digest
from geometry_identity import implementation_sha256


def near(a, b, path='root'):
    if isinstance(a, bool) or isinstance(b, bool):
        return None if type(a) is type(b) and a == b else path
    if isinstance(a, (float, int)) and isinstance(b, (float, int)):
        return None if math.isfinite(a) and math.isfinite(b) and math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6) else path
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b): return path + '.keys'
        return next((m for k in a if (m := near(a[k], b[k], path + '.' + k))), None)
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b): return path + '.length'
        return next((m for i, (x, y) in enumerate(zip(a, b)) if (m := near(x, y, path + '[' + str(i) + ']'))), None)
    return None if type(a) is type(b) and a == b else path


def stable_evaluation(row, include_observations=False):
    keys = ('base', 'evaluated', 'instances', 'observations') if include_observations else ('base', 'evaluated', 'instances')
    return {key: row[key] for key in keys if key in row}


def activate(context):
    import bpy
    scene = bpy.data.scenes.get(context['scene'])
    if scene is None: raise ValueError('Missing scene: ' + context['scene'])
    layer = scene.view_layers.get(context['view_layer'])
    if layer is None: raise ValueError('Missing view layer: ' + context['view_layer'])
    bpy.context.window.scene = scene
    bpy.context.window.view_layer = layer
    scene.frame_set(context['frame'])
    layer.update()


def verify(config_path):
    import bpy
    from geometry_nodes_workflow_addon import core, inspection
    config = json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
    output = Path(config['output'])
    report = {'geometry_verification_version': '1.0', 'status': 'failed',
              'candidate_sha256': config['candidate']['sha256'],
              'implementation_sha256': implementation_sha256(),
              'blender': {'version': bpy.app.version_string, 'version_tuple': list(bpy.app.version), 'build_hash': bpy.app.build_hash.decode()},
              'targets': [], 'float_tolerance': {'absolute': 1e-6, 'relative': 1e-6}}
    try:
        if report['implementation_sha256'] != config['implementation_sha256']:
            raise ValueError('Geometry implementation changed before independent verification')
        if digest(config['candidate']['file']) != config['candidate']['sha256']:
            raise ValueError('Candidate SHA mismatch')
        bpy.ops.wm.open_mainfile(filepath=config['candidate']['file'], load_ui=False, use_scripts=False)
        temporal = config.get('temporal')
        cache = None
        if temporal:
            from geometry_temporal import validate_cache, validate_loaded_bindings
            cache = validate_cache(temporal['cache_manifest'], candidate=config['candidate'],
                                   identity=config['implementation_sha256'])
            validate_loaded_bindings(cache)
        activate(config['context'])
        if config.get('protection'):
            guard = config['protection']
            actual = inspection.scene_fingerprint(exclude_objects=guard['exclude_objects'], persistence_exceptions=guard.get('persistence_exceptions', ()))
            if actual != guard['non_target_sha256']: raise ValueError('Independent non-target content differs')
            report['non_target_protection'] = 'passed'
        for expected in config['targets']:
            binding = core.inspect_binding(expected['object'], expected['modifier'])
            evaluation = inspection.inspect_target(expected['object'], expected['modifier'], expected.get('observations'))
            mismatch = near(expected['binding'], binding, 'binding')
            mismatch = mismatch or near(stable_evaluation(expected['evaluation'], True), stable_evaluation(evaluation, True), 'evaluation')
            row = {'id': expected['id'], 'object': expected['object'], 'modifier': expected['modifier'],
                   'binding': binding, 'evaluation': evaluation, 'status': 'failed' if mismatch else 'passed'}
            if mismatch: row['mismatch'] = mismatch
            report['targets'].append(row)
            if mismatch: raise ValueError('Independent reopen differs: ' + expected['id'] + ' ' + mismatch)
        if cache:
            temporal_rows = []
            expected_samples = {sample['frame']: sample for sample in cache['samples']}
            target_rows = {target['id']: target for target in cache['targets']}
            for frame in cache['readback_order']:
                bpy.context.scene.frame_set(frame)
                bpy.context.view_layer.update()
                row = {'frame': frame, 'targets': [], 'status': 'passed'}
                temporal_rows.append(row)
                for expected in expected_samples[frame]['targets']:
                    target = target_rows[expected['target']]
                    observations = [o for o in cache['observations'] if not o.get('object') or o['object'] == target['object']]
                    evaluation = stable_evaluation(inspection.inspect_target(target['object'], target['modifier'], observations), True)
                    mismatch = near(expected['evaluation'], evaluation, 'temporal.evaluation')
                    row['targets'].append({'target': expected['target'], 'evaluation': evaluation,
                                           'status': 'failed' if mismatch else 'passed', 'mismatch': mismatch})
                    if mismatch:
                        row['status'] = 'failed'
                        report['temporal'] = {'status': 'failed', 'samples': temporal_rows}
                        raise ValueError('Independent temporal replay differs at frame ' + str(frame) + ': ' + mismatch)
            # Recheck the full receipt after reading, including immutable members.
            validate_cache(temporal['cache_manifest'], candidate=config['candidate'], identity=config['implementation_sha256'])
            report['temporal'] = {'status': 'passed', 'samples': temporal_rows,
                                  'readback_order': cache['readback_order'], 'cache_manifest': temporal['cache_manifest']}
        if digest(config['candidate']['file']) != config['candidate']['sha256']:
            raise ValueError('Candidate changed during read-only verification')
        report['status'] = 'passed'
        atomic_json(output, report)
        return report
    except Exception as exc:
        report['error'] = str(exc)
        atomic_json(output, report)
        raise


if __name__ == '__main__':
    args = sys.argv[sys.argv.index('--') + 1:]
    verify(args[0])
