"""Run a frozen Toon style or media component on one saved material slot."""
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
import uuid

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
sys.path.insert(0, str(ENTRY.parent))

from filesystem import FileGuard, PathLocks
from material_draft_handoff import Parser, canonical, fingerprint, guard_file, open_saved_context, read_document, require
from material_projection_handoff import error_payload, run_worker
from protocol import CODES, DEFAULT_BLENDER, DEFAULT_JOBS, SCHEMA, Failure, atomic_json, digest, envelope, read_json, timeout_value
from toon_panel_handoff import PRODUCTS, bindings, dependencies, load_modules, name, named_slot, native_product, verify_slot_isolation


COMMAND = 'toon-workflow.component-slot'
STYLES = ('GAME', 'MANGA', 'ALPHA_SHEET', 'GLASS', 'WATER', 'SMOKE', 'EMISSION')
OPERATIONS = ('apply', 'update', 'inspect', 'restore', 'import-json', 'export-json', 'enable-transmission')


def parser():
    cli = Parser(description=__doc__)
    cli.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    cli.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS)
    cli.add_argument('--transactions-dir', help=argparse.SUPPRESS)
    cli.add_argument('--timeout', type=float, default=120)
    mode = cli.add_mutually_exclusive_group()
    for flag in ('--human', '--compact', '--full'):
        mode.add_argument(flag, action='store_true')
    sub = cli.add_subparsers(required=True, parser_class=Parser).add_parser('component-slot', description=__doc__,
        epilog='Params: operation, edition, file, expected_sha256, context {scene,view_layer,frame}, '
               'target {object,material_slot,expected_material,expected_link}. Apply requires style (GAME, '
               'MANGA, ALPHA_SHEET, GLASS, WATER, SMOKE, EMISSION). Apply/update accept settings and an explicit '
               'Sun name for GAME. Restore a managed surface or component before applying a new style. '
               'import-json needs component_json {file,expected_sha256}. export-json needs a new .json output. '
               'enable-transmission targets the saved EEVEE scene (omit target) and requires a new .blend output.')
    sub.add_argument('--params', type=Path, required=True, help='UTF-8 JSON with absolute source .blend')
    sub.add_argument('--output', type=Path, help='New absolute .blend or export .json. Omit for inspect.')
    return cli


def request_for(args, guards):
    request = read_document(guard_file(args.params.absolute(), guards))
    common = {'operation', 'edition', 'file', 'expected_sha256', 'context'}
    require(isinstance(request, dict) and common <= request.keys(), 'INVALID_REQUEST', 'Missing component-slot fields')
    operation = request['operation']
    require(operation in OPERATIONS, 'INVALID_REQUEST', 'Invalid component operation')
    if operation != 'enable-transmission':
        require('target' in request, 'INVALID_REQUEST', 'This operation needs a named material slot')
        common.add('target')
    if operation == 'apply':
        common.add('style')
        require(request.get('style') in STYLES, 'INVALID_REQUEST', 'Apply requires a supported component style')
    extra = {'settings', 'sun'} if operation in ('apply', 'update') else set()
    if operation == 'import-json':
        extra = {'component_json'}
    require(request.keys() <= common | extra, 'INVALID_REQUEST', f'Unexpected fields for {operation}')
    require(isinstance(request['edition'], str) and request['edition'] in PRODUCTS, 'UNSUPPORTED', 'Unknown Toon edition')
    require(isinstance(request.get('settings', {}), dict) and ('sun' not in request or name(request['sun'])),
            'INVALID_REQUEST', 'Expected settings object and optional exact Sun name')
    if operation == 'update':
        require(request.get('settings') or 'sun' in request, 'INVALID_REQUEST', 'Update requires settings or a Sun snapshot')
    ctx = request['context']
    require(isinstance(ctx, dict) and ctx.keys() == {'scene', 'view_layer', 'frame'}
            and name(ctx['scene']) and name(ctx['view_layer']) and type(ctx['frame']) is int,
            'INVALID_REQUEST', 'Expected exact scene, view_layer and integer frame')
    if operation != 'enable-transmission':
        target = request['target']
        require(isinstance(target, dict) and target.keys() == {'object', 'material_slot', 'expected_material', 'expected_link'}
                and name(target['object']) and type(target['material_slot']) is int and target['material_slot'] >= 0
                and (target['expected_material'] is None or name(target['expected_material']))
                and target['expected_link'] in ('DATA', 'OBJECT'), 'INVALID_REQUEST',
                'Expected a named slot and guarded material/link')
    require(isinstance(request['file'], str) and Path(request['file']).suffix.lower() == '.blend'
            and isinstance(request['expected_sha256'], str), 'INVALID_REQUEST', 'Expected a hash-pinned .blend')
    request['file'] = str(guard_file(request['file'], guards, request['expected_sha256']))
    if operation == 'import-json':
        config = request.get('component_json')
        require(isinstance(config, dict) and config.keys() == {'file', 'expected_sha256'}
                and isinstance(config['file'], str) and Path(config['file']).suffix.lower() == '.json'
                and isinstance(config['expected_sha256'], str), 'INVALID_REQUEST',
                'Expected component_json with a hash-pinned .json file')
        path = guard_file(config['file'], guards, config['expected_sha256'])
        require(path.stat().st_size <= 16 * 1024 * 1024, 'INVALID_REQUEST', 'Component JSON exceeds 16 MiB')
        config['file'] = str(path)
    require((operation == 'inspect') == (args.output is None), 'INVALID_REQUEST',
            'Specify --output for mutations and omit it for inspect')
    return request


def desired_settings(ui, context, request, baseline):
    from mathutils import Vector

    style = baseline['style']
    patch = request.get('settings', {})
    require(patch.keys() <= set(ui._PUBLIC_FIELDS[style]), 'INVALID_REQUEST',
            f'Settings include a field outside the native {style} panel')
    desired = {**baseline, **patch}
    for key in ui._COLOR_FIELDS & patch.keys():
        if type(desired[key]) is list and len(desired[key]) == 3:
            desired[key] = [*desired[key], 1.0]
    if style == 'MANGA':
        if request['operation'] == 'update':
            require(desired['pattern_space'] == baseline['pattern_space'], 'CONFLICT',
                    'Changing MANGA mapping requires restoring the slot and reapplying')
        if desired['pattern_space'] != 'UV':
            require('uv_map' not in patch, 'INVALID_REQUEST', 'uv_map applies only to UV mapping')
            desired['uv_map'] = None
        render = context.scene.render
        desired['resolution'] = ([round(render.resolution_x * render.resolution_percentage / 100),
                                  round(render.resolution_y * render.resolution_percentage / 100)]
                                 if desired['pattern_space'] == 'WINDOW' else None)
    if 'sun' in request:
        require('light_direction' in desired, 'INVALID_REQUEST', 'A Sun snapshot applies only to GAME')
        sun = context.scene.objects.get(request['sun'])
        require(sun and sun.type == 'LIGHT' and sun.data.type == 'SUN', 'NOT_FOUND', 'Named Sun is absent from the scene')
        evaluated = sun.evaluated_get(context.evaluated_depsgraph_get())
        direction = -(evaluated.matrix_world.to_3x3() @ Vector((0.0, 0.0, -1.0)))
        require(all(math.isfinite(x) for x in direction) and direction.length > 1e-12, 'INVALID_REQUEST', 'Invalid Sun direction')
        desired['light_direction'] = list(direction.normalized())
    try:
        return ui._normalized(desired)
    except (TypeError, ValueError, RuntimeError, OverflowError) as error:
        raise Failure('INVALID_REQUEST', str(error)) from error


def protected_graphs(bpy, exclude=None, expected=None):
    from inspection import identity, key, record

    def visit(tree, values):
        for node in tree.nodes:
            group = getattr(node, 'node_tree', None)
            if group is not None and group not in values:
                values.add(group)
                visit(group, values)

    if expected is None:
        items = {slot.material for obj in bpy.data.objects for slot in obj.material_slots if slot.material}
        items.discard(exclude)
        for material in tuple(items):
            if material.node_tree:
                visit(material.node_tree, items)
    else:
        items = [item for item in (*bpy.data.materials, *bpy.data.node_groups) if key(identity(item)) in expected]
    return {key(identity(item)): fingerprint(record(item)) for item in items}


def native_readback(ui, material, report):
    if report['status'] != 'valid':
        return None
    C = ui.components
    if report['settings']['style'] in C.MEDIA_STYLES:
        graph = C.media._snapshot(material)
        return {'graph_sha256': fingerprint(graph), 'nodes': graph['nodes'], 'links': graph['links'],
                'material': graph['material']}
    observed = C.styles.inspect(material)
    return {'actual': observed['actual'], 'signature': observed['signature'], 'generation': observed['generation']}


def invoke_native(ui, context, obj, index, request):
    slots, catalog = ui.slots, ui.components
    operation, scene = request['operation'], context.scene
    if operation == 'inspect':
        return slots.inspect(scene, obj, index)
    if operation == 'restore':
        return slots.restore(scene, obj, index)
    if operation == 'import-json':
        ui.register()
        ui._settings(context).json_path = request['component_json']['file']
        report = ui.import_current(context)
        require(report['status'] == 'valid', 'CONFLICT', 'Imported component failed native validation')
        return report
    if operation == 'apply':
        baseline = catalog.defaults(request['style'])
        operation_fn = slots.apply
    else:
        report = slots.inspect(scene, obj, index)
        require(report['status'] == 'valid', 'CONFLICT', '; '.join(report['issues']))
        material = obj.material_slots[index].material
        require(material.users - int(material.use_fake_user) == 1, 'CONFLICT',
                'The component material is also used outside the named slot')
        baseline = report['settings']
        operation_fn = slots.update
    wanted = desired_settings(ui, context, request, baseline)
    result = operation_fn(scene, obj, index, wanted)
    require(result['status'] == 'valid', 'CONFLICT', '; '.join(result.get('issues', [])) or 'Native component edit failed')
    require(result['settings'].keys() == wanted.keys() and all(catalog.styles._close(value, result['settings'][key])
            for key, value in wanted.items()), 'VALIDATION_FAILED', 'Native shader values differ from the request')
    return result


def work(phase, record, job):
    import bpy

    request = record['params']
    sys.pycache_prefix = str(job / (phase + '-no-bytecode'))
    surface = load_modules(bpy, request)
    domain = importlib.import_module(surface.__package__ + '.component_ui')
    runtime = {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(), 'pid': os.getpid()}
    if phase == 'verify':
        authored = read_json(job / 'author-result.json')['data']
        request = {**request, 'file': request['stage']}
        if request['operation'] != 'enable-transmission':
            request = {**request, 'operation': 'inspect',
                       'target': {**request['target'], 'expected_material': authored['actual_material']}}
    context = open_saved_context(bpy, request)
    if request['operation'] == 'enable-transmission':
        proof = authored['verification'] if phase == 'verify' else None
        return {**transmission(bpy, domain, context, request, proof), 'runtime': runtime}
    target, action = request['target'], request['operation']
    before, external = bindings(bpy), dependencies(bpy)
    with named_slot(context, target) as obj:
        index = target['material_slot']
        if action == 'export-json':
            return {**export_component(domain, context, obj, index, request), 'runtime': runtime}
        if phase == 'author' and action != 'inspect':
            edit = action in ('update', 'restore')
            if action == 'import-json':
                edit = domain.slots.inspect(context.scene, obj, index)['status'] == 'valid'
                if edit:
                    material = obj.material_slots[index].material
                    require(material.users - int(material.use_fake_user) == 1, 'CONFLICT',
                            'The imported component is also used outside the named slot')
            graphs = protected_graphs(bpy, exclude=obj.material_slots[index].material if edit else None)
        try:
            report = invoke_native(domain, context, obj, index, request)
        except (ValueError, RuntimeError) as error:
            if isinstance(error, Failure):
                raise
            raise Failure('CONFLICT', str(error)) from error
        actual = obj.material_slots[index].material
        native = native_readback(domain, actual, report)
    result = {'report': report, 'native': native, 'actual_material': actual.name_full if actual else None,
              'runtime': runtime}
    if phase == 'verify':
        expected = authored['verification']
        require(before == expected['bindings'] and external == expected['dependencies'] and report == expected['report']
                and native == authored['native'] and protected_graphs(bpy, expected=expected['graphs']) == expected['graphs'],
                'VALIDATION_FAILED', 'Reopened Toon material, protected shaders, slots or dependency paths differ')
        require(not expected['retained'] or bpy.data.materials.get(expected['retained']) is not None,
                'VALIDATION_FAILED', 'Reopened file lost the retained component')
        return {**result, 'sha256': digest(request['stage'])}
    if action == 'inspect':
        return result

    after = bindings(bpy)
    original = next(row['active_slot'] for row in before if row['object'] == target['object'] and row['library'] is None)
    verify_slot_isolation(before, after, target, original)
    require(protected_graphs(bpy, expected=graphs) == graphs, 'VALIDATION_FAILED', 'A protected material or node group changed')
    retained = report.get('retained_material')
    expected_report = report
    if action == 'restore':
        require(report['original_material'] == result['actual_material'], 'VALIDATION_FAILED', 'Original slot material did not reattach')
        held = bpy.data.materials.get(retained)
        require(held is not None, 'VALIDATION_FAILED', 'Native component did not remain available for recovery')
        if not held.users:
            held.use_fake_user = True
        with named_slot(context, {**target, 'expected_material': result['actual_material']}) as obj:
            expected_report = domain.slots.inspect(context.scene, obj, target['material_slot'])
    save_candidate(bpy, request)
    result.update(sha256=digest(request['stage']), verification={'bindings': after, 'dependencies': external,
                  'graphs': graphs, 'retained': retained, 'report': expected_report})
    return result


def save_candidate(bpy, request):
    bpy.context.preferences.filepaths.save_version = 0
    require(bpy.ops.wm.save_as_mainfile(filepath=request['stage'], copy=True, relative_remap=True, check_existing=False)
            == {'FINISHED'}, 'IO_ERROR', 'Cannot save the Toon component candidate')


def export_component(domain, context, obj, index, request):
    state = domain.slots.inspect(context.scene, obj, index)
    require(state['status'] == 'valid', 'CONFLICT', 'Export requires a valid native component in the named slot')
    domain.register()
    domain._settings(context).json_path = request['stage']
    report = domain.export_current(context)
    expected = domain._normalized(state['settings'])
    return {'report': report, 'settings': expected, 'actual_material': state['material'],
            'sha256': digest(request['stage'])}


def transmission_settings(bpy):
    from inspection import configuration, properties

    return {scene.name_full: {'render': properties(scene.render), 'eevee': configuration(scene.eevee),
                              'ray_options': properties(scene.eevee.ray_tracing_options)}
            for scene in bpy.data.scenes}


def transmission(bpy, domain, context, request, proof):
    scene = context.scene
    require(scene.library is None and scene.override_library is None and scene.is_editable, 'CONFLICT',
            'Use an editable local scene for transmission')
    require(scene.render.engine == 'BLENDER_EEVEE', 'UNSUPPORTED', 'Transmission opt-in requires EEVEE')
    settings = transmission_settings(bpy)
    report = {'status': 'enabled', 'scene': scene.name_full, 'eevee_raytracing': True}
    if proof is not None:
        require(settings == proof['settings'] and bindings(bpy) == proof['bindings']
                and dependencies(bpy) == proof['dependencies']
                and protected_graphs(bpy, expected=proof['graphs']) == proof['graphs'],
                'VALIDATION_FAILED', 'Cold reopen changed scene settings, slots, dependencies or materials')
        return {'report': report, 'sha256': digest(request['stage'])}

    initial_slots, external, graphs = bindings(bpy), dependencies(bpy), protected_graphs(bpy)
    # The native action also writes a panel message. Keep saved panel fields intact.
    panel_exists = domain.PROP in scene
    domain.register()
    panel = domain._settings(context)
    saved_message = panel.message if panel.is_property_set('message') else None
    result = domain.enable_transmission_current(context)
    if not panel_exists and domain.PROP in scene:
        del scene[domain.PROP]
    elif saved_message is None:
        panel.property_unset('message')
    else:
        panel.message = saved_message

    settings[scene.name_full]['eevee']['use_raytracing'] = True
    require(result['status'] == 'enabled' and transmission_settings(bpy) == settings and bindings(bpy) == initial_slots
            and protected_graphs(bpy, expected=graphs) == graphs,
            'VALIDATION_FAILED', 'Transmission changed settings or materials outside the named scene switch')
    save_candidate(bpy, request)
    return {'report': report, 'actual_material': None, 'sha256': digest(request['stage']),
            'verification': {'settings': settings, 'bindings': initial_slots, 'dependencies': external, 'graphs': graphs}}


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
            raise Failure('CANCELLED', 'Component operation cancelled before publication')
        require(time.monotonic() < deadline, 'TIMEOUT', 'Component operation exceeded its timeout')

    def status(state, phase):
        atomic_json(job / 'status.json', {'job_id': job.name, 'state': state, 'phase': phase,
                    'pid': os.getpid(), 'heartbeat_at': time.time()})

    status('running', 'verify_inputs')
    try:
        with ExitStack() as guards:
            request = request_for(args, guards)
            request['product'] = native_product(request, guards)
            blender = canonical(args.blender)
            output = None
            json_output = request['operation'] == 'export-json'
            if args.output:
                extension = '.json' if json_output else '.blend'
                require(args.output.is_absolute() and args.output.suffix.lower() == extension,
                        'INVALID_REQUEST', 'Output requires a new absolute ' + extension + ' path')
                parent = canonical(args.output.parent)
                for path in (parent, *parent.parents):
                    if path != Path(path.anchor):
                        guards.enter_context(FileGuard(path, directory=True))
                output = parent / args.output.name
                guards.enter_context(PathLocks([output]))
                require(not os.path.lexists(output), 'CONFLICT', 'Output already exists')
                if json_output:
                    staging = guards.enter_context(tempfile.TemporaryDirectory(dir=parent, prefix='.toon-component-'))
                    stage = Path(staging) / 'component.json'
                else:
                    with tempfile.NamedTemporaryFile(dir=parent, prefix='.toon-component-', suffix='.blend', delete=False) as file:
                        stage = Path(file.name)
                    guards.callback(stage.unlink, missing_ok=True)
                request['stage'] = str(stage)
            atomic_json(job / 'request.json', {'schema_version': SCHEMA, 'command': COMMAND, 'params': request})
            status('running', 'author')
            result = run_worker(blender, job, 'author', deadline, checkpoint, status, entry=ENTRY)
            if output:
                status('running', 'verify')
                expected_sha = result.pop('sha256')
                if not json_output:
                    verified = run_worker(blender, job, 'verify', deadline, checkpoint, status, entry=ENTRY)
                    require(verified['runtime']['pid'] != result['runtime']['pid'],
                            'VALIDATION_FAILED', 'Expected independent reopen')
                    require(verified['sha256'] == expected_sha, 'CONFLICT', 'Candidate SHA differs after reopen')
                with FileGuard(stage, renameable=True) as lock:
                    require(lock.sha256(checkpoint) == expected_sha, 'CONFLICT', 'Verified output changed before publication')
                    if json_output:
                        require(read_document(stage) == result['settings'], 'VALIDATION_FAILED',
                                'Exported JSON differs from the native component settings')
                    checkpoint()
                    lock.rename_new(output)
                if json_output:
                    result['component_json'] = {'file': str(output), 'sha256': expected_sha}
                    result['verification'] = {'status': 'passed', 'native_settings': 'reopened_json_match'}
                    artifacts['component_json'] = str(output)
                else:
                    proof = result.pop('verification')
                    result.update(candidate={'file': str(output), 'sha256': expected_sha}, independent_reopen=verified['runtime'],
                                  verification={'status': 'passed', 'protected_graphs': len(proof['graphs']),
                                                'slot_bindings_and_dependency_paths': 'preserved'})
                    artifacts.update(candidate=str(output), independent_reopen=str(job / 'verify-result.json'))
            data = {**result, 'operation': request['operation'], 'context': request['context'],
                    'source': {'file': request['file'], 'sha256': request['expected_sha256']},
                    'source_unchanged': True, 'product': request['product']}
            if 'target' in request:
                data['target'] = request['target']
            if 'component_json' in request:
                data['json_source'] = request['component_json']
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
            print(result['data']['report']['status'], result['data']['actual_material'])
            output = result['artifacts'].get('candidate') or result['artifacts'].get('component_json')
            if output:
                print(output)
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
