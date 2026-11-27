"""Dispatch to verified 5.3 add-on functions and save an isolated candidate."""

import importlib
import sys

import bpy

from protocol import CODES, Failure, atomic_json, digest, read_json
from production_contract import (HARD_SURFACE, PACKAGES, implementation_files, implementation_identity,
                                 package_root, release_manifest)
from projects import canonical, file_paths


def _context(selector):
    if selector is None:
        return bpy.context
    scene = bpy.data.scenes.get(selector['scene'])
    layer = scene.view_layers.get(selector['view_layer']) if scene is not None else None
    if layer is None:
        raise Failure('NOT_FOUND', 'Production scene or view layer does not exist')
    bpy.context.window.scene = scene
    bpy.context.window.view_layer = layer
    layer.update()
    return bpy.context


def _apply(domain, context, request, job):
    package = PACKAGES[domain]
    if domain == HARD_SURFACE:
        from production_hard_surface import apply
        return apply(context, request)
    if domain == 'animation':
        module = importlib.import_module(package + '.production_operations')
        result = module.apply(context, request)
        return result, result.get('status') not in ('inspected', 'unchanged')
    if domain == 'compositing':
        module = importlib.import_module(package + '.scene_effects')
        result = module.dispatch(context.scene.name, request)
        return result, request.get('operation') != 'inspect'
    if domain == 'video':
        module = importlib.import_module(package + '.core')
        checkpoint_dir = job / 'video-checkpoints'
        checkpoint_dir.mkdir()
        result = module.batch_edit(request, checkpoint_dir=checkpoint_dir)
        return result, result['status'] in ('committed', 'partial')
    module = importlib.import_module(package + '.goals')
    result = module.tune(request)
    return result, bool(result.get('applied'))


def _dependencies():
    ids = tuple(bpy.data.user_map())
    missing = sorted(item.name_full for item in ids if getattr(item, 'is_missing', False))
    if missing:
        raise Failure('VALIDATION_FAILED', f'Linked Blender data is missing: {missing}')
    loaded_libraries = {item.library for item in ids if item.library is not None}
    loaded_libraries.update(library.parent for library in bpy.data.libraries if library.parent is not None)
    provenance = {
        canonical(bpy.path.abspath(reference.filepath))
        for item in ids
        if (reference := getattr(item, 'library_weak_reference', None)) and reference.filepath
    }
    temporary_libraries = {
        canonical(bpy.path.abspath(library.filepath))
        for library in bpy.data.libraries
        if library not in loaded_libraries and library.filepath
    } & provenance
    return [path for path in file_paths() if path not in temporary_libraries]


def run(params, job):
    if tuple(bpy.app.version) != (5, 3, 0):
        raise Failure('UNSUPPORTED', 'Production operations require Blender 5.3.0')
    domain, request = params['domain'], params['request']
    launch = read_json(job / 'launch.json')
    release = launch['production_release']
    manifest = release_manifest(release)
    if manifest['blender_build'] != bpy.app.build_hash.decode():
        raise Failure('UNSUPPORTED', 'Packed production release requires its qualified Blender build')
    identity = implementation_identity(
        {path: digest(path) for path in implementation_files(domain, release)}, domain, release)
    if identity != launch.get('production_implementation_sha256'):
        raise Failure('CONFLICT', 'Production add-on candidate changed before Blender dispatch')
    sys.dont_write_bytecode = True
    sys.pycache_prefix = str(job / 'unused-production-pycache')
    sys.path.insert(0, str(package_root(domain, release).parent))
    source = params['file']
    if digest(source) != params['expected_sha256']:
        raise Failure('CONFLICT', 'Production source fingerprint changed')
    bpy.ops.wm.open_mainfile(filepath=source, load_ui=False, use_scripts=False)
    context = _context(params.get('context'))
    video_verify = None
    video_resources = {}
    video_cache = {}
    if domain == 'video':
        module = importlib.import_module(PACKAGES[domain] + '.core')
        video_verify = module.verify_batch_resources
    try:
        source_paths = _dependencies()
        if video_verify is not None:
            video_resources['source'] = video_verify(request, params['resources'], cache=video_cache)
        result, changed = _apply(domain, context, request, job)
    except Exception as error:
        code = getattr(error, 'code', 'WORKER_FAILED')
        if isinstance(error, (TypeError, ValueError)) and not hasattr(error, 'code'):
            code = 'INVALID_REQUEST'
        atomic_json(job / 'production-report.json', {
            'domain': domain, 'status': 'failed', 'message': str(error),
            'details': getattr(error, 'details', {}),
        })
        raise Failure(code if code in CODES else 'VALIDATION_FAILED', str(error)) from error

    report = {'domain': domain, 'blender_build': bpy.app.build_hash.decode(),
              'release': release, 'implementation_sha256': identity, 'source_sha256': params['expected_sha256'],
              'declared_resources': params['resources'], 'source_external_paths': source_paths,
              'result': result}
    if video_verify is not None:
        report['video_resources'] = video_resources
    if (domain == 'video' and result['status'] == 'rejected'
            or domain == 'lighting' and result['status'] == 'unsatisfied'):
        atomic_json(job / 'production-report.json', report)
        raise Failure('CONFLICT', 'Production request could not be applied; see the operation report')
    if changed:
        paths = _dependencies()
        staged = job / 'production-unverified.blend'
        output = job / 'production-candidate.blend'
        if staged.exists() or output.exists():
            raise Failure('CONFLICT', 'Production job output already exists')
        if video_verify is not None:
            video_resources['saved'] = video_verify(request, params['resources'], cache=video_cache)
        bpy.context.preferences.filepaths.save_version = 0
        bpy.ops.wm.save_as_mainfile(filepath=str(staged), check_existing=False, relative_remap=True, copy=True)
        if not staged.is_file():
            raise Failure('IO_ERROR', 'Blender did not save the production candidate')
        try:
            bpy.ops.wm.open_mainfile(filepath=str(staged), load_ui=False, use_scripts=False)
            reopened_paths = _dependencies()
            if paths != reopened_paths:
                report['expected_external_paths'] = paths
                report['observed_external_paths'] = reopened_paths
                raise Failure('VALIDATION_FAILED', 'Candidate changed external path targets on reopen')
            if video_verify is not None:
                video_resources['reopened'] = video_verify(
                    request, params['resources'], expected=video_resources['saved'], cache=video_cache)
            if domain == HARD_SURFACE:
                from production_hard_surface import check_reopen
                report['sweep_reopened'] = check_reopen(_context(params.get('context')), result)
        except Exception as error:
            report['reopen'] = {'status': 'failed', 'message': str(error)}
            atomic_json(job / 'production-report.json', report)
            raise
        staged.replace(output)
        report['reopen'] = {'status': 'passed', 'external_paths': paths}
        report['candidate'] = {'file': str(output), 'expected_sha256': digest(output)}
    atomic_json(job / 'production-report.json', report)
    return report
