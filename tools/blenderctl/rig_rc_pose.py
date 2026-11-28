"""Match or record a saved C94 pose with the frozen Rigging Workflow 0.42.3 RC."""
import argparse
from contextlib import ExitStack
import importlib
import math
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
sys.path.insert(0, str(ENTRY.parent))

from host_compat import is_supported_host
from filesystem import FileGuard, PathLocks
from material_draft_handoff import Parser, canonical, fingerprint, guard_file, read_document, require
from material_projection_handoff import error_payload, run_worker
from protocol import CODES, DEFAULT_BLENDER, DEFAULT_JOBS, SCHEMA, Failure, atomic_json, digest, envelope, read_json, timeout_value


PRODUCT = ENTRY.parents[2] / 'runtime/rigging-workflow/0.42.3-rc/rigging_workflow'
MANIFEST_SHA = '31df5dbb466a2783d90a4c8b3ea3f6bdc39a08133a305b8b07de6aa2f76627c3'
COMMAND = 'rigging-workflow.rc-pose'
MAX_DISTANCE_METERS = .001
OPERATIONS = {'inspect': set(), 'switch': {'limb', 'side', 'target'}, 'record-modes': set(),
              'record-limb': {'limb', 'side'}, 'switch-and-key': {'limb', 'side', 'target'}}
MODES = {(limb, side): prefix + chinese_limb + 'FK-IK'
         for limb, chinese_limb in (('ARM', '臂'), ('LEG', '腿'))
         for side, prefix in (('L', '左'), ('R', '右'))}
LANDMARKS = {(limb, side): name + '.' + side for limb, name in (('ARM', '手掌'), ('LEG', '脚掌')) for side in ('L', 'R')}


def parser():
    cli = Parser(description=__doc__)
    cli.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    cli.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS)
    cli.add_argument('--transactions-dir', help=argparse.SUPPRESS)
    cli.add_argument('--timeout', type=float, default=180)
    mode = cli.add_mutually_exclusive_group()
    for flag in ('--compact', '--full', '--human'):
        mode.add_argument(flag, action='store_true')
    actions = cli.add_subparsers(dest='command', required=True, parser_class=Parser)
    command = actions.add_parser('rc-pose', description=__doc__,
        epilog='Requires Blender 5.3.0 or later and a generated C94 rig. Params use file, '
               'expected_sha256, scene, view_layer, rig, frame and operation. Operations: inspect, switch, '
               'record-modes, record-limb, switch-and-key. Provide limb ARM/LEG and side L/R for a limb, '
               'and target FK/IK for a switch. A keyed switch requires prior source keys for that limb.')
    command.add_argument('--params', type=Path, required=True, help='UTF-8 JSON describing the saved scene and the pose operation')
    command.add_argument('--output', type=Path, help='New absolute .blend for a mutation. Omit for inspect.')
    return cli


def read_request(args, guards, *, allow_bone_measurement=False):
    request = read_document(guard_file(args.params.absolute(), guards))
    common = {'file', 'expected_sha256', 'scene', 'view_layer', 'rig', 'frame', 'operation'}
    require(isinstance(request, dict) and common <= set(request), 'INVALID_REQUEST', 'Missing saved rig pose fields')
    operation = request['operation']
    extra = {'measurement'} if allow_bone_measurement and operation == 'switch' and 'measurement' in request else set()
    require(isinstance(operation, str) and operation in OPERATIONS
            and set(request) == common | OPERATIONS[operation] | extra,
            'INVALID_REQUEST', 'Unexpected operation or fields. See rc-pose --help.')
    require(not extra or request['measurement'] == 'bone-matrices', 'INVALID_REQUEST',
            'The switch measurement must be bone-matrices')
    require(all(isinstance(request[name], str) and request[name] and '\0' not in request[name] for name in
                ('file', 'expected_sha256', 'scene', 'view_layer', 'rig')), 'INVALID_REQUEST', 'Expected named saved rig, scene and view layer')
    frame = request['frame']
    require(type(frame) in (int, float) and abs(frame) <= 1048574, 'INVALID_REQUEST', 'Expected a finite Blender frame')
    if 'limb' in request:
        require(request['limb'] in ('ARM', 'LEG') and request['side'] in ('L', 'R'), 'INVALID_REQUEST', 'Expected ARM/LEG and L/R')
    if 'target' in request:
        require(request['target'] in ('FK', 'IK'), 'INVALID_REQUEST', 'Expected FK or IK')
    source = guard_file(request['file'], guards, request['expected_sha256'])
    require(source.suffix.lower() == '.blend', 'INVALID_REQUEST', 'Expected a saved .blend file')
    require((args.output is None) == (operation == 'inspect'), 'INVALID_REQUEST', 'Use --output for a mutation and omit it for inspect')
    request['file'] = str(source)
    return request


def guard_product(guards):
    manifest = guard_file(PRODUCT / 'exchange-runtime-manifest.json', guards, MANIFEST_SHA)
    members = read_json(manifest)['files']
    modules = {name: expected for name, expected in members.items() if name.endswith('.py')}
    for name, expected in modules.items():
        path = canonical(PRODUCT / name)
        require(path.is_relative_to(PRODUCT), 'CONFLICT', 'RC module escapes the frozen tree')
        module = guards.enter_context(FileGuard(path))
        require(module.sha256() == expected, 'CONFLICT', f'RC source SHA differs: {name}')
    return {'edition': '0.42.3-rc1', 'manifest_sha256': MANIFEST_SHA, 'directory': str(PRODUCT),
            'guarded_modules': len(modules), 'pose_switching_sha256': members['pose_switching.py'],
            'mode_animation_sha256': members['mode_animation.py']}


def open_rig(bpy, path, request, pose):
    require(bpy.ops.wm.open_mainfile(filepath=path, load_ui=True, use_scripts=False) == {'FINISHED'}
            and Path(bpy.data.filepath).samefile(path), 'VALIDATION_FAILED', 'Could not reopen saved .blend')
    require(tuple(bpy.data.version[:2]) == (5, 2), 'UNSUPPORTED', 'Saved file must match Blender 5.2')
    context = bpy.context
    require(context.scene.name == request['scene'] and context.view_layer.name == request['view_layer'],
            'CONFLICT', 'Saved active scene or view layer differs')
    actual_frame = context.scene.frame_current + context.scene.frame_subframe
    require(math.isclose(actual_frame, request['frame'], rel_tol=0, abs_tol=1e-7), 'CONFLICT', 'Saved frame differs')
    rig = context.view_layer.objects.get(request['rig'])
    require(rig is not None and rig.type == 'ARMATURE', 'NOT_FOUND', 'Named armature absent from the saved view layer')
    require(not (rig.library or rig.override_library or rig.data.library or rig.data.override_library or rig.mode == 'EDIT'),
            'CONFLICT', 'Named rig must be local and outside Edit Mode')
    pose._metadata(rig)
    return rig


def binding(rig, frame):
    from inspection import record
    animation = rig.animation_data
    action = animation.action if animation else None
    slot = animation.action_slot if animation else None
    if action is None:
        return None
    rows = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in getattr(strip, 'channelbags', ()):
                if slot is None or bag.slot_handle != slot.handle:
                    continue
                for curve in bag.fcurves:
                    for key in curve.keyframe_points:
                        if math.isclose(frame, key.co.x, rel_tol=0, abs_tol=1e-6):
                            rows.append({'path': curve.data_path, 'index': curve.array_index,
                                         'value': key.co.y, 'interpolation': key.interpolation})
    return {'action': action.name, 'slot': slot.identifier if slot else None,
            'action_sha256': fingerprint(record(action)['action']),
            'current_frame_keys': sorted(rows, key=lambda row: (row['path'], row['index']))}


def pose_observation(bpy, rig, frame):
    bpy.context.view_layer.update()
    evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
    controls = evaluated.pose.bones.get('骨架设置')
    modes = {f'{limb}.{side}': controls[key] for (limb, side), key in MODES.items() if controls and key in controls}
    positions = {}
    for (limb, side), name in LANDMARKS.items():
        bone = evaluated.pose.bones.get(name)
        if bone is not None:
            positions[f'{limb}.{side}'] = {'bone': name, 'world_head': list(evaluated.matrix_world @ bone.head),
                                           'world_tail': list(evaluated.matrix_world @ bone.tail)}
    return {'modes': modes, 'landmarks': positions, 'binding': binding(rig, frame)}


def protected_state(bpy, rig, original, replacement):
    from inspection import path_value
    import scenes

    state = scenes.state()
    kept = []
    for block in state['datablocks']:
        if block['type'] == 'Action' and block['name'] == replacement and replacement != original:
            continue
        if block['type'] == 'Action' and block['name'] == original and original != replacement:
            block.pop('fake_user', None)  # Native transition retains the old Action via Fake User.
        if block['type'] == 'Object' and block['name'] == rig.name:
            for bone in block.get('pose', ()):
                bone.pop('matrix_basis', None)
                for field in ('matrix', 'matrix_basis', 'location', 'rotation_euler', 'rotation_quaternion',
                              'rotation_axis_angle', 'scale', 'rotation_mode'):
                    bone['settings'].pop(field, None)
                for constraint in bone['constraints']:
                    constraint.pop('influence', None)  # Evaluated driver output can follow FK/IK mode.
            animation = block.get('animation', {'drivers': [], 'nla': []})
            block['animation'] = {key: animation[key] for key in ('drivers', 'nla')}
        kept.append(block)
    state['datablocks'] = kept
    for item in state['objects']:
        item.pop('matrix_world', None)  # Bone-mounted objects legitimately follow the evaluated pose.
    state['external_paths'] = sorted({path_value(path) for path in bpy.utils.blend_paths(absolute=True, packed=False)})
    return state


def untouched_controls(rig, pose, side):
    if side is None:
        return {}
    opposite = '.R' if side == 'L' else '.L'
    return {bone.name: pose._channels(bone) for bone in rig.pose.bones if bone.name.endswith(opposite)}


def invoke_core(rig, request, pose, animation):
    operation = request['operation']
    limb = {key: request[key] for key in ('limb', 'side', 'target') if key in request}
    if operation == 'switch':
        return pose.switch(rig, **limb)
    if operation == 'record-modes':
        return animation.record_modes(rig)
    if operation == 'record-limb':
        return animation.record_limb(rig, **limb)
    return animation.switch_and_key(rig, **limb)


def verify_effect(bpy, request, before, after, result):
    switch = 'target' in request
    name = f"{request.get('limb')}.{request.get('side')}"
    wanted = dict(before['modes'])
    if switch:
        require(name in wanted and name in before['landmarks'], 'UNSUPPORTED', 'Named limb mode or world landmark is unavailable')
        wanted[name] = 1. if request['target'] == 'IK' else 0.
    require(after['modes'] == wanted, 'VALIDATION_FAILED', 'Saved mode or an untargeted mode differs')
    meters = bpy.context.scene.unit_settings.scale_length
    drift = {key: {point: math.dist(before['landmarks'][key][point], value[point]) * meters
                   for point in ('world_head', 'world_tail')} for key, value in after['landmarks'].items()}
    require(after['landmarks'].keys() == before['landmarks'].keys()
            and all(distance <= MAX_DISTANCE_METERS for points in drift.values() for distance in points.values()),
            'VALIDATION_FAILED', 'A measured wrist or ankle jumps beyond the native 1 mm guard')
    if request['operation'] == 'switch':
        require(after['binding'] == before['binding'], 'VALIDATION_FAILED', 'An unkeyed switch changed the Action')
    else:
        actual = after['binding']
        require(actual and actual['action'] == result['action'] and actual['slot'] == result['slot']
                and len(actual['current_frame_keys']) >= result['keyed_channels'] > 0,
                'VALIDATION_FAILED', 'Named Action or persistent frame keys differ from the native report')
    return drift


def work(phase, request_path):
    import bpy
    import scenes

    request = read_json(request_path)['params']
    require(bpy.app.background and not bpy.app.online_access and is_supported_host(bpy.app.version),
            'UNSUPPORTED', 'Rigging RC pose requires offline Blender 5.3.0 or later')
    bpy.context.preferences.filepaths.use_scripts_auto_execute = False
    sys.path.insert(0, str(PRODUCT.parent))
    product = importlib.import_module('rigging_workflow')
    require(Path(product.__file__).parent == PRODUCT and product.bl_info['version'] == (0, 42, 3),
            'CONFLICT', 'Loaded product is not the frozen C94 0.42.3 RC')
    from rigging_workflow import mode_animation, pose_switching

    runtime = {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(), 'pid': os.getpid()}
    path = request['stage'] if phase == 'verify' else request['file']
    rig = open_rig(bpy, path, request, pose_switching)
    observation = pose_observation(bpy, rig, request['frame'])
    if phase == 'inspect':
        return {'runtime': runtime, 'pose': observation}
    if phase == 'verify':
        authored = read_json(request_path.parent / 'author-result.json')['data']
        protected = protected_state(bpy, rig, *authored['action_transition'])
        return {'runtime': runtime, 'pose': observation, 'protected_sha256': fingerprint(protected), 'sha256': digest(Path(path))}

    old_action = observation['binding']['action'] if observation['binding'] else None
    replacing = request['operation'] != 'switch'
    before = protected_state(bpy, rig, old_action, None if replacing else old_action)
    other_side = untouched_controls(rig, pose_switching, request.get('side'))
    try:
        result = invoke_core(rig, request, pose_switching, mode_animation)
    except ValueError as error:
        raise Failure('CONFLICT', str(error)) from error
    require(result['ok'], 'VALIDATION_FAILED', 'Native pose operation did not succeed')
    after_observation = pose_observation(bpy, rig, request['frame'])
    distances = verify_effect(bpy, request, observation, after_observation, result)
    require(untouched_controls(rig, pose_switching, request.get('side')) == other_side,
            'VALIDATION_FAILED', 'Unselected side pose channels changed')
    new_action = after_observation['binding']['action'] if after_observation['binding'] else None
    if replacing and old_action is not None:
        require(new_action != old_action and bpy.data.actions[old_action].use_fake_user, 'VALIDATION_FAILED', 'Prior Action not retained')
    protected = protected_state(bpy, rig, old_action, new_action)
    mismatch = scenes.compare(before, protected)
    require(mismatch is None, 'VALIDATION_FAILED', f'Protected content changed at {mismatch}')
    bpy.context.preferences.filepaths.save_version = 0
    stage = Path(request['stage'])
    require(bpy.ops.wm.save_as_mainfile(filepath=str(stage), copy=True, relative_remap=True, check_existing=False) == {'FINISHED'},
            'IO_ERROR', 'Could not save the pose candidate')
    return {'runtime': runtime, 'before': observation, 'pose': after_observation, 'native_result': result,
            'action_transition': [old_action, new_action], 'world_displacement_meters': distances,
            'protected_sha256': fingerprint(protected), 'sha256': digest(stage)}


def worker(phase, request_path):
    try:
        result = {'ok': True, 'data': work(phase, request_path)}
    except Exception as error:
        result = {'ok': False, 'error': error_payload(error, 'VALIDATION_FAILED')}
    atomic_json(request_path.parent / (phase + '-result.json'), result)


def run(args):
    deadline = time.monotonic() + timeout_value(args.timeout)
    job = args.jobs_dir.resolve() / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    artifacts = {'job_directory': str(job), 'result': str(job / 'result.json')}
    data = error = None

    def checkpoint():
        if (job / 'cancel.request').exists():
            raise Failure('CANCELLED', 'Pose operation cancelled before publication')
        if time.monotonic() >= deadline:
            raise Failure('TIMEOUT', 'Pose operation exceeded its timeout')

    def status(state, phase):
        atomic_json(job / 'status.json', {'state': state, 'job_id': job.name, 'pid': os.getpid(),
                    'phase': phase, 'heartbeat_at': time.time()})

    status('running', 'verify_inputs')
    try:
        with ExitStack() as guards:
            request = read_request(args, guards)
            product = guard_product(guards)
            blender = canonical(args.blender)
            output = None
            if args.output is not None:
                require(args.output.is_absolute() and args.output.suffix.lower() == '.blend',
                        'INVALID_REQUEST', 'Mutation output requires a new absolute .blend path')
                parent = canonical(args.output.parent)
                output = parent / args.output.name
                for directory in (parent, *parent.parents):
                    if directory != Path(directory.anchor):
                        guards.enter_context(FileGuard(directory, directory=True))
                guards.enter_context(PathLocks([output]))
                require(not os.path.lexists(output), 'CONFLICT', f'Output already exists: {output}')
                with tempfile.NamedTemporaryFile(prefix='.rig-rc-pose-', suffix='.blend', dir=parent, delete=False) as file:
                    stage = Path(file.name)
                guards.callback(stage.unlink, missing_ok=True)
                request['stage'] = str(stage)
            atomic_json(job / 'request.json', {'schema_version': SCHEMA, 'command': COMMAND, 'params': request})
            phase = 'author' if output else 'inspect'
            status('running', phase)
            authored = run_worker(blender, job, phase, deadline, checkpoint, status, entry=ENTRY)
            data = {**authored, 'operation': request['operation'], 'rig': request['rig'], 'scene': request['scene'],
                    'view_layer': request['view_layer'], 'frame': request['frame'], 'product': product,
                    'source': {key: request[key] for key in ('file', 'expected_sha256')}, 'source_unchanged': True}
            if output:
                status('running', 'verify')
                verified = run_worker(blender, job, 'verify', deadline, checkpoint, status, entry=ENTRY)
                require(all(verified[key] == authored[key] for key in ('pose', 'protected_sha256', 'sha256'))
                        and verified['runtime']['pid'] != authored['runtime']['pid'], 'VALIDATION_FAILED',
                        'Independent reopen differs from the authored pose, persistent keys, or protected state')
                with FileGuard(stage, renameable=True) as locked:
                    require(locked.sha256(checkpoint) == verified['sha256'], 'CONFLICT', 'Staged .blend changed after verification')
                    checkpoint()
                    locked.rename_new(output)
                data['candidate'] = {'file': str(output), 'sha256': verified['sha256']}
                data['independent_reopen'] = {'process': 'fresh_process', **verified['runtime']}
                artifacts.update(candidate=str(output), independent_reopen=str(job / 'verify-result.json'))
    except (Failure, OSError, RuntimeError, ValueError) as exc:
        data, error = None, error_payload(exc, 'IO_ERROR')
    for file in sorted(job.glob('*.log')):
        artifacts[file.name] = str(file)
    result = envelope(COMMAND, job.name, data, error, artifacts)
    atomic_json(job / 'result.json', result)
    states = {'TIMEOUT': 'timed_out', 'CANCELLED': 'cancelled'}
    status(states.get(error['code'], 'failed') if error else 'succeeded', 'done')
    return result


def main():
    import json

    sys.stdout.reconfigure(encoding='utf-8')
    args = None
    try:
        args = parser().parse_args()
        result = run(args)
    except (Failure, OSError, ValueError) as exc:
        result = envelope(command=COMMAND, error=error_payload(exc, 'IO_ERROR'))
    if args and args.human:
        print(result['error']['message'] if not result['ok'] else result['data'].get('candidate', {}).get('file', result['data']['rig']))
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['ok'] else CODES.get(result['error']['code'], 1)


if __name__ == '__main__':
    if '--background' in sys.argv and '--' in sys.argv:
        phase, path = sys.argv[sys.argv.index('--') + 1:]
        worker(phase, Path(path))
    else:
        raise SystemExit(main())
