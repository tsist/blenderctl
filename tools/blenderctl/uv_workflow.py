"""Protected saved-file UV adapter.

Blender is imported lazily so host-side protocol/discovery checks remain pure
Python. Mutations always write only ``<job>/uv-candidate.blend`` and then ask
the separately implemented ``uv_verifier.py`` to reopen that file in a fresh
Blender process.

Verifier request contract (``uv-verification-request.json``):
  candidate: {file, sha256}
  source: {file, sha256}
  implementation_sha256, runtime: {version, build, binary_sha256}
  operation, scene, view_layer, objects
  excluded_layers: [] (full post-operation protection is compared)
  expected_state: core.state(scene) after the operation
  expected_protection: core.protection(objects) after the operation
  output: path for the verifier's JSON report
The verifier must open with use_scripts=False, never save, compare both JSON
values exactly, and write status=passed only when every check succeeds.
"""
import copy
import json
import os
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path

from protocol import Failure, atomic_json, digest, read_json
from uv_workflow_contract import (implementation_sha256, normalize_inspect_params,
                                  normalize_params, signature)


def descriptor(path):
    path = Path(path).resolve()
    return {'file': str(path), 'sha256': digest(path)}


def _runtime_descriptor():
    import bpy
    return {'version': bpy.app.version_string,
            'build': bpy.app.build_hash.decode(),
            'binary_sha256': digest(bpy.app.binary_path)}


def _call(fn, *args, **kwargs):
    from uv_workflow_addon.contract import WorkflowError
    try:
        return fn(*args, **kwargs)
    except WorkflowError as exc:
        raise Failure(exc.code, str(exc)) from exc


def _core():
    from uv_workflow_addon import core
    return core


def _plain(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _check_identity(params):
    if implementation_sha256() != params['implementation_sha256']:
        raise Failure('CONFLICT', 'UV implementation changed after request normalization')


def _guard_inputs(params, stack):
    """Hold deny-write handles on the source and every declared resource."""
    from filesystem import FileGuard
    source = Path(params['file'])
    if not source.is_file():
        raise Failure('NOT_FOUND', 'UV source not found: ' + str(source))
    source_guard = stack.enter_context(FileGuard(source))
    if source_guard.sha256() != params['expected_sha256']:
        raise Failure('CONFLICT', 'UV source SHA mismatch')
    for row in params['resources']:
        path = Path(row['file'])
        if not path.is_file():
            raise Failure('NOT_FOUND', 'UV resource not found: ' + str(path))
        guard = stack.enter_context(FileGuard(path))
        if guard.sha256() != row['expected_sha256']:
            raise Failure('CONFLICT', 'UV resource SHA mismatch: ' + str(path))
    return source_guard


def _open_source(params):
    import bpy
    bpy.ops.wm.open_mainfile(filepath=params['file'], load_ui=False, use_scripts=False)
    if tuple(bpy.data.version[:2]) != tuple(bpy.app.version[:2]):
        raise Failure('UNSUPPORTED', 'UV requires matching Blender major/minor')


def _activate(manifest):
    import bpy
    scene = bpy.data.scenes.get(manifest['scene'])
    if scene is None:
        raise Failure('NOT_FOUND', 'Missing UV scene: ' + manifest['scene'])
    if manifest['view_layer'] not in scene.view_layers:
        raise Failure('NOT_FOUND', 'Missing UV view layer: ' + manifest['view_layer'])
    window = bpy.context.window
    if window is None:
        raise Failure('UNSUPPORTED', 'UV workflow requires a window context')
    window.scene = scene
    window.view_layer = scene.view_layers[manifest['view_layer']]
    bpy.context.view_layer.update()
    return scene


def _targets(scene, manifest):
    view_layer = scene.view_layers[manifest['view_layer']]
    objects = []
    for name in manifest['objects']:
        obj = view_layer.objects.get(name)
        if obj is None or obj.type != 'MESH':
            raise Failure('NOT_FOUND', 'Missing UV mesh target: ' + name)
        objects.append(obj)
    return objects


def _resources(params):
    """Check every explicit external tile under the caller's held FileGuards."""
    import bpy
    from uv_resources import resources,absolute_paths
    closure=resources(bpy,params)
    absolute_paths(bpy)
    return closure


def _progress(report, job, started, cancel_parent=None):
    job = Path(job)

    def progress(stage, **extra):
        if (job / 'cancel.request').exists():
            raise Failure('CANCELLED', 'UV cancelled at checkpoint')
        if cancel_parent and (Path(cancel_parent) / 'cancel.request').exists():
            raise Failure('CANCELLED', 'UV parent cancelled at checkpoint')
        report['stage'] = stage
        atomic_json(job / 'uv-report.json', report)
        atomic_json(job / 'uv-progress.json',
                    {'uv_progress_version': '1.0', 'stage': stage, 'status': 'running',
                     'elapsed_seconds': round(time.monotonic() - started, 3), **extra})

    return progress


def _candidate_layers(state):
    rows = state.get('candidates') if isinstance(state, dict) else None
    if not isinstance(rows, dict):
        return []
    return sorted({row.get('layer') for row in rows.values()
                   if isinstance(row, dict) and isinstance(row.get('layer'), str)})


def _render_bindings(objects):
    return {obj.name: sorted(layer.name for layer in obj.data.uv_layers
                             if layer.active_render) for obj in objects}


def _quality_acceptance(result, operation):
    value = result.get('quality_acceptance') if isinstance(result, dict) else None
    if value in ('pass', 'passed', 'approved'):
        value = None
    if value:
        return value
    return {'generate': 'requires_visual_and_purpose_review',
            'validate': 'requires_visual_and_purpose_review',
            'adopt': 'not_automatically_approved',
            'restore': 'not_applicable_restore',
            'seams': 'seams_updated_not_uv_evaluated',
            'lightmap': 'auxiliary_lightmap_requires_bake_review'}.get(
                operation, 'requires_visual_and_purpose_review')


def _verification_config(candidate, params, manifest, runtime, inspection,
                         expected_state, expected_protection, non_target_sha256,
                         persistence_exceptions, job, read_only_sources=None):
    if not isinstance(non_target_sha256, str) or not non_target_sha256:
        raise Failure('VALIDATION_FAILED', 'UV non-target fingerprint is required')
    config = {
        'uv_verification_version': '1.0',
        'candidate': candidate,
        'source': {'file': params['file'], 'sha256': params['expected_sha256']},
        'implementation_sha256': params['implementation_sha256'],
        'runtime': runtime,
        'operation': manifest['operation'],
        'manifest': manifest,
        'scene': manifest['scene'],
        'view_layer': manifest['view_layer'],
        'objects': list(manifest['objects']),
        'excluded_layers': [],
        'inspection': inspection,
        'expected_state': expected_state,
        'expected_protection': expected_protection,
        'protection': expected_protection,
        'non_target_sha256': non_target_sha256,
        'persistence_exceptions': list(persistence_exceptions),
        'output': str(Path(job).resolve() / 'uv-reopen.json'),
    }
    if read_only_sources is not None:
        config['read_only_sources'] = read_only_sources
    return config


def _launch_verifier(config, job):
    import bpy
    job = Path(job)
    request = job / 'uv-verification-request.json'
    atomic_json(request, config)
    verifier = Path(__file__).with_name('uv_verifier.py')
    if not verifier.is_file():
        raise Failure('NOT_FOUND',
                      'uv_verifier.py is required for independent UV reopen verification')
    args = [bpy.app.binary_path, '--background', '--factory-startup', '--disable-autoexec',
            '--offline-mode', '--python', str(verifier), '--', str(request)]
    try:
        with (job / 'uv-reopen.log').open('wb') as log:
            process = subprocess.run(
                args, stdout=log, stderr=subprocess.STDOUT, timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    except subprocess.TimeoutExpired as exc:
        raise Failure('TIMEOUT', 'Independent UV reopen timed out') from exc
    output = Path(config['output'])
    result = read_json(output) if output.is_file() else {
        'status': 'failed', 'error': 'No independent reader result'}
    if process.returncode or result.get('status') != 'passed':
        raise Failure('VALIDATION_FAILED',
                      'Independent UV reopen: ' + str(result.get('error')))
    return result


def run(params, job, *, cancel_parent=None):
    params = normalize_params(params, 'uv.run')
    job = Path(job).resolve()
    started = time.monotonic()
    manifest = params['manifest']
    operation = manifest['operation']
    runtime = None
    report = {
        'uv_report_version': '1.0',
        'status': 'running',
        'operation': operation,
        'source': {'file': params['file'], 'sha256': params['expected_sha256']},
        'runtime': None,
        'implementation_sha256': params['implementation_sha256'],
        'signature': signature(params),
        'candidate': None,
        'verification': None,
        'checks': {'source_sha256': 'pending', 'resources': 'pending',
                   'scene_view_layer': 'pending', 'non_target': 'pending',
                   'independent_reopen': 'pending',
                   'quality': 'not_automatically_approved'},
        'quality_acceptance': 'requires_visual_and_purpose_review',
        'support': {
            'milestone': 'M3 protected correspondence and transfer adapter',
            'limitations': [
                'UDIM layouts use full tiles and explicit reuse groups; sequence/movie expansion is unsupported',
                'Candidate quality checks never imply visual or purpose-stretch acceptance',
                'Source files are never written; only a new job candidate is saved',
            ],
        },
        'recovery': {'available': False,
                     'note': 'UV mutations start from the protected source or an explicitly adopted saved candidate state'},
    }
    progress = _progress(report, job, started, cancel_parent)
    try:
        runtime = _runtime_descriptor()
        report['runtime'] = runtime
        _check_identity(params)
        progress('source_open')
        with ExitStack() as stack:
            source_guard = _guard_inputs(params, stack)
            _open_source(params)
            closure = _resources(params)
            report['resources'] = closure
            from geometry_workflow import _retain_unlinked_ids
            report['persistence_exceptions'] = _retain_unlinked_ids()
            scene = _activate(manifest)
            objects = _targets(scene, manifest)
            core = _core()
            from uv_saved_protection import scene_fingerprint
            non_target_before = scene_fingerprint(
                exclude_objects=manifest['objects'],
                persistence_exceptions=report['persistence_exceptions'])
            render_before = _render_bindings(objects)
            progress('apply', operation=operation)
            result = _call(core.execute, manifest, progress=progress)
            render_after = _render_bindings(objects)
            lost_render = {name: sorted(set(render_before[name]) - set(render_after[name]))
                           for name in render_before
                           if set(render_before[name]) - set(render_after[name])}
            if lost_render:
                raise Failure('VALIDATION_FAILED',
                              'UV render bindings changed: ' + str(lost_render))
            report['render_bindings'] = {'status': 'original_layers_retained',
                                         'before': render_before,
                                         'after': render_after}
            non_target_after = scene_fingerprint(
                exclude_objects=manifest['objects'],
                persistence_exceptions=report['persistence_exceptions'])
            if non_target_after != non_target_before:
                raise Failure('VALIDATION_FAILED',
                              'UV non-target scene data changed during the operation')
            non_target_sha256 = non_target_after
            report['non_target_check'] = {'status': 'unchanged',
                                          'before': non_target_before,
                                          'after': non_target_after}
            inspection_manifest = copy.deepcopy(manifest)
            inspection_manifest['operation'] = 'inspect'
            committed = result.get('record', {}).get('manifest', {})
            if 'udim' in committed:
                inspection_manifest.update({key: committed[key] for key in ('width', 'height', 'border_px', 'margin_px', 'pair_budget')})
                inspection_manifest['udim'] = {'layer': result['record']['layer']}
            inspection = _plain(_call(core.execute, inspection_manifest))
            expected_state = _plain(_call(core.state, scene))
            expected_protection = _plain(_call(core.protection, objects))
            candidate_path = job / 'uv-candidate.blend'
            if os.path.lexists(candidate_path):
                raise Failure('CONFLICT', 'Owned UV candidate already exists')
            if os.path.normcase(str(candidate_path)) == os.path.normcase(str(Path(params['file']))):
                raise Failure('CONFLICT', 'UV candidate path equals source')
            progress('save_checkpoint')
            import bpy
            bpy.ops.wm.save_as_mainfile(filepath=str(candidate_path),
                                        check_existing=False, relative_remap=False)
            candidate = descriptor(candidate_path)
            report['candidate'] = candidate
            report['candidate_layers'] = _candidate_layers(expected_state)
            report['inspection'] = inspection
            report['non_target_sha256'] = non_target_sha256
            report['saved_expectation'] = {
                'state': expected_state,
                'protection': expected_protection,
                'inspection': inspection,
                'non_target_sha256': non_target_sha256,
                'excluded_layers': [],
            }
            report['recovery'] = {
                'available': True,
                'candidate': candidate,
                'note': 'The source remains unchanged; retain this candidate as the working copy.',
            }
            from filesystem import FileGuard
            candidate_guard = stack.enter_context(FileGuard(candidate['file']))
            if candidate_guard.sha256() != candidate['sha256']:
                raise Failure('CONFLICT', 'UV candidate changed before guarded verification')
            progress('independent_reopen')
            config = _verification_config(candidate, params, manifest, runtime, inspection,
                                          expected_state, expected_protection,
                                          non_target_sha256,
                                          report.get('persistence_exceptions', []), job,
                                          result.get('read_only_sources'))
            if 'udim' in inspection_manifest:
                config['inspection_manifest'] = inspection_manifest
            verification = _launch_verifier(config, job)
            report['verification'] = verification
            if 'read_only_sources' in result:
                report['read_only_sources'] = result['read_only_sources']
            if candidate_guard.sha256() != candidate['sha256']:
                raise Failure('CONFLICT', 'UV candidate changed during verification')
            if source_guard.sha256() != params['expected_sha256']:
                raise Failure('CONFLICT', 'UV source changed during job')
            quality_acceptance = _quality_acceptance(result, operation)
            report['status'] = result.get('status') or ('candidates_created'
                                                        if operation == 'generate' else 'completed')
            report['core_status'] = result.get('status')
            if 'candidates' in result:
                report['candidates'] = result['candidates']
            if 'record' in result:
                report['record'] = result['record']
            if 'udim' in inspection:
                from uv_workflow_contract import udim_report_fields
                report.update(udim_report_fields(inspection['udim']))
            for key in ('lightmap', 'bounds', 'active_render_preserved', 'overwrote_existing',
                        'layer', 'target_scope', 'seam_action'):
                if key in result:
                    report[key] = result[key]
            if operation in ('lightmap', 'seams') and 'objects' in result:
                report['objects'] = result['objects']
            if operation == 'lightmap' and 'metrics' in result:
                report['lightmap_metrics'] = result['metrics']
            report['quality_acceptance'] = quality_acceptance
            report['checks'].update({
                'source_sha256': 'passed',
                'resources': 'static_paths_checked',
                'scene_view_layer': 'passed',
                'non_target': 'passed_unchanged',
                'independent_reopen': 'passed',
                'quality': 'not_automatically_approved',
            })
            report['metrics'] = {'wall_seconds': round(time.monotonic() - started, 3)}
            atomic_json(job / 'uv-manifest.json', manifest)
            atomic_json(job / 'uv-report.json', report)
            atomic_json(job / 'uv-progress.json',
                        {'uv_progress_version': '1.0', 'stage': 'complete',
                         'status': report['status'],
                         'elapsed_seconds': round(time.monotonic() - started, 3),
                         'quality_acceptance': quality_acceptance})
            data = dict(result)
            data.update(report=descriptor(job / 'uv-report.json'),
                        candidate=candidate,
                        verification=verification,
                        implementation_sha256=params['implementation_sha256'],
                        quality_acceptance=quality_acceptance)
            return data
    except Failure as exc:
        report.update(status='failed', error={'code': exc.code, 'message': str(exc)})
        atomic_json(job / 'uv-report.json', report)
        atomic_json(job / 'uv-progress.json',
                    {'uv_progress_version': '1.0', 'stage': 'failed', 'status': 'failed',
                     'elapsed_seconds': round(time.monotonic() - started, 3),
                     'error': {'code': exc.code, 'message': str(exc)}})
        raise
    except Exception as exc:
        report.update(status='failed',
                      error={'code': 'WORKER_FAILED', 'message': str(exc)})
        atomic_json(job / 'uv-report.json', report)
        atomic_json(job / 'uv-progress.json',
                    {'uv_progress_version': '1.0', 'stage': 'failed', 'status': 'failed',
                     'elapsed_seconds': round(time.monotonic() - started, 3),
                     'error': {'code': 'WORKER_FAILED', 'message': str(exc)}})
        raise


def inspect(params, job):
    params = normalize_inspect_params(params)
    job = Path(job).resolve()
    started = time.monotonic()
    manifest = params['manifest']
    runtime = None
    report = {
        'uv_report_version': '1.0',
        'status': 'running',
        'operation': 'inspect',
        'source': {'file': params['file'], 'sha256': params['expected_sha256']},
        'runtime': None,
        'implementation_sha256': params['implementation_sha256'],
        'signature': signature(params),
        'quality_acceptance': 'not_applicable_read_only',
        'checks': {'source_sha256': 'pending', 'resources': 'pending',
                   'scene_view_layer': 'pending', 'saves': 'none'},
        'support': {'milestone': 'M3 protected correspondence and transfer adapter',
                    'limitations': ['Read-only inspection; no candidate or source save']},
    }
    progress = _progress(report, job, started)
    try:
        runtime = _runtime_descriptor()
        report['runtime'] = runtime
        _check_identity(params)
        with ExitStack() as stack:
            source_guard = _guard_inputs(params, stack)
            _open_source(params)
            closure = _resources(params)
            report['resources'] = closure
            scene = _activate(manifest)
            _targets(scene, manifest)
            core = _core()
            inspection = _call(core.execute, manifest, progress=progress)
            if source_guard.sha256() != params['expected_sha256']:
                raise Failure('CONFLICT', 'UV source changed during inspection')
            report.update(status='inspected', inspection=inspection,
                          state=inspection.get('state'))
            if 'udim' in inspection:
                from uv_workflow_contract import udim_report_fields
                report.update(udim_report_fields(inspection['udim']))
            if 'surface' in inspection:
                report['surface']=inspection['surface']
                report['quality_acceptance']=inspection['surface']['quality_acceptance']
            if 'diagnostics' in inspection:
                report['diagnostics']=inspection['diagnostics']
            if 'seam_suggestions' in inspection:
                report['seam_suggestions']=inspection['seam_suggestions']
            report['checks'].update({'source_sha256': 'passed',
                                     'resources': 'static_paths_checked',
                                     'scene_view_layer': 'passed',
                                     'saves': 'none'})
            report['metrics'] = {'wall_seconds': round(time.monotonic() - started, 3)}
            atomic_json(job / 'uv-manifest.json', manifest)
            atomic_json(job / 'uv-report.json', report)
            atomic_json(job / 'uv-progress.json',
                        {'uv_progress_version': '1.0', 'stage': 'complete',
                         'status': 'inspected',
                         'elapsed_seconds': round(time.monotonic() - started, 3)})
            return {'status': 'inspected', 'inspection': inspection,
                    'resources': closure, 'implementation_sha256': params['implementation_sha256'],
                    'quality_acceptance': report['quality_acceptance'],
                    'report': descriptor(job / 'uv-report.json')}
    except Failure as exc:
        report.update(status='failed', error={'code': exc.code, 'message': str(exc)})
        atomic_json(job / 'uv-report.json', report)
        atomic_json(job / 'uv-progress.json',
                    {'uv_progress_version': '1.0', 'stage': 'failed', 'status': 'failed',
                     'elapsed_seconds': round(time.monotonic() - started, 3),
                     'error': {'code': exc.code, 'message': str(exc)}})
        raise
    except Exception as exc:
        report.update(status='failed',
                      error={'code': 'WORKER_FAILED', 'message': str(exc)})
        atomic_json(job / 'uv-report.json', report)
        atomic_json(job / 'uv-progress.json',
                    {'uv_progress_version': '1.0', 'stage': 'failed', 'status': 'failed',
                     'elapsed_seconds': round(time.monotonic() - started, 3),
                     'error': {'code': 'WORKER_FAILED', 'message': str(exc)}})
        raise
