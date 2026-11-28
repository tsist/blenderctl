"""Save an explicitly identified Relight preview from an existing .blend snapshot."""
import argparse
from contextlib import ExitStack
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
import uuid
from zipfile import ZipFile

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
ROOT = ENTRY.parents[2]
sys.path.insert(0, str(ENTRY.parent))

from filesystem import FileGuard, PathLocks
from material_draft_handoff import Parser, canonical, fingerprint, guard_file, read_document, require
from material_projection_handoff import error_payload, run_worker
from protocol import CODES, DEFAULT_BLENDER, DEFAULT_JOBS, SCHEMA, Failure, atomic_json, digest, envelope, read_json, timeout_value
from toon_panel_handoff import dependencies, name


COMMAND = 'compositing.preview-copy'
EDITION = '0.9.0'
from host_compat import is_supported_host
RELEASE = ROOT / 'runtime/plugin-archives/compositing_workflow_addon-0.9.0.zip'
RELEASE_SHA256 = '3a682caf90a14048d137f7ff7804b637f8410bd8298e2b089be64d8dc62b744f'
SAVE_FIELDS = {'rp_save_path', 'rp_current_config', 'rp_pending_local_edits'}
REGION_FIELDS = {'center', 'size', 'rotation', 'feather', 'strength', 'scale', 'offset', 'value', 'enabled'}


def parser():
    cli = Parser(description=__doc__)
    cli.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    cli.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS)
    cli.add_argument('--transactions-dir', help=argparse.SUPPRESS)
    cli.add_argument('--timeout', type=float, default=120)
    mode = cli.add_mutually_exclusive_group()
    for flag in ('--human', '--compact', '--full'):
        mode.add_argument(flag, action='store_true')
    sub = cli.add_subparsers(required=True, parser_class=Parser).add_parser('preview-copy', description=__doc__,
        epilog='Params: edition 0.9.0, file, expected_sha256, context {scene,view_layer,frame}, '
               'expected_owner (the saved relight_preview_owner_v1 UUID hex). '
               'The exact scene and owner select the preview even if another saved window was active. '
               'Optional region_edits: ordered add {target,shape,fields?}, edit {id,fields}, '
               'toggle/remove/cycle {id}. Positions and sizes use full-image normalized coordinates. '
               'Edits change only the pending native draft. Apply in a bound preview to generate a new revision. '
               'This command consumes a captured .blend and never connects to a hot session.')
    sub.add_argument('--params', type=Path, required=True)
    sub.add_argument('--output', type=Path, required=True, help='New absolute .blend')
    return cli


def request_for(args, guards):
    request = read_document(guard_file(args.params.absolute(), guards))
    required = {'edition', 'file', 'expected_sha256', 'context', 'expected_owner'}
    require(type(request) is dict and required <= request.keys() <= required | {'region_edits'},
            'INVALID_REQUEST', 'Expected the preview-copy fields and optional region_edits')
    require(request['edition'] == EDITION, 'UNSUPPORTED', 'Unknown Compositing Workflow preview edition')
    ctx = request['context']
    require(type(ctx) is dict and ctx.keys() == {'scene', 'view_layer', 'frame'}
            and name(ctx['scene']) and name(ctx['view_layer']) and type(ctx['frame']) is int,
            'INVALID_REQUEST', 'Expected exact scene, view layer and integer frame')
    owner = request['expected_owner']
    require(isinstance(owner, str) and re.fullmatch(r'[0-9a-f]{32}', owner), 'INVALID_REQUEST',
            'expected_owner must be the saved 32-character lowercase Relight owner ID')
    source = request['file']
    require(isinstance(source, str) and Path(source).suffix.lower() == '.blend'
            and isinstance(request['expected_sha256'], str), 'INVALID_REQUEST', 'Expected a hash-pinned .blend source')
    request['file'] = str(guard_file(source, guards, request['expected_sha256']))
    request['release'] = {'file': str(guard_file(RELEASE, guards, RELEASE_SHA256)), 'sha256': RELEASE_SHA256}
    if 'region_edits' in request:
        validate_edits(request['region_edits'])
    return request


def validate_edits(operations):
    require(type(operations) is list and operations, 'INVALID_REQUEST', 'region_edits must be a nonempty array')
    for step in operations:
        require(type(step) is dict and type(step.get('action')) is str, 'INVALID_REQUEST', 'Expected a region action object')
        action = step['action']
        if action == 'add':
            require({'action', 'target', 'shape'} <= step.keys() <= {'action', 'target', 'shape', 'fields'}
                    and type(step['target']) is str and step['shape'] in ('ellipse', 'box'),
                    'INVALID_REQUEST', 'Add requires target, shape and optional fields')
        elif action == 'edit':
            require(step.keys() == {'action', 'id', 'fields'}, 'INVALID_REQUEST', 'Edit requires id and fields')
        else:
            require(action in ('toggle', 'remove', 'cycle') and step.keys() == {'action', 'id'},
                    'INVALID_REQUEST', 'Expected toggle, remove or cycle with one id')
        if action != 'add':
            require(type(step['id']) is str and 0 < len(step['id']) <= 64,
                    'INVALID_REQUEST', 'Expected a saved region id')
        if 'fields' in step:
            require(type(step['fields']) is dict and step['fields'] and step['fields'].keys() <= REGION_FIELDS,
                    'INVALID_REQUEST', 'Unknown or empty region fields')


def native_ui(bpy, request):
    host, build = tuple(bpy.app.version), bpy.app.build_hash.decode()
    require(is_supported_host(host), 'UNSUPPORTED', 'Preview copy requires Blender 5.3.0 or later')
    archive = request['release']['file']
    package = ModuleType('compositing_workflow_addon')
    package.__path__ = [archive]
    package.__file__ = archive + '/__init__.py'
    package.__package__ = package.__name__
    sys.modules[package.__name__] = package
    with ZipFile(archive) as source:
        exec(compile(source.read('__init__.py'), package.__file__, 'exec'), package.__dict__)
    require('.'.join(map(str, package.bl_info['version'])) == EDITION, 'CONFLICT', 'Native package edition differs')
    try:
        package.register()
        ui = importlib.import_module(f'{package.__name__}.relight_preview_ui')
    except (ImportError, RuntimeError) as error:
        raise Failure('CONFLICT', 'Cannot register the frozen Relight preview: ' + str(error)) from error
    require(ui.__file__.startswith(archive) and ui._save_pre in bpy.app.handlers.save_pre and not ui._sessions
            and bpy.ops.relight_preview.save.get_rna_type().identifier == 'RELIGHT_PREVIEW_OT_save',
            'CONFLICT', 'Expected the isolated Relight panel registration')
    return ui


def scene_owner(bpy, ui, context, request):
    scene = context.scene
    owner = request['expected_owner']
    require(scene.get(ui.OWNER) == owner and not scene.library and not scene.override_library,
            'CONFLICT', 'Saved preview scene owner does not match the request')
    require(sum(row.get(ui.OWNER) == owner for row in bpy.data.scenes) == 1,
            'CONFLICT', 'The requested owner belongs to multiple scenes')
    texts = [item for item in bpy.data.texts if item.get(ui.OWNER) == owner]
    require(len(texts) <= 1 and all(not item.library and not item.override_library for item in texts),
            'CONFLICT', 'Preview owner Text is ambiguous or linked')
    return texts[0] if texts else None


def snapshot(bpy, scene, author_text, *, region_scope=None):
    from inspection import identity, key, record

    classes = ('scenes', 'collections', 'objects', 'meshes', 'lights', 'cameras', 'materials',
               'node_groups', 'worlds', 'images', 'texts', 'actions', 'armatures')
    witness = {}
    objects, meshes, owner_collection = region_scope or ((), (), None)
    for kind in classes:
        for item in getattr(bpy.data, kind):
            if (kind == 'objects' and item in objects) or (kind == 'meshes' and item in meshes):
                continue
            state = record(item)
            if item == scene:
                for field in SAVE_FIELDS:
                    state['custom_properties'].pop(field, None)
                if region_scope is not None:
                    for field in ('rp_region_order', 'rp_local_status'):
                        state['custom_properties'].pop(field, None)
            if kind == 'texts' and item != author_text:
                state['content'] = fingerprint(item.as_string())
            if kind == 'scenes':
                state['membership'] = {'objects': sorted(key(identity(obj)) for obj in item.objects
                                                         if item != scene or obj not in objects),
                                       'children': sorted(key(identity(c)) for c in item.collection.children),
                                       'layers': [layer.name for layer in item.view_layers]}
            if kind == 'collections':
                state['membership'] = {'objects': sorted(key(identity(obj)) for obj in item.objects
                                                         if item != owner_collection or obj not in objects),
                                       'children': sorted(key(identity(c)) for c in item.children)}
            witness[key(identity(item))] = fingerprint(state)
    return {'data': witness, 'dependencies': dependencies(bpy)}


def region_state(bpy, ui, scene, text):
    from inspection import record

    recipe = ui.extract_local_edits(scene)
    ordered = [row['id'] for row in recipe['regions']]
    owner = scene[ui.OWNER]
    collections = [item for item in scene.collection.children if item.get(ui.OWNER) == owner]
    require(len(collections) == 1 and collections[0].users == 1
            and not collections[0].library and not collections[0].override_library,
            'CONFLICT', 'The saved preview has no unique local owner collection')
    objects = [obj for obj in scene.objects if obj.get(ui.OWNER) == owner and obj.get('rp_region_id') in ordered]
    meshes = [obj.data for obj in objects if obj.type == 'MESH']
    outlines = {obj['rp_region_id']: {'object': fingerprint(record(obj)),
                'mesh': fingerprint(record(obj.data)) if obj.type == 'MESH' else None}
                for obj in objects}
    return (snapshot(bpy, scene, text, region_scope=(objects, meshes, collections[0])), outlines)


def normalize_regions(recipe):
    from compositing_relight_local_edits import normalize

    try:
        return normalize(recipe)
    except ValueError as error:
        raise Failure('INVALID_REQUEST', 'Invalid saved local region request: ' + str(error)) from error


def place_region(ui, scene, obj, changes, expected):
    geometry = json.loads(scene['rp_geometry'])
    if 'center' in changes:
        x, y = expected['center']
        base_x, base_y, _ = geometry['center']
        obj.location.x = base_x + (x - .5) * geometry['width']
        obj.location.y = base_y + (.5 - y) * geometry['height']
    if 'size' in changes:
        x, y = expected['size']
        obj.scale.x = x * geometry['width'] * .5
        obj.scale.y = y * geometry['height'] * .5
    if 'rotation' in changes:
        obj.rotation_euler.z = -expected['rotation']
    for field in changes.keys() - {'center', 'size', 'rotation'}:
        obj['rp_' + field] = (bool(expected[field]) if field == 'enabled' else ui._native_continuous(expected[field]))


def same_region_value(actual, expected):
    if type(expected) is list:
        return len(actual) == len(expected) and all(same_region_value(a, b) for a, b in zip(actual, expected))
    if type(expected) is float:
        return math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-6)
    return actual == expected


def apply_region_edits(bpy, ui, scene, operations):
    from copy import deepcopy

    history = []
    for step in operations:
        action = step['action']
        previous = ui.extract_local_edits(scene)
        expected = deepcopy(previous)
        existing = {row['id']: row for row in expected['regions']}
        changes = step.get('fields', {})
        if action == 'add':
            require(step['target'] in ui.LOCAL_OPERATIONS and len(existing) < 8,
                    'INVALID_REQUEST', 'Use a supported target and at most eight local regions')
            # Use the native authoring function so IDs, outline topology and owner
            # tags agree with a region created in the panel.
            obj = ui.add_region(scene, target=step['target'], shape=step['shape'])
            identity = obj['rp_region_id']
            row = ui.extract_local_edits(scene)['regions'][-1]
            expected['regions'].append(row)
        else:
            identity = step['id']
            require(identity in existing, 'NOT_FOUND', f'Saved region not found: {identity}')
            row = existing[identity]
            matches = [obj for obj in scene.objects if obj.get(ui.OWNER) == scene[ui.OWNER]
                       and obj.get('rp_region_id') == identity]
            require(len(matches) == 1 and not matches[0].library and not matches[0].override_library,
                    'CONFLICT', 'Saved region object must be unique and local')
            obj = matches[0]
        if action == 'remove':
            expected['regions'].remove(row)
        elif action == 'toggle':
            row['enabled'] = not row['enabled']
        elif action == 'cycle':
            choices = ui.LOCAL_OPERATIONS[row['target']]
            row['operation'] = choices[(choices.index(row['operation']) + 1) % len(choices)]
        else:
            row.update(changes)
        checked = normalize_regions(expected)
        if action in ('remove', 'toggle', 'cycle'):
            with bpy.context.temp_override(object=obj, active_object=obj):
                require(bpy.ops.relight_preview.region_action(action='operation' if action == 'cycle' else action) == {'FINISHED'},
                        'WORKER_FAILED', 'Native local region action did not finish')
        elif changes:
            row = next(item for item in checked['regions'] if item['id'] == identity)
            place_region(ui, scene, obj, changes, row)
        actual = ui.extract_local_edits(scene)
        require([row['id'] for row in actual['regions']] == [row['id'] for row in checked['regions']],
                'VALIDATION_FAILED', 'Native local region ordering differs')
        for observed, predicted in zip(actual['regions'], checked['regions'], strict=True):
            if observed['id'] == identity and changes:
                require(all(same_region_value(observed[key], predicted[key]) for key in predicted),
                        'VALIDATION_FAILED', 'Native local region fields differ')
            else:
                require(observed == predicted, 'VALIDATION_FAILED', 'Unrequested local region fields changed')
        history.append({'action': action, 'id': identity})
    return history


def read_preview(ui, scene, author_text):
    config = ui.extract_config(scene)
    pending = ui.extract_local_edits(scene)
    ready = json.loads(scene['rp_ready_snapshot'])
    background = [obj for obj in scene.objects if obj.get(ui.OWNER) == scene[ui.OWNER] and obj.get('rp_background')]
    require(len(background) == 1 and background[0].type == 'EMPTY', 'CONFLICT', 'Preview image proxy is absent or ambiguous')
    image = background[0].data
    return {'scene': scene.name, 'owner_id': scene[ui.OWNER],
            'current_config': config, 'pending_local_edits': pending,
            'applied_config': json.loads(scene.get('rp_applied_config', ui._json(config))),
            'applied_local_edits': json.loads(scene.get('rp_applied_local_edits', ui._json(ui._empty_local_edits()))),
            'shown_config': json.loads(scene.get('rp_shown_config', 'null')),
            'shown_local_edits': json.loads(scene.get('rp_shown_local_edits', 'null')),
            'shown_manifest': json.loads(scene.get('rp_shown_manifest', 'null')),
            'revisions': {'latest': scene.get('rp_latest_revision'), 'shown': scene.get('rp_shown_revision')},
            'display': {'mode': scene.get('rp_display_mode'), 'view': scene.get('rp_displayed_view'),
                        'image': image.name_full if image else None,
                        'external_file': str(Path(ui.bpy.path.abspath(image.filepath, library=image.library)).resolve())
                                         if image and image.filepath and not image.packed_file else None},
            'ready_file': scene['rp_ready_file'], 'ready_snapshot_sha256': fingerprint(ready),
            'author_text': author_text.name_full if author_text else None,
            'author_text_sha256': hashlib.sha256(author_text.as_string().encode('utf-8')).hexdigest() if author_text else None,
            'save_path': scene['rp_save_path'], 'hot_session': 'explicit_new_bind_required'}


def verify_persisted(ui, scene, author_text, preview):
    require(json.loads(scene['rp_current_config']) == preview['current_config']
            and json.loads(scene['rp_pending_local_edits']) == preview['pending_local_edits'],
            'VALIDATION_FAILED', 'The native save callback did not persist the captured controls')
    if author_text:
        archived = json.loads(author_text.as_string())
        fields = ('ready_file', 'current_config', 'pending_local_edits', 'applied_config', 'applied_local_edits',
                  'shown_config', 'shown_local_edits', 'shown_manifest')
        require(all(archived[field] == preview[field] for field in fields) and archived['telemetry'] == ui.telemetry(scene),
                'VALIDATION_FAILED', 'The native author Text did not retain the captured configuration and telemetry')


def save_candidate(bpy, ui, scene, stage, output):
    handlers = bpy.app.handlers.save_pre
    native = ui._save_pre
    index = handlers.index(native)
    called = []

    def scoped_handler(filepath):
        # The native callback normally visits every Relight scene in the .blend.
        # Give that exact callback only this scene; its owner Text stays live.
        if Path(filepath).resolve() != Path(stage).resolve():
            return
        scene['rp_save_path'] = output
        try:
            ui.bpy = SimpleNamespace(data=SimpleNamespace(scenes=(scene,), texts=bpy.data.texts))
            native(filepath)
            called.append(filepath)
        finally:
            ui.bpy = bpy

    scene['rp_save_path'] = stage
    handlers[index] = scoped_handler
    try:
        require(bpy.ops.relight_preview.save() == {'FINISHED'} and len(called) == 1,
                'IO_ERROR', 'Native preview save or its scoped persistence callback did not finish')
    finally:
        handlers.remove(scoped_handler)
        handlers.insert(index, native)


def work(phase, record, job):
    import bpy

    request = record['params']
    source = request['stage'] if phase == 'verify' else request['file']
    require(bpy.ops.wm.open_mainfile(filepath=source, load_ui=True, use_scripts=False) == {'FINISHED'}
            and Path(bpy.data.filepath).samefile(source), 'VALIDATION_FAILED', 'Saved preview could not reopen')
    require(tuple(bpy.data.version[:2]) == tuple(bpy.app.version[:2]), 'UNSUPPORTED',
            'Saved .blend and Blender must share major/minor version')
    ui = native_ui(bpy, request)
    default = bpy.context.scene
    default_context = {'scene': default.name, 'view_layer': bpy.context.view_layer.name, 'frame': default.frame_current,
                       'subframe': default.frame_subframe}
    target = request['context']
    scene = bpy.data.scenes.get(target['scene'])
    require(scene is not None and scene.name == target['scene'], 'CONFLICT', 'The requested saved preview scene is absent')
    layer = scene.view_layers.get(target['view_layer'])
    require(layer is not None and layer.name == target['view_layer'] and scene.frame_current == target['frame']
            and not scene.frame_subframe, 'CONFLICT', 'The requested preview view layer or saved frame differs')
    # Select the exact named scene for authoring and validation without changing
    # the default scene in any saved window or guessing by the active window.
    with bpy.context.temp_override(scene=scene, view_layer=layer):
        text = scene_owner(bpy, ui, bpy.context, request)
        return copy_preview(bpy, ui, scene, text, request, phase, job, default_context)


def copy_preview(bpy, ui, scene, text, request, phase, job, default_context):
    runtime = {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(), 'pid': os.getpid()}
    try:
        if phase == 'verify':
            authored = read_json(job / 'author-result.json')['data']
            # Compare the persisted controls before extraction can synchronize Z.
            require(snapshot(bpy, scene, text) == authored['verification'] and default_context == authored['default_context'],
                    'VALIDATION_FAILED',
                    'The independent reopen changed an object, graph, other scene, or dependency path')
            preview = read_preview(ui, scene, text)
            require(preview == authored['preview'] and preview['save_path'] == request['output'],
                    'VALIDATION_FAILED', 'Saved preview author, final path or native configuration differs on reopen')
            verify_persisted(ui, scene, text, preview)
            return {'runtime': runtime, 'sha256': digest(request['stage'])}
        before = read_preview(ui, scene, text)
        history = []
        pending = before['pending_local_edits']
        if 'region_edits' in request:
            baseline, old_outlines = region_state(bpy, ui, scene, text)
            history = apply_region_edits(bpy, ui, scene, request['region_edits'])
            actual, new_outlines = region_state(bpy, ui, scene, text)
            require(actual == baseline, 'VALIDATION_FAILED', 'Region draft changed unrelated scene data')
            touched = {item['id'] for item in history}
            for identity in old_outlines.keys() & new_outlines.keys():
                fields = ('mesh',) if identity in touched else ('object', 'mesh')
                require(all(old_outlines[identity][field] == new_outlines[identity][field] for field in fields),
                        'VALIDATION_FAILED', 'A protected outline or its topology changed')
            pending = ui.extract_local_edits(scene)
        protected = snapshot(bpy, scene, text)
        save_candidate(bpy, ui, scene, request['stage'], request['output'])
        require(Path(bpy.data.filepath).samefile(request['file']) and not ui._sessions,
                'VALIDATION_FAILED', 'Native action did not remain a saved copy without a live session')
        after = read_preview(ui, scene, text)
        require(snapshot(bpy, scene, text) == protected, 'VALIDATION_FAILED',
                'Saving changed protected scene data or dependency paths')
        expected = {**before, 'save_path': request['output'], 'author_text_sha256': after['author_text_sha256'],
                    'pending_local_edits': pending}
        require(after == expected,
                'VALIDATION_FAILED', 'Native save did not retain the captured author and final destination')
        verify_persisted(ui, scene, text, after)
        result = {'runtime': runtime, 'preview': after, 'sha256': digest(request['stage']), 'verification': protected,
                  'default_context': default_context}
        if history:
            result['region_edits'] = {'state': 'pending_native_apply', 'actions': history,
                                      'before': before['pending_local_edits'], 'after': pending}
        return result
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        if isinstance(error, Failure):
            raise
        raise Failure('CONFLICT', 'Captured preview author state is invalid: ' + str(error)) from error


def worker(phase, params):
    job = Path(params).parent
    try:
        result = {'ok': True, 'data': work(phase, read_json(params), job)}
    except Exception as error:
        result = {'ok': False, 'error': error_payload(error, 'VALIDATION_FAILED')}
    atomic_json(job / (phase + '-result.json'), result)


def run(args):
    deadline = time.monotonic() + timeout_value(args.timeout)
    job = args.jobs_dir.resolve() / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    result_path = job / 'result.json'
    artifacts = {'job_directory': str(job), 'result': str(result_path)}
    data = error = None

    def checkpoint():
        if (job / 'cancel.request').exists():
            raise Failure('CANCELLED', 'Preview copy cancelled before publication')
        require(time.monotonic() < deadline, 'TIMEOUT', 'Preview copy exceeded its timeout')

    def status(state, phase):
        atomic_json(job / 'status.json', {'job_id': job.name, 'state': state, 'phase': phase,
                    'pid': os.getpid(), 'heartbeat_at': time.time()})

    status('running', 'verify_inputs')
    try:
        with ExitStack() as guards:
            request = request_for(args, guards)
            blender = canonical(args.blender)
            if args.output:
                require(args.output.is_absolute() and args.output.suffix.lower() == '.blend',
                        'INVALID_REQUEST', 'Output requires a new absolute .blend path')
                parent = canonical(args.output.parent)
                for path in (parent, *parent.parents):
                    if path != Path(path.anchor):
                        guards.enter_context(FileGuard(path, directory=True))
                output = parent / args.output.name
                guards.enter_context(PathLocks([output]))
                require(not os.path.lexists(output), 'CONFLICT', 'Output already exists')
                with tempfile.NamedTemporaryFile(dir=parent, prefix='.relight-preview-', suffix='.blend', delete=False) as file:
                    stage = Path(file.name)
                guards.callback(stage.unlink, missing_ok=True)
                stage.unlink()
                request['stage'] = str(stage)
                request['output'] = str(output)
            atomic_json(job / 'request.json', {'schema_version': SCHEMA, 'command': COMMAND, 'params': request})
            status('running', 'author')
            result = run_worker(blender, job, 'author', deadline, checkpoint, status, entry=ENTRY)
            if output:
                status('running', 'verify')
                verified = run_worker(blender, job, 'verify', deadline, checkpoint, status, entry=ENTRY)
                require(verified['runtime']['pid'] != result['runtime']['pid'], 'VALIDATION_FAILED', 'Expected independent reopen')
                require(verified['sha256'] == result.pop('sha256'), 'CONFLICT', 'Candidate SHA differs after reopen')
                with FileGuard(stage, renameable=True) as lock:
                    require(lock.sha256(checkpoint) == verified['sha256'], 'CONFLICT', 'Verified candidate changed before publication')
                    checkpoint()
                    lock.rename_new(output)
                proof = result.pop('verification')
                result.update(candidate={'file': str(output), 'sha256': verified['sha256']}, independent_reopen=verified['runtime'],
                              verification={'status': 'passed', 'protected_data_blocks': len(proof['data']),
                                            'dependency_paths': proof['dependencies']})
                artifacts.update(candidate=str(output), independent_reopen=str(job / 'verify-result.json'))
            data = {**result, 'context': request['context'],
                    'source': {'file': request['file'], 'sha256': request['expected_sha256']},
                    'source_unchanged': True, 'product': {'edition': EDITION, **request['release']}}
    except (Failure, OSError, RuntimeError, ValueError) as exc:
        error = error_payload(exc, 'IO_ERROR')
    for path in sorted(job.glob('*.log')):
        artifacts[path.name] = str(path)
    result = envelope(COMMAND, job.name, data, error, artifacts)
    atomic_json(result_path, result)
    terminal = {'TIMEOUT': 'timed_out', 'CANCELLED': 'cancelled'}
    status(terminal.get(error['code'], 'failed') if error else 'succeeded', 'done')
    return result


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    args = None
    try:
        args = parser().parse_args()
        result = run(args)
    except (Failure, OSError, ValueError) as exc:
        result = envelope(command=COMMAND, error=error_payload(exc, 'IO_ERROR'))
    if args and args.human:
        if result['ok']:
            print(result['data']['preview']['scene'])
            if 'candidate' in result['artifacts']:
                print(result['artifacts']['candidate'])
        else:
            print(result['error']['message'])
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['ok'] else CODES[result['error']['code']]


if __name__ == '__main__':
    if '--background' in sys.argv and '--' in sys.argv:
        worker(*sys.argv[sys.argv.index('--') + 1:])
    else:
        raise SystemExit(main())
