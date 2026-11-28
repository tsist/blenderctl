"""Inspect, update or export a saved Toon Pencil brush author."""

import argparse
from contextlib import ExitStack
import copy
import importlib
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

from filesystem import FileGuard, PathLocks
from material_draft_handoff import Parser, canonical, failure, fingerprint, guard_file, open_saved_context, read_document, require
from material_projection_handoff import run_worker
from protocol import CODES, DEFAULT_BLENDER, DEFAULT_JOBS, Failure, atomic_json, digest, envelope, read_json, timeout_value
from toon_panel_handoff import PRODUCTS, dependencies, load_modules, name, native_product
import toon_brush_controls as controls


COMMAND = 'toon-workflow.brush-settings'
DOMAIN = 'toon_workflow.toon_viewport_preview'
OPEN_DRAFT = 'open-draft'
BLEND_WRITES = ('update', controls.WRITE, OPEN_DRAFT)
NULLABLE = {
    ('raster', 'antialiasing'): 0.0,
    ('raster', 'samples_per_axis'): 1,
    ('chain', 'weld_edges'): False,
    ('chain', 'closed_orientation'): '',
}


def parser():
    cli = Parser(description=__doc__)
    cli.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    cli.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS)
    cli.add_argument('--transactions-dir', help=argparse.SUPPRESS)
    cli.add_argument('--timeout', type=float, default=120)
    display = cli.add_mutually_exclusive_group()
    for flag in ('--human', '--compact', '--full'):
        display.add_argument(flag, action='store_true')
    action = cli.add_subparsers(required=True, parser_class=Parser).add_parser(
        'brush-settings', description='Read and edit a saved Pencil brush, or run export-style to write native JSON.',
        epilog='Params: operation, edition, file, expected_sha256, context {scene,view_layer,frame}. '
               'inspect lists native names/routes/actual IDs, or reads one actual. '
               'update takes actual and edits [{field,value,optional expected_value}]. Use dotted fields, '
               'for example shape.stretch or maps.size.uv_name. Update saves a new .blend. '
               'open-draft takes actual and saves a native editable Text to a new .blend; inspect returns its identity. '
               'inspect-controls takes actual and optional sample_frame. apply-controls takes actual, '
               'expected_controls_sha256, and actions PREPARE_CHAINS/PREPARE_DASHES/PREPARE_MAP_AMOUNTS, '
               'RESTORE_CHAIN/RESTORE_DASH/RESTORE_MAP_AMOUNT, SET_AMOUNT_ENABLED(enabled), '
               'SET_CHAIN/SET_DASH/SET_MAP_AMOUNT(field,value). Chain angles use split_angle_degrees '
               'and line_direction_degrees, with separate split_enabled and direction_enabled switches. '
               'Set dash mode to DASHED before changing dash_length_px or gap_length_px. '
               'STAGE_AMOUNT_KEY(expected_value,value) edits the saved pending slider, '
               'KEY_STAGED_AMOUNT(expected_value, optional frame) keys it on the specified brush. '
               'KEY_AMOUNT(value, optional frame), REMOVE_AMOUNT_KEY(optional frame). '
               'apply-controls saves a new .blend. export-style writes a new native author JSON.')
    action.add_argument('--params', type=Path, required=True, help='UTF-8 JSON with an absolute .blend input')
    action.add_argument('--output', type=Path, help='New absolute .blend or native style .json')
    return cli


def request_for(args, guards):
    data = read_document(guard_file(args.params.absolute(), guards))
    common = {'operation', 'edition', 'file', 'expected_sha256', 'context'}
    require(isinstance(data, dict) and common <= data.keys(), 'INVALID_REQUEST', 'Missing brush-settings fields')
    op = data['operation']
    require(op in ('inspect', 'export-style', controls.READ, *BLEND_WRITES), 'INVALID_REQUEST', 'Unknown brush operation')
    if op in (controls.READ, controls.WRITE):
        controls.normalize(data, common)
    elif op == 'update':
        require(data.keys() == common | {'actual', 'edits'}, 'INVALID_REQUEST', 'Update requires exact actual and edits')
        edits = data['edits']
        require(type(edits) is list and 1 <= len(edits) <= 256, 'INVALID_REQUEST', 'Expected 1..256 explicit leaf edits')
        fields = set()
        for edit in edits:
            require(type(edit) is dict and {'field', 'value'} <= edit.keys() <= {'field', 'value', 'expected_value'}
                    and name(edit['field']) and edit['field'] not in fields,
                    'INVALID_REQUEST', 'Each edit requires one unique dotted field, value, and optional expected_value')
            fields.add(edit['field'])
    elif op == OPEN_DRAFT:
        require(data.keys() == common | {'actual'}, 'INVALID_REQUEST', 'Opening a brush draft requires its exact actual')
    else:
        expected = common | ({'actual'} if op == 'inspect' else set())
        require(data.keys() <= expected, 'INVALID_REQUEST', f'Unexpected fields for {op}')
    require('actual' not in data or name(data['actual']), 'INVALID_REQUEST', 'Expected exact actual brush ID')
    require(isinstance(data['edition'], str) and data['edition'] in PRODUCTS, 'UNSUPPORTED', 'Unknown Toon edition')
    context = data['context']
    require(isinstance(context, dict) and context.keys() == {'scene', 'view_layer', 'frame'}
            and name(context['scene']) and name(context['view_layer']) and type(context['frame']) is int,
            'INVALID_REQUEST', 'Expected saved scene, view layer and integer frame')
    require(isinstance(data['file'], str) and Path(data['file']).suffix.lower() == '.blend'
            and isinstance(data['expected_sha256'], str), 'INVALID_REQUEST', 'Expected a hash-pinned .blend')
    data['file'] = str(guard_file(data['file'], guards, data['expected_sha256']))
    require((op in ('inspect', controls.READ)) == (args.output is None), 'INVALID_REQUEST',
            'Only brush edits and export-style require --output')
    return data


def brush_product(request, guards):
    product = native_product(request, guards)
    root = Path(product['directory'])
    manifest = read_json(root / 'bundle.json')
    entries = {key: value for key, value in manifest['files'].items()
               if key.startswith('toon_viewport_preview/') and key.endswith('.py')}
    require(entries, 'UNSUPPORTED', 'Frozen Toon package has no Pencil author files')
    for relative, expected in entries.items():
        path = canonical(root / relative)
        guard = guards.enter_context(FileGuard(path))
        require(guard.sha256() == expected, 'CONFLICT', f'Pencil author source SHA differs: {relative}')
    return {**product, 'pencil_files_verified': len(entries), 'pencil_version': manifest['pencil_source_version']}


def native_modules(bpy, request):
    load_modules(bpy, request)
    branch = importlib.import_module(DOMAIN)
    require('.'.join(map(str, branch.bl_info['version'])) == request['product']['pencil_version'],
            'CONFLICT', 'Loaded Pencil implementation differs from the manifest')
    modules = [importlib.import_module(DOMAIN + '.' + name) for name in (
        'addon', 'brush_ui', 'brush_effects', 'file_authoring', 'source_bindings',
        'curve_editor', 'amount_animation', 'parameter_animation',
    )]
    modules[0].register()
    return modules


def field_names(ui):
    return {'.'.join(path): path for path in ui._FIELDS}


def flat_effects(ui, row):
    return {key: ui._get(row, path) for key, path in field_names(ui).items()}


def actual_inventory(plan, identities):
    labels = {group['id']: group['label'] for group in plan['groups']}
    routes = {key: [] for key in identities['actuals']}
    for route in identities['routes']:
        group = route['group_id']
        routes[route['actual_brush_id']].append({
            'group': group, 'label': labels.get(group) or group,
            'kind': route['kind'], 'slot': route['slot'],
        })
    return [{'actual': key, 'name': row['source_brush_key'], 'routes': routes[key]}
            for key, row in sorted(identities['actuals'].items())]


def valid_type(path, default, value):
    if default is None:
        if value is None:
            return True
        default = NULLABLE[path]
    kind = type(default)
    if kind is float:
        return type(value) in (int, float) and abs(value) <= 1e12 and math.isfinite(value)
    if kind is list:
        return type(value) is list and len(value) == len(default) and all(
            valid_type(path, default[0], item) for item in value)
    return type(value) is kind


def make_patch(ui, effects, existing, edits):
    registry = field_names(ui)
    defaults = effects.defaults()
    patch = {}
    for edit in edits:
        field = edit['field']
        require(field in registry, 'INVALID_REQUEST', f'Field is not a native scalar, enum or vector control: {field}')
        path = registry[field]
        default = ui._get(defaults, path)
        value = edit['value']
        require(valid_type(path, default, value), 'INVALID_REQUEST', f'Wrong native field type: {field}')
        if 'expected_value' in edit:
            expected = edit['expected_value']
            require(valid_type(path, default, expected), 'INVALID_REQUEST', f'Wrong expected field type: {field}')
            require(ui._get(existing, path) == expected, 'CONFLICT', f'Saved brush value differs: {field}')
        row = patch
        for token in path[:-1]:
            row = row.setdefault(token, {})
        row[path[-1]] = value
    try:
        desired = effects.checked(effects.merge(existing, patch))
    except (ValueError, TypeError, OverflowError) as exc:
        raise Failure('INVALID_REQUEST', str(exc)) from exc
    changes = [{'field': field, 'before': ui._get(existing, registry[field]),
                'after': ui._get(desired, registry[field])}
               for field in (edit['field'] for edit in edits)
               if ui._get(existing, registry[field]) != ui._get(desired, registry[field])]
    require(changes, 'INVALID_REQUEST', 'No stored brush field would change')
    return patch, desired, changes


def animation_witness(scene, amount, animation):
    result = {'amount': amount.signature(scene), 'parameter': animation.signature(scene)}
    if result['amount'] or result['parameter']:
        action, slot, bag, _ = amount._graph(scene)
        result['action'] = action.name if action else None
        result['slot'] = slot.identifier if slot else None
        result['keys'] = [(fc.data_path, fc.array_index, [amount._key_state(key) for key in fc.keyframe_points])
                          for fc in bag.fcurves] if bag else []
    return fingerprint(result)


def other_styles(bpy, selected):
    result = {}
    for scene in bpy.data.scenes:
        if scene == selected:
            continue
        settings = scene.toon_viewport_preview
        text = settings.preset_text
        result[scene.name_full] = [text.name, fingerprint(text.as_string()), settings.preset_revision] if text else None
    return result


def draft_ref(bpy, scene):
    text = bpy.data.texts.get(scene.get('toon_brush_effect_draft', ''))
    if text is None:
        return None
    return {'name': text.name_full, 'actual': text.get('actual_brush'), 'sha256': fingerprint(text.as_string()),
            'owned': bool(text.get('toon_brush_effect_draft')), 'persistent': bool(text.use_fake_user)}


def saved_witness(bpy, scene, file_author, amount, animation):
    return {'style': fingerprint(file_author.read_scene(scene)),
            'pending_amount_key': scene.toon_viewport_preview.amount_key_value,
            'controls': fingerprint(file_author.capture_controls(scene)),
            'animation': animation_witness(scene, amount, animation),
            'other_scenes': other_styles(bpy, scene), 'dependencies': dependencies(bpy), 'draft': draft_ref(bpy, scene),
            'active': bpy.context.object.name if bpy.context.object else None,
            'selected': sorted(obj.name for obj in bpy.context.selected_objects)}


def preserve_pending(scene, curve_editor):
    settings = scene.toon_viewport_preview
    tree = settings.curve_tree
    if tree and tree.get('toon_viewport_curve_role') == curve_editor.ROLE:
        require(curve_editor.signature(tree, settings) == tree.get('last_signature'), 'CONFLICT',
                'Save the pending curve editor changes from the author before handing off brush settings')


def work(phase, request, job):
    import bpy

    modules = native_modules(bpy, request)
    addon, ui, effects, file_author, sources, curves, amount, animation = modules
    file = request['stage'] if phase == 'verify' else request['file']
    context = open_saved_context(bpy, {**request, 'file': file})
    scene = context.scene
    addon.wire_ui._require(scene)
    operation = request['operation']
    runtime = {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(), 'pid': os.getpid()}
    if phase == 'verify':
        assert operation in BLEND_WRITES
        return {'witness': saved_witness(bpy, scene, file_author, amount, animation), 'runtime': runtime}

    plan = addon._preset(scene)
    doc = plan['raw']
    ids = sources.recover(doc)
    inventory = actual_inventory(plan, ids)
    actual = request.get('actual')
    record = {'runtime': runtime, 'operation': operation, 'fields': list(field_names(ui))}
    if actual is not None:
        selected = next((row for row in inventory if row['actual'] == actual), None)
        require(selected, 'NOT_FOUND', 'Unknown actual brush identity in this saved author')
        record['brush'] = selected
    if operation == 'inspect':
        record['draft'] = draft_ref(bpy, scene)
        if actual is None:
            return {**record, 'brushes': inventory}
        return {**record, 'settings': flat_effects(ui, ui.get_effects(scene, actual))}
    if operation == controls.READ:
        current = controls.state(scene, actual, file_author, amount)
        observed = controls.sample(context, addon, plan, actual, request.get('sample_frame'))
        return {**record, 'controls': current, 'controls_sha256': fingerprint(current), 'sample': observed,
                'pending_amount_key': scene.toon_viewport_preview.amount_key_value}

    preserve_pending(scene, curves)
    if operation == OPEN_DRAFT:
        before = saved_witness(bpy, scene, file_author, amount, animation)
        effects_before = ui.get_effects(scene, actual)
        text = ui.open_author(scene, actual)
        draft = draft_ref(bpy, scene)
        require(draft and draft['owned'] and draft['persistent'] and draft['actual'] == actual
                and json.loads(text.as_string()) == {'actual': actual, 'effects': effects_before},
                'VALIDATION_FAILED', 'Native draft did not copy the chosen brush settings')
        witness = saved_witness(bpy, scene, file_author, amount, animation)
        require(witness == {**before, 'draft': draft}, 'VALIDATION_FAILED', 'Opening a draft changed saved brush content')
        if before['draft'] is not None:
            previous = bpy.data.texts[before['draft']['name']]
            require(fingerprint(previous.as_string()) == before['draft']['sha256'], 'VALIDATION_FAILED',
                    'Opening a draft replaced the prior Text content')
        bpy.context.preferences.filepaths.save_version = 0
        require(bpy.ops.wm.save_as_mainfile(filepath=request['stage'], copy=True, relative_remap=True,
                                          check_existing=False) == {'FINISHED'}, 'IO_ERROR', 'Could not save the brush draft candidate')
        atomic_json(job / 'expected-witness.json', witness)
        return {**record, 'draft': draft, 'previous_draft': before['draft'], 'output_sha256': digest(request['stage'])}
    if operation == controls.WRITE:
        current, events = controls.apply(bpy, context, request, modules, plan, ids['actuals'], other_styles, dependencies)
        witness = saved_witness(bpy, scene, file_author, amount, animation)
        bpy.context.preferences.filepaths.save_version = 0
        require(bpy.ops.wm.save_as_mainfile(filepath=request['stage'], copy=True, relative_remap=True,
                                          check_existing=False) == {'FINISHED'}, 'IO_ERROR', 'Could not save the brush controls candidate')
        atomic_json(job / 'expected-witness.json', witness)
        return {**record, 'controls': current, 'controls_sha256': fingerprint(current),
                'pending_amount_key': scene.toon_viewport_preview.amount_key_value,
                'events': events, 'output_sha256': digest(request['stage'])}
    if operation == 'export-style':
        original = file_author.read_scene(scene)
        file_author.export_style(scene, request['stage'])
        exported = file_author.checked(read_json(request['stage']))
        require(exported == {**original, 'schema': file_author.SCHEMA}, 'VALIDATION_FAILED',
                'Exported style differs from the saved author document')
        return {**record, 'output_sha256': digest(request['stage']), 'style_schema': file_author.SCHEMA}

    existing = ui.get_effects(scene, actual)
    patch, desired, changes = make_patch(ui, effects, existing, request['edits'])
    before = file_author.read_scene(scene)
    protected = saved_witness(bpy, scene, file_author, amount, animation)
    rows = effects.records(before['document'], ids['actuals'])
    rows[actual] = desired
    desired_document = copy.deepcopy(before)
    desired_document['document']['request']['brush_effects'] = {'schema': effects.SCHEMA, 'brushes': rows}
    desired_document['document']['inputs']['projection']['editor_document']['revision'] += 1

    native_result = ui.set_effects(scene, [{'actual': actual, 'effects': patch}])
    live = file_author.read_scene(scene)
    desired_controls = desired_document['scene_controls']
    # The native migration can initialize an absent derived table. Explicit
    # authored tables must retain their values exactly.
    for key in ('chain_controls', 'map_amounts', 'dash_patterns'):
        if desired_controls[key] is None:
            desired_controls[key] = live['scene_controls'][key]
    require(live == desired_document and ui.get_effects(scene, actual) == desired,
            'VALIDATION_FAILED', 'Local brush update touched unrelated author fields or did not persist')
    witness = saved_witness(bpy, scene, file_author, amount, animation)
    for key in ('animation', 'other_scenes', 'dependencies', 'active', 'selected'):
        require(witness[key] == protected[key], 'VALIDATION_FAILED', f'Brush update changed protected {key}')
    require(native_result['updated_actuals'] == [actual] and not native_result['native_flags_changed'],
            'VALIDATION_FAILED', 'Native brush migration touched a different scope')
    bpy.context.preferences.filepaths.save_version = 0
    require(bpy.ops.wm.save_as_mainfile(filepath=request['stage'], copy=True, relative_remap=True,
                                      check_existing=False) == {'FINISHED'}, 'IO_ERROR', 'Could not save the brush candidate')
    atomic_json(job / 'expected-witness.json', witness)
    return {**record, 'changes': changes, 'settings': flat_effects(ui, desired),
            'native_result': native_result, 'output_sha256': digest(request['stage'])}


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
    output_receipt = job / 'result.json'
    artifacts = {'job_directory': str(job), 'result': str(output_receipt)}
    data = error = None

    def checkpoint():
        require(time.monotonic() < deadline, 'TIMEOUT', 'Brush author deadline expired')
        require(not (job / 'cancel.request').exists(), 'CANCELLED', 'Brush author job cancelled')

    def heartbeat(state, phase):
        atomic_json(job / 'status.json', {'job_id': job.name, 'state': state, 'phase': phase, 'heartbeat_at': time.time()})

    try:
        with ExitStack() as guards:
            request = request_for(args, guards)
            blender = canonical(args.blender)
            request['product'] = brush_product(request, guards)
            operation = request['operation']
            if args.output is not None:
                suffix = '.blend' if operation in BLEND_WRITES else '.json'
                require(args.output.is_absolute() and args.output.suffix.lower() == suffix,
                        'INVALID_REQUEST', f'{operation} requires a new absolute {suffix} output')
                parent = canonical(args.output.parent)
                for directory in reversed((parent, *parent.parents)):
                    if directory != Path(directory.anchor):
                        guards.enter_context(FileGuard(directory, directory=True))
                output = parent / args.output.name
                guards.enter_context(PathLocks([output]))
                require(not os.path.lexists(output), 'CONFLICT', 'Output already exists')
                stage = parent / ('.toon-brush-' + job.name + suffix)
                guards.callback(stage.unlink, missing_ok=True)
                request['stage'] = str(stage)
            atomic_json(job / 'request.json', request)
            heartbeat('running', 'author')
            result = run_worker(blender, job, 'author', deadline, checkpoint, heartbeat, ENTRY)
            if args.output is not None:
                expected = result.pop('output_sha256')
                if operation in BLEND_WRITES:
                    with FileGuard(stage) as guard:
                        require(guard.sha256() == expected, 'CONFLICT', 'Staged author SHA differs')
                        heartbeat('running', 'verify')
                        cold = run_worker(blender, job, 'verify', deadline, checkpoint, heartbeat, ENTRY)
                    require(cold['runtime']['pid'] != result['runtime']['pid']
                            and cold['witness'] == read_json(job / 'expected-witness.json'),
                            'VALIDATION_FAILED', 'Independent reopen changed the author, animation or protected content')
                    result['independent_reopen'] = cold['runtime']
                    artifacts['brush_witness'] = str(job / 'verify-result.json')
                with FileGuard(stage, renameable=True) as guard:
                    require(guard.sha256() == expected, 'CONFLICT', 'Verified author SHA changed before publication')
                    checkpoint()
                    guard.rename_new(output)
                key = 'candidate' if operation in BLEND_WRITES else 'style'
                result[key] = {'file': str(output), 'sha256': expected}
                artifacts[key] = str(output)
            data = {**result, 'source': {'file': request['file'], 'sha256': request['expected_sha256']},
                    'source_unchanged': True, 'context': request['context'], 'product': request['product']}
    except (Failure, OSError, RuntimeError, ValueError) as exc:
        error = failure(exc, 'IO_ERROR')
    receipt = envelope(COMMAND, job.name, data, error, artifacts)
    atomic_json(output_receipt, receipt)
    state = {'TIMEOUT': 'timed_out', 'CANCELLED': 'cancelled'}.get(error['code'], 'failed') if error else 'succeeded'
    heartbeat(state, 'done')
    return receipt


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    args = None
    try:
        args = parser().parse_args()
        result = run(args)
    except (Failure, OSError) as exc:
        result = envelope(COMMAND, error=failure(exc, 'IO_ERROR'))
    if args and args.human:
        print(result['artifacts']['result'] if result['ok'] else result['error']['message'])
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['ok'] else CODES[result['error']['code']]


if __name__ == '__main__':
    if '--background' in sys.argv and '--' in sys.argv:
        phase, request = sys.argv[sys.argv.index('--') + 1:]
        worker(phase, Path(request))
    else:
        sys.exit(main())
