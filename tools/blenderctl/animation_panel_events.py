"""Saved-file entry points for the selected Animation Curves event panel."""

from contextlib import ExitStack
from host_compat import is_supported_host
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
sys.path.insert(0, str(ENTRY.parent))

from animation_author import load_author
from filesystem import FileGuard, PathLocks
from material_draft_handoff import (Parser, canonical, failure, fingerprint, guard_file, name,
                                    open_saved_context, read_document, require)
from material_projection_handoff import run_worker
from protocol import CODES, DEFAULT_BLENDER, DEFAULT_JOBS, Failure, atomic_json, digest, envelope, read_json, timeout_value


COMMAND = 'animation.panel-events'
FIELDS = {'key-capture': {'object', 'channel', 'event', 'frame', 'locked'}, 'key-inspect': set(),
          'key-adopt': {'object', 'channel', 'event'}, 'key-reset': {'expected_record_sha256'},
          'key-shift': {'event', 'to_frame'}, 'clip-review': {'manifest'},
          'clip-clear': {'expected_record_sha256'}, 'clip-fill': set(),
          'clip-pick-selection': {'mode', 'objects'}, 'clip-pick-active': {'role', 'object'},
          'channel-read-value': {'object','channel'}, 'head-plan': {'event','rig_a','rig_b','frame_a','frame_b','delta'},
          'head-apply': {'plan'}, 'asset-playback-range': {'manifest'}}
READ_ONLY = {'key-inspect', 'clip-review', 'channel-read-value', 'head-plan'}
EXTRAS = {'channel-read-value','head-plan','head-apply','asset-playback-range'}
CLIP_FIELDS = ('participant_mode', 'mode', 'event_id', 'start', 'contact', 'end', 'target_contact', 'locks',
               'rig_a_name', 'rig_b_name', 'root_a_name', 'root_b_name', 'light_name', 'material_a_name',
               'material_b_name', 'object_names')


def prepare(args, guards):
    data = read_document(guard_file(args.params.absolute(), guards))
    require(type(data) is dict and data.keys() == {'file', 'expected_sha256', 'context', 'operation', 'parameters'},
            'INVALID_REQUEST', 'Specify a source, context, event operation and parameters')
    operation, params = data['operation'], data['parameters']
    require(type(operation) is str and operation in FIELDS and type(params) is dict and params.keys() == FIELDS[operation],
            'INVALID_REQUEST', 'Unexpected or missing event parameters')
    context = data['context']
    context_fields = {'scene', 'view_layer', 'frame'}
    allowed_fields = context_fields | {'subframe'} if operation == 'channel-read-value' else context_fields
    require(type(context) is dict and context_fields <= context.keys() <= allowed_fields and name(context['scene'])
            and name(context['view_layer']) and type(context['frame']) is int,
            'INVALID_REQUEST', 'Specify the saved scene, layer and frame')
    if 'subframe' in context:
        subframe = context['subframe']
        require(type(subframe) in (int, float) and 0 <= subframe < 1, 'INVALID_REQUEST',
                'Specify the saved channel-read-value subframe in [0, 1)')
    require(type(data['file']) is str and type(data['expected_sha256']) is str, 'INVALID_REQUEST', 'Expected a SHA-bound .blend')
    source = guard_file(data['file'], guards, data['expected_sha256'])
    require(source.suffix.lower() == '.blend', 'INVALID_REQUEST', 'Expected a .blend source')
    data['file'] = str(source)
    for key in params.keys() & {'event', 'object', 'channel'}:
        require(name(params[key]), 'INVALID_REQUEST', f'Invalid {key} name')
    for key in params.keys() & {'frame', 'to_frame'}:
        require(type(params[key]) in (int, float) and -1e6 <= params[key] <= 1e6 and math.isfinite(params[key]),
                'INVALID_REQUEST', f'Invalid key {key}')
    if operation == 'key-capture':
        require(type(params['locked']) is bool, 'INVALID_REQUEST', 'Choose whether the event is locked')
    if operation == 'clip-review':
        manifest = params['manifest']
        require(type(manifest) is dict and manifest.get('schema') == 'animation-clip-event-v1'
                and manifest.get('context') == {key: context[key] for key in ('scene', 'view_layer')},
                'INVALID_REQUEST', 'Supply a clip manifest for the saved scene and view layer')
    if operation == 'clip-pick-selection':
        objects = params['objects']
        require(params['mode'] in ('rig', 'objects') and type(objects) is list and 1 <= len(objects) <= 8
                and all(name(item) for item in objects) and len(set(objects)) == len(objects), 'INVALID_REQUEST',
                'Supply explicit distinct object names and a participant mode')
    if operation == 'clip-pick-active':
        require(params['role'] in ('rig_a','rig_b','root_a','root_b','light','material_a','material_b','object'),
                'INVALID_REQUEST', 'Specify the native panel role')
    if operation in EXTRAS:
        from animation_panel_extras import prepare_extra
        prepare_extra(data, guards)
    data['edition'] = args.edition
    return data


def report_for(bpy, settings, module):
    text = bpy.data.texts.get(settings.report_text)
    require(text is not None and text.get(module.REPORT_OWNER_KEY) == module.REPORT_OWNER_VALUE, 'VALIDATION_FAILED',
            'The native event action did not produce its owned report')
    return text, json.loads(text.as_string())


def animate_witness(bpy, pc):
    actions = {action.name: {'animation': fingerprint(pc.action_witness(action)),
                             'metadata': fingerprint(pc.metadata_witness(action)), 'fake_user': action.use_fake_user}
               for action in bpy.data.actions}
    # The panel may animate an object, light/camera data or a material tree.
    data = (*bpy.data.objects, *bpy.data.lights, *bpy.data.cameras)
    owners = [(owner.bl_rna.identifier + ':' + owner.name, owner) for owner in data]
    owners += [('Material:' + material.name, material.node_tree) for material in bpy.data.materials if material.node_tree is not None]
    bindings = {}
    for key, owner in owners:
        ad = owner.animation_data
        if ad and ad.action:
            bindings[key] = [ad.action.name, ad.action_slot.identifier if ad.action_slot else None]
    return actions, bindings


def witness(bpy, ctx, ui, clip_ui, clip, pc, *, include_subframe=False):
    scene = ctx.scene
    actions, bindings = animate_witness(bpy, pc)
    active = ctx.view_layer.objects.active
    timeline = {'start': scene.frame_start, 'end': scene.frame_end, 'frame': scene.frame_current,
                'fps': scene.render.fps, 'fps_base': scene.render.fps_base,
                'markers': [[m.name,m.frame,m.camera.name if m.camera else None] for m in scene.timeline_markers]}
    if include_subframe:
        timeline['subframe'] = scene.frame_subframe
    return {'actions': actions, 'bindings': bindings, 'timeline': timeline,
            'selection': {'active': active.name if active else None, 'selected': sorted(obj.name for obj in ctx.selected_objects)},
            'dependencies': sorted(set(os.path.normcase(os.path.abspath(file)) for file in bpy.utils.blend_paths(absolute=True, packed=False))),
            'key_record': ui._event_state(ctx), 'clip_record': clip.record_summary(scene),
            'clip_fields': {key: getattr(clip_ui._settings(ctx), key) for key in CLIP_FIELDS}}


def invoke(operator, settings, **params):
    try:
        status = operator(**params)
    except RuntimeError as error:
        message = settings.last_report or str(error)
        code = message.split('：', 1)[0]
        raise Failure(code if code in CODES else 'VALIDATION_FAILED', message) from error
    require(status == {'FINISHED'}, 'VALIDATION_FAILED', settings.last_report or 'Native action did not finish')


def key_action(bpy, context, ui, operation, params, old):
    settings = ui._settings(context)
    if operation == 'key-reset':
        require(fingerprint(old) == params['expected_record_sha256'], 'CONFLICT', 'The saved key-event ledger changed')
    obj = context.view_layer.objects.get(params['object']) if 'object' in params else context.view_layer.objects.active
    require(obj is not None and context.mode == 'OBJECT', 'CONFLICT', 'Expected a saved active or explicitly named Object-mode target')
    relative = {'key-capture': 'event_capture', 'key-adopt': 'event_rebind', 'key-reset': 'event_reset',
                'key-inspect': 'event_inspect', 'key-shift': 'event_apply'}
    with bpy.context.temp_override(active_object=obj, object=obj, selected_objects=[obj]):
        if 'channel' in params:
            require(params['channel'] in {item[0] for item in ui._channel_items(settings, bpy.context)}, 'INVALID_REQUEST',
                    'This native channel is unavailable for the selected object')
            settings.channel = params['channel']
        if 'event' in params:
            settings.event_name = params['event']
        if operation == 'key-capture':
            settings.use_current_frame = False
            settings.frame = params['frame']
            settings.event_locked = params['locked']
        if operation == 'key-shift':
            settings.to_frame = params['to_frame']
        invoke(getattr(bpy.ops.animation_curves, relative[operation]), settings)
    text, report = report_for(bpy, settings, ui)
    if operation != 'key-inspect':
        text.use_fake_user = True
    if operation == 'key-shift':
        require(report.get('event_state_refresh') == 'passed', 'VALIDATION_FAILED', 'Native shift did not refresh its saved ledger')
    return report


def clip_action(bpy, context, clip_ui, clip, operation, params, old):
    settings = clip_ui._settings(context)
    if operation == 'clip-review':
        manifest = params['manifest']
        review = clip.review(context, manifest)
        return {key: review.get(key) for key in ('schema','action','review_sha256','counts','warp','handle_impact')}
    if operation == 'clip-clear':
        require(old is not None and fingerprint(old) == params['expected_record_sha256'], 'CONFLICT', 'The saved clip record changed')
        invoke(bpy.ops.animation_curves.clip_clear_record, settings)
        text, report = report_for(bpy, settings, clip_ui)
        text.use_fake_user = True
        return report
    if operation == 'clip-fill':
        invoke(bpy.ops.animation_curves.clip_fill_record, settings)
    elif operation == 'clip-pick-selection':
        objects = [context.view_layer.objects.get(item) for item in params['objects']]
        require(all(objects), 'CONFLICT', 'An explicitly selected participant is absent from the view layer')
        settings.participant_mode = params['mode']
        with bpy.context.temp_override(selected_objects=objects):
            invoke(bpy.ops.animation_curves.clip_use_selection, settings)
    else:
        obj = context.view_layer.objects.get(params['object'])
        require(obj is not None, 'CONFLICT', 'The explicitly selected active object is absent from the view layer')
        with bpy.context.temp_override(active_object=obj, object=obj):
            invoke(bpy.ops.animation_curves.clip_pick_active, settings, role=params['role'])
    return {key: getattr(settings, key) for key in CLIP_FIELDS}


def work(phase, data, job):
    import bpy

    operation, params = data['operation'], data['parameters']
    edition = data.get('edition', '0.5.0')
    participants = params['manifest'].get('participants') if operation == 'clip-review' else None
    rig_review = isinstance(participants, dict) and bool(participants.get('rigs'))
    load_author(bpy, edition, allow_53=True)
    runtime = {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(), 'edition': edition}
    if is_supported_host(bpy.app.version) and (rig_review or operation in {'head-plan', 'head-apply'}):
        from animation_curves_workflow_addon import pose_gate
        from animation_pose_handoff import observed_pose_runtime, pose_runtime_identity

        runtime.update(pose_runtime_identity(bpy, pose_gate, 'animation-0.4.4-panel-pose-win53-v1'))
        with observed_pose_runtime(pose_gate, True):
            return run_panel(bpy, phase, data, job, runtime)
    return run_panel(bpy, phase, data, job, runtime)


def run_panel(bpy, phase, data, job, runtime):
    from animation_curves_workflow_addon import clip_event as clip, clip_event_ui as clip_ui, pose_channel as pc, ui

    operation, params = data['operation'], data['parameters']
    edition = runtime['edition']
    ui.register()
    clip_ui.register()
    subframe = 'subframe' in data['context']
    context = open_saved_context(bpy, {**data, 'file': data['stage'] if phase == 'verify' else data['file']},
                                 allow_subframe=subframe)
    require(not context.scene.library and not context.scene.override_library, 'CONFLICT', 'Expected a local source scene')
    output = {'runtime': runtime, 'worker_pid': os.getpid()}
    before = witness(bpy, context, ui, clip_ui, clip, pc, include_subframe=subframe)
    if phase == 'verify':
        if operation == 'asset-playback-range':
            authored = read_json(job/'author-result.json')['data']
            protect_asset_paths(before, authored['state']['dependencies'], data['asset_manifest'])
        return {**output, 'state': before}
    if operation in EXTRAS:
        from animation_panel_extras import apply_asset_range, explicit_heads, read_channel
        if operation == 'channel-read-value':
            report = read_channel(bpy, context, ui, params, invoke)
        elif operation == 'asset-playback-range':
            report = apply_asset_range(bpy, context, data['asset_manifest'], job)
        else:
            if operation == 'head-apply':
                approved = data['head_plan'].copy()
                matches_runtime = (approved.get('runtime') == output['runtime'] if edition != '0.4.3'
                                   else 'runtime' not in approved)
                require(matches_runtime, 'CONFLICT',
                        'Head plan belongs to a different Animation Curves release or Blender build')
                approved.pop('runtime', None)
                data = {**data, 'head_plan': approved}
            report = explicit_heads(bpy, context, clip, ui, data, before)
            if operation == 'head-plan' and edition != '0.4.3':
                report['runtime'] = output['runtime']
    elif operation.startswith('key-'):
        report = key_action(bpy, context, ui, operation, params, before['key_record'])
    else:
        report = clip_action(bpy, context, clip_ui, clip, operation, params, before['clip_record'])
    if operation == 'key-shift':
        # Retain pre-existing native actions whose binding the shared core replaced.
        for action in bpy.data.actions:
            if action.name in before['actions'] and not action.library and action.users == 0:
                action.use_fake_user = True
    after = witness(bpy, context, ui, clip_ui, clip, pc, include_subframe=subframe)
    timeline = before['timeline'].copy()
    if operation == 'asset-playback-range':
        timeline['start'], timeline['end'] = report['playback_range']
        require(all(after['actions'].get(key) == value for key, value in before['actions'].items())
                and all(after['bindings'].get(key) == value for key, value in before['bindings'].items()),
                'VALIDATION_FAILED', 'Existing animation changed during the native asset import')
        report['library_reference_observed'] = protect_asset_paths(after, before['dependencies'], data['asset_manifest'])
    require(after['timeline'] == timeline, 'VALIDATION_FAILED',
            f'The native panel changed timeline state: expected {timeline}, observed {after["timeline"]}')
    require(after['selection'] == before['selection'], 'VALIDATION_FAILED', 'The native panel changed object selection')
    require(after['dependencies'] == before['dependencies'], 'VALIDATION_FAILED',
            f'The native panel changed dependency paths: {before["dependencies"]} -> {after["dependencies"]}')
    if operation not in {'key-shift','head-apply','asset-playback-range'}:
        require(after['actions'] == before['actions'] and after['bindings'] == before['bindings'], 'VALIDATION_FAILED',
                'A non-timing event action changed native animation')
    require(operation == 'clip-clear' or after['clip_record'] == before['clip_record'], 'VALIDATION_FAILED',
            'The native panel action changed an unrelated clip record')
    require(operation.startswith('key-') or after['key_record'] == before['key_record'], 'VALIDATION_FAILED',
            'The native panel action changed the unrelated key-event ledger')
    if operation not in READ_ONLY:
        bpy.context.preferences.filepaths.save_version = 0
        require(bpy.ops.wm.save_as_mainfile(filepath=data['stage'], copy=True, relative_remap=True, check_existing=False) == {'FINISHED'},
                'IO_ERROR', 'Could not save the event-panel candidate')
    return {**output, 'report': report, 'state': after,
            'key_record_sha256': fingerprint(after['key_record']),
            'clip_record_sha256': fingerprint(after['clip_record']) if after['clip_record'] else None}


def protect_asset_paths(state, original, manifest):
    # A native append can retain the signed library as a provenance reference
    # until save/reopen. All other external paths remain those of the source.
    library = os.path.normcase(os.path.abspath(manifest['clip']['library']['file']))
    observed = set(state['dependencies'])
    require(set(original) <= observed <= set(original) | {library}, 'VALIDATION_FAILED',
            'Native asset import removed a source dependency or referenced a different library')
    state['dependencies'] = original
    return library in observed


def worker(phase, path):
    try:
        result = {'ok': True, 'data': work(phase, read_json(path), path.parent)}
    except Exception as exc:
        result = {'ok': False, 'error': failure(exc)}
    atomic_json(path.parent / (phase + '-result.json'), result)


def run(args):
    deadline = time.monotonic() + timeout_value(args.timeout)
    job = args.jobs_dir.resolve() / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    artifacts = {'job_directory': str(job), 'result': str(job / 'result.json')}
    error = None

    def check():
        require(time.monotonic() < deadline, 'TIMEOUT', 'Event panel job timed out')
        require(not (job / 'cancel.request').exists(), 'CANCELLED', 'Event panel job cancelled')

    def status(state, phase):
        atomic_json(job / 'status.json', {'job_id': job.name, 'state': state, 'phase': phase, 'heartbeat_at': time.time()})

    try:
        with ExitStack() as guards:
            data = prepare(args, guards)
            writing = data['operation'] not in READ_ONLY
            output = args.output
            if writing:
                require(output and output.is_absolute() and output.suffix.lower() == '.blend', 'INVALID_REQUEST',
                        'The native action requires a new absolute .blend output')
                parent = canonical(output.parent)
                require(parent == output.parent, 'CONFLICT', 'Select a canonical output directory')
                for path in reversed((parent, *parent.parents)):
                    if path != Path(path.anchor):
                        guards.enter_context(FileGuard(path, directory=True))
                guards.enter_context(PathLocks([output]))
                require(not os.path.lexists(output), 'CONFLICT', 'The output already exists')
                stage = parent / ('.events-' + job.name + '.blend')
                guards.callback(stage.unlink, missing_ok=True)
                data['stage'] = str(stage)
            else:
                require(output is None, 'INVALID_REQUEST', 'Read-only operations take no --output')
            atomic_json(job / 'request.json', data)
            blender = canonical(args.blender)

            def execute(phase):
                status('running', phase)
                return run_worker(blender, job, phase, deadline, check, status, ENTRY, private_temp=True)

            result = execute('author')
            source = {'file': data['file'], 'sha256': data['expected_sha256']}
            if data['operation'] == 'clip-review':
                plan = job / 'review.json'
                review = {'schema': 'animation.clip-panel-review.v1', 'source': source, 'context': data['context'],
                          'manifest': data['parameters']['manifest'], 'native_review': result['report']}
                if data['edition'] != '0.4.3':
                    review['runtime'] = result['runtime']
                atomic_json(plan, review)
                artifacts['review'] = {'file': str(plan), 'sha256': digest(plan)}
            if data['operation'] == 'head-plan':
                plan = job / 'head-plan.json'
                atomic_json(plan, result['report'])
                artifacts['plan'] = {'file': str(plan), 'sha256': digest(plan)}
            if writing:
                with FileGuard(stage) as guard:
                    cold = execute('verify')
                    require(cold['worker_pid'] != result['worker_pid'] and cold['runtime'] == result['runtime']
                            and cold['state'] == result['state'],
                            'VALIDATION_FAILED', 'The native event state differs on independent reopen')
                    sha = guard.sha256(check)
                with FileGuard(stage, renameable=True) as guard:
                    require(guard.sha256(check) == sha, 'CONFLICT', 'The verified candidate changed before publication')
                    guard.rename_new(output)
                result['candidate'] = {'file': str(output), 'sha256': sha}
                result['independent_reopen'] = {'pid': cold['worker_pid'], **cold['runtime']}
            result['source'] = source
    except (OSError, ValueError, RuntimeError, Failure) as exc:
        result, error = None, failure(exc)
    reply = envelope(COMMAND, job.name, result, error, artifacts)
    atomic_json(job / 'result.json', reply)
    status({'CANCELLED': 'cancelled', 'TIMEOUT': 'timed_out'}.get(error['code'], 'failed') if error else 'succeeded', 'done')
    return reply


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    cli = Parser(description=__doc__, allow_abbrev=False)
    cli.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    cli.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS)
    cli.add_argument('--timeout', type=float, default=120)
    cli.add_argument('--transactions-dir')
    mode = cli.add_mutually_exclusive_group()
    for flag in ('--compact', '--human', '--full'):
        mode.add_argument(flag, action='store_true')
    epilog = 'JSON: file, expected_sha256, context {scene,view_layer,frame}, operation, and parameters. ' + ' '.join(
        operation + ': {' + ','.join(sorted(fields)) + '}.' for operation, fields in FIELDS.items())
    epilog += ' channel-read-value also accepts context.subframe for the exact saved playhead.'
    epilog += (' The default 0.4.4 release on project Windows Blender 5.3.0 includes head planning, '
               'head application and rig clip reviews.')
    cmd = cli.add_subparsers(required=True, parser_class=Parser).add_parser('panel-events', epilog=epilog)
    cmd.add_argument('--edition', choices=('0.5.0',), default='0.5.0',
                     help='Animation Curves Workflow release (default: %(default)s)')
    cmd.add_argument('--params', type=Path, required=True)
    cmd.add_argument('--output', type=Path, help='New absolute .blend for a writing operation')
    try:
        reply = run(cli.parse_args())
    except (OSError, ValueError, Failure) as exc:
        reply = envelope(COMMAND, error=failure(exc))
    print(json.dumps(reply, ensure_ascii=False, allow_nan=False))
    return 0 if reply['ok'] else CODES[reply['error']['code']]


if __name__ == '__main__':
    if '--background' in sys.argv and '--' in sys.argv:
        phase, filename = sys.argv[sys.argv.index('--')+1:]
        worker(phase, Path(filename))
    else:
        sys.exit(main())
