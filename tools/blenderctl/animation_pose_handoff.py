"""Plan and apply owned FK pose panel actions on saved copies."""

from contextlib import ExitStack, contextmanager
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import uuid

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
sys.path.insert(0, str(ENTRY.parent))

from host_compat import is_supported_host
from filesystem import FileGuard, PathLocks
from material_draft_handoff import Parser, canonical, failure, fingerprint, guard_file, name, read_document, require
from material_projection_handoff import run_worker
from protocol import CODES, DEFAULT_BLENDER, DEFAULT_JOBS, Failure, atomic_json, digest, envelope, read_json, timeout_value

COMMAND = 'animation.pose-edit'
SCHEMA = 'animation.pose-plan.v1'
POSE_WIN53_VARIANT = 'animation-0.4.4-pose-writer-win53-v1'
POSE_WIN53_EDITS = frozenset({'set_key', 'move_key', 'delete_key', 'prepare-axis', 'retime-action', 'capture-batch'})
POSE_044_GATE_SHA256 = 'f911f471abdba4572c0dcb4de830cfb92a5550c1e179359b421131cd33c92e7a'
INPUTS = {
    'set_key': {'bone', 'axis', 'frame', 'value_deg', 'interpolation'},
    'move_key': {'bone', 'axis', 'frame', 'to_frame'},
    'delete_key': {'bone', 'axis', 'frame'},
    'prepare-axis': {'bone', 'axis'},
    'retime-action': {'scope', 'action', 'start', 'end'},
    'capture-batch': {'bones', 'active_bone'},
}


def finite(value, low, high):
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def plain(value):
    return json.loads(json.dumps(value, allow_nan=False))


def validate_edit(edit, parameters):
    require(type(edit) is str and edit in INPUTS and type(parameters) is dict and parameters.keys() == INPUTS[edit],
            'INVALID_REQUEST', 'Specify one supported pose action and its exact parameters')
    for key in {'frame', 'to_frame', 'start', 'end'} & parameters.keys():
        require(finite(parameters[key], -1000000, 1000000), 'INVALID_REQUEST', f'{key} must be a finite Blender key frame')
    if 'axis' in parameters:
        require(name(parameters['bone']) and parameters['axis'] in ('X', 'Y', 'Z'), 'INVALID_REQUEST',
                'Specify the named pose bone and local X, Y or Z Euler axis')
    if edit == 'set_key':
        require(finite(parameters['value_deg'], -180, 180) and parameters['interpolation'] in ('LINEAR', 'CONSTANT'),
                'INVALID_REQUEST', 'Specify a finite angle and LINEAR or CONSTANT interpolation')
    if edit == 'retime-action':
        require(parameters['scope'] == 'WHOLE_ACTION' and name(parameters['action'])
                and parameters['start'] < parameters['end'], 'INVALID_REQUEST',
                'Retime requires the named WHOLE_ACTION and increasing new endpoints')
    if edit == 'capture-batch':
        bones = parameters['bones']
        require(type(bones) is list and 1 <= len(bones) <= 17 and all(name(bone) for bone in bones)
                and len(set(bones)) == len(bones) and parameters['active_bone'] in bones,
                'INVALID_REQUEST', 'Specify distinct bones and one active bone in the selection')


def prepare(args, stack):
    data = read_document(guard_file(args.params.absolute(), stack))
    require(type(data) is dict and data.get('operation') in ('inspect', 'plan', 'apply'), 'INVALID_REQUEST',
            'Select inspect, plan or apply')
    operation = data['operation']
    expected = {'inspect': set(), 'plan': {'edit', 'parameters'}, 'apply': {'plan'}}[operation]
    require(data.keys() == {'file', 'expected_sha256', 'context', 'rig', 'operation'} | expected,
            'INVALID_REQUEST', 'Unexpected or missing pose request fields')
    ctx = data['context']
    require(type(ctx) is dict and ctx.keys() in ({'scene', 'view_layer', 'frame'}, {'scene', 'view_layer', 'frame', 'subframe'})
            and name(ctx['scene']) and name(ctx['view_layer']) and type(ctx['frame']) is int
            and finite(ctx.get('subframe', 0), 0, 1) and ctx.get('subframe', 0) < 1,
            'INVALID_REQUEST', 'Specify the saved scene, view layer, integer frame and optional subframe')
    ctx.setdefault('subframe', 0)
    rig = data['rig']
    require(type(rig) is dict and rig.keys() == {'object', 'instance_uuid'} and name(rig['object'])
            and type(rig['instance_uuid']) is str and re.fullmatch(r'[0-9a-f]{32}', rig['instance_uuid']),
            'INVALID_REQUEST', 'Specify the saved armature object and owned instance UUID')
    require(type(data['file']) is str and type(data['expected_sha256']) is str, 'INVALID_REQUEST', 'Specify the source file and SHA-256')
    source = guard_file(data['file'], stack, data['expected_sha256'])
    require(source.suffix.lower() == '.blend', 'INVALID_REQUEST', 'Expected a saved .blend')
    data['file'] = str(source)
    data['edition'] = args.edition
    if operation == 'apply':
        reference = data.pop('plan')
        require(type(reference) is dict and reference.keys() == {'file', 'expected_sha256'}
                and all(type(value) is str for value in reference.values()), 'INVALID_REQUEST', 'Apply requires the reviewed plan file and SHA-256')
        plan = read_document(guard_file(reference['file'], stack, reference['expected_sha256']))
        require(type(plan) is dict and plan.get('schema') == SCHEMA
                and plan.get('source') == {'file': data['file'], 'sha256': data['expected_sha256']}
                and plan.get('context') == ctx and plan.get('rig') == rig, 'CONFLICT', 'Pose plan belongs to a different source or target')
        require(type(plan.get('runtime')) is dict and plan['runtime'].get('edition') == data['edition'],
                'CONFLICT', 'Pose plan uses a different Animation Curves Workflow edition')
        data.update(edit=plan.get('edit'), parameters=plan.get('parameters'), reviewed=plan)
    if operation != 'inspect':
        validate_edit(data['edit'], data['parameters'])
    return data


def open_saved(bpy, path, expected):
    require(bpy.ops.wm.open_mainfile(filepath=path, load_ui=True, use_scripts=False) == {'FINISHED'}
            and Path(bpy.data.filepath).samefile(path), 'VALIDATION_FAILED', 'Could not reopen the exact saved project')
    major, minor = bpy.app.version[:2]
    require(tuple(bpy.data.version[:2]) == (major, minor), 'UNSUPPORTED',
            f'Expected a Blender {major}.{minor} saved project for this pose writer')
    context = bpy.context
    scene = context.scene
    observed = dict(scene=scene.name, view_layer=context.view_layer.name, frame=scene.frame_current, subframe=scene.frame_subframe)
    require(observed == expected and not scene.library and not scene.override_library, 'CONFLICT',
            f'Saved scene, layer or playhead differs: {observed}')
    return context


def selection(context):
    active = context.view_layer.objects.active
    active_bone = context.active_pose_bone if context.mode == 'POSE' else None
    return {'mode': context.mode, 'active': active.name if active else None,
            'objects': sorted(item.name for item in context.selected_objects),
            'bones': sorted(item.name for item in context.selected_pose_bones or []) if context.mode == 'POSE' else [],
            'active_bone': active_bone.name if active_bone else None}


def set_bones(rig, names, active_bone):
    for item in rig.pose.bones:
        item.select = item.name in names
    rig.data.bones.active = rig.data.bones.get(active_bone) if active_bone else None


@contextmanager
def select_pose(bpy, context, rig, bones, active_bone):
    view = context.view_layer
    old = selection(context)
    require(old['mode'] == 'OBJECT' or (old['mode'] == 'POSE' and old['active'] == rig.name and old['objects'] == [rig.name]),
            'UNSUPPORTED', 'Use Object mode or the named armature alone in Pose mode before saving')
    require(not rig.hide_get() and not rig.hide_select and all(rig.pose.bones.get(bone) for bone in bones),
            'CONFLICT', 'The named rig or one of its pose bones is unavailable')
    saved_bones = [bone.name for bone in rig.pose.bones if bone.select]
    previous_active = rig.data.bones.active
    if old['mode'] == 'OBJECT':
        for item in context.selected_objects:
            item.select_set(False)
        rig.select_set(True)
        view.objects.active = rig
        require(bpy.ops.object.mode_set(mode='POSE') == {'FINISHED'}, 'UNSUPPORTED', 'Could not enter native Pose mode')
    try:
        if active_bone:
            set_bones(rig, bones, active_bone)
        current = selection(context)
        require(current['mode'] == 'POSE' and current['active'] == rig.name and current['objects'] == [rig.name]
                and (not active_bone or (current['bones'] == sorted(bones) and current['active_bone'] == active_bone)),
                'CONFLICT', 'Explicit selected bones or active bone do not match the native Pose context')
        yield
    finally:
        set_bones(rig, saved_bones, previous_active.name if previous_active else None)
        if old['mode'] == 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
            rig.select_set(False)
            for name_ in old['objects']:
                view.objects[name_].select_set(True)
            view.objects.active = view.objects.get(old['active']) if old['active'] else None
        require(selection(context) == old, 'VALIDATION_FAILED', 'Could not restore the saved editor selection')


def state(bpy, context, rig, pc):
    scene = context.scene
    actions = {action.name: {'sha256': fingerprint(pc.action_witness(action)),
                             'metadata_sha256': fingerprint(pc.metadata_witness(action)), 'fake_user': action.use_fake_user}
               for action in bpy.data.actions}
    bindings = {obj.name: [obj.animation_data.action.name, obj.animation_data.action_slot.identifier if obj.animation_data.action_slot else None]
                for obj in bpy.data.objects if obj.animation_data and obj.animation_data.action}
    ad = rig.animation_data
    action = ad.action if ad else None
    timeline = {'start': scene.frame_start, 'end': scene.frame_end, 'preview': scene.use_preview_range,
                'preview_start': scene.frame_preview_start, 'preview_end': scene.frame_preview_end,
                'playhead': scene.frame_current, 'subframe': scene.frame_subframe, 'fps': scene.render.fps,
                'fps_base': scene.render.fps_base,
                'markers': [[marker.name, marker.frame, marker.camera.name if marker.camera else None] for marker in scene.timeline_markers]}
    return {'actions': actions, 'bindings': bindings, 'timeline': timeline, 'selection': selection(context),
            'dependencies': sorted(set(os.path.normcase(os.path.abspath(file)) for file in bpy.utils.blend_paths(absolute=True, packed=False))),
            'action': action.name if action else None, 'witness': plain(pc.action_witness(action)) if action else None}


def records_clear(scene, rig, modules):
    from types import SimpleNamespace
    paired, cross, clip = modules
    action = rig.animation_data.action if rig.animation_data else None
    if not action:
        return
    for store in (paired, cross):
        record = store.parse(store.raw(SimpleNamespace(scene=scene)))
        require(not record or all(row.get('action') != action.name for row in record['rows']), 'CONFLICT',
                'The selected Action is bound to a saved event; use its event editing workflow')
    text = scene.get(clip.RECORD_PROPERTY)
    if text:
        def includes(value):
            if isinstance(value, dict):
                return any(includes(item) for item in value.values())
            if isinstance(value, list):
                return any(includes(item) for item in value)
            return value == action.name or value == rig.get('ac_instance_uuid')
        require(not includes(json.loads(text)), 'CONFLICT', 'The selected Action belongs to a saved clip event')


def scoped_edit(bpy, context, rig, data, modules):
    pc, prepare_axis, batch, retime, native = modules
    edit, p = data['edit'], data['parameters']
    active = p.get('bone', p.get('active_bone'))
    bones = p.get('bones', [active] if active else [])
    with select_pose(bpy, context, rig, bones, active):
        if edit == 'capture-batch':
            origin = batch.origin(context)
            review = {'frame': context.scene.frame_current_final, 'channels': origin['rows']}
            perform = lambda: batch.execute(context, origin)
        elif edit == 'retime-action':
            identity, _ = retime.selected(context)
            w, channels, span = retime.review(context, identity)
            require(w['action'].name == p['action'], 'CONFLICT', 'Named whole Action is not bound to the selected rig')
            source = native.action_state(w['action'])
            after = retime.plan(source, p['start'], p['end'])
            require(source != after, 'CONFLICT', 'The requested Action times are unchanged')
            review = {'scope': p['scope'], 'action': p['action'], 'channels': channels,
                      'source_span': [float(item) for item in span], 'target_span': [p['start'], p['end']]}
            origin = retime.origin(w)
            perform = lambda: retime.execute(context, identity, p['start'], p['end'], expected_origin=origin)
        else:
            axis = 'XYZ'.index(p['axis'])
            identity, w, pb, knee = pc.selected(context, allow_body_colors=edit != 'prepare-axis')
            path = pb.path_from_id('rotation_euler')
            pc.target(rig, path, axis, allow_body_colors=edit != 'prepare-axis')
            review = {'bone': pb.name, 'axis': p['axis'], 'data_path': path, 'index': axis,
                      'angle_input': 'physical_knee_flexion' if knee else 'local_euler'}
            if knee:
                review['rest_flexion_deg'] = w['spec']['joint_limits'][pb.name]['rest_flexion_deg']
            if edit == 'prepare-axis':
                _, _, _, count = prepare_axis.review(context, identity, axis)
                require(count > 0, 'CONFLICT', 'The selected axis already has no automatic handles')
                review['auto_sides'] = count
                origin = prepare_axis.origin(w)
                perform = lambda: prepare_axis.execute(context, identity, axis, expected_origin=origin)
            else:
                curve = w['bag'].fcurves.find(path, index=axis) if w['bag'] else None
                key = native.find_key(curve, p['frame']) if curve else None
                review['before_key'] = native.key_state(key) if key else None
                origin = pc.display_origin(context)
                perform = lambda: pc.execute(context, identity, axis, edit, p['frame'], p.get('value_deg', 0),
                                             p.get('to_frame', 0), p.get('interpolation', 'LINEAR'), expected_display_origin=origin)
        recipe = {**data['identity'], 'impact': plain(review)}
        if data['operation'] == 'plan':
            if edit in ('set_key', 'move_key', 'delete_key'):
                # The key transaction is the shared source of collision, FCurve
                # and angle rules. Validate on this disposable in-memory load.
                perform()
            return {'plan': recipe}
        require(recipe == data['reviewed'], 'CONFLICT', 'Native Pose review no longer matches the approved plan')
        return {'native': plain(perform()), 'impact': plain(review)}


def pose_runtime_identity(bpy, gate, variant):
    from animation_curves_workflow_addon.windows_owned import runtime_identity

    gate_sha = digest(Path(gate.__file__))
    try:
        host = runtime_identity(bpy)
    except (OSError, ValueError) as exc:
        raise Failure('UNSUPPORTED', str(exc)) from exc
    return dict(compatibility_variant=variant, blender_sha256=host['sha256'], source_pose_gate_sha256=gate_sha)


@contextmanager
def observed_pose_runtime(gate, compatibility):
    if not compatibility:
        yield
        return
    original = gate.discover

    def discover(*args, support_runtime=False, **kwargs):
        # The existing native gate uses the observed Windows runtime verifier
        # for this flag and then applies the same owned-pose checks.
        return original(*args, **kwargs, support_runtime=True)

    gate.discover = discover
    try:
        yield
    finally:
        gate.discover = original


def work(phase, data):
    import bpy
    from animation_author import load_author

    edition = data.get('edition', '0.5.0')
    load_author(bpy, edition)
    from animation_curves_workflow_addon import (action_retime, clip_event, cross_event_store, native, pose_batch,
                                                pose_channel, pose_event_store, pose_gate, pose_prepare, templates_native, ui)

    compatibility = is_supported_host(bpy.app.version)
    if compatibility:
        gate_sha = digest(Path(pose_gate.__file__))
    
    ui.register()
    path = data['stage'] if phase == 'verify' else data['file']
    context = open_saved(bpy, path, data['context'])
    target = data['rig']
    rig = context.view_layer.objects.get(target['object'])
    require(rig and rig.type == 'ARMATURE' and not rig.library and not rig.override_library
            and rig.get('ac_template_owner') == templates_native.OWNER and rig.get('ac_instance_uuid') == target['instance_uuid'],
            'CONFLICT', 'Saved armature object or owned instance UUID differs')
    runtime = dict(host=bpy.app.version_string, build=bpy.app.build_hash.decode(), edition=edition)
    if compatibility:
        runtime.update(compatibility_variant=POSE_WIN53_VARIANT, source_pose_gate_sha256=gate_sha)
    before = state(bpy, context, rig, pose_channel)
    output = {'runtime': runtime, 'worker_pid': os.getpid()}
    if phase == 'verify' or data['operation'] == 'inspect':
        return {**output, 'state': before}
    records_clear(context.scene, rig, (pose_event_store, cross_event_store, clip_event))
    data['identity'] = {'schema': SCHEMA, 'source': {'file': data['file'], 'sha256': data['expected_sha256']},
                        'runtime': runtime, 'context': data['context'], 'rig': target, 'edit': data['edit'],
                        'parameters': data['parameters'], 'action_before': before['action'],
                        'snapshot_sha256': fingerprint(before)}
    with observed_pose_runtime(pose_gate, compatibility):
        result = scoped_edit(bpy, context, rig, data, (pose_channel, pose_prepare, pose_batch, action_retime, native))
    if data['operation'] == 'plan':
        return {**output, **result}
    after = state(bpy, context, rig, pose_channel)
    require(all(after[key] == before[key] for key in ('timeline', 'selection', 'dependencies')), 'VALIDATION_FAILED',
            'Pose candidate changed time, selection or dependency resolution')
    desired_bindings = {**before['bindings'], rig.name: after['bindings'].get(rig.name)}
    require(after['bindings'] == desired_bindings and after['action'] and after['action'] not in before['actions']
            and len(after['actions']) == len(before['actions']) + 1, 'VALIDATION_FAILED', 'Pose candidate bindings or Action set differs')
    for previous, snapshot in before['actions'].items():
        expected = {**snapshot, 'fake_user': True} if previous == before['action'] else snapshot
        require(after['actions'].get(previous) == expected, 'VALIDATION_FAILED', f'Prior Action contents changed: {previous}')
    bpy.context.preferences.filepaths.save_version = 0
    require(bpy.ops.wm.save_as_mainfile(filepath=data['stage'], copy=True, relative_remap=True, check_existing=False) == {'FINISHED'},
            'IO_ERROR', 'Could not save the Pose work copy')
    return {**output, **result, 'state': after, 'previous_action': before['action']}


def worker(phase, path):
    try:
        result = {'ok': True, 'data': work(phase, read_json(path))}
    except Exception as error:
        result = {'ok': False, 'error': failure(error)}
    atomic_json(path.parent / (phase + '-result.json'), result)


def run(args):
    deadline = time.monotonic() + timeout_value(args.timeout)
    job = args.jobs_dir.resolve() / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    artifacts = {'job_directory': str(job), 'result': str(job / 'result.json')}
    error = None

    def checkpoint():
        require(time.monotonic() < deadline, 'TIMEOUT', 'Pose job timed out')
        require(not (job / 'cancel.request').exists(), 'CANCELLED', 'Pose job cancelled')

    def heartbeat(status, phase):
        atomic_json(job / 'status.json', {'job_id': job.name, 'state': status, 'phase': phase, 'heartbeat_at': time.time()})

    try:
        with ExitStack() as stack:
            data = prepare(args, stack)
            applying = data['operation'] == 'apply'
            if applying:
                output = args.output
                require(output and output.is_absolute() and output.suffix.lower() == '.blend', 'INVALID_REQUEST',
                        'Apply requires a new absolute .blend output')
                parent = canonical(output.parent)
                require(parent == output.parent, 'CONFLICT', 'Select a canonical output directory')
                for item in reversed((parent, *parent.parents)):
                    if item != Path(item.anchor):
                        stack.enter_context(FileGuard(item, directory=True))
                stack.enter_context(PathLocks([output]))
                require(not os.path.lexists(output), 'CONFLICT', 'Pose output already exists')
                stage = parent / ('.pose-' + job.name + '.blend')
                stack.callback(stage.unlink, missing_ok=True)
                data['stage'] = str(stage)
            else:
                require(args.output is None, 'INVALID_REQUEST', 'Only apply takes --output')
            atomic_json(job / 'request.json', data)
            blender = canonical(args.blender)

            def execute(phase):
                heartbeat('running', phase)
                return run_worker(blender, job, phase, deadline, checkpoint, heartbeat, ENTRY, private_temp=True)

            result = execute('author')
            if data['operation'] == 'plan':
                plan = job / 'plan.json'
                atomic_json(plan, result['plan'])
                artifacts['plan'] = {'file': str(plan), 'sha256': digest(plan)}
            if applying:
                with FileGuard(stage) as guard:
                    cold = execute('verify')
                    require(cold['state'] == result['state'] and cold['runtime'] == result['runtime']
                            and cold['worker_pid'] != result['worker_pid'], 'VALIDATION_FAILED',
                            'The Pose candidate differed on independent reopen')
                    sha = guard.sha256(checkpoint)
                with FileGuard(stage, renameable=True) as guard:
                    require(guard.sha256(checkpoint) == sha, 'CONFLICT', 'Pose candidate changed after independent verification')
                    checkpoint()
                    guard.rename_new(output)
                result['candidate'] = {'file': str(output), 'sha256': sha}
                result['independent_reopen'] = {'pid': cold['worker_pid'], **cold['runtime']}
                result['persisted'] = True
            result['source'] = {'file': data['file'], 'sha256': data['expected_sha256']}
    except (OSError, ValueError, RuntimeError, TypeError, Failure) as exc:
        result, error = None, failure(exc)
    reply = envelope(COMMAND, job.name, result, error, artifacts)
    atomic_json(job / 'result.json', reply)
    heartbeat({'CANCELLED': 'cancelled', 'TIMEOUT': 'timed_out'}.get(error['code'], 'failed') if error else 'succeeded', 'done')
    return reply


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    cli = Parser(description=__doc__, allow_abbrev=False)
    cli.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    cli.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS)
    cli.add_argument('--timeout', type=float, default=120)
    cli.add_argument('--transactions-dir')
    display = cli.add_mutually_exclusive_group()
    for flag in ('--compact', '--human', '--full'):
        display.add_argument(flag, action='store_true')
    command = cli.add_subparsers(required=True, parser_class=Parser).add_parser('pose-edit', description=__doc__,
        epilog='JSON fields are file, expected_sha256, context {scene,view_layer,frame,optional subframe}, rig {object,instance_uuid}, '
               'and operation inspect|plan|apply. Plan takes edit and parameters; apply takes a plan file and expected_sha256. '
               'set_key takes bone,axis,frame,value_deg,interpolation; move_key takes bone,axis,frame,to_frame; '
               'delete_key takes bone,axis,frame; prepare-axis takes bone,axis; retime-action takes scope=WHOLE_ACTION,action,start,end; '
               'capture-batch takes bones and active_bone, and reviews the saved inputs at the playhead. '
               'Edit and inspect owned poses on Windows Blender 5.3.0 or later.')
    command.add_argument('--edition', choices=('0.5.0',), default='0.5.0',
                         help='Animation Curves Workflow edition used to review and apply the plan (default: 0.4.3)')
    command.add_argument('--params', type=Path, required=True, help='Request JSON')
    command.add_argument('--output', type=Path, help='New absolute .blend for apply')
    try:
        reply = run(cli.parse_args())
    except (OSError, ValueError, Failure) as exc:
        reply = envelope(COMMAND, error=failure(exc))
    print(json.dumps(reply, ensure_ascii=False, allow_nan=False))
    return 0 if reply['ok'] else CODES[reply['error']['code']]


if __name__ == '__main__':
    if '--background' in sys.argv and '--' in sys.argv:
        phase, filename = sys.argv[sys.argv.index('--') + 1:]
        worker(phase, Path(filename))
    else:
        sys.exit(main())
