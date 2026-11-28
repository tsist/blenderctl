"""Read or author explicit point pins through the native Physics Workbench operator."""
import argparse
from contextlib import ExitStack
import importlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
import tomllib
import uuid

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
sys.path.insert(0, str(ENTRY.parent))

from filesystem import FileGuard, PathLocks
from material_draft_handoff import Parser, canonical, fingerprint, guard_file, read_document, require
from material_projection_handoff import error_payload, run_worker
from protocol import CODES, DEFAULT_BLENDER, DEFAULT_JOBS, SCHEMA, Failure, atomic_json, digest, envelope, read_json, timeout_value


ROOT = ENTRY.parents[2]
PACKAGE = 'physics_nodes_workflow_addon'
PIN = 'pnw_pinned'
COMMAND = 'physics.pin-selection'
EDITION = '1.0.0-rc.1'
ADDON_DIR = ROOT / 'tools' / PACKAGE


def parser():
    cli = Parser(description=__doc__)
    cli.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    cli.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS)
    cli.add_argument('--transactions-dir', help=argparse.SUPPRESS)
    cli.add_argument('--timeout', type=float, default=120)
    mode = cli.add_mutually_exclusive_group()
    for flag in ('--compact', '--full', '--human'):
        mode.add_argument(flag, action='store_true')
    subcommands = cli.add_subparsers(dest='command', required=True, parser_class=Parser)
    action = subcommands.add_parser('pin-selection', description=__doc__,
        epilog='Params: file, expected_sha256, scene, view_layer, object, operation. For pin or unpin, include '
               'vertices (unique zero-based indices) and give a new --output .blend. Omit both for inspect. '
               'Object Mode isolates shared mesh data. Edit Mode uses the saved active single-user mesh.')
    action.add_argument('--params', type=Path, required=True, help='UTF-8 JSON describing the saved mesh')
    action.add_argument('--output', type=Path, help='New absolute .blend for pin and unpin')
    return cli


def read_request(args, guards):
    request = read_document(guard_file(args.params.absolute(), guards))
    common = {'file', 'expected_sha256', 'scene', 'view_layer', 'object', 'operation'}
    require(isinstance(request, dict) and common <= set(request), 'INVALID_REQUEST', 'Missing saved mesh pin fields')
    action = request['operation']
    require(action in ('inspect', 'pin', 'unpin') and set(request) == (common if action == 'inspect' else common | {'vertices'}),
            'INVALID_REQUEST', 'Unexpected pin operation or request fields')
    require(all(isinstance(request[key], str) and request[key] and '\0' not in request[key] for key in common),
            'INVALID_REQUEST', 'Expected a saved file, scene, view layer and target name')
    if action != 'inspect':
        vertices = request['vertices']
        require(isinstance(vertices, list) and vertices and all(type(i) is int and i >= 0 for i in vertices)
                and len(set(vertices)) == len(vertices), 'INVALID_REQUEST', 'Expected nonempty unique zero-based vertex indices')
        request['vertices'] = sorted(vertices)
    require((action == 'inspect') == (args.output is None), 'INVALID_REQUEST', 'Provide --output for pin or unpin, and omit it for inspect')
    path = guard_file(request['file'], guards, request['expected_sha256'])
    require(path.suffix.lower() == '.blend', 'INVALID_REQUEST', 'Expected a saved .blend file')
    request['file'] = str(path)
    return request


def guard_products(guards):
    manifest = guard_file(ADDON_DIR / 'blender_manifest.toml', guards)
    with manifest.open('rb') as stream:
        require(tomllib.load(stream)['version'] == EDITION, 'CONFLICT', f'Unexpected physics package edition at {ADDON_DIR}')
    files = {'blender_manifest.toml': digest(manifest)}
    for path in sorted(ADDON_DIR.glob('*.py')):
        source = guards.enter_context(FileGuard(canonical(path)))
        files[path.name] = source.sha256()
    require({'__init__.py', 'ui.py'} <= files.keys(), 'CONFLICT', f'Physics package modules missing at {ADDON_DIR}')
    return {EDITION: {'edition': EDITION, 'directory': str(ADDON_DIR), 'files': files}}


def load_product(bpy, job, products):
    require(tuple(bpy.app.version) >= (5, 3, 0), 'UNSUPPORTED',
            'Physics pin-selection requires Blender 5.3.0 or later')
    edition, directory = EDITION, ADDON_DIR
    sys.pycache_prefix = str(job / 'uncached-modules')
    sys.path.insert(0, str(directory.parent))
    ui = importlib.import_module(PACKAGE + '.ui')
    require(Path(ui.__file__).parent == directory and digest(Path(ui.__file__)) == products[edition]['files']['ui.py'],
            'CONFLICT', 'Loaded pin operator is not the selected Physics Workbench package')
    return ui.PNW_OT_pin_selection, products[edition]


def open_target(bpy, path, request):
    require(bpy.ops.wm.open_mainfile(filepath=path, load_ui=True, use_scripts=False) == {'FINISHED'}
            and Path(bpy.data.filepath).samefile(path), 'VALIDATION_FAILED', 'Could not reopen the saved .blend')
    require(tuple(bpy.data.version[:2]) == tuple(bpy.app.version[:2]), 'UNSUPPORTED', 'Saved .blend requires its matching Blender major/minor')
    context = bpy.context
    require(context.scene.name == request['scene'] and context.view_layer.name == request['view_layer'],
            'CONFLICT', 'Saved active scene or view layer differs')
    obj = context.view_layer.objects.get(request['object'])
    require(obj is not None and obj.type == 'MESH', 'NOT_FOUND', 'Named mesh absent from the saved view layer')
    require(not (obj.library or obj.override_library or obj.data.library or obj.data.override_library),
            'CONFLICT', 'Pin authoring requires a local object and local mesh')
    require(context.mode == 'OBJECT' or (context.mode == 'EDIT_MESH' and context.active_object == obj and obj.mode == 'EDIT'),
            'UNSUPPORTED', 'Use Object Mode or save the named mesh as the active object in Edit Mode')
    return obj


def edit_domains(mesh):
    import bmesh
    bm = bmesh.from_edit_mesh(mesh)
    domains = {'VERT': bm.verts, 'EDGE': bm.edges, 'FACE': bm.faces}
    for elements in domains.values():
        elements.ensure_lookup_table()
        elements.index_update()
    return bm, domains


def selection(obj):
    mesh = obj.data
    if obj.mode == 'EDIT':
        import bmesh
        bm, domains = edit_domains(mesh)
        kinds = {bmesh.types.BMVert: 'VERT', bmesh.types.BMEdge: 'EDGE', bmesh.types.BMFace: 'FACE'}
        return {'selected': {kind: [i for i, item in enumerate(elements) if item.select] for kind, elements in domains.items()},
                'hidden': {kind: [i for i, item in enumerate(elements) if item.hide] for kind, elements in domains.items()},
                'history': [[kinds[type(item)], item.index] for item in bm.select_history],
                'active_face': bm.faces.active.index if bm.faces.active else None}
    domains = {'VERT': mesh.vertices, 'EDGE': mesh.edges, 'FACE': mesh.polygons}
    return {'selected': {kind: [item.index for item in elements if item.select] for kind, elements in domains.items()},
            'hidden': {kind: [item.index for item in elements if item.hide] for kind, elements in domains.items()}}


def pin_values(obj):
    mesh = obj.data
    attr = mesh.attributes.get(PIN)
    require(not attr or (attr.domain == 'POINT' and attr.data_type == 'FLOAT'), 'CONFLICT', f'{PIN} must be FLOAT on POINT domain')
    if obj.mode == 'EDIT':
        bm, _ = edit_domains(mesh)
        layer = bm.verts.layers.float.get(PIN)
        result = [float(vertex[layer]) for vertex in bm.verts] if layer else None
        return len(bm.verts), result
    return len(mesh.vertices), [row.value for row in attr.data] if attr else None


def restore_edit(mesh, before):
    import bmesh
    bm, domains = edit_domains(mesh)
    for kind, elements in domains.items():
        selected = set(before['selected'][kind])
        for index, item in enumerate(elements):
            item.select = index in selected
    bm.select_history.clear()
    for kind, index in before['history']:
        bm.select_history.add(domains[kind][index])
    face = before['active_face']
    bm.faces.active = bm.faces[face] if face is not None else None
    bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)


def invoke_native(bpy, obj, indices, unpin, cls, before):
    wanted = set(indices)
    mesh = obj.data
    edit = obj.mode == 'EDIT'
    require(not edit or mesh.users == 1, 'CONFLICT', 'Make shared mesh data single-user before editing pins in Edit Mode')
    bpy.utils.register_class(cls)
    try:
        if edit:
            _, domains = edit_domains(mesh)
            vertices = domains['VERT']
        else:
            vertices = mesh.vertices
        for index, vertex in enumerate(vertices):
            vertex.select = index in wanted
        with bpy.context.temp_override(active_object=obj, object=obj):
            status = bpy.ops.pnw.pin_selection(unpin=unpin)
        require(status == {'FINISHED'}, 'CONFLICT', 'Native pin operator refused the target')
    finally:
        try:
            if edit:
                restore_edit(mesh, before)
            else:
                selected = set(before['selected']['VERT'])
                for data in (mesh, obj.data) if mesh != obj.data else (mesh,):
                    for vertex in data.vertices:
                        vertex.select = vertex.index in selected
                    data.update()
        finally:
            bpy.utils.unregister_class(cls)


def protected_state(bpy, obj, detach, original_mesh):
    from inspection import path_value
    import scenes

    state = scenes.state()
    mesh_name = obj.data.name
    target_content = None
    blocks = []
    for block in state['datablocks']:
        if block['type'] == 'Mesh' and block['name'] == mesh_name and block['library'] is None:
            if not detach or mesh_name != original_mesh:
                target_content = block
                continue
            target_content = {**block, 'mesh': dict(block['mesh'])}
        if block['type'] == 'Object' and block['name'] == obj.name and block['library'] is None and detach:
            block['object'].pop('data')
        blocks.append(block)
    require(target_content is not None, 'VALIDATION_FAILED', 'Target mesh content is absent from the snapshot')
    target_content.pop('name')
    target_content['mesh']['attributes'] = [row for row in target_content['mesh']['attributes'] if row['name'] != PIN]
    state['datablocks'] = blocks
    state['pins_target_mesh'] = target_content
    state['pins_target_uv'] = [[layer.name, layer.active_render, layer == obj.data.uv_layers.active] for layer in obj.data.uv_layers]
    state['pins_target_selection'] = selection(obj)
    state['pins_tool_mode'] = list(bpy.context.tool_settings.mesh_select_mode)
    state['external_paths'] = sorted({path_value(path) for path in bpy.utils.blend_paths(absolute=True, packed=False)})
    return state


def pin_summary(obj, size, values):
    return {'object': obj.name, 'mesh': obj.data.name, 'mode': obj.mode, 'vertex_count': size,
            'attribute': PIN, 'present': values is not None, 'domain': 'POINT', 'data_type': 'FLOAT',
            'values_sha256': fingerprint(values), 'pinned_point_count': sum(value > .5 for value in values or ())}


def work(phase, request_path):
    import bpy
    import scenes

    record = read_json(request_path)
    request = record['params']
    job = request_path.parent
    require(bpy.app.background and not bpy.app.online_access, 'UNSUPPORTED', 'Pin authoring expects offline background Blender')
    bpy.context.preferences.filepaths.use_scripts_auto_execute = False
    cls, product = load_product(bpy, job, record['products'])
    runtime = {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(), 'pid': os.getpid()}
    path = request['stage'] if phase == 'verify' else request['file']
    obj = open_target(bpy, path, request)
    size, existing = pin_values(obj)
    require(existing is None or all(math.isfinite(value) for value in existing), 'CONFLICT', 'Existing pin values must be finite')
    observed = pin_summary(obj, size, existing)
    result = {'pin': observed, 'product': product, 'runtime': runtime}
    if phase == 'inspect':
        return result
    if phase == 'verify':
        authored = read_json(job / 'author-result.json')['data']
        state = protected_state(bpy, obj, authored['isolated_shared_mesh'], authored['source_mesh'])
        return {**result, 'protected_sha256': fingerprint(state), 'sha256': digest(Path(path))}

    vertices = request['vertices']
    require(vertices[-1] < size, 'INVALID_REQUEST', f'Vertex index exceeds saved mesh range 0..{size - 1}')
    old_mesh = obj.data
    source_name = old_mesh.name
    detach = obj.mode == 'OBJECT' and old_mesh.users > 1
    original = protected_state(bpy, obj, detach, source_name)
    expected = list(existing) if existing is not None else [0.] * size
    value = 0. if request['operation'] == 'unpin' else 1.
    changed = [index for index in vertices if expected[index] != value]
    for index in vertices:
        expected[index] = value
    invoke_native(bpy, obj, vertices, value == 0., cls, original['pins_target_selection'])
    require((obj.data != old_mesh) == detach, 'VALIDATION_FAILED', 'Native mesh sharing behavior differs')
    after_size, actual = pin_values(obj)
    require(after_size == size and actual == expected, 'VALIDATION_FAILED', 'Point pin changes differ from explicit vertex indices')
    state = protected_state(bpy, obj, detach, source_name)
    mismatch = scenes.compare(original, state)
    require(mismatch is None, 'VALIDATION_FAILED', f'Protected scene or mesh state differs at {mismatch}')
    bpy.context.preferences.filepaths.save_version = 0
    stage = Path(request['stage'])
    require(bpy.ops.wm.save_as_mainfile(filepath=str(stage), copy=True, relative_remap=True, check_existing=False) == {'FINISHED'},
            'IO_ERROR', 'Could not save the pin candidate')
    return {**result, 'pin': pin_summary(obj, size, actual), 'pin_before': observed, 'vertices': vertices,
            'changed_vertices': changed, 'source_mesh': source_name, 'isolated_shared_mesh': detach,
            'protected_sha256': fingerprint(state), 'sha256': digest(stage)}


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
            raise Failure('CANCELLED', 'Pin operation cancelled before publication')
        if time.monotonic() >= deadline:
            raise Failure('TIMEOUT', 'Pin operation exceeded its timeout')

    def status(state, phase):
        atomic_json(job / 'status.json', {'state': state, 'job_id': job.name, 'pid': os.getpid(),
                    'phase': phase, 'heartbeat_at': time.time()})

    status('running', 'verify_inputs')
    try:
        with ExitStack() as guards:
            request = read_request(args, guards)
            products = guard_products(guards)
            blender = canonical(args.blender)
            output = None
            if args.output is not None:
                require(args.output.is_absolute() and args.output.suffix.lower() == '.blend',
                        'INVALID_REQUEST', 'Output needs a new absolute .blend path')
                parent = canonical(args.output.parent)
                output = parent / args.output.name
                for directory in (parent, *parent.parents):
                    if directory != Path(directory.anchor):
                        guards.enter_context(FileGuard(directory, directory=True))
                guards.enter_context(PathLocks([output]))
                require(not os.path.lexists(output), 'CONFLICT', f'Output already exists: {output}')
                with tempfile.NamedTemporaryFile(prefix='.physics-pin-', suffix='.blend', dir=parent, delete=False) as file:
                    stage = Path(file.name)
                guards.callback(stage.unlink, missing_ok=True)
                request['stage'] = str(stage)
            atomic_json(job / 'request.json', {'schema_version': SCHEMA, 'command': COMMAND, 'params': request, 'products': products})
            phase = 'author' if output else 'inspect'
            status('running', phase)
            authored = run_worker(blender, job, phase, deadline, checkpoint, status, entry=ENTRY)
            data = {**authored, 'operation': request['operation'], 'scene': request['scene'], 'view_layer': request['view_layer'],
                    'source': {key: request[key] for key in ('file', 'expected_sha256')}, 'source_unchanged': True}
            if output:
                status('running', 'verify')
                verified = run_worker(blender, job, 'verify', deadline, checkpoint, status, entry=ENTRY)
                require(all(verified[key] == authored[key] for key in ('pin', 'protected_sha256', 'sha256', 'product'))
                        and verified['runtime']['pid'] != authored['runtime']['pid'], 'VALIDATION_FAILED',
                        'Independent reopen differs from the authored pin or protected scene state')
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
    sys.stdout.reconfigure(encoding='utf-8')
    args = None
    try:
        args = parser().parse_args()
        result = run(args)
    except (Failure, OSError, ValueError) as exc:
        result = envelope(command=COMMAND, error=error_payload(exc, 'IO_ERROR'))
    if args and args.human:
        print(result['error']['message'] if not result['ok'] else result['data'].get('candidate', {}).get('file', result['data']['pin']['object']))
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['ok'] else CODES.get(result['error']['code'], 1)


if __name__ == '__main__':
    if '--background' in sys.argv and '--' in sys.argv:
        phase, path = sys.argv[sys.argv.index('--') + 1:]
        worker(phase, Path(path))
    else:
        raise SystemExit(main())
