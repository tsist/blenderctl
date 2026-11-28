"""Read and edit the frozen Camera Live shot table in a saved Blender copy."""
import argparse
from contextlib import ExitStack
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
sys.path.insert(0, str(ENTRY.parent))

from filesystem import FileGuard, PathLocks
from material_draft_handoff import Parser, canonical, fingerprint, guard_file, read_document, require
from material_projection_handoff import error_payload, run_worker
from protocol import CODES, DEFAULT_BLENDER, DEFAULT_JOBS, SCHEMA, Failure, atomic_json, digest, envelope, read_json, timeout_value


PACKAGE = ENTRY.parents[1] / 'addons/camera_workflow/camera_windows_static'
LIVE_PATH = '_vendor/camera_live_rebuilt'
MANIFEST_SHA = 'fe09180477b9c09bc931e94a03579ebfb5c0fd2d03fa815654fa4c223f4833c5'
LIMIT = 1048574
COMMAND = 'camera.shot-table'
FIELDS = {'rename': 'name', 'camera': 'camera', 'first': 'first', 'last': 'last'}
READ_ONLY = {'inspect', 'export-candidate', 'review-candidate'}
SOURCE_MODES = ('SCENE', 'SEQUENCE')


def parser():
    cli = Parser(description=__doc__)
    cli.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    cli.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS)
    cli.add_argument('--transactions-dir', help=argparse.SUPPRESS)
    cli.add_argument('--timeout', type=float, default=120)
    mode = cli.add_mutually_exclusive_group()
    for flag in ('--human', '--compact', '--full'):
        mode.add_argument(flag, action='store_true')
    actions = cli.add_subparsers(dest='command', required=True, parser_class=Parser)
    command = actions.add_parser('shot-table', description='Read or edit the native Camera Live shot table',
        epilog='Params: file, expected_sha256, scene, operation. Inspect and add take no extra fields. '
               'Remove requires shot and expected={name,camera,first,last}. Rename, camera, first and last also require value. '
               'Move-cut requires left, right, expected_left, expected_right and frame (first frame of the right shot). '
               'source-mode requires expected and value, each SCENE or SEQUENCE, and a new --output .blend. '
               'export-candidate returns artifacts.table_candidate JSON for the standalone table editor. '
               'review-candidate and adopt-candidate take candidate {file,expected_sha256}. Adopt saves a new .blend '
               'and exports a verified baseline for that copy.')
    command.add_argument('--params', type=Path, required=True, help='UTF-8 JSON with an absolute source path')
    command.add_argument('--output', type=Path, help='New absolute .blend for mutations. Omit for read-only operations.')
    return cli


def text_value(value, allow_empty=False):
    return isinstance(value, str) and (allow_empty or bool(value)) and '\0' not in value


def frame_value(value):
    return type(value) is int and -LIMIT <= value <= LIMIT


def row_value(value):
    return (isinstance(value, dict) and set(value) == {'name', 'camera', 'first', 'last'}
            and text_value(value['name'], allow_empty=True)
            and (value['camera'] is None or text_value(value['camera']))
            and frame_value(value['first']) and frame_value(value['last']))


def read_request(args, guards):
    path = guard_file(args.params.absolute(), guards)
    request = read_document(path)
    common = {'file', 'expected_sha256', 'scene', 'operation'}
    require(isinstance(request, dict) and common <= set(request), 'INVALID_REQUEST',
            'Expected file, expected_sha256, scene and operation')
    operation = request['operation']
    require(isinstance(operation, str), 'INVALID_REQUEST', 'operation must be text')
    extras = {'inspect': set(), 'export-candidate': set(), 'review-candidate': {'candidate'},
              'adopt-candidate': {'candidate'}, 'add': set(), 'remove': {'shot', 'expected'},
              'move-cut': {'left', 'right', 'expected_left', 'expected_right', 'frame'},
              'source-mode': {'expected', 'value'}}
    extras.update({name: {'shot', 'expected', 'value'} for name in FIELDS})
    require(operation in extras and set(request) == common | extras[operation], 'INVALID_REQUEST',
            'Unexpected operation or request fields. See shot-table --help.')
    require(text_value(request['scene']) and text_value(request['file']) and isinstance(request['expected_sha256'], str),
            'INVALID_REQUEST', 'Expected a saved file, a SHA-256 and an exact scene name')
    if 'shot' in request:
        require(row_value(request['expected']) and request['shot'] == request['expected']['name'],
                'INVALID_REQUEST', 'shot and its full expected row must name the same entry')
    if operation == 'move-cut':
        require(row_value(request['expected_left']) and row_value(request['expected_right'])
                and request['left'] == request['expected_left']['name'] and request['right'] == request['expected_right']['name']
                and frame_value(request['frame']), 'INVALID_REQUEST', 'Move-cut requires both named rows and a valid right start frame')
    if operation in FIELDS:
        value = request['value']
        valid = {'rename': lambda: text_value(value, allow_empty=True),
                 'camera': lambda: value is None or text_value(value),
                 'first': lambda: frame_value(value), 'last': lambda: frame_value(value)}
        require(valid[operation](), 'INVALID_REQUEST', f'Invalid value for {operation}')
    if operation == 'source-mode':
        require(all(request[key] in SOURCE_MODES for key in ('expected', 'value')),
                'INVALID_REQUEST', 'source-mode requires expected and value to be SCENE or SEQUENCE')
    source = guard_file(request['file'], guards, request['expected_sha256'])
    require(source.suffix.lower() == '.blend', 'INVALID_REQUEST', 'Expected a .blend source')
    require((operation in READ_ONLY) == (args.output is None), 'INVALID_REQUEST',
            'Specify --output for mutations; inspect, export-candidate and review-candidate use the job directory')
    if 'candidate' in request:
        ref = request['candidate']
        require(isinstance(ref, dict) and ref.keys() == {'file', 'expected_sha256'}
                and text_value(ref['file']) and isinstance(ref['expected_sha256'], str),
                'INVALID_REQUEST', 'Supply a candidate JSON file and its expected_sha256')
        path = guard_file(ref['file'], guards, ref['expected_sha256'])
        require(path.suffix.lower() == '.json', 'INVALID_REQUEST', 'Expected a table candidate JSON')
        ref['file'] = str(path)
    request['file'] = str(source)
    return request


def guard_live_package(guards):
    manifest = guard_file(PACKAGE / 'CODE-MANIFEST.json', guards, MANIFEST_SHA)
    product_files = {}
    for name, expected in read_json(manifest)['files'].items():
        if Path(name).parent.as_posix() == LIVE_PATH:
            source = guard_file(PACKAGE / name, guards, expected)
            product_files[source.name] = expected
    require({'__init__.py', 'core.py'} <= set(product_files), 'CONFLICT', 'Frozen Camera Live source is incomplete')
    return {'edition': '0.7.2', 'camera_workflow_edition': '1.0.0', 'source': str(PACKAGE / LIVE_PATH),
            'manifest_sha256': MANIFEST_SHA, 'files': product_files}


def snap_row(item):
    return {'name': item.name, 'camera': item.camera.name if item.camera else None,
            'first': item.first, 'last': item.last}


def locate_row(rows, name, baseline):
    named = [index for index, row in enumerate(rows) if row['name'] == name]
    require(named, 'NOT_FOUND', f'Shot not found: {name}')
    matches = [index for index in named if rows[index] == baseline]
    require(len(matches) == 1, 'CONFLICT', f'Expected a unique shot matching the saved row: {name}')
    return matches[0]


def edit_table(bpy, scene, request):
    operation = request['operation']
    shots = scene.camera_live_shots
    expected = [snap_row(item) for item in shots]
    if operation == 'source-mode':
        require(scene.camera_live_source == request['expected'], 'CONFLICT',
                'Saved shot-table source mode differs from expected')
        scene.camera_live_source = request['value']
        require(scene.camera_live_source == request['value'], 'VALIDATION_FAILED',
                'The requested shot-table source mode did not round-trip')
    elif operation == 'add':
        with bpy.context.temp_override(scene=scene):
            try:
                status = bpy.ops.camera_live_rebuilt.add_shot()
            except RuntimeError as error:
                raise Failure('CONFLICT', str(error)) from error
        require(status == {'FINISHED'} and len(shots) == len(expected) + 1, 'CONFLICT',
                'Native Camera Live refused to add a shot')
        expected.append(snap_row(shots[-1]))
    elif operation == 'move-cut':
        left = locate_row(expected, request['left'], request['expected_left'])
        right = locate_row(expected, request['right'], request['expected_right'])
        frame = request['frame']
        require(right == left + 1, 'CONFLICT', 'Move-cut requires adjacent shots in table order')
        require(expected[left]['first'] < frame <= expected[right]['last'], 'INVALID_REQUEST',
                'The new cut must leave at least one frame in each adjacent shot')
        shots[left].last = expected[left]['last'] = frame - 1
        shots[right].first = expected[right]['first'] = frame
    else:
        index = locate_row(expected, request['shot'], request['expected'])
        if operation == 'remove':
            with bpy.context.temp_override(scene=scene):
                status = bpy.ops.camera_live_rebuilt.remove_shot(index=index)
            require(status == {'FINISHED'}, 'CONFLICT', 'Native Camera Live refused to remove the shot')
            del expected[index]
        else:
            field = FIELDS[operation]
            value = request['value']
            new = value
            if field == 'camera' and value is not None:
                matches = [obj for obj in scene.objects if obj.name == value and obj.type == 'CAMERA']
                require(matches, 'NOT_FOUND', f'Camera not found in the named scene: {value}')
                require(len(matches) == 1, 'CONFLICT', f'Camera name is ambiguous in the named scene: {value}')
                new = matches[0]
            setattr(shots[index], field, new)
            expected[index][field] = value
    require([snap_row(item) for item in shots] == expected, 'VALIDATION_FAILED',
            'A field did not round-trip or an untargeted shot changed')


def table_report(scene, core):
    rows = [snap_row(item) for item in scene.camera_live_shots]
    table = {'rows': rows, 'source_mode': scene.camera_live_source}
    membership = {obj.as_pointer() for obj in scene.objects if obj.type == 'CAMERA'}
    tokens = [item.camera.as_pointer() if item.camera and item.camera.as_pointer() in membership else 0
              for item in scene.camera_live_shots]
    verdict = {'status': 'ready', 'reason': None, 'downstream_eligible': True}
    try:
        core.validate_shots([(row['name'], token, row['first'], row['last']) for row, token in zip(rows, tokens)])
    except core.PreviewError as error:
        verdict = {'status': 'draft', 'reason': str(error), 'downstream_eligible': False}
    return {**table, 'table_sha256': fingerprint(table), 'qualification': verdict,
            'timing': {'fps': scene.render.fps, 'fps_base': scene.render.fps_base,
                       'scene_first': scene.frame_start, 'scene_last': scene.frame_end}}


def native_candidate(bpy, live, scene, path):
    # Use the same guard as the GUI load action. It checks source bytes, scene,
    # camera pointers, the full original table and replayed candidate operations.
    try:
        return live.Candidate(bpy, scene, str(path))
    except (ValueError, RuntimeError, OSError) as exc:
        raise Failure('CONFLICT', f'Camera Live could not load this candidate: {exc}') from exc


def review_document(loaded):
    doc = loaded.document
    return {'name': doc['candidate_name'], 'content_sha256': loaded.document_sha,
            'source_sha256': doc['source_sha256'], 'base_table_sha256': doc['base_table_sha256'],
            'operations': doc['operations'], 'changes': loaded.diff, 'rows': doc['rows']}


def adopt_rows(scene, loaded):
    for item, original, revised in zip(scene.camera_live_shots, loaded.document['base_rows'], loaded.document['rows']):
        if original['camera'] != revised['camera']:
            item.camera = loaded.cameras[revised['camera']]
        for field in ('first', 'last'):
            if original[field] != revised[field]:
                setattr(item, field, revised[field])
    require([snap_row(row) for row in scene.camera_live_shots] == loaded.document['rows'],
            'VALIDATION_FAILED', 'Adopted shots differ from the replayed candidate')


def protected_state(bpy, edited, *, source_mode_only=False):
    import scenes
    from inspection import identity, path_value

    state = scenes.state()
    edited_property = 'camera_live_source' if source_mode_only else 'camera_live_shots'
    for block in state['datablocks']:
        if block['type'] == 'Scene' and block['name'] == edited:
            block['custom_properties'].pop(edited_property, None)
    for info in state['scenes']:
        scene = bpy.data.scenes[info['name']]
        info['subframe'] = scene.frame_subframe
        info['preview'] = [scene.use_preview_range, scene.frame_preview_start, scene.frame_preview_end]
        info['render'] = [scene.render.engine, scene.render.resolution_x, scene.render.resolution_y,
                          scene.render.resolution_percentage]
        info['markers'] = [[marker.name, marker.frame, marker.select, identity(marker.camera) if marker.camera else None]
                           for marker in scene.timeline_markers]
        if scene.name != edited or not source_mode_only:
            info['camera_live_source'] = scene.camera_live_source
        if scene.name != edited or source_mode_only:
            info['rows'] = [snap_row(item) for item in scene.camera_live_shots]
    state['external_paths'] = sorted({path_value(path) for path in bpy.utils.blend_paths(absolute=True, packed=False)})
    return fingerprint(state)


def open_source(bpy, path):
    require(bpy.ops.wm.open_mainfile(filepath=path, load_ui=True, use_scripts=False) == {'FINISHED'}
            and Path(bpy.data.filepath).samefile(path), 'VALIDATION_FAILED', 'The requested saved file could not reopen')
    require(tuple(bpy.app.version[:2]) == tuple(bpy.data.version[:2]), 'UNSUPPORTED',
            'Shot-table handoff requires matching Blender and saved file major/minor versions')


def work(phase, record, job):
    import bpy

    request = record['params']
    flags = {'--factory-startup', '--disable-autoexec', '--offline-mode'}
    require(bpy.app.background and flags.issubset(sys.argv) and not bpy.app.online_access,
            'UNSUPPORTED', 'Camera Live shot-table worker requires offline factory startup')
    bpy.context.preferences.filepaths.use_scripts_auto_execute = False
    sys.pycache_prefix = str(job / (phase + '-no-bytecode'))
    sys.path.insert(0, str((PACKAGE / LIVE_PATH).parent))
    live = importlib.import_module('camera_live_rebuilt')
    require(tuple(live.bl_info['version']) == (0, 7, 2) and Path(live.__file__).parent == PACKAGE / LIVE_PATH,
            'CONFLICT', 'Loaded Camera Live is not the frozen 0.7.2 module')
    live.register()
    try:
        runtime = {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(), 'pid': os.getpid()}
        path = request['stage'] if phase == 'verify' else request['file']
        open_source(bpy, path)
        scene = bpy.data.scenes.get(request['scene'])
        require(scene is not None, 'NOT_FOUND', f'Scene not found: {request["scene"]}')
        before = table_report(scene, live.core)
        if request['operation'] == 'inspect':
            return {'table': before, 'runtime': runtime}
        op = request['operation']
        if op == 'export-candidate':
            require(before['qualification']['downstream_eligible'] and before['source_mode'] == 'SEQUENCE',
                    'CONFLICT', 'Export requires a valid SEQUENCE shot table')
            from camera_live_rebuilt import candidate_model
            path = job / 'table-candidate.json'
            doc = candidate_model.export_snapshot(request['expected_sha256'], scene.name, before['source_mode'],
                before['rows'], sorted(obj.name for obj in scene.objects if obj.type == 'CAMERA'))
            live.save_new_json(str(path), doc)
            loaded = native_candidate(bpy, live, scene, path)
            return {'table': before, 'runtime': runtime, 'review': review_document(loaded),
                    'table_candidate': {'file': str(path), 'sha256': loaded.json_sha}}
        source_mode_only = op == 'source-mode'
        protected = protected_state(bpy, scene.name, source_mode_only=source_mode_only)
        if phase == 'verify':
            checksum = digest(path)
            result = {'table': before, 'protected_sha256': protected, 'sha256': checksum, 'runtime': runtime}
            if op == 'adopt-candidate':
                baseline = job / 'baseline.json'
                snapshot = live.candidate_model.export_snapshot(checksum, scene.name, before['source_mode'],
                    before['rows'], sorted(obj.name for obj in scene.objects if obj.type == 'CAMERA'))
                live.save_new_json(str(baseline), snapshot)
                loaded = native_candidate(bpy, live, scene, baseline)
                require(loaded.document == snapshot and loaded.document['operations'] == [],
                        'VALIDATION_FAILED', 'Reopened baseline did not bind to the copied table')
                result['baseline'] = {'file': str(baseline), 'sha256': loaded.json_sha}
            return result
        review = None
        if op in ('review-candidate', 'adopt-candidate'):
            loaded = native_candidate(bpy, live, scene, request['candidate']['file'])
            review = review_document(loaded)
            if op == 'review-candidate':
                return {'table': before, 'runtime': runtime, 'review': review}
        require(scene.library is None and not scene.override_library, 'CONFLICT', 'Named scene must be a local editable scene')
        if review is not None:
            adopt_rows(scene, loaded)
        else:
            edit_table(bpy, scene, request)
        after = table_report(scene, live.core)
        require(protected_state(bpy, scene.name, source_mode_only=source_mode_only) == protected, 'VALIDATION_FAILED',
                'Content outside the named shot table changed')
        bpy.context.preferences.filepaths.save_version = 0
        stage = request['stage']
        require(bpy.ops.wm.save_as_mainfile(filepath=stage, copy=True, relative_remap=True, check_existing=False)
                == {'FINISHED'}, 'IO_ERROR', 'Failed to save shot-table candidate')
        result = {'table': after, 'before_table_sha256': before['table_sha256'], 'protected_sha256': protected,
                  'sha256': digest(stage), 'runtime': runtime}
        if review is not None:
            result['review'] = review
        return result
    finally:
        live.unregister()


def worker(phase, request_path):
    job = Path(request_path).parent
    try:
        result = {'ok': True, 'data': work(phase, read_json(request_path), job)}
    except Exception as error:
        result = {'ok': False, 'error': error_payload(error, 'VALIDATION_FAILED')}
    atomic_json(job / (phase + '-result.json'), result)


def run(args):
    deadline = time.monotonic() + timeout_value(args.timeout)
    job = args.jobs_dir.resolve() / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    artifacts = {'job_directory': str(job), 'result': str(job / 'result.json')}
    data = error = None

    def checkpoint():
        if (job / 'cancel.request').exists():
            raise Failure('CANCELLED', 'Shot-table operation cancelled before publication')
        if time.monotonic() >= deadline:
            raise Failure('TIMEOUT', 'Shot-table operation exceeded its timeout')

    def status(state, phase):
        atomic_json(job / 'status.json', {'state': state, 'job_id': job.name, 'pid': os.getpid(),
                    'phase': phase, 'heartbeat_at': time.time()})

    status('running', 'verify_inputs')
    try:
        with ExitStack() as guards:
            request = read_request(args, guards)
            product = guard_live_package(guards)
            blender = canonical(args.blender)
            output = None
            if args.output is not None:
                require(args.output.is_absolute() and args.output.suffix.lower() == '.blend',
                        'INVALID_REQUEST', 'Output requires a new absolute .blend path')
                parent = canonical(args.output.parent)
                output = parent / args.output.name
                for directory in (parent, *parent.parents):
                    if directory != Path(directory.anchor):
                        guards.enter_context(FileGuard(directory, directory=True))
                guards.enter_context(PathLocks([output]))
                require(not os.path.lexists(output), 'CONFLICT', f'Output already exists: {output}')
                with tempfile.NamedTemporaryFile(prefix='.camera-shot-table-', suffix='.blend', dir=parent, delete=False) as file:
                    stage = Path(file.name)
                guards.callback(stage.unlink, missing_ok=True)
                request['stage'] = str(stage)
            atomic_json(job / 'request.json', {'schema_version': SCHEMA, 'command': COMMAND, 'params': request})
            phase = 'author' if output else 'inspect'
            status('running', phase)
            authored = run_worker(blender, job, phase, deadline, checkpoint, status, entry=ENTRY)
            data = {key: authored[key] for key in ('table', 'runtime')}
            if 'review' in authored:
                data['review'] = authored['review']
            if 'table_candidate' in authored:
                artifacts['table_candidate'] = authored['table_candidate']
            data.update(operation=request['operation'], scene=request['scene'], product=product, state_source='SAVED_DISK_V1',
                        source={key: request[key] for key in ('file', 'expected_sha256')}, source_unchanged=True)
            if output:
                status('running', 'verify')
                verified = run_worker(blender, job, 'verify', deadline, checkpoint, status, entry=ENTRY)
                require(all(verified[key] == authored[key] for key in ('table', 'protected_sha256', 'sha256'))
                        and verified['runtime']['pid'] != authored['runtime']['pid'], 'VALIDATION_FAILED',
                        'Independent candidate reopen differs from the authored table or protected content')
                if request['operation'] == 'adopt-candidate':
                    baseline = verified['baseline']
                    path = guard_file(baseline['file'], guards, baseline['sha256'])
                    document = read_document(path)
                    require(path == job / 'baseline.json' and document['source_sha256'] == verified['sha256']
                            and document['rows'] == document['base_rows'] == verified['table']['rows'],
                            'VALIDATION_FAILED', 'Cold-reopened baseline differs from the approved copy')
                    data['baseline'] = baseline
                    artifacts['baseline'] = str(path)
                with FileGuard(stage, renameable=True) as locked:
                    require(locked.sha256(checkpoint) == verified['sha256'], 'CONFLICT', 'Staged .blend changed after verification')
                    checkpoint()
                    locked.rename_new(output)
                data.update(candidate={'file': str(output), 'sha256': verified['sha256']},
                            before_table_sha256=authored['before_table_sha256'], protected_sha256=verified['protected_sha256'],
                            independent_reopen={'process': 'fresh_process', **verified['runtime']})
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
    sys.stdout.reconfigure(encoding='utf-8')
    args = None
    try:
        args = parser().parse_args()
        result = run(args)
    except (Failure, OSError, ValueError) as exc:
        result = envelope(command=COMMAND, error=error_payload(exc, 'IO_ERROR'))
    if args and args.human:
        if result['ok']:
            row = result['data']
            print(row['scene'], row['table']['qualification']['status'], len(row['table']['rows']), 'shots')
            if 'candidate' in row:
                print(row['candidate']['file'])
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
