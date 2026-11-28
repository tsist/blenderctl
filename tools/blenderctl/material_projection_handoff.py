"""Attach or deliver a verified Material Workflow projection from a saved .blend."""
import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
sys.path[:0] = [str(ENTRY.parent), str(ENTRY.parent.parent)]

from filesystem import FileGuard, PathLocks
from material_projection_contract import MAX_PAIRS, MAX_SOURCES
from processes import ProcessTree
from protocol import (CODES, DEFAULT_BLENDER, DEFAULT_JOBS, SCHEMA, Failure,
                      atomic_json, digest, envelope, read_json, timeout_value)


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
    mode.add_argument('--human', action='store_true')
    mode.add_argument('--compact', action='store_true', help='Return the JSON result envelope')
    mode.add_argument('--full', action='store_true', help='Return the JSON result envelope')
    actions = cli.add_subparsers(dest='action', required=True, parser_class=Parser)
    for name, help_text in (
        ('projection-attach', 'Bind existing Normal, Curvature and AO maps on a saved working copy'),
        ('projection-deliver', 'Verify existing bindings and export a relocatable copy and texture set'),
        ('projection-effects', 'Edit wear and dust on one named material slot per target and export a verified copy'),
    ):
        action = actions.add_parser(name, description=help_text, help=help_text,
            epilog='bindings: {"bindings":[{"target":"Low","scene":"Scene","view_layer":"ViewLayer",'
                   '"frame":1,"projection":{"file":"projection.json","expected_sha256":"..."}}],'
                   '"resources":[]} . Use retain:true for already connected targets in an attach operation. '
                   'For projection-effects, add effects:{slot:0,material:"exact material name",'
                   'values:{"Show Wear":true,"Wear":0.8}} to each binding to edit.')
        action.add_argument('source', type=Path, help='Saved source .blend')
        action.add_argument('--expected-sha256', required=True, help='Source SHA-256')
        action.add_argument('--bindings', type=Path, required=True, help='UTF-8 bindings JSON')
        action.add_argument('--bindings-sha256', required=True, help='Bindings file SHA-256')
        action.add_argument('--output', type=Path, required=True, help='New absolute directory with an existing parent')
    return cli


def require(condition, code, message):
    if not condition:
        raise Failure(code, message)


def real_path(path):
    requested = Path(os.path.abspath(path))
    actual = requested.resolve(strict=True)
    require(requested == actual, 'CONFLICT', f'Redirected path: {requested}')
    return actual


def descriptor(value, directory):
    require(isinstance(value, dict) and set(value) == {'file', 'expected_sha256'},
            'INVALID_REQUEST', 'Expected a resource file and expected_sha256')
    require(isinstance(value['file'], str) and value['file'] and '\x00' not in value['file'],
            'INVALID_REQUEST', 'Resource path must be nonempty text')
    path = Path(value['file'])
    if not path.is_absolute():
        require(not path.anchor and '..' not in path.parts,
                'INVALID_REQUEST', f'Relative path must stay inside the bindings directory: {path}')
        path = directory / path
    return path, value['expected_sha256']


def read_inputs(args, stack, checkpoint):
    dirs, files = set(), {}

    def protect_directory(path):
        for directory in reversed((path, *path.parents)):
            if directory == Path(directory.anchor) or directory in dirs:
                continue
            stack.enter_context(FileGuard(directory, directory=True))
            dirs.add(directory)

    def protect(path, expected, suffix=None):
        require(isinstance(expected, str) and re.fullmatch(r'[0-9a-f]{64}', expected),
                'INVALID_REQUEST', f'Expected a lowercase SHA-256 for {path}')
        path = real_path(path)
        require(suffix is None or path.suffix.lower() == suffix, 'INVALID_REQUEST', f'Expected {suffix}: {path}')
        if path not in files:
            protect_directory(path.parent)
            guard = stack.enter_context(FileGuard(path))
            files[path] = guard.sha256(checkpoint)
        require(files[path] == expected, 'CONFLICT', f'SHA-256 mismatch: {path}')
        return {'file': str(path), 'expected_sha256': expected}

    source = protect(args.source, args.expected_sha256, '.blend')
    bindings_file = protect(args.bindings, args.bindings_sha256, '.json')
    bindings_path = Path(bindings_file['file'])
    require(bindings_path.stat().st_size <= 1024 * 1024,
            'RESOURCE_LIMIT', 'Bindings JSON exceeds 1 MiB')
    raw = read_json(bindings_path)
    require(isinstance(raw, dict) and 'bindings' in raw and not set(raw) - {'bindings', 'resources'},
            'INVALID_REQUEST', 'Bindings JSON requires bindings and optionally resources')
    records = raw['bindings']
    require(isinstance(records, list) and 1 <= len(records) <= MAX_PAIRS,
            'INVALID_REQUEST', f'Provide 1..{MAX_PAIRS} projection bindings')
    bindings, names, maps = [], set(), {}
    for record in records:
        keys = {'scene', 'view_layer', 'frame', 'target', 'projection'}
        optional = {'effects'} if args.action == 'projection-effects' else {'retain'}
        require(isinstance(record, dict) and keys <= set(record) <= keys | optional,
                'INVALID_REQUEST', 'Each binding requires scene, view_layer, frame, target and projection')
        for key in ('scene', 'view_layer', 'target'):
            value = record[key]
            require(isinstance(value, str) and 1 <= len(value) <= 63 and not any(ord(char) < 32 for char in value),
                    'INVALID_REQUEST', f'Invalid binding name: {key}')
        require(type(record['frame']) is int and 1 <= record['frame'] <= 100000,
                'INVALID_REQUEST', 'Binding frame must be an integer in 1..100000')
        require(type(record.get('retain', False)) is bool, 'INVALID_REQUEST', 'retain must be a boolean')
        if 'effects' in record:
            edit = record['effects']
            require(isinstance(edit, dict) and set(edit) == {'slot', 'material', 'values'} and
                    type(edit['slot']) is int and edit['slot'] >= 0 and isinstance(edit['material'], str)
                    and edit['material'] and isinstance(edit['values'], dict) and bool(edit['values']),
                    'INVALID_REQUEST', 'effects requires slot, material and nonempty values')
        target = record['target']
        require(target not in names, 'INVALID_REQUEST', f'Duplicate projection target: {target}')
        names.add(target)
        manifest_ref = protect(*descriptor(record['projection'], bindings_path.parent), '.json')
        manifest = Path(manifest_ref['file'])
        require(manifest.stat().st_size <= 16 * 1024 * 1024,
                'RESOURCE_LIMIT', f'Projection report exceeds 16 MiB: {manifest}')
        report = read_json(manifest)
        require(isinstance(report, dict) and report.get('schema') == 'mw-projection-1.0'
                and report.get('status') == 'complete',
                'INVALID_REQUEST', f'Expected a complete raw projection.json, got {manifest}')
        require(report.get('target') == target, 'CONFLICT', f'Projection target differs: {target}')
        identity = report.get('identity')
        require(isinstance(identity, dict) and identity.get('frame') == record['frame'],
                'CONFLICT', f'Projection frame differs: {target}')
        sources, source_ids = report.get('sources'), identity.get('sources')
        require(isinstance(sources, list) and 1 <= len(sources) <= MAX_SOURCES
                and all(isinstance(name, str) and name for name in sources)
                and len(set(sources)) == len(sources) and isinstance(source_ids, dict)
                and set(sources) == set(source_ids),
                'INVALID_REQUEST', f'Projection requires explicit contributor identities: {target}')
        cage = identity.get('cage')
        require(cage is None or (isinstance(cage, dict) and isinstance(cage.get('name'), str)),
                'INVALID_REQUEST', f'Invalid cage identity: {target}')
        require(target not in sources and (cage is None or cage['name'] not in [target, *sources]),
                'INVALID_REQUEST', f'Target, contributors and cage must be distinct: {target}')
        entries = report.get('maps')
        require(isinstance(entries, dict) and set(entries) == {'normal', 'ao', 'curvature'},
                'INVALID_REQUEST', f'Projection map set is incomplete: {target}')
        for channel, data in entries.items():
            filename = channel + '.png'
            require(isinstance(data, dict) and data.get('file') == filename,
                    'INVALID_REQUEST', f'Expected {filename} adjacent to the raw report')
            ref = protect(manifest.with_name(filename), data.get('sha256'), '.png')
            maps[ref['file']] = ref['expected_sha256']
        bindings.append({**record, 'projection': manifest_ref})

    if args.action == 'projection-effects':
        require(any('effects' in row for row in bindings), 'INVALID_REQUEST', 'Specify at least one effects edit')
    resources = raw.get('resources', [])
    require(isinstance(resources, list) and len(resources) <= 256,
            'INVALID_REQUEST', 'resources must contain at most 256 file descriptors')
    ordinary = {}
    for item in resources:
        ref = protect(*descriptor(item, bindings_path.parent))
        require(ref['file'] not in ordinary, 'INVALID_REQUEST', f'Duplicate resource: {ref["file"]}')
        ordinary[ref['file']] = ref['expected_sha256']

    require(args.output.is_absolute() and bool(args.output.name), 'INVALID_REQUEST', '--output must be an absolute directory')
    parent = real_path(args.output.parent)
    protect_directory(parent)
    output = parent / args.output.name
    require(not os.path.lexists(output), 'CONFLICT', f'Output directory already exists: {output}')
    return {'action': args.action, 'source': source, 'input_bindings': bindings_file, 'bindings': bindings,
            'resources': ordinary, 'maps': maps, 'output': str(output), 'state_source': 'SAVED_DISK_V1'}


def run_worker(blender, job, phase, deadline, checkpoint, heartbeat, entry=ENTRY, *, private_temp=False):
    argv = [str(blender), '--background', '--factory-startup', '--disable-autoexec', '--offline-mode',
            '--threads', '2', '--python-exit-code', '23', '--python', str(entry), '--', phase,
            str(job / 'request.json')]
    profile = job / f'{phase}-profile'
    profile.mkdir()
    env = {**os.environ, 'BLENDER_USER_RESOURCES': str(profile),
           'BLENDER_USER_CONFIG': str(profile / 'config'), 'BLENDER_USER_SCRIPTS': str(profile / 'scripts'),
           'BLENDER_USER_DATAFILES': str(profile / 'datafiles'), 'BLENDER_USER_EXTENSIONS': str(profile / 'extensions')}
    if private_temp:
        env.update(TEMP=str(profile), TMP=str(profile), TMPDIR=str(profile), PYTHONDONTWRITEBYTECODE='1')
    with (job / (phase + '.log')).open('xb') as log:
        tree = ProcessTree(argv, cwd=str(job), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            while True:
                checkpoint()
                try:
                    exit_code = tree.process.wait(timeout=min(2, max(.01, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    heartbeat('running', phase)
        except BaseException:
            tree.terminate()
            raise
        finally:
            tree.close()
    checkpoint()
    result = job / (phase + '-result.json')
    require(exit_code == 0 and result.is_file(), 'WORKER_FAILED', f'{phase} exited {exit_code}. See {phase}.log')
    data = read_json(result)
    if not data['ok']:
        raise Failure(data['error']['code'], data['error']['message'])
    return data['data']


def file_inventory(root, checkpoint=None):
    return {file.relative_to(root).as_posix(): digest(file, checkpoint)
            for file in sorted(root.rglob('*')) if file.is_file()}


def run(args):
    deadline = time.monotonic() + timeout_value(args.timeout)
    blender = real_path(args.blender)
    job = args.jobs_dir.resolve() / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    command = 'material.' + args.action
    artifacts = {'job_directory': str(job), 'result': str(job / 'result.json')}
    result_file = job / 'status.json'
    stage = None
    data = error = None

    def checkpoint():
        if (job / 'cancel.request').exists():
            raise Failure('CANCELLED', 'Projection handoff cancelled before publication')
        if time.monotonic() >= deadline:
            raise Failure('TIMEOUT', 'Projection handoff exceeded its timeout')

    def status(state, phase):
        atomic_json(result_file, {'state': state, 'job_id': job.name, 'pid': os.getpid(),
                                  'phase': phase, 'heartbeat_at': time.time()})

    status('running', 'verify_inputs')
    try:
        with ExitStack() as stack:
            request = read_inputs(args, stack, checkpoint)
            output = Path(request['output'])
            stack.enter_context(PathLocks([output]))
            require(not os.path.lexists(output), 'CONFLICT', f'Output directory already exists: {output}')
            stage = output.with_name(f'.projection-{job.name}.partial')
            request['stage'] = str(stage)
            atomic_json(job / 'request.json', {'schema_version': SCHEMA, 'command': command, 'params': request})
            status('running', 'author')
            authored = run_worker(blender, job, 'author', deadline, checkpoint, status)
            status('running', 'verify')
            verified = run_worker(blender, job, 'verify', deadline, checkpoint, status)
            require(file_inventory(stage, checkpoint) == verified['inventory'], 'CONFLICT',
                    'Staged files changed after independent verification')
            checkpoint()
            require(not os.path.lexists(output), 'CONFLICT', f'Output directory already exists: {output}')
            os.rename(stage, output)
            candidate = output / authored['candidate']
            next_bindings = output / 'bindings.json'
            artifacts.update(candidate=str(candidate), bindings=str(next_bindings),
                             texture_index=str(output / 'textures' / 'maps.json'),
                             author_result=str(job / 'author-result.json'),
                             independent_reopen=str(job / 'verify-result.json'))
            data = {'status': 'pass', 'source': request['source'], 'state_source': request['state_source'],
                    'source_unchanged': True, 'output': str(output),
                    'candidate': {'file': str(candidate), 'sha256': verified['inventory'][authored['candidate']]},
                    'bindings': {'file': str(next_bindings), 'sha256': verified['inventory']['bindings.json']},
                    'targets': verified['targets'],
                    'independent_reopen': {'process': 'fresh_process', **verified['runtime']}}
    except (Failure, OSError, RuntimeError) as exc:
        error = error_payload(exc, 'IO_ERROR')
        if stage is not None and stage.exists():
            artifacts['unpublished_stage'] = str(stage)
    for log in sorted(job.glob('*.log')):
        artifacts[log.name] = str(log)
    result = envelope(command, job.name, data, error, artifacts)
    atomic_json(job / 'result.json', result)
    terminal = {'TIMEOUT': 'timed_out', 'CANCELLED': 'cancelled'}
    status(terminal.get(error['code'], 'failed') if error else 'succeeded', 'done')
    return result


def error_payload(exc, default):
    code = getattr(exc, 'code', 'NOT_FOUND' if isinstance(exc, FileNotFoundError) else default)
    return {'code': code if code in CODES else default, 'message': str(exc)}

@contextmanager
def binding_context(bpy, binding):
    from material_projection import _activate

    window = bpy.context.window
    previous_scene, previous_layer = window.scene, window.view_layer
    selected = bpy.data.scenes.get(binding['scene'])
    old_frame = (selected.frame_current, selected.frame_subframe) if selected else None
    try:
        yield _activate(binding)
    finally:
        if selected is not None:
            selected.frame_set(old_frame[0], subframe=old_frame[1])
        window.scene, window.view_layer = previous_scene, previous_layer


def open_workfile(bpy, filepath):
    status = bpy.ops.wm.open_mainfile(filepath=str(filepath), load_ui=True, use_scripts=False)
    require(status == {'FINISHED'} and Path(bpy.data.filepath).samefile(filepath),
            'VALIDATION_FAILED', f'Unable to reopen the requested .blend: {filepath}')
    require(tuple(bpy.data.version[:2]) == tuple(bpy.app.version[:2]),
            'UNSUPPORTED', 'Projection handoff requires matching Blender major/minor')
    require(not any((bpy.data.libraries, bpy.data.cache_files, bpy.data.movieclips, bpy.data.sounds,
                     bpy.data.volumes)), 'UNSUPPORTED', 'Projection handoff requires a local scene without external caches and media')


def material_state(bpy, edited):
    """Check object membership, datablock assignment and slot identity outside edit targets."""
    state = {}
    for obj in bpy.data.objects:
        if obj.name not in edited:
            state[obj.name] = [obj.type, obj.data.name if obj.data else None,
                               [list(row) for row in obj.matrix_world],
                               sorted(collection.name for collection in obj.users_collection),
                               [[slot.link, slot.material.name if slot.material else None] for slot in obj.material_slots]]
    return hashlib.sha256(json.dumps(state, sort_keys=True, allow_nan=False).encode('utf-8')).hexdigest()


def control_state(sockets):
    from material_workflow_addon.projection_material import EFFECT_CONTROLS

    names = {name for fields in EFFECT_CONTROLS.values() for name in fields}
    result = []
    for socket in sockets or ():
        if socket.name in names:
            value = socket.default_value
            if not isinstance(value, (bool, float, int)):
                value = list(value)
            links = [[link.from_node.name, link.from_socket.identifier] for link in socket.links]
            result.append([socket.name, value, links])
    return result


def animated_paths(tree):
    animation = tree.animation_data
    if not animation:
        return set()
    paths = {curve.data_path for curve in animation.drivers}
    def add_curves(owner):
        action = getattr(owner, 'action', None)
        if action:
            for layer in action.layers:
                for strip in layer.strips:
                    for bag in getattr(strip, 'channelbags', ()):
                        if bag.slot_handle == owner.action_slot_handle:
                            paths.update(curve.data_path for curve in bag.fcurves)
        for child in getattr(owner, 'strips', ()):
            add_curves(child)
    add_curves(animation)
    for track in animation.nla_tracks:
        add_curves(track)
    return paths


def protected_controls(bpy, bindings):
    from material_workflow_addon.projection_material import effect_controls
    edits = {(row['target'], row['effects']['slot']) for row in bindings if 'effects' in row}
    states = [[obj.name, index, slot.link, slot.material.name, control_state(effect_controls(slot.material))]
              for obj in bpy.data.objects for index, slot in enumerate(obj.material_slots)
              if slot.material and (obj.name, index) not in edits]
    return hashlib.sha256(json.dumps(states, allow_nan=False).encode()).hexdigest()


def set_effect_values(target, edit):
    from material_workflow_addon.projection_material import EFFECT_CONTROLS, effect_controls

    index = edit['slot']
    require(index < len(target.material_slots), 'CONFLICT', f'{target.name}: material slot {index} is absent')
    slot = target.material_slots[index]
    material = slot.material
    require(material and material.name == edit['material'], 'CONFLICT', f'{target.name}: selected material differs')
    sockets = effect_controls(material)
    allowed = {name for fields in EFFECT_CONTROLS.values() for name in fields}
    require(sockets is not None and edit['values'].keys() <= allowed,
            'INVALID_REQUEST', 'Expected named wear or dust controls on the bound projection node')
    animated = animated_paths(material.node_tree)
    for name, value in edit['values'].items():
        socket = sockets.get(name)
        require(socket is not None and not socket.is_linked, 'CONFLICT', f'{name}: missing or linked input')
        # Editing an animated default may be undone by the next evaluation.
        require(socket.path_from_id('default_value') not in animated, 'CONFLICT',
                f'{name}: an animation curve or driver controls this input')
        def bounded(number, maximum):
            return type(number) in (int, float) and 0 <= number <= maximum
        valid = ((socket.type == 'BOOLEAN' and type(value) is bool) or
                 (socket.type == 'VALUE' and bounded(value, 1)) or
                 (socket.type == 'RGBA' and isinstance(value, list) and len(value) == 4 and
                  all(bounded(component, socket.bl_rna.properties['default_value'].hard_max) for component in value)))
        require(valid, 'INVALID_REQUEST', f'{name}: expected a boolean, unit scalar or four nonnegative Blender RGBA components matching its socket')
    if material.users > 1 or (slot.link == 'DATA' and target.data.users > 1):
        material = material.copy()
        slot.link = 'OBJECT'
        slot.material = material
        sockets = effect_controls(material)
    for name, value in edit['values'].items():
        sockets[name].default_value = value
        actual = sockets[name].default_value
        matches = (all(math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6) for a, b in zip(actual, value, strict=True))
                   if isinstance(value, list) else math.isclose(actual, value, abs_tol=1e-6))
        require(matches, 'VALIDATION_FAILED', f'{name}: Blender did not retain the requested value')


def capture_binding(target, report):
    from material_workflow_addon.projection_material import effect_controls, PROJECTION_NODE

    maps = report['maps']
    slots = []
    for slot in target.material_slots:
        material = slot.material
        group = material.node_tree.nodes[PROJECTION_NODE].node_tree
        for channel in maps:
            image = group.nodes['MW ' + channel].image
            size = list(image.size)
            require(image.has_data and size == [maps[channel]['width'], maps[channel]['height']]
                    and image.colorspace_settings.name == 'Non-Color',
                    'VALIDATION_FAILED', f'Unable to read {target.name} {channel}: {size}, {image.colorspace_settings.name}, {image.filepath}')
        controls = control_state(effect_controls(material))
        slots.append({'material': material.name, 'link': slot.link, 'controls': controls})
    return {'target': target.name, 'uv_layer': report['uv_layer'], 'slots': slots,
            'maps': {name: row['sha256'] for name, row in sorted(maps.items())},
            'coverage_fraction': report['witness']['coverage_fraction']}


def record_panel_pair(scene, target, sources, cage, binding, report, attach):
    matches = [pair for pair in scene.mwp_projection_pairs if pair.target == target]
    require(len(matches) <= 1, 'CONFLICT', f'More than one saved panel pair for {target.name}')
    pair = matches[0] if matches else scene.mwp_projection_pairs.add()
    if attach or not matches:
        confirmed = pair.confirmed
        pair.target = target
        pair.sources.clear()
        for source in sources:
            pair.sources.add().object = source
        pair.uv_layer = report['uv_layer']
        pair.cage = cage
        pair.confirmed = confirmed
    if not matches:
        for key in ('extrusion', 'distance', 'ao_distance', 'curvature_radius'):
            setattr(pair, key, report['settings'][key])
    pair.hit_rate = report['witness']['coverage_fraction']
    require(pair.target == target and pair.uv_layer == report['uv_layer'] and pair.cage == cage
            and {row.object for row in pair.sources} == set(sources),
            'CONFLICT', f'Saved panel pair differs from the validated bake: {target.name}')
    pair.manifest = binding['projection']['file']
    return pair


def prepare_binding(bpy, request, binding):
    from material_projection import _object
    from material_workflow_addon.projection_material import (attach_projection, effect_controls,
                                                              read_projection, verify_projection_binding,
                                                              verify_projection_inputs)

    manifest = binding['projection']['file']
    with binding_context(bpy, binding) as (scene, layer):
        report, _ = read_projection(manifest)
        require(str(report.get('blender', '')).split('.')[:2] == bpy.app.version_string.split('.')[:2],
                'UNSUPPORTED', 'Projection manifest was produced with another Blender major/minor')
        target = _object(binding['target'], layer)
        sources = [_object(name, layer) for name in report['sources']]
        cage_record = report['identity'].get('cage')
        cage = _object(cage_record['name'], layer) if cage_record else None
        require(all(obj.type == 'MESH' and obj.data and not obj.library and not obj.override_library
                    for obj in [target, *sources, *([cage] if cage else [])]), 'UNSUPPORTED',
                f'Projection requires local mesh objects: {target.name}')
        attach = request['action'] == 'projection-attach' and not binding.get('retain', False)
        previous = None
        if attach:
            # The existing author's controls may already layer wear and dust
            # on another bake. Preserve them while replacing maps.
            previous = [control_state(effect_controls(slot.material)) for slot in target.material_slots]
            attach_projection(bpy.context, target, manifest, sources=sources, cage=cage)
        else:
            verify_projection_inputs(bpy.context, target, report, sources=sources, cage=cage)
        verify_projection_binding(target, manifest)
        if request['action'] == 'projection-effects' and 'effects' in binding:
            set_effect_values(target, binding['effects'])
            verify_projection_binding(target, manifest)
        result = capture_binding(target, report)
        if previous is not None:
            current = [row['controls'] for row in result['slots']]
            require(all(not old or old == new for old, new in zip(previous, current, strict=True)),
                    'VALIDATION_FAILED', f'Existing author effect controls changed: {target.name}')
        pair = record_panel_pair(scene, target, sources, cage, binding, report, attach)
        return pair, result


def live_external_images(bpy):
    images = set()
    for image in bpy.data.images:
        if image.users and not image.packed_file and image.source not in {'GENERATED', 'VIEWER'}:
            require(image.source == 'FILE' and bool(image.filepath),
                    'UNSUPPORTED', f'Unsupported external image: {image.name}')
            images.add(real_path(Path(bpy.path.abspath(image.filepath, library=image.library))))
    return images


def package_work(bpy, request):
    from material_workflow_addon import projection_ui
    from material_workflow_addon.projection_delivery import deliver_project

    projection_ui.register()
    open_workfile(bpy, request['source']['file'])
    bindings = request['bindings']
    configured_pairs = {(row['scene'], row['target']) for row in bindings}
    for scene in bpy.data.scenes:
        for pair in scene.mwp_projection_pairs:
            if pair.manifest:
                require(pair.target is not None and (scene.name, pair.target.name) in configured_pairs,
                        'INVALID_REQUEST', f'Include every saved projection pair with a report in bindings ({scene.name})')
    edited = {row['target'] for row in bindings
              if (request['action'] == 'projection-attach' and not row.get('retain', False)) or 'effects' in row}
    before = material_state(bpy, edited)
    untouched = protected_controls(bpy, bindings) if request['action'] == 'projection-effects' else None
    panels, witnesses = zip(*(prepare_binding(bpy, request, row) for row in bindings))
    require(material_state(bpy, edited) == before, 'VALIDATION_FAILED',
            'Projection changed object or material-slot assignments outside the selected attach targets')

    declared = {Path(path) for path in [*request['resources'], *request['maps']]}
    live = live_external_images(bpy)
    unresolved = live - declared
    require(not unresolved, 'INVALID_REQUEST', f'External images require file and SHA in resources: {sorted(map(str, unresolved))}')
    paths = [Path(row['projection']['file']) for row in bindings]

    def remap(remapping):
        for panel, path in zip(panels, paths):
            panel.manifest = remapping[path]
        def restore():
            for panel, path in zip(panels, paths):
                panel.manifest = str(path)
        return restore

    stage = Path(request['stage'])
    blend = deliver_project(bpy.context, paths, stage, set_relative_paths=remap)
    texture_path = stage / 'textures'
    index = read_json(texture_path / 'maps.json')['targets']
    next_rows = []
    for record, item in zip(bindings, index, strict=True):
        require(item['target'] == record['target'], 'VALIDATION_FAILED', 'Delivered texture index order changed')
        path = texture_path / item['manifest']
        wanted = record['projection']['expected_sha256']
        require(digest(path) == wanted, 'VALIDATION_FAILED', f'Delivered projection report changed: {record["target"]}')
        next_rows.append({key: record[key] for key in ('target', 'scene', 'view_layer', 'frame')}
                         | {'projection': {'file': path.relative_to(stage).as_posix(), 'expected_sha256': wanted}})
    map_inputs = set(request['maps'].values())
    ordinary_inputs = set(request['resources'].values())
    resource_folder = texture_path / 'existing'
    resources = []
    for path in sorted(resource_folder.glob('*')):
        sha = digest(path)
        require(sha in ordinary_inputs, 'VALIDATION_FAILED', f'Unexpected copied resource: {path}')
        resources.append({'file': path.relative_to(stage).as_posix(), 'expected_sha256': sha})
    atomic_json(stage / 'bindings.json', {'bindings': next_rows, 'resources': resources})
    return {'candidate': blend.name, 'targets': witnesses, 'before': before, 'edited': sorted(edited),
            'protected_controls': untouched,
            'map_hashes': sorted(map_inputs)}


def verify_work(bpy, request, authored):
    from material_workflow_addon import projection_ui
    from material_workflow_addon.projection_material import read_projection

    projection_ui.register()
    stage = Path(request['stage'])
    candidate = stage / authored['candidate']
    open_workfile(bpy, candidate)
    if authored.get('protected_controls') is not None:
        require(protected_controls(bpy, request['bindings']) == authored['protected_controls'], 'VALIDATION_FAILED',
                'Independent reader found changed controls or material-slot assignments outside the requested edits')
    packaged = read_json(stage / 'bindings.json')
    require(material_state(bpy, set(authored['edited'])) == authored['before'], 'VALIDATION_FAILED',
            'Independent reader found changed non-target object or material-slot assignments')
    witnesses = []
    for row in packaged['bindings']:
        ref = row['projection']
        path, expected = descriptor(ref, stage)
        require(digest(path) == expected, 'VALIDATION_FAILED', 'Packaged report SHA differs')
        _, maps = read_projection(path)
        require(all(digest(f) in authored['map_hashes'] for f in maps.values()), 'VALIDATION_FAILED',
                'Packaged projection image SHA differs')
        row['projection']['file'] = str(path)
        with binding_context(bpy, row) as (scene, _):
            pairs = [pair for pair in scene.mwp_projection_pairs if pair.target and pair.target.name == row['target']]
            require(len(pairs) == 1 and pairs[0].manifest.startswith('//') and
                    Path(bpy.path.abspath(pairs[0].manifest)).resolve() == path,
                    'VALIDATION_FAILED', f'Panel cannot resolve its relocated manifest: {row["target"]}')
        _, witness = prepare_binding(bpy, {'action': 'projection-deliver'}, row)
        witnesses.append(witness)
    require(witnesses == authored['targets'], 'VALIDATION_FAILED', 'Independent reader found changed shader bindings or effect controls')
    all_paths = bpy.utils.blend_paths(absolute=True, packed=False, local=False)
    for raw in all_paths:
        if raw:
            actual = Path(raw).resolve(strict=True)
            require(actual.is_relative_to(stage) and actual.is_file(),
                    'VALIDATION_FAILED', f'External dependency escaped the delivered directory: {actual}')
    require(all(image.is_relative_to(stage) for image in live_external_images(bpy)), 'VALIDATION_FAILED',
            'Packaged project retains a live external image outside its delivery directory')
    return {'targets': witnesses, 'inventory': file_inventory(stage)}


def worker(phase, request_file):
    import bpy
    from material_workflow_addon.contract import WorkflowError
    job = request_file.parent
    try:
        flags = {'--factory-startup', '--disable-autoexec', '--offline-mode'}
        require(bpy.app.background and flags.issubset(sys.argv) and not bpy.app.online_access
                and not bpy.context.preferences.filepaths.use_scripts_auto_execute,
                'UNSUPPORTED', 'Worker must run in offline background factory mode without auto-execution')
        request = read_json(request_file)['params']
        if phase == 'author':
            result = package_work(bpy, request)
        else:
            result = verify_work(bpy, request, read_json(job / 'author-result.json')['data'])
        result['runtime'] = {'status': 'pass', 'blender': bpy.app.version_string,
                             'build': bpy.app.build_hash.decode(), 'pid': os.getpid()}
        payload = {'ok': True, 'data': result, 'error': None}
    except (Failure, ValueError, TypeError, KeyError, RuntimeError, OSError, WorkflowError) as exc:
        payload = {'ok': False, 'data': None, 'error': error_payload(exc, 'VALIDATION_FAILED')}
    atomic_json(job / (phase + '-result.json'), payload)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    args = None
    try:
        args = parser().parse_args()
        result = run(args)
    except (Failure, OSError, RuntimeError) as exc:
        result = envelope(error=error_payload(exc, 'IO_ERROR'))
    if args and args.human:
        print(result['artifacts']['candidate'] if result['ok'] else result['error']['message'])
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['ok'] else CODES[result['error']['code']]


if __name__ == '__main__':
    if '--background' in sys.argv and '--' in sys.argv:
        worker_phase, worker_request = sys.argv[sys.argv.index('--') + 1:]
        worker(worker_phase, Path(worker_request))
    else:
        sys.exit(main())
