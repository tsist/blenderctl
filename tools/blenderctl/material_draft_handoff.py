"""Read panel-backed material drafts and mesh face selections from saved Blender copies."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from zipfile import ZipFile

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
ROOT = ENTRY.parents[2]
sys.path.insert(0, str(ENTRY.parent))

from host_compat import is_supported_host
from filesystem import FileGuard, PathLocks
from processes import ProcessTree
from protocol import CODES, DEFAULT_BLENDER, DEFAULT_JOBS, Failure, atomic_json, digest, envelope, read_json, timeout_value

EDITIONS = {
    '0.9.2': (((5, 2), (5, 3)), ROOT / 'tools/material_workflow_addon'),
    '0.9.1': (((5, 2),), ROOT / 'runtime/material-workflow/0.9.1/material_workflow_addon'),
    '0.8.0': (((5, 3),), ROOT / 'runtime/blender/profiles/5.3-migration-20261119/extensions/user_default/material_workflow_addon'),
}
EDITION_HELP = 'Select edition 0.9.2 (5.2 or later), 0.9.1 (5.2), or 0.8.0 (5.3 migration profile)'
FROZEN_091 = ROOT / 'runtime/plugin-archives/material_workflow_addon-0.9.1.zip'
FROZEN_091_SHA = 'd87e4bb4b39821770ca1986b79c88bd76094540945cb4f8899127e7be868e1d9'


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure('INVALID_REQUEST', message)


def parser():
    cli = Parser(description=__doc__)
    cli.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    cli.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS)
    cli.add_argument('--transactions-dir', help=argparse.SUPPRESS)
    cli.add_argument('--timeout', type=float, default=120)
    mode = cli.add_mutually_exclusive_group()
    for name in ('--human', '--compact', '--full'):
        mode.add_argument(name, action='store_true')
    actions = cli.add_subparsers(dest='action', required=True, parser_class=Parser)
    for name, help_text in (
        ('read-current', 'Read the saved managed material baseline and report hand edits'),
        ('capture-faces', 'Capture the saved active mesh face selection for exactly one batch assignment'),
    ):
        action = actions.add_parser(name, description=help_text, help=help_text,
            epilog='params: file, expected_sha256, edition, context(scene/view_layer/frame), '
                   'target(object/material_slot/uv_layer); read-current optional expected_material_id; '
                   'capture-faces requires assignment_id and may use an explicit batch file/expected_sha256.')
        action.add_argument('--params', type=Path, required=True, help='UTF-8 JSON with absolute input paths')
        action.add_argument('--output', type=Path, required=True, help='New absolute JSON path')
        if name == 'read-current':
            action.add_argument('--upgrade-v2', action='store_true', help='Explicitly generate a v2 draft from a legacy baseline')
    return cli


def require(condition, code, detail):
    if not condition:
        raise Failure(code, detail)


def canonical(path):
    path = Path(path)
    require(path.is_absolute(), 'INVALID_REQUEST', f'Expected an absolute path: {path}')
    normalized = Path(os.path.abspath(path))
    resolved = path.resolve(strict=True)
    require(normalized == resolved, 'CONFLICT', f'Redirected path: {path}')
    return resolved


def guard_file(path, stack, expected=None):
    path = canonical(path)
    for parent in reversed(path.parents):
        if parent != Path(parent.anchor):
            stack.enter_context(FileGuard(parent, directory=True))
    guard = stack.enter_context(FileGuard(path))
    if expected is not None:
        require(isinstance(expected, str) and re.fullmatch(r'[0-9a-f]{64}', expected),
                'INVALID_REQUEST', f'Expected lowercase SHA-256 for {path}')
        require(guard.sha256() == expected, 'CONFLICT', f'SHA-256 mismatch: {path}')
    return path


def read_document(path):
    require(path.suffix.lower() == '.json' and path.stat().st_size <= 16 * 1024 * 1024,
            'INVALID_REQUEST', f'Expected a JSON file up to 16 MiB: {path}')
    return read_json(path)


def name(value):
    return isinstance(value, str) and 0 < len(value) <= 63 and not any(ord(char) < 32 for char in value)


def prepare(args, stack):
    path = guard_file(args.params.absolute(), stack)
    request = read_document(path)
    require(isinstance(request, dict), 'INVALID_REQUEST', 'Material handoff params must be a JSON object')
    common = {'file', 'expected_sha256', 'edition', 'context', 'target'}
    capturing = args.action == 'capture-faces'
    loading = args.action == 'load-procedural-template'
    creating = args.action == 'create-procedural'
    if args.action == 'edit-layer-draft':
        required = common | {'expected_material_id', 'layer_id', 'operation'}
        allowed = required | {'manifest', 'values', 'image', 'channel', 'engine', 'group', 'parameters'}
    elif args.action == 'update-procedural':
        required = common | {'expected_material_id'}
        allowed = required | {'edits'}
    elif args.action == 'load-layer-template':
        required = common | {'template'}
        allowed = required | {'bindings'}
    elif creating:
        required = common | {'family', 'expected_slot_material'}
        allowed = required | {'material_name', 'mesh_map_size', 'seed', 'weathering'}
        if request.get('family') == 'wood':
            allowed.update(('grain_axis', 'timber_scale'))
        elif request.get('family') == 'painted_metal':
            allowed.add('metal_region')
    elif loading:
        required = common | {'template'}
        allowed = required | {'material_name', 'mesh_map_size'}
    elif capturing:
        required = common | {'assignment_id'}
        allowed = required | {'batch'}
    else:
        required = common
        allowed = required | {'expected_material_id'}
    require(isinstance(request, dict) and required <= set(request) <= allowed,
            'INVALID_REQUEST', 'Unexpected or missing names in material handoff params')
    require(isinstance(request['edition'], str) and request['edition'] in EDITIONS, 'UNSUPPORTED',
            EDITION_HELP)
    context = request['context']
    require(isinstance(context, dict) and set(context) == {'scene', 'view_layer', 'frame'}
            and name(context['scene']) and name(context['view_layer'])
            and type(context['frame']) is int and 1 <= context['frame'] <= 100000,
            'INVALID_REQUEST', 'Expected a named scene, view_layer and frame in 1..100000')
    target = request['target']
    require(isinstance(target, dict) and set(target) == {'object', 'material_slot', 'uv_layer'}
            and name(target['object']) and name(target['uv_layer'])
            and type(target['material_slot']) is int and 0 <= target['material_slot'] <= 63,
            'INVALID_REQUEST', 'Expected a named object, UV layer and material slot index')
    field = 'assignment_id' if capturing else 'expected_material_id'
    if field in request:
        require(name(request[field]), 'INVALID_REQUEST', f'Invalid {field}')
    require(isinstance(request['file'], str) and isinstance(request['expected_sha256'], str),
            'INVALID_REQUEST', 'Expected a saved .blend file and expected_sha256')
    source = guard_file(request['file'], stack, request['expected_sha256'])
    require(source.suffix.lower() == '.blend', 'INVALID_REQUEST', 'Expected a saved .blend file')
    if args.action == 'edit-layer-draft':
        operation = request['operation']
        options = {
            'select': set(), 'set': {'values'}, 'add': set(), 'delete': set(), 'up': set(), 'down': set(),
            'read-channel': {'channel'}, 'assign-image': {'channel', 'image'}, 'remove-image': {'channel'},
            'preview-engine': {'engine'}, 'bind-effect': {'group', 'parameters'}, 'remove-effect': set(),
        }
        require(name(request['layer_id']) and isinstance(operation, str) and operation in options,
                'INVALID_REQUEST', 'Unknown layer operation or invalid layer_id')
        require((set(request) - required - {'manifest'}) == options[operation], 'INVALID_REQUEST',
                f'Operation {operation} requires parameters {sorted(options[operation])}')
        if 'values' in request:
            require(isinstance(request['values'], dict) and 0 < len(request['values']) <= 32,
                    'INVALID_REQUEST', 'set requires a nonempty values object')
        if 'image' in request:
            ref = request['image']
            require(isinstance(ref, dict) and set(ref) == {'file', 'expected_sha256'}
                    and isinstance(ref['file'], str) and isinstance(ref['expected_sha256'], str),
                    'INVALID_REQUEST', 'Image requires file and expected_sha256')
            guard_file(ref['file'], stack, ref['expected_sha256'])
        if 'group' in request:
            require(name(request['group']) and isinstance(request['parameters'], list), 'INVALID_REQUEST',
                    'bind-effect requires the saved group name and parameter array')
        if 'engine' in request:
            require(request['engine'] in ('CYCLES', 'BLENDER_EEVEE'), 'INVALID_REQUEST', 'Unknown preview engine')
        if 'manifest' in request:
            ref = request['manifest']
            require(isinstance(ref, dict) and set(ref) == {'file', 'expected_sha256'}
                    and isinstance(ref['file'], str) and isinstance(ref['expected_sha256'], str),
                    'INVALID_REQUEST', 'Manifest requires file and expected_sha256')
            read_document(guard_file(ref['file'], stack, ref['expected_sha256']))
    if args.action == 'load-layer-template':
        builtin = request['template'] in ('base_overlay', 'single_layer')
        require(builtin or 'bindings' in request, 'INVALID_REQUEST', 'File templates require explicit bindings')
        for key in ('template', 'bindings'):
            if key not in request or key == 'template' and builtin:
                continue
            ref = request[key]
            require(isinstance(ref, dict) and set(ref) == {'file', 'expected_sha256'}
                    and isinstance(ref['file'], str) and isinstance(ref['expected_sha256'], str),
                    'INVALID_REQUEST', f'{key} requires file and expected_sha256')
            file = guard_file(ref['file'], stack, ref['expected_sha256'])
            read_document(file)
            ref['file'] = str(file)
    if loading:
        template = request['template']
        require(isinstance(template, dict) and set(template) == {'file', 'expected_sha256'}
                and isinstance(template['file'], str) and isinstance(template['expected_sha256'], str),
                'INVALID_REQUEST', 'Template descriptor requires file and expected_sha256')
        template_path = guard_file(template['file'], stack, template['expected_sha256'])
        require(template_path.suffix.lower() == '.json' and template_path.stat().st_size <= 8 * 1024 * 1024,
                'INVALID_REQUEST', 'Expected a template JSON up to 8 MiB')
        template['file'] = str(template_path)
    if creating:
        require(request['family'] in ('painted_metal', 'wood', 'glass_clear', 'glass_tinted', 'glass_frosted'),
                'INVALID_REQUEST', 'Unknown procedural family')
        require(request['expected_slot_material'] is None or name(request['expected_slot_material']),
                'INVALID_REQUEST', 'Expected the existing material name, or null for an empty slot')
        require('seed' not in request or type(request['seed']) is int and 0 <= request['seed'] <= 100000,
                'INVALID_REQUEST', 'seed must be an integer in 0..100000')
        require('weathering' not in request or type(request['weathering']) in (int, float) and 0 <= request['weathering'] <= 1,
                'INVALID_REQUEST', 'weathering must be in 0..1')
        require(request.get('grain_axis', 'X') in ('X', 'Y', 'Z') and
                request.get('metal_region', 'lower') in ('lower', 'edges', 'seam'),
                'INVALID_REQUEST', 'Unknown grain_axis or metal_region')
        if 'timber_scale' in request:
            scale = request['timber_scale']
            require(isinstance(scale, list) and len(scale) == 3 and
                    all(type(v) in (int, float) and 1e-6 <= v <= 3.402823466e38 for v in scale),
                    'INVALID_REQUEST', 'timber_scale requires three positive RNA float values of at least 1e-6')
    if loading or creating:
        require('material_name' not in request or name(request['material_name']),
                'INVALID_REQUEST', 'Invalid material_name')
        require('mesh_map_size' not in request or
                (type(request['mesh_map_size']) is int and request['mesh_map_size'] in (128, 256, 512, 1024, 2048)),
                'INVALID_REQUEST', 'mesh_map_size must be one of 128, 256, 512, 1024, 2048')
    if 'batch' in request:
        row = request['batch']
        require(isinstance(row, dict) and set(row) == {'file', 'expected_sha256'}
                and isinstance(row['file'], str) and isinstance(row['expected_sha256'], str),
                'INVALID_REQUEST', 'Batch descriptor requires file and expected_sha256')
        batch = guard_file(row['file'], stack, row['expected_sha256'])
        request['batch'] = read_document(batch)
        request['batch_origin'] = {'file': str(batch), 'sha256': row['expected_sha256']}
    request.update(file=str(source), action=args.action, upgrade_v2=getattr(args, 'upgrade_v2', False))
    return request


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def field_differences(saved, live):
    """Report bounded managed node fields. Structure and links are separate sections."""
    def flatten(fields):
        result = {}
        for node, values in fields.items():
            for field, value in values.items():
                if isinstance(value, dict):
                    for leaf, child in value.items():
                        result[node, field, leaf] = child
                else:
                    result[node, field] = value
        return result

    def show(fields, key):
        if key not in fields:
            return {'present': False}
        value = fields[key]
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(encoded) > 1024:
            return {'present': True, 'truncated': True, 'bytes': len(encoded),
                    'sha256': hashlib.sha256(encoded).hexdigest()}
        return {'present': True, 'value': value}

    previous, current = flatten(saved), flatten(live)
    missing = object()
    changed = [key for key in sorted(previous.keys() | current.keys())
               if previous.get(key, missing) != current.get(key, missing)]
    entries = [{'node': key[0], 'field': list(key[1:]), 'baseline': show(previous, key), 'live': show(current, key)}
               for key in changed[:64]]
    return {'count': len(changed), 'truncated': len(changed) > len(entries), 'entries': entries}


def run_operator(bpy, operator, scene, **kwargs):
    try:
        status = getattr(bpy.ops.material_workflow, operator)(**kwargs)
    except RuntimeError as exc:
        raise Failure('VALIDATION_FAILED', scene.mw_last_error or str(exc)) from exc
    require(status == {'FINISHED'}, 'VALIDATION_FAILED', f'{operator}: {scene.mw_last_error or status}')


def edition_directory(edition):
    directory = EDITIONS[edition][1]
    if edition != '0.9.1':
        return directory
    with ExitStack() as guards:
        archive = guard_file(FROZEN_091, guards, FROZEN_091_SHA)
        with ZipFile(archive) as frozen:
            names = frozen.namelist()
            require(len(names) == len({name.casefold() for name in names}) == 28
                    and all(name not in ('.', '..') and re.fullmatch(r'[A-Za-z0-9_.-]+', name) for name in names),
                    'CONFLICT', 'Unexpected frozen Material Workflow package members')
            members = {name: frozen.read(name) for name in names}
    if not os.path.lexists(directory):
        for parent in (directory.parent.parent, directory.parent):
            parent.mkdir(exist_ok=True)
            canonical(parent)
        with tempfile.TemporaryDirectory(prefix='.material-edition-', dir=directory.parent) as scratch:
            candidate = Path(scratch) / directory.name
            candidate.mkdir()
            for name, content in members.items():
                (candidate / name).write_bytes(content)
            try:
                candidate.rename(directory)  # Windows directory rename is atomic and does not replace a winner.
            except FileExistsError:
                pass
    canonical(directory)
    require({entry.name for entry in directory.iterdir()} == members.keys(),
            'CONFLICT', 'Frozen Material Workflow cache membership differs from 0.9.1')
    for name, content in members.items():
        entry = canonical(directory / name)
        require(entry.is_file() and entry.read_bytes() == content, 'CONFLICT',
                f'Frozen Material Workflow 0.9.1 cache differs: {name}')
    return directory


def supports_edition(edition, version):
    if edition == '0.9.2' and is_supported_host(version):
        return True
    return edition in EDITIONS and tuple(version[:2]) in EDITIONS[edition][0]


def select_product(request, bpy):
    edition = request['edition']
    require(supports_edition(edition, bpy.app.version), 'UNSUPPORTED',
            f'Edition {edition} does not support this Blender host. {EDITION_HELP}')
    directory = edition_directory(edition)
    sys.path.insert(0, str(directory.parent))
    import material_workflow_addon as product
    require(Path(product.__file__).resolve().parent == directory.resolve()
            and '.'.join(map(str, product.bl_info['version'])) == request['edition'],
            'CONFLICT', 'Material Workflow edition or source directory differs')
    names = ('__init__.py', 'ui.py', 'ui_batch.py', 'core.py', 'core_v2.py', 'contract.py', 'batch_contract.py')
    return {'edition': request['edition'], 'directory': str(directory),
            'files': {filename: digest(directory / filename) for filename in names}}


def open_saved_context(bpy, request, *, allow_subframe=False):
    require(bpy.ops.wm.open_mainfile(filepath=request['file'], load_ui=True, use_scripts=False) == {'FINISHED'}
            and Path(bpy.data.filepath).samefile(request['file']), 'VALIDATION_FAILED', 'Saved source could not reopen')
    require(tuple(bpy.data.version[:2]) == tuple(bpy.app.version[:2]),
            'UNSUPPORTED', 'Saved .blend and Blender must share major/minor version')
    context = bpy.context
    actual = {'scene': context.scene.name, 'view_layer': context.view_layer.name, 'frame': context.scene.frame_current}
    expected = request['context']
    if allow_subframe:
        actual['subframe'] = context.scene.frame_subframe
        # Compare to the precision of Scene.frame_subframe RNA, without seeking.
        value, = struct.unpack('f', struct.pack('f', expected['subframe']))
        matches = actual == {**expected, 'subframe': value}
    else:
        matches = actual == expected and not context.scene.frame_subframe
    require(matches, 'CONFLICT', f'Saved scene, view layer or frame differs: {actual}')
    return context


def active_target(bpy, request):
    context = open_saved_context(bpy, request)
    obj = context.object
    target = request['target']
    require(obj is not None and obj.name == target['object'] and obj.type == 'MESH',
            'CONFLICT', f'Expected saved active mesh: {target["object"]}')
    require(obj.library is None and obj.override_library is None and obj.data.library is None
            and target['uv_layer'] in obj.data.uv_layers, 'CONFLICT', 'Expected local mesh with the named UV layer')
    return context, obj


def read_current(bpy, context, obj, request, guards):
    from material_workflow_addon import core, core_v2, ui

    require(obj.active_material_index == request['target']['material_slot'] and obj.active_material is not None,
            'CONFLICT', 'Saved active material slot differs or has no material')
    material = obj.active_material
    require(material.library is None and not material.override_library, 'CONFLICT', 'Expected local managed material')
    baseline = core_v2.get_stored_manifest(material)
    identity = baseline['material']['id']
    require(baseline['target'] == request['target'] and baseline['context'] == request['context']
            and material.get(core.KEY) == identity and request.get('expected_material_id', identity) == identity,
            'CONFLICT', 'Stored managed material, target or context identity differs')
    existing = set()
    for node in core._nodes(material).values():
        image = node.image if node.type == 'TEX_IMAGE' else None
        if image and image.source == 'FILE':
            path = bpy.path.abspath(image.filepath, library=image.library)
            if path and path not in existing:
                guard_file(path, guards)
                existing.add(path)
    engine = core_v2 if core_v2.STATE in material else core
    state = engine.verify_managed_state(material)
    saved_fields = json.loads(material[engine.STATE])['snapshot']['fields']
    differences = field_differences(saved_fields, state['state']['fields'])
    scene = context.scene
    run_operator(bpy, 'load_current', scene)
    require(ui._manifest(scene) == baseline, 'VALIDATION_FAILED', 'Panel returned a different request baseline')
    notice = scene.mw_manual_notice
    if request['upgrade_v2']:
        require(baseline['schema_version'] == '1.0', 'INVALID_REQUEST', 'Explicit upgrade requires a v1 material baseline')
        run_operator(bpy, 'upgrade', scene)
        require(scene.mw_upgrade_authorized and ui._manifest(scene)['schema_version'] == '1.1',
                'VALIDATION_FAILED', 'The panel did not authorize its v2 draft')
    return ui._manifest(scene), {'material_id': identity, 'material': material.name,
            'managed_state_ok': state['ok'], 'manual_conflicts': state['conflicts'], 'manual_notice': notice,
            'managed_field_differences': differences,
            'live_state_sha256': fingerprint(state['state']), 'saved_baseline_sha256': fingerprint(baseline),
            'explicit_v2_draft': request['upgrade_v2'], 'consumer': 'material run'}


def capture_faces(bpy, context, obj, request):
    from copy import deepcopy
    from material_workflow_addon import ui_batch

    scene = context.scene
    if 'batch' in request:
        ui_batch._store(scene, request['batch'])
    baseline = ui_batch.get_manifest(scene)
    ui_batch.validate_context(context, baseline)
    match = [(index, row) for index, row in enumerate(baseline['assignments']) if row['id'] == request['assignment_id']]
    require(len(match) == 1 and match[0][1]['target'] == request['target'], 'CONFLICT', 'Assignment identity or target differs')
    index, row = match[0]
    run_operator(bpy, 'batch_select', scene, index=index)
    mode = obj.mode
    run_operator(bpy, 'batch_faces', scene)
    faces = json.loads(scene.mw_batch_faces)
    if mode == 'EDIT':
        obj.update_from_editmode()
    mesh = obj.data
    require(faces == [face.index for face in mesh.polygons if face.select],
            'VALIDATION_FAILED', 'Panel output differs from saved face selection')
    run_operator(bpy, 'batch_target', scene)
    result = ui_batch.get_manifest(scene)
    expected = deepcopy(baseline)
    expected['assignments'][index]['faces'] = faces
    require(result == expected, 'VALIDATION_FAILED', 'Capture changed other batch draft content')
    return result, {'assignment_id': row['id'], 'faces': faces,
            'selection_source': 'SAVED_EDIT_MESH' if mode == 'EDIT' else 'SAVED_MESH_POLYGONS',
            'saved_object_mode': mode, 'mesh': mesh.name, 'mesh_users': mesh.users,
            'face_topology_sha256': fingerprint([len(mesh.vertices), [list(face.vertices) for face in mesh.polygons]]),
            'batch_origin': request.get('batch_origin', 'saved scene batch draft'), 'consumer': 'material batch'}


def work(request):
    import bpy

    flags = {'--factory-startup', '--disable-autoexec', '--offline-mode'}
    require(bpy.app.background and flags.issubset(sys.argv) and not bpy.app.online_access
            and not bpy.context.preferences.filepaths.use_scripts_auto_execute,
            'UNSUPPORTED', 'Expected an isolated offline Blender worker with automatic scripts disabled')
    product = select_product(request, bpy)
    from material_workflow_addon import ui, ui_batch
    from material_workflow_addon.contract import image_documents

    ui.register()
    ui_batch.register()
    context, obj = active_target(bpy, request)
    with ExitStack() as guards:
        if request['action'] == 'read-current':
            manifest, details = read_current(bpy, context, obj, request, guards)
            image_inputs = image_documents(manifest)
        else:
            manifest, details = capture_faces(bpy, context, obj, request)
            image_inputs = [item for row in manifest['assignments'] for item in image_documents(row)]
        resources = {}
        for row in image_inputs:
            if row['file'] not in resources:
                guard_file(row['file'], guards, row['expected_sha256'])
                resources[row['file']] = row['expected_sha256']
    return {'manifest': manifest, 'details': details, 'product': product,
            'resources': [{'file': path, 'expected_sha256': sha} for path, sha in sorted(resources.items())],
            'runtime': {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(),
                        'pid': os.getpid(), 'process': 'cold_open_saved_source'}}


def failure(error, fallback='VALIDATION_FAILED'):
    code = getattr(error, 'code', 'NOT_FOUND' if isinstance(error, FileNotFoundError) else fallback)
    row = {'code': code if code in CODES else fallback, 'message': str(error)}
    if code not in CODES:
        row['product_code'] = code
    return row


def worker(request_path, result_path):
    try:
        payload = {'ok': True, 'data': work(read_json(request_path))}
    except Exception as error:
        payload = {'ok': False, 'error': failure(error)}
    atomic_json(result_path, payload)


def execute_worker(args, job, checkpoint):
    blender = canonical(args.blender)
    profile = job / 'profile'
    profile.mkdir()
    environment = dict(os.environ)
    environment['BLENDER_USER_RESOURCES'] = str(profile)
    for tag in ('CONFIG', 'EXTENSIONS', 'DATAFILES', 'SCRIPTS'):
        environment['BLENDER_USER_' + tag] = str(profile / tag.lower())
    result_path = job / 'worker-result.json'
    argv = [str(blender), '--background', '--factory-startup', '--offline-mode', '--disable-autoexec',
            '--threads', '2', '--python-exit-code', '23', '--python', str(ENTRY), '--',
            str(job / 'request.json'), str(result_path)]
    with (job / 'worker.log').open('xb') as log:
        tree = ProcessTree(argv, cwd=str(job), env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            while True:
                checkpoint()
                try:
                    code = tree.process.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            tree.terminate()
            raise
        finally:
            tree.close()
    require(code == 0 and result_path.is_file(), 'WORKER_FAILED', f'Worker exited {code}; see {log.name}')
    result = read_json(result_path)
    if not result['ok']:
        raise Failure(result['error']['code'], result['error']['message'])
    return result['data']


def run(args):
    job = args.jobs_dir.absolute() / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    command = 'material.' + args.action
    deadline = time.monotonic() + timeout_value(args.timeout)
    data = error = None
    artifacts = {'job_directory': str(job), 'result': str(job / 'result.json')}

    def checkpoint():
        require(not (job / 'cancel.request').exists(), 'CANCELLED', 'Material draft handoff cancelled')
        require(time.monotonic() < deadline, 'TIMEOUT', 'Material draft handoff timed out')

    try:
        require(args.output.is_absolute() and args.output.suffix.lower() == '.json', 'INVALID_REQUEST',
                'Output requires an absolute .json path')
        with ExitStack() as stack:
            request = prepare(args, stack)
            parent = canonical(args.output.parent)
            output = parent / args.output.name
            stack.enter_context(FileGuard(parent, directory=True))
            stack.enter_context(PathLocks([output]))
            require(not os.path.lexists(output), 'CONFLICT', f'Output already exists: {output}')
            atomic_json(job / 'request.json', request)
            result = execute_worker(args, job, checkpoint)
            with tempfile.TemporaryDirectory(prefix='.material-draft-', dir=parent) as directory:
                staged = Path(directory) / 'candidate.json'
                atomic_json(staged, result.pop('manifest'))
                with FileGuard(staged, renameable=True) as guard:
                    sha = guard.sha256(checkpoint)
                    guard.rename_new(output)
            artifacts.update(output=str(output), worker_result=str(job / 'worker-result.json'))
            data = {**result, 'output': {'file': str(output), 'sha256': sha},
                    'source': {'file': request['file'], 'expected_sha256': request['expected_sha256']},
                    'context': request['context'], 'target': request['target'], 'source_unchanged': True}
    except (Failure, ValueError, OSError, RuntimeError) as exc:
        error = failure(exc, 'IO_ERROR')
    if (job / 'worker.log').exists():
        artifacts['worker_log'] = str(job / 'worker.log')
    document = envelope(command, job.name, data, error, artifacts)
    atomic_json(job / 'result.json', document)
    return document


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    args = None
    try:
        args = parser().parse_args()
        result = run(args)
    except (Failure, OSError) as error:
        result = envelope(error=failure(error, 'IO_ERROR'))
    if args and args.human:
        print(result['artifacts']['output'] if result['ok'] else result['error']['message'])
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['ok'] else CODES[result['error']['code']]


if __name__ == '__main__':
    if '--background' in sys.argv and '--' in sys.argv:
        worker(*(Path(arg) for arg in sys.argv[sys.argv.index('--') + 1:]))
    else:
        raise SystemExit(main())
