"""Render the UV Workflow checker on a saved candidate and verify a work copy."""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
sys.path[:0] = [str(ENTRY.parent), str(ENTRY.parent.parent)]

from filesystem import FileGuard
from protocol import (CODES, DEFAULT_BLENDER, DEFAULT_JOBS, Failure, atomic_json,
                      digest, envelope, read_json, timeout_value)
from uv_saved_reader import run_saved_reader
from uv_workflow_contract import normalize_inspect_params


COMMAND = 'uv.checker-preview'
MARKER = '--uv-checker-worker'
CONTEXT_FIELDS = {'version', 'operation', 'scene', 'view_layer', 'candidate', 'objects'}
WORK_COPY = 'uv-checker-work-copy.blend'
PREVIEW = 'uv-checker-preview.png'


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure('INVALID_REQUEST', message)


def parser():
    cli = Parser(description=__doc__)
    cli.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    cli.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS)
    cli.add_argument('--transactions-dir', help=argparse.SUPPRESS)
    cli.add_argument('--timeout', type=float, default=120)
    output = cli.add_mutually_exclusive_group()
    output.add_argument('--full', action='store_true')
    output.add_argument('--compact', action='store_true')
    output.add_argument('--human', action='store_true')
    route = cli.add_subparsers(dest='action', required=True, parser_class=Parser)
    preview = route.add_parser('checker-preview', description=__doc__)
    preview.add_argument('--params', type=Path, required=True,
                         help='UV inspect parameters with explicit scene, view_layer, candidate and complete objects')
    preview.add_argument('--size', type=int, default=1024, metavar='64..2048', help='Square PNG size in pixels')
    return cli


def require(condition, code, message):
    if not condition:
        raise Failure(code, message)


def candidate_context(request, *, require_saved_context=False):
    import bpy
    from uv_workflow import _activate, _check_identity, _resources
    from uv_workflow_addon import core, ui

    base = request['base']
    _check_identity(base)
    require(bpy.app.background and not bpy.app.online_access, 'UNSUPPORTED', 'Checker requires offline background Blender')
    ui.register()
    outcome = bpy.ops.wm.open_mainfile(filepath=base['file'], load_ui=True, use_scripts=False)
    require(outcome == {'FINISHED'} and Path(bpy.data.filepath).resolve() == Path(base['file']),
            'VALIDATION_FAILED', 'Could not reopen the requested work file')
    require(bpy.data.version[:2] == bpy.app.version[:2], 'UNSUPPORTED', 'UV checker requires matching Blender major/minor')
    manifest = base['manifest']
    if require_saved_context:
        require(bpy.context.scene.name == manifest['scene'] and bpy.context.view_layer.name == manifest['view_layer'],
                'VALIDATION_FAILED', 'Reopened work copy did not persist the requested scene and view layer')
    scene = _activate(manifest)
    _, targets = core._resolve(manifest)
    candidate = manifest['candidate']
    record = core.state(scene)['candidates'].get(candidate)
    require(record and set(record['objects']) == set(manifest['objects']), 'CONFLICT',
            'Candidate is missing or its complete target set differs')
    recorded = record['manifest']
    require(all(recorded[key] == manifest[key] for key in ('scene', 'view_layer')), 'CONFLICT',
            'Candidate was bound to a different saved scene or view layer')
    require(scene.uvw_candidate in ('', candidate), 'CONFLICT',
            'Saved candidate selection differs; save the intended candidate selection first')
    for obj in targets:
        require(obj.visible_get(view_layer=bpy.context.view_layer), 'CONFLICT', f'Checker target is hidden: {obj.name}')
        require(obj.data.name == record['meshes'].get(obj.name) and record['layer'] in obj.data.uv_layers,
                'CONFLICT', f'Saved mesh or candidate UV layer differs: {obj.name}')
    closure = _resources(base)
    return scene, targets, candidate, record, closure


def witness(scene, targets, record, closure):
    """Check authored state and the current evaluated surface on both disk reads."""
    import bpy
    from uv_workflow_addon import core
    from uv_workflow_addon.surface import _surface_snapshot

    graph = bpy.context.evaluated_depsgraph_get()
    require(graph.mode == 'VIEWPORT', 'VALIDATION_FAILED', 'Checker requires the VIEWPORT dependency graph')
    layer = record['layer']
    evaluated = {}
    for obj in targets:
        ev = obj.evaluated_get(graph)
        require(ev.is_evaluated, 'CONFLICT', f'Target missing from VIEWPORT graph: {obj.name}')
        mesh = ev.to_mesh(preserve_all_data_layers=True, depsgraph=graph)
        try:
            require(mesh is not None and layer in mesh.uv_layers, 'CONFLICT',
                    f'Candidate UV missing from evaluated mesh: {obj.name}')
            mesh.calc_loop_triangles()
            snapshot = _surface_snapshot(mesh, ev.matrix_world, layer, scene.unit_settings.scale_length)
            require(not snapshot['nonfinite'], 'VALIDATION_FAILED', f'Nonfinite candidate surface: {obj.name}')
            evaluated[obj.name] = {'surface_sha256': snapshot['sha256'], 'vertices': len(mesh.vertices),
                                   'triangles': len(mesh.loop_triangles),
                                   'current_uv_sha256': core._uv_fingerprint(obj.data, layer)}
        finally:
            ev.to_mesh_clear()
    return {'scene': scene.name, 'view_layer': bpy.context.view_layer.name,
            'frame': scene.frame_current, 'subframe': scene.frame_subframe,
            'state_sha256': core._hash(core.state(scene)), 'record_sha256': core._hash(record), 'layer': layer,
            'protected_targets_sha256': core._hash(core.protection(targets)), 'evaluated': evaluated,
            'scenes': sorted(bpy.data.scenes.keys()), 'objects': sorted(bpy.data.objects.keys()),
            'resources': sorted(row['file'] for row in closure['rows'])}


def render_copy(request, job):
    import bpy
    from uv_workflow_addon import bl_info
    from uv_workflow_addon.diagnostics import render

    scene, targets, candidate, record, closure = candidate_context(request)
    before = witness(scene, targets, record, closure)
    image = job / PREVIEW
    copy = job / WORK_COPY
    require(not image.exists() and not copy.exists(), 'CONFLICT', 'Checker output already exists in this job')
    preview = render(scene, candidate, str(image), size=request['size'])
    require(image.is_file() and image.stat().st_size > 0, 'VALIDATION_FAILED', 'Checker renderer did not write PNG')
    require(witness(scene, targets, record, closure) == before, 'VALIDATION_FAILED',
            'Checker changed the authored context or evaluated candidate')
    if scene.uvw_candidate != candidate:
        drafts = {key: scene[key] for key in ('uvw_edit_selection', 'uvw_stack_reference') if key in scene}
        scene.uvw_candidate = candidate
        for key, value in drafts.items():
            scene[key] = value
    outcome = bpy.ops.wm.save_as_mainfile(filepath=str(copy), check_existing=False, relative_remap=True, copy=True)
    require(outcome == {'FINISHED'}, 'WORKER_FAILED', 'Unable to save a new checker work copy')
    return {'producer_pid': os.getpid(), 'blender': bpy.app.version_string,
            'uv_workflow_version': '.'.join(map(str, bl_info['version'])),
            'build': bpy.app.build_hash.decode(), 'candidate': candidate, 'candidate_witness': before,
            'render': preview, 'preview': {'file': str(image), 'sha256': digest(image)},
            'work_copy': {'file': str(copy), 'sha256': digest(copy)}}


def verify_copy(request):
    import bpy

    scene, targets, candidate, record, closure = candidate_context(request, require_saved_context=True)
    rendered = request['rendered']
    require(scene.uvw_candidate == candidate, 'VALIDATION_FAILED', 'Reopened UV panel resolves a different candidate')
    require(rendered['candidate_witness'] == witness(scene, targets, record, closure), 'VALIDATION_FAILED',
            'Reopened work copy differs in recorded candidate state or evaluated geometry/UV')
    image = bpy.data.images.load(rendered['preview']['file'], check_existing=False)
    observed = list(image.size)
    bpy.data.images.remove(image)
    require(observed == [request['size'], request['size']], 'VALIDATION_FAILED', 'Checker PNG failed independent size decode')
    return {'status': 'pass', 'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(),
            'pid': os.getpid(), 'candidate_and_evaluated_surface': 'pass', 'decoded_size': observed}


def worker(mode, params, result, job):
    from uv_workflow_addon.contract import WorkflowError

    try:
        request = read_json(params)
        data = render_copy(request, job) if mode == 'render' else verify_copy(request)
        response = {'data': data}
    except (Failure, WorkflowError, KeyError, ValueError, OSError, RuntimeError) as exc:
        response = {'error': {'code': getattr(exc, 'code', 'VALIDATION_FAILED'), 'message': str(exc)}}
    atomic_json(result, response)


def run(args):
    from uv_workflow import _guard_inputs

    value = read_json(args.params)
    require(isinstance(value, dict) and isinstance(value.get('manifest'), dict)
            and set(value['manifest']) == CONTEXT_FIELDS, 'INVALID_REQUEST',
            'Checker needs an inspect manifest with version, operation, scene, view_layer, candidate and complete objects')
    require(64 <= args.size <= 2048, 'INVALID_REQUEST', 'Checker size must be 64..2048 pixels')
    base = normalize_inspect_params(value)
    blender = args.blender.resolve(strict=True)
    deadline = time.monotonic() + timeout_value(args.timeout)
    jobs = args.jobs_dir.resolve()
    require(not jobs.exists() or jobs.is_dir(), 'CONFLICT', 'Checker jobs directory is an existing file')
    job = jobs / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    result_file = job / 'result.json'
    artifacts = {'job_directory': str(job), 'result': str(result_file)}

    def remaining():
        seconds = deadline - time.monotonic()
        require(seconds > 0, 'TIMEOUT', 'UV checker deadline expired')
        return seconds

    try:
        request = {'base': base, 'size': args.size}
        atomic_json(job / 'request.json', request)
        with ExitStack() as guards:
            source_guard = _guard_inputs(base, guards)
            data = run_saved_reader(blender, request, ENTRY, MARKER, remaining(), 'render', job)
            copy, preview = data['work_copy'], data['preview']
            with FileGuard(preview['file']) as png_guard:
                require(png_guard.sha256() == preview['sha256'], 'CONFLICT', 'Checker PNG changed before verification')
                verify_base = {**base, 'file': copy['file'], 'expected_sha256': copy['sha256']}
                verify = {'base': verify_base, 'size': args.size, 'rendered': data}
                cold = run_saved_reader(blender, verify, ENTRY, MARKER, remaining(), 'verify', job)
            require(cold['pid'] != data['producer_pid'], 'VALIDATION_FAILED', 'Checker verifier was not a separate worker')
            require(source_guard.sha256() == base['expected_sha256'], 'CONFLICT', 'Checker source changed')
        data.update(independent_reopen=cold, source={'file': base['file'], 'sha256': base['expected_sha256']},
                    source_unchanged=True, state_source='SAVED_DISK_V1', implementation_sha256=base['implementation_sha256'])
        artifacts.update(work_copy=copy['file'], preview=preview['file'])
        result = envelope(COMMAND, job.name, data, artifacts=artifacts)
    except (Failure, OSError, subprocess.TimeoutExpired) as exc:
        code = getattr(exc, 'code', 'TIMEOUT' if isinstance(exc, subprocess.TimeoutExpired) else 'IO_ERROR')
        result = envelope(COMMAND, job.name, error={'code': code, 'message': str(exc)}, artifacts=artifacts)
    atomic_json(result_file, result)
    code = result['error']['code'] if not result['ok'] else None
    state = {'TIMEOUT': 'timed_out', 'CANCELLED': 'cancelled'}.get(code, 'failed') if code else 'succeeded'
    atomic_json(job / 'status.json', {'job_id': job.name, 'state': state, 'heartbeat_at': time.time()})
    return result


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    args = None
    try:
        args = parser().parse_args()
        result = run(args)
    except (Failure, OSError) as exc:
        code = getattr(exc, 'code', 'NOT_FOUND' if isinstance(exc, FileNotFoundError) else 'IO_ERROR')
        result = envelope(COMMAND, error={'code': code, 'message': str(exc)})
    if args and args.human:
        print(result['artifacts']['result'] if result['ok'] else result['error']['message'])
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['ok'] else CODES.get(result['error']['code'], 5)


if __name__ == '__main__':
    if '--' in sys.argv and '--background' in sys.argv:
        marker, params, result, mode, job = sys.argv[sys.argv.index('--') + 1:]
        require(marker == MARKER and mode in ('render', 'verify'), 'INVALID_REQUEST', 'Invalid UV checker worker arguments')
        worker(mode, Path(params), Path(result), Path(job))
    else:
        sys.exit(main())
