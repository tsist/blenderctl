"""Protected Cycles projection bake in one owned Blender worker.

The add-on writes only fresh map generations below this job's raw directory.
The source .blend is opened read-only in practice: this adapter never saves it.
"""
from contextlib import ExitStack
from pathlib import Path
import time

import bpy

from protocol import CODES, Failure, atomic_json, digest
from filesystem import FileGuard
from material_projection_contract import normalize_params, verify_input_hashes
from material_workflow_addon.contract import WorkflowError
from material_workflow_addon.projection_bake import bake_projection
from material_workflow_addon.projection_material import read_projection


def _failure(error):
    if isinstance(error, Failure):
        return error
    code = getattr(error, 'code', '')
    return Failure(code if code in CODES else 'VALIDATION_FAILED', str(error))


def _find(collection, name):
    matches = [item for item in collection if item.name == name]
    if len(matches) != 1:
        raise Failure('CONFLICT' if matches else 'NOT_FOUND',
                      'Name must resolve uniquely: ' + name)
    return matches[0]


def _activate(context):
    scene = _find(bpy.data.scenes, context['scene'])
    layer = _find(scene.view_layers, context['view_layer'])
    bpy.context.window.scene = scene
    bpy.context.window.view_layer = layer
    scene.frame_set(context['frame'])
    layer.update()
    if bpy.context.mode != 'OBJECT':
        raise Failure('UNSUPPORTED', 'Projection bake requires Object Mode')
    return scene, layer


def _object(name, layer):
    obj = _find(bpy.data.objects, name)
    if obj.name not in layer.objects:
        raise Failure('INVALID_REQUEST', 'Object is outside the selected view layer: ' + name)
    return obj


def _progress(job, stage, report, current=None, planned=None):
    value = {'stage': stage, 'status': report['status'],
             'planned_pairs': planned,
             'completed_pairs': sum(1 for row in report['pairs'] if row.get('manifest')),
             'current_pair': current}
    atomic_json(job / 'projection-progress.json', value)


def _relative(job, path):
    path = Path(path).resolve()
    if not path.is_relative_to(job.resolve()):
        raise Failure('VALIDATION_FAILED', 'Projection output escaped its job directory: ' + str(path))
    return path.relative_to(job.resolve()).as_posix()


def _map_summary(job, report, files):
    maps = {}
    for channel, path in sorted(files.items()):
        entry = report['maps'][channel]
        maps[channel] = {
            'file': _relative(job, path),
            'sha256': entry['sha256'], 'bytes': entry['bytes'],
            'width': entry['width'], 'height': entry['height'],
            'bit_depth': entry['bit_depth'], 'color_space': entry['color_space'],
            'format': entry.get('format'), 'decoded': entry.get('decoded'),
            'meaning': entry.get('meaning'),
        }
    return maps


def _pair_report(job, manifest, pair, index):
    manifest = Path(manifest).resolve()
    report, files = read_projection(manifest)
    if report['target'] != pair['target'] or list(report['sources']) != list(pair['sources']):
        raise Failure('VALIDATION_FAILED', 'Projection manifest target/sources differ from request')
    if report['uv_layer'] != pair['uv_layer']:
        raise Failure('VALIDATION_FAILED', 'Projection manifest UV layer differs from request')
    maps = _map_summary(job, report, files)
    return {
        'index': index, 'target': report['target'], 'sources': list(report['sources']),
        'uv_layer': report['uv_layer'], 'cage': pair.get('cage'),
        'settings': report['settings'], 'identity': report.get('identity'),
        'uv_check': report.get('uv_check'), 'witness': report.get('witness'), 'maps': maps,
        'manifest': {'file': _relative(job, manifest), 'sha256': digest(manifest),
                     'bytes': manifest.stat().st_size, 'schema': report.get('schema'),
                     'status': report.get('status')},
    }


def _execute(params, job):
    job = Path(job).resolve()
    report = {'report_version': '1.0', 'operation': 'material.projection-bake',
              'status': 'running', 'source_saved': False, 'checks': {},
              'pairs': [], 'outputs': [], 'limitations': [
                  'Raw Cycles projection maps. Use the material panel to bind maps and deliver the project.',
                  'Review the Normal, Curvature and AO result in the target Blender scene.']}
    started = time.monotonic()
    try:
        params = normalize_params(params)
        report['source'] = {'file': params['file'], 'expected_sha256': params['expected_sha256']}
        report['context'] = dict(params['context'])
        total = len(params['pairs'])
        _progress(job, 'preflight', report, planned=total)
        with ExitStack() as guards:
            guard = guards.enter_context(FileGuard(params['file']))
            if guard.sha256() != params['expected_sha256']:
                raise Failure('CONFLICT', 'Projection source changed before execution')
            verify_input_hashes(params)
            bpy.ops.wm.open_mainfile(filepath=params['file'], load_ui=False, use_scripts=False)
            if tuple(bpy.data.version[:2]) != tuple(bpy.app.version[:2]):
                raise Failure('UNSUPPORTED',
                              'Projection bake requires matching Blender major/minor')
            if not Path(bpy.data.filepath).samefile(params['file']):
                raise Failure('VALIDATION_FAILED', 'Worker opened an unexpected source file')
            if bpy.data.libraries or bpy.data.cache_files:
                raise Failure('UNSUPPORTED', 'Use local projection geometry without linked libraries or external caches')
            scene, layer = _activate(params['context'])
            report['environment'] = {'blender': bpy.app.version_string,
                                     'build': bpy.app.build_hash.decode(),
                                     'scene': scene.name, 'view_layer': layer.name}
            output_root = job / 'raw'
            for index, pair in enumerate(params['pairs'], 1):
                _progress(job, 'baking', report, index, total)
                target = _object(pair['target'], layer)
                sources = [_object(name, layer) for name in pair['sources']]
                cage = _object(pair['cage'], layer) if pair.get('cage') else None
                if target.type != 'MESH' or target.data is None:
                    raise Failure('INVALID_REQUEST',
                                  'Projection target must be a mesh: ' + pair['target'])
                if pair['uv_layer'] not in target.data.uv_layers:
                    raise Failure('NOT_FOUND', 'Target UV layer missing: ' + pair['uv_layer'])
                settings = pair['settings']
                manifest = bake_projection(
                    bpy.context, target, sources, pair['uv_layer'], output_root,
                    size=settings['resolution'], extrusion=settings['extrusion'],
                    distance=settings['distance'], cage=cage,
                    ao_distance=settings['ao_distance'],
                    curvature_radius=settings['curvature_radius'],
                    samples=settings['samples'], margin=settings['margin'])
                entry = _pair_report(job, manifest, pair, index)
                report['pairs'].append(entry)
                report['outputs'].extend(
                    {'pair': index, 'channel': channel, 'status': 'pass', **document}
                    for channel, document in entry['maps'].items())
            verify_input_hashes(params)
            report['checks'].update(source_sha_unchanged='pass', source_saved='false',
                                    input_hash_recheck='pass', manifests_verified='pass',
                                    raw_maps_job_owned='pass')
            report['status'] = 'pass'
            _progress(job, 'complete', report, planned=total)
    except Exception as error:
        err = _failure(error)
        report['status'] = 'failed'
        report['error'] = {'code': err.code, 'message': str(err)}
        _progress(job, 'failed', report, planned=locals().get('total'))
        raise err from error
    finally:
        report['seconds'] = time.monotonic() - started
        atomic_json(job / 'projection-report.json', report)
    return report


def run(params, job):
    return _execute(params, job)
