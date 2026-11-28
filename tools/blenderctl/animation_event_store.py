"""Review, inspect and apply saved two-character or character/light timing events."""
from argparse import Namespace
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import time
import uuid

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
sys.path.insert(0, str(ENTRY.parent))

from host_compat import is_supported_host
from filesystem import FileGuard, PathLocks
from material_draft_handoff import (Parser, canonical, failure, fingerprint, guard_file, name,
                                   open_saved_context, read_document, require)
from material_projection_handoff import run_worker
from protocol import CODES, DEFAULT_BLENDER, DEFAULT_JOBS, Failure, atomic_json, digest, envelope, read_json, timeout_value

COMMAND = 'animation.event-store'
WIN53_VARIANTS = {
    'paired': 'animation-0.4.4-paired-event-win53-v1',
    'cross': 'animation-0.4.4-cross-event-win53-v1',
}
OPERATIONS = ('inspect', 'capture-plan', 'capture-apply', 'shift-plan', 'shift-apply',
              'cue-plan', 'cue-apply', 'cue-inspect')


def prepare(args, guards):
    data = read_document(guard_file(args.params.absolute(), guards))
    base = {'file', 'expected_sha256', 'context', 'store', 'operation'}
    require(isinstance(data, dict) and base <= data.keys() and data['operation'] in OPERATIONS
            and data['store'] in ('paired', 'cross'), 'INVALID_REQUEST', 'Specify the event store and operation')
    operation = data['operation']
    if operation.startswith('cue-'):
        require(data['store'] == 'cross', 'INVALID_REQUEST', 'A cue binds the saved cross-domain event')
    inputs = {'parameters'} if operation.endswith('-plan') else {'plan'} if operation.endswith('-apply') else set()
    require(data.keys() == base | inputs, 'INVALID_REQUEST', f'Unexpected or missing fields for {operation}')
    context = data['context']
    require(isinstance(context, dict) and context.keys() == {'scene', 'view_layer', 'frame'}
            and name(context['scene']) and name(context['view_layer']) and type(context['frame']) is int,
            'INVALID_REQUEST', 'Specify saved scene, view layer and integer frame')
    require(isinstance(data['file'], str) and isinstance(data['expected_sha256'], str), 'INVALID_REQUEST',
            'Expected a SHA-bound saved Blender file')
    source = guard_file(data['file'], guards, data['expected_sha256'])
    require(source.suffix.lower() == '.blend', 'INVALID_REQUEST', 'Expected a .blend source')
    data['file'] = str(source)
    if operation.endswith('-apply'):
        ref = data.pop('plan')
        require(isinstance(ref, dict) and ref.keys() == {'file', 'expected_sha256'}
                and isinstance(ref['file'], str) and isinstance(ref['expected_sha256'], str),
                'INVALID_REQUEST', 'Supply the reviewed plan file and its SHA-256')
        plan = read_document(guard_file(ref['file'], guards, ref['expected_sha256']))
        require(isinstance(plan, dict) and plan.get('schema') == 'animation.event-plan.v1'
                and plan.get('store') == data['store'] and plan.get('edit') == operation.split('-')[0]
                and plan.get('source') == {'file': data['file'], 'sha256': data['expected_sha256']}
                and plan.get('context') == context, 'CONFLICT', 'Plan belongs to a different edit, source or context')
        runtime = plan.get('runtime')
        require(isinstance(runtime, dict) and runtime.get('edition') == args.edition, 'CONFLICT',
                f'Plan was reviewed for another Animation Curves Workflow edition; selected {args.edition}')
        data['parameters'] = plan.get('parameters')
        data['reviewed_plan'] = plan
    if 'parameters' in data:
        params = data['parameters']
        if operation.startswith('cue-'):
            require(isinstance(params, dict) and params.get('policy') in ('exact','explicit_integer'),
                    'INVALID_REQUEST', 'Select exact or explicit_integer beat timing')
            fields = {'cue_id', 'bpm', 'fps', 'start_frame', 'beat', 'policy'}
            if params['policy'] == 'explicit_integer':
                fields |= {'chosen_frame', 'tolerance_frames'}
        else:
            fields = {'delta'} if operation.startswith('shift') else (
                {'event', 'rig_a', 'rig_b', 'frame_a', 'frame_b'} if data['store'] == 'paired' else {'light_object', 'light_frame'})
        require(isinstance(params, dict) and params.keys() == fields, 'INVALID_REQUEST',
                'Expected exactly these parameters: ' + ', '.join(sorted(fields)))
    data['edition'] = args.edition
    return data


def participants(rows):
    return [{'object': row.get('rig') or row['object'], 'action': row['action'], 'slot': row['slot'],
             'path': row.get('path', 'pose.bones["head"].rotation_euler'), 'index': row.get('index', 1),
             'frame': row['frame']} for row in rows]


def planned_keys(rows, delta):
    result = participants(rows)
    for item, row in zip(result, rows):
        curves = row['witness']['layers'][0]['strips'][0]['bags'][0][1]
        curve = next(c for c in curves if (c['data_path'], c['array_index']) == (item['path'], item['index']))
        frames = [key['co'][0] for key in curve['keys']]
        index = frames.index(item['frame'])
        item.update(target_frame=item['frame'] + delta, previous_locked=frames[index - 1], next_locked=frames[index + 1])
    return result


def describe(record):
    return {'id': record['id'], 'revision': record['revision'], 'event': record['event'],
            'offsets': record.get('offsets', [record.get('offset')]), 'participants': participants(record['rows']),
            'record_sha256': fingerprint(record)}


def inspect_record(store, context):
    record, _ = store.inspect(context)
    return describe(record)


def dependencies(bpy):
    paths = {os.path.normcase(os.path.abspath(path))
             for path in bpy.utils.blend_paths(absolute=True, packed=False)}
    if is_supported_host(bpy.app.version):
        from toon_panel_handoff import workspace_brush_paths

        # Factory workspace weak references can be discarded when saving a copy.
        # The shared classifier leaves actual linked libraries in the closure.
        paths.difference_update(workspace_brush_paths(bpy, paths))
    return sorted(paths)


def work(phase, data):
    import bpy

    edition = data.get('edition', '0.5.0')
    from animation_author import load_author
    load_author(bpy, edition)
    from animation_curves_workflow_addon import cross_event_store, cue_ui, pose_event_store, pose_gate, ui
    from animation_pose_handoff import observed_pose_runtime, pose_runtime_identity

    environment = {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(), 'edition': edition}
    compatibility = is_supported_host(bpy.app.version)
    if compatibility:
        environment.update(pose_runtime_identity(bpy, pose_gate, WIN53_VARIANTS[data['store']]))
    ui.register()
    cue_ui.register()
    with observed_pose_runtime(pose_gate, compatibility):
        return work_event(phase, data, bpy, environment, pose_event_store, cross_event_store, cue_ui)


def work_event(phase, data, bpy, environment, paired, cross, cue_ui):
    file = data['stage'] if phase == 'verify' else data['file']
    context = open_saved_context(bpy, {**data, 'file': file})
    store = paired if data['store'] == 'paired' else cross
    scene = context.scene
    timing = {'fps': scene.render.fps, 'fps_base': scene.render.fps_base,
              'frame_start': scene.frame_start, 'frame_end': scene.frame_end}
    state = {'runtime': environment, 'worker_pid': os.getpid(), 'store': data['store'], 'timing': timing}
    operation = data['operation']
    if operation.startswith('cue-'):
        from animation_event_cue import work_cue
        return work_cue(phase, bpy, context, cross, cue_ui, data, state)
    if operation == 'inspect' or phase == 'verify':
        return {**state, 'record': inspect_record(store, context), 'dependencies': dependencies(bpy)}
    editing = operation.split('-')[0]
    params = data['parameters']
    if store is paired:
        cross.require_empty_cue(context)
        require(not cross.raw(context), 'CONFLICT',
                'This paired record feeds a saved cross-domain event. Continue via the cross event on this copy.')
    if editing == 'shift':
        native = store.sync_origin(context, params['delta'])
        current = native['record']
        rows = current['rows']
    else:
        native = (store.capture_origin(context, params) if store is paired else
                  store.capture_origin(context, params['light_object'], params['light_frame']))
        current = store.parse(native['raw'])
        rows = (native if store is paired else native['snapshot'])['rows']
    # The GUI origin contains RNA pointers scoped to one Blender process. Bind the
    # portable review to the exact saved input, participants, old record and edit,
    # then let the native author revalidate its complete origin at apply time.
    recipe = {'schema': 'animation.event-plan.v1', 'runtime': environment,
              'source': {'file': data['file'], 'sha256': data['expected_sha256']}, 'context': data['context'],
              'store': data['store'], 'edit': editing, 'parameters': params, 'timing': timing,
              'participants': planned_keys(rows, params.get('delta', 0)),
              'previous': describe(current) if current else None}
    if operation.endswith('-plan'):
        return {**state, 'plan': recipe}
    require(recipe == data['reviewed_plan'], 'CONFLICT', 'Native event review differs from the saved plan')
    deps = dependencies(bpy)
    if editing == 'shift':
        store.execute(context, native)
    else:
        store.capture(context, native)
    record = inspect_record(store, context)
    require(bpy.ops.wm.save_as_mainfile(filepath=data['stage'], copy=True, relative_remap=True,
                                        check_existing=False) == {'FINISHED'}, 'IO_ERROR', 'Could not save animation candidate')
    return {**state, 'record': record, 'dependencies': deps}


def worker(phase, path):
    try:
        data = read_json(path)
        if data.get('command') == 'fk-character':
            from animation_fk_character import work_character
            result = work_character(phase, data, path.parent)
        else:
            result = work(phase, data)
        response = {'ok': True, 'data': result}
    except Exception as exc:
        response = {'ok': False, 'error': failure(exc)}
    atomic_json(path.parent / (phase + '-result.json'), response)


def run(args):
    character = args.command == 'fk-character'
    command = 'animation.fk-character' if character else COMMAND
    deadline = time.monotonic() + timeout_value(args.timeout)
    job = args.jobs_dir.resolve() / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    artifacts = {'job_directory': str(job), 'result': str(job / 'result.json')}
    result = error = None

    def checkpoint():
        require(time.monotonic() < deadline, 'TIMEOUT', 'Animation event worker timed out')
        require(not (job / 'cancel.request').exists(), 'CANCELLED', 'Animation event job cancelled')

    def heartbeat(status, phase):
        atomic_json(job / 'status.json', {'job_id': job.name, 'state': status, 'phase': phase, 'heartbeat_at': time.time()})

    try:
        with ExitStack() as guards:
            if character:
                from animation_fk_character import prepare_character
                data = prepare_character(args, guards)
                applying = data['operation'] in ('create', 'practice')
            else:
                data = prepare(args, guards)
                applying = data['operation'].endswith('-apply')
            if applying:
                require(args.output and args.output.is_absolute() and args.output.suffix.lower() == '.blend',
                        'INVALID_REQUEST', 'Apply requires a new absolute .blend output')
                parent = canonical(args.output.parent)
                guards.enter_context(FileGuard(parent, directory=True))
                guards.enter_context(PathLocks([args.output]))
                require(not os.path.lexists(args.output), 'CONFLICT', 'Output already exists')
                stage = parent / ('.event-' + job.name + '.blend')
                guards.callback(stage.unlink, missing_ok=True)
                data['stage'] = str(stage)
            else:
                require(args.output is None, 'INVALID_REQUEST', 'A read-only operation takes no --output')
            atomic_json(job / 'request.json', data)
            blender = canonical(args.blender)

            def execute(phase):
                heartbeat('running', phase)
                return run_worker(blender, job, phase, deadline, checkpoint, heartbeat, ENTRY)

            result = execute('author')
            if 'plan' in result:
                plan_path = job / 'plan.json'
                atomic_json(plan_path, result['plan'])
                artifacts['plan'] = {'file': str(plan_path), 'sha256': digest(plan_path)}
            if applying:
                with FileGuard(stage) as reading:
                    cold = execute('verify')
                    require(cold['worker_pid'] != result['worker_pid'] and
                            all(result[key] == cold[key] for key in ('runtime', 'record', 'timing', 'dependencies')),
                            'VALIDATION_FAILED', 'Saved event, runtime or dependency paths differ on cold reopen')
                    sha = reading.sha256(checkpoint)
                with FileGuard(stage, renameable=True) as file:
                    require(file.sha256(checkpoint) == sha, 'CONFLICT', 'Candidate changed after verification')
                    checkpoint()
                    file.rename_new(args.output)
                result['candidate'] = {'file': str(args.output), 'sha256': sha}
                result['independent_reopen'] = {'pid': cold['worker_pid'], **cold['runtime']}
            result['source'] = {'file': data['file'], 'sha256': data['expected_sha256']}
    except (OSError, ValueError, RuntimeError, Failure) as exc:
        result, error = None, failure(exc)
    receipt = envelope(command, job.name, result, error, artifacts)
    atomic_json(job / 'result.json', receipt)
    heartbeat({'CANCELLED': 'cancelled', 'TIMEOUT': 'timed_out'}.get(error['code'], 'failed') if error else 'succeeded', 'done')
    return receipt


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    cli = Parser(description=__doc__)
    cli.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    cli.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS)
    cli.add_argument('--transactions-dir')
    cli.add_argument('--timeout', type=float, default=120)
    display = cli.add_mutually_exclusive_group()
    for flag in ('--human', '--compact', '--full'):
        display.add_argument(flag, action='store_true')
    commands = cli.add_subparsers(dest='command', required=True)
    parser = commands.add_parser('event-store', description=__doc__,
        epilog='store: paired or cross. operation: inspect, capture-plan, capture-apply, shift-plan, shift-apply. '
               'On a cross event use cue-plan, cue-apply or cue-inspect to bind a beat to three actions. '
               'Cue parameters: cue_id, bpm, fps, start_frame, beat, policy exact or explicit_integer '
               '(chosen_frame and tolerance_frames). '
               'Edition 0.4.4 also supports qualified Windows Blender 5.3.0. Plans bind the runtime, '
               'parameters and participants to the saved source SHA. Apply takes plan.file and plan.expected_sha256.')
    parser.add_argument('--edition', choices=('0.5.0',), default='0.5.0',
                        help='Animation Curves Workflow release (default: 0.4.3)')
    parser.add_argument('--params', type=Path, required=True, help='JSON: file, expected_sha256, context, store, operation and parameters or plan')
    parser.add_argument('--output', type=Path, help='New absolute .blend for apply')
    author = commands.add_parser('fk-character', description='Create or inspect the native FK teaching character or add its practice action.',
        epilog='Params: file, expected_sha256, context{scene,view_layer,frame}, operation=create|practice|inspect|knees. '
               'create takes kind (cylinder_v1 or humanoid_fk_two_segment_v2) and height_m. Other operations require rig and '
               'instance_uuid. practice takes frames [start,peak,end], knee_start and knee_peak in degrees. '
               'Use edition 0.4.4 for create, inspect, knees and practice on the project Windows Blender 5.3.0 build.')
    author.add_argument('--edition', choices=('0.5.0',), default='0.5.0',
                        help='Animation Curves Workflow release (default: 0.4.3)')
    author.add_argument('--params', type=Path, required=True, help='UTF-8 JSON on disk')
    author.add_argument('--output', type=Path, help='New absolute .blend for create or practice')
    options = Namespace()
    try:
        args = cli.parse_args(namespace=options)
        result = run(args)
    except (Failure, OSError) as exc:
        command = getattr(options, 'command', None)
        result = envelope('animation.' + command if command else 'animation', error=failure(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['ok'] else CODES[result['error']['code']]


if __name__ == '__main__':
    if '--background' in sys.argv and '--' in sys.argv:
        phase, path = sys.argv[sys.argv.index('--') + 1:]
        worker(phase, Path(path))
    else:
        sys.exit(main())
