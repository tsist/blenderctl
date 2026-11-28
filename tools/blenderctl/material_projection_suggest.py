"""Save panel projection suggestions or edit a low/high mesh pairing in a new Blender file."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
from host_compat import is_supported_host
import sys
import time
import uuid

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
sys.path[:0] = [str(ENTRY.parent), str(ENTRY.parent.parent)]

from filesystem import FileGuard, PathLocks
from material_draft_handoff import canonical, failure, guard_file, name, read_document
from material_projection_handoff import Parser, open_workfile, require, run_worker
from material_projection_pairs import apply_edit, edit_args, pair_state
from protocol import (CODES, DEFAULT_BLENDER, DEFAULT_JOBS, Failure, atomic_json,
                      digest, envelope, read_json, timeout_value)


def inspect(phase, request):
    import bpy
    from material_projection import _activate
    from material_workflow_addon import projection_ui

    version = tuple(bpy.app.version[:3])
    require(is_supported_host(version), 'UNSUPPORTED',
            'Workspace projection panel replay requires Blender 5.3.0 or later')
    projection_ui.register()
    open_workfile(bpy, request['file'] if phase == 'author' else request['stage'])
    scene, layer = _activate(request['context'])
    before = [pair_state(pair, scene) for pair in scene.mwp_projection_pairs]
    if phase == 'verify':
        return {'pairs': before, 'active_pair_index': scene.mwp_projection_index,
                'pid': os.getpid(), 'blender': bpy.app.version_string}
    if 'edit' in request:
        edit = request['edit']
        target, prior = apply_edit(bpy, scene, layer, edit, request, request['context'])
        after = [pair_state(pair, scene) for pair in scene.mwp_projection_pairs]
        if edit['operation'] == 'update':
            index = scene.mwp_projection_index
            current = after[index]
            require(before[index] == prior and len(after) == len(before) and
                    after[:index] == before[:index] and after[index + 1:] == before[index + 1:],
                    'VALIDATION_FAILED', 'An unrelated saved projection pair changed')
            change = {'active_pair_index': index}
        else:
            other = lambda rows: [row for row in rows if row['pair']['target'] != target]
            require(other(before) == other(after), 'VALIDATION_FAILED', 'An unrelated saved projection pair changed')
            current = next((row for row in after if row['pair']['target'] == target), None)
            change = {}
        change.update(operation=edit['operation'], target=target, before=prior, after=current)
    else:
        status = bpy.ops.material_workflow.projection_suggest()
        require(status == {'FINISHED'}, 'VALIDATION_FAILED', scene.mwp_projection_status or str(status))
        after = [pair_state(pair, scene) for pair in scene.mwp_projection_pairs]
        require(after[:len(before)] == before and all(not row['confirmed'] for row in after[len(before):]),
                'VALIDATION_FAILED', 'Existing projection decisions changed or a suggestion was confirmed implicitly')
        change = {'added': [row['pair']['target'] for row in after[len(before):]]}
    status = bpy.ops.wm.save_as_mainfile(filepath=request['stage'], copy=True, relative_remap=True)
    require(status == {'FINISHED'}, 'VALIDATION_FAILED', 'Could not save projection suggestions')
    return {'pairs': after, **change, 'pid': os.getpid()}


def worker(phase, request_file):
    try:
        result = {'ok': True, 'data': inspect(phase, read_json(request_file)['params'])}
    except Exception as error:
        result = {'ok': False, 'error': failure(error)}
    atomic_json(request_file.with_name(phase + '-result.json'), result)


def run(args):
    job = args.jobs_dir.resolve() / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    artifacts = {'job_directory': str(job), 'result': str(job / 'result.json')}
    stage = data = error = None
    try:
        deadline = time.monotonic() + timeout_value(args.timeout)
        def checkpoint():
            require(not (job / 'cancel.request').exists(), 'CANCELLED', 'Projection suggestion cancelled')
            require(time.monotonic() < deadline, 'TIMEOUT', 'Projection suggestion timed out')
        with ExitStack() as stack:
            source = guard_file(args.source, stack, args.expected_sha256)
            require(source.suffix.lower() == '.blend' and args.output.is_absolute()
                    and args.output.suffix.lower() == '.blend', 'INVALID_REQUEST', 'Provide absolute .blend paths')
            parent = canonical(args.output.parent)
            stack.enter_context(FileGuard(parent, directory=True))
            stack.enter_context(PathLocks([args.output]))
            require(not os.path.lexists(args.output), 'CONFLICT', 'Output already exists')
            context = {'scene': args.scene, 'view_layer': args.view_layer, 'frame': args.frame}
            require(name(args.scene) and name(args.view_layer) and 1 <= args.frame <= 100000,
                    'INVALID_REQUEST', 'Specify scene, view layer and frame in 1..100000')
            stage = parent / ('.projection-suggest-' + job.name + '.blend')
            request = {'file': str(source), 'expected_sha256': args.expected_sha256, 'context': context, 'stage': str(stage)}
            if args.action == 'projection-edit-pair':
                changes = None
                if args.changes is not None:
                    require(args.operation == 'update', 'INVALID_REQUEST', '--changes requires update')
                    changes = read_document(guard_file(args.changes.absolute(), stack))
                request['edit'] = edit_args(args, changes)
            atomic_json(job / 'request.json', {'params': request})
            blender = canonical(args.blender)
            def heartbeat(state, phase):
                atomic_json(job / 'status.json', {'job_id': job.name, 'state': state, 'phase': phase, 'heartbeat_at': time.time()})
            def execute(phase):
                heartbeat('running', phase)
                return run_worker(blender, job, phase, deadline, checkpoint, heartbeat, ENTRY)
            authored = execute('author')
            with FileGuard(stage) as reader:
                checked = execute('verify')
                require(authored['pairs'] == checked['pairs'], 'VALIDATION_FAILED', 'Stored pair list differs on independent reopen')
                if authored.get('operation') == 'update':
                    require(authored['active_pair_index'] == checked['active_pair_index'],
                            'VALIDATION_FAILED', 'Selected projection pair differs on independent reopen')
                sha = reader.sha256(checkpoint)
            with FileGuard(stage, renameable=True) as guard:
                require(guard.sha256(checkpoint) == sha, 'CONFLICT', 'Verified candidate changed before publication')
                if authored.get('operation') == 'confirm':
                    bake = job / 'bake-request.json'
                    atomic_json(bake, {'schema_version': '1.0', 'command': 'material.projection-bake',
                                       'params': {'file': str(args.output), 'expected_sha256': sha, 'context': context,
                                                  'pairs': [authored['after']['pair']]}})
                    artifacts['bake_request'] = {'file': str(bake), 'sha256': digest(bake)}
                checkpoint()
                guard.rename_new(args.output)
            data = {'source': {'file': str(source), 'expected_sha256': args.expected_sha256}, 'source_unchanged': True,
                    'candidate': {'file': str(args.output), 'sha256': sha}, 'context': context,
                    **{key: value for key, value in authored.items() if key not in ('pid', 'pairs')}, 'pairs': checked['pairs'],
                    'independent_reopen': {'pid': checked['pid'], 'blender': checked['blender']}}
    except (Failure, ValueError, OSError, RuntimeError) as exc:
        error = failure(exc)
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)
    result = envelope('material.' + args.action, job.name, data, error, artifacts)
    atomic_json(job / 'result.json', result)
    state = {'TIMEOUT': 'timed_out', 'CANCELLED': 'cancelled'}.get(error['code'], 'failed') if error else 'succeeded'
    atomic_json(job / 'status.json', {'job_id': job.name, 'state': state, 'phase': 'done'})
    return result


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    cli = Parser(description=__doc__)
    cli.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    cli.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS)
    cli.add_argument('--transactions-dir')
    cli.add_argument('--timeout', type=float, default=120)
    mode = cli.add_mutually_exclusive_group()
    for flag in ('--human', '--compact', '--full'):
        mode.add_argument(flag, action='store_true')
    actions = cli.add_subparsers(dest='action', required=True)
    suggest = actions.add_parser('projection-suggest', description='Suggest high/low pairs by name')
    edit = actions.add_parser('projection-edit-pair', description='Edit one saved high/low mesh pairing. Confirm emits a request ready for projection baking.')
    for action in (suggest, edit):
        action.add_argument('source', type=Path)
        action.add_argument('--expected-sha256', required=True)
        action.add_argument('--scene', required=True)
        action.add_argument('--view-layer', required=True)
        action.add_argument('--frame', type=int, required=True)
        action.add_argument('--output', type=Path, required=True, help='New absolute .blend path')
    edit.add_argument('--operation', required=True,
                      choices=('add', 'remove', 'add-source', 'remove-source', 'confirm', 'unconfirm', 'update'))
    edit.add_argument('--target', help='Current low mesh name; add without object names uses the saved selection')
    edit.add_argument('--high', action='append', help='High mesh name; repeat for add, or omit for an empty source row with add-source')
    edit.add_argument('--source-index', type=int, help='Zero-based source row for remove-source; --high can guard the expected name')
    edit.add_argument('--pair-index', type=int, help='Update only: zero-based pair row, guarded by --target or an empty target')
    edit.add_argument('--changes', type=Path,
                      help='Update only: JSON with target, uv_layer, cage, sources [{index, expected, object}], and/or settings')
    try:
        args = cli.parse_args()
        result = run(args)
        human = args.human
    except (Failure, OSError) as exc:
        command = next((key for key in sys.argv[1:] if key in ('projection-suggest', 'projection-edit-pair')), 'projection-suggest')
        result = envelope('material.' + command, error=failure(exc))
        human = False
    if human:
        print(result['data']['candidate']['file'] if result['ok'] else result['error']['message'])
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else CODES[result['error']['code']]


if __name__ == '__main__':
    if '--background' in sys.argv and '--' in sys.argv:
        phase, request = sys.argv[sys.argv.index('--') + 1:]
        worker(phase, Path(request))
    else:
        sys.exit(main())
