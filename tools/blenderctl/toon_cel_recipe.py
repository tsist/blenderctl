"""Run the frozen Toon CEL author on an explicitly identified saved scene."""
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
from toon_component_slot import protected_graphs
from toon_panel_handoff import PRODUCTS, bindings, dependencies, load_modules, name, named_slot, native_product


COMMAND = 'toon-workflow.cel-recipe'
EXTRA = {
    'new': (), 'new-composite': (), 'fork-composite': (), 'edit': ('patch',),
    'bind-slot': ('slot',), 'bind-line': ('line',), 'remove-binding': ('binding',),
    'inspect': ('rebind_slots',), 'apply': ('rebind_slots',),
    'import': ('recipe_file', 'recipe_sha256'), 'export': (), 'read-archive': ('archive',),
}
READ_ONLY = {'inspect', 'read-archive'}
COMPOSITE = 'toon-cel-recipe.v2'
ARCHIVE = 'toon-cel-author-archive.v1'


def parser():
    cli = Parser(description=__doc__)
    cli.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    cli.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS)
    cli.add_argument('--transactions-dir', help=argparse.SUPPRESS)
    cli.add_argument('--timeout', type=float, default=120)
    mode = cli.add_mutually_exclusive_group()
    for flag in ('--human', '--compact', '--full'):
        mode.add_argument(flag, action='store_true')
    sub = cli.add_subparsers(required=True, parser_class=Parser).add_parser(
        'cel-recipe', description=__doc__, epilog=(
            'Params: operation, edition (0.51.1 or 0.52.1), file, expected_sha256, '
            'context {scene,view_layer,frame}, expected_author_id (null for an empty author). '
            'Operations: ' + ', '.join(EXTRA) + '. Explicit rebind_slots are required to inspect/apply an '
            'unapplied draft with saved slots. A recipe export carries no object bindings or scene line policy.'))
    sub.add_argument('--params', type=Path, required=True, help='UTF-8 JSON with absolute input paths')
    sub.add_argument('--output', type=Path, help='New absolute .blend or exported recipe .json')
    return cli


def validate_slot(slot):
    require(isinstance(slot, dict) and slot.keys() == {'object', 'slot_index', 'material', 'link', 'role'}
            and name(slot['object']) and type(slot['slot_index']) is int and slot['slot_index'] >= 0
            and (slot['material'] is None or name(slot['material'])) and slot['link'] in ('DATA', 'OBJECT')
            and name(slot['role']), 'INVALID_REQUEST', 'Expected exact object, slot_index, material, link and role')


def request_for(args, guards):
    request = read_document(guard_file(args.params.absolute(), guards))
    common = {'operation', 'edition', 'file', 'expected_sha256', 'context', 'expected_author_id'}
    require(isinstance(request, dict) and common <= request.keys(), 'INVALID_REQUEST', 'Missing CEL recipe fields')
    op = request['operation']
    require(isinstance(op, str) and op in EXTRA, 'INVALID_REQUEST', 'Unknown CEL recipe operation')
    extras = set(EXTRA[op])
    require(request.keys() <= common | extras
            and (op in ('inspect', 'apply') or extras <= request.keys()),
            'INVALID_REQUEST', f'Missing or unexpected parameters for {op}')
    require(isinstance(request['edition'], str) and request['edition'] in PRODUCTS, 'UNSUPPORTED', 'Unknown Toon edition')
    ctx = request['context']
    require(isinstance(ctx, dict) and ctx.keys() == {'scene', 'view_layer', 'frame'}
            and name(ctx['scene']) and name(ctx['view_layer']) and type(ctx['frame']) is int,
            'INVALID_REQUEST', 'Expected exact scene, view_layer and integer frame')
    expected = request['expected_author_id']
    require(expected is None or name(expected), 'INVALID_REQUEST', 'Expected an author identity or null')
    require(op not in ('new', 'new-composite') or expected is None, 'INVALID_REQUEST', 'New author requires null baseline')
    require(op in ('new', 'new-composite', 'import', 'read-archive') or expected is not None,
            'INVALID_REQUEST', 'This command requires an exact existing author identity')
    require(isinstance(request['file'], str) and Path(request['file']).suffix.lower() == '.blend'
            and isinstance(request['expected_sha256'], str), 'INVALID_REQUEST', 'Expected a hash-pinned .blend')
    request['file'] = str(guard_file(request['file'], guards, request['expected_sha256']))
    require((op in READ_ONLY) == (args.output is None), 'INVALID_REQUEST',
            'Specify --output for file creation and omit it for read-only commands')
    if op == 'import':
        path, sha = request['recipe_file'], request['recipe_sha256']
        require(isinstance(path, str) and Path(path).suffix.lower() == '.json' and isinstance(sha, str),
                'INVALID_REQUEST', 'Import requires a hash-pinned recipe JSON')
        request['recipe_file'] = str(guard_file(path, guards, sha))
    if op == 'edit':
        patch = request['patch']
        require(isinstance(patch, dict) and patch and patch.keys() <= {'name', 'revision', 'line_policy', 'roles', 'lines'},
                'INVALID_REQUEST', 'Expected a nonempty recipe patch')
        require(all(isinstance(patch.get(key, {}), dict) for key in ('roles', 'lines')),
                'INVALID_REQUEST', 'roles and lines must be keyed objects')
    if op == 'bind-slot':
        validate_slot(request['slot'])
    if 'rebind_slots' in request:
        require(isinstance(request['rebind_slots'], list), 'INVALID_REQUEST', 'rebind_slots must be an explicit list')
        for slot in request['rebind_slots']:
            validate_slot(slot)
    if op == 'bind-line':
        line = request['line']
        require(isinstance(line, dict) and line.keys() == {'object', 'role'} and all(map(name, line.values())),
                'INVALID_REQUEST', 'Expected exact line object and role')
    if op == 'remove-binding':
        remove = request['binding']
        require(isinstance(remove, dict) and remove.keys() == {'kind', 'index'}
                and remove['kind'] in ('slots', 'line_targets')
                and type(remove['index']) is int and remove['index'] >= 0,
                'INVALID_REQUEST', 'Expected slots or line_targets and a nonnegative index')
    if op == 'read-archive':
        require(name(request['archive']), 'INVALID_REQUEST', 'Expected an exact archive Text name')
    return request


def guard_product(request, guards):
    product = native_product(request, guards)
    directory = Path(product['directory'])
    manifest = read_json(directory / 'bundle.json')
    pencil = [key for key in manifest['files'] if key.startswith('toon_viewport_preview/')]
    require('toon_viewport_preview/__init__.py' in pencil, 'CONFLICT', 'Frozen Pencil payload is absent')
    for relative in pencil:
        guard_file(directory / relative, guards, manifest['files'][relative])
    product['pencil_files_verified'] = len(pencil)
    return product


def slot_target(reference):
    return {'object': reference['object'], 'material_slot': reference['slot_index'],
            'expected_material': reference['material'], 'expected_link': reference['link']}


def stored_slots(settings):
    return [{'object': row.target.name_full if row.target else None, 'slot_index': row.slot,
             'material': row.expected_name or None, 'link': row.link, 'role': row.role}
            for row in settings.slots]


def explicit_bind_slot(ui, context, reference):
    with named_slot(context, slot_target(reference)):
        ui._settings(context).bind_role = reference['role']
        ui.bind_slot(context)


def rebind(ui, context, supplied):
    settings = ui._settings(context)
    if supplied is None:
        return
    require(not settings.receipt, 'CONFLICT', 'An applied author retains its original binding identities')
    require(supplied == stored_slots(settings), 'CONFLICT',
            'rebind_slots must identify every saved slot in its current order, with its exact role and baseline')
    require(all(row.target == context.view_layer.objects.get(slot['object'])
                for row, slot in zip(settings.slots, supplied)), 'CONFLICT',
            'The saved slot target no longer resolves to the same object in the view layer')
    previous_role = settings.bind_role
    settings.slots.clear()
    for slot in supplied:
        explicit_bind_slot(ui, context, slot)
    settings.bind_role = previous_role


def mapping_patch(values, patch, label):
    require(isinstance(patch, dict) and patch and patch.keys() <= values.keys(), 'INVALID_REQUEST',
            f'Invalid {label} fields')
    return {**values, **patch}


def values_from_rna(row, fields):
    return {key: list(value) if hasattr(value, '__len__') and not isinstance(value, str) else value
            for key in fields for value in (getattr(row, key),)}


def role_change(ui, row, patch, composite):
    require(isinstance(patch, dict) and patch and patch.keys() <= {'action', 'settings', 'boundary', 'finish'},
            'INVALID_REQUEST', 'Invalid CEL role fields')
    action = patch.get('action', row.action)
    if action == 'PRESERVE':
        require(patch.keys() == {'action'}, 'INVALID_REQUEST', 'A PRESERVE role carries no surface, boundary or finish')
        return {'action': action}
    require(action == 'SURFACE', 'INVALID_REQUEST', 'Unknown CEL surface role action')
    requested = patch.get('settings', {})
    require(isinstance(requested, dict), 'INVALID_REQUEST', 'Surface settings must be an object')
    existing_mode = row.surface.mode
    if existing_mode not in ('eevee_cel3', 'eevee_cel3_reflect'):
        existing_mode = 'eevee_cel3'
    mode = requested.get('mode', existing_mode)
    require(mode in ('eevee_cel3', 'eevee_cel3_reflect'), 'INVALID_REQUEST', 'Unsupported CEL surface mode')
    surface = {**values_from_rna(row.surface, ui.surface.materials._fields(mode)), 'mode': mode}
    values = {'action': action, 'settings': mapping_patch(surface, requested, 'surface') if requested else surface}
    require(composite or not patch.keys() & {'boundary', 'finish'},
            'INVALID_REQUEST', 'Boundary and finish require a composite author')
    if composite:
        boundary = {'width_low': row.boundary_width_low, 'width_high': row.boundary_width_high}
        if 'boundary' in patch and patch['boundary'] is not None:
            values['boundary'] = mapping_patch(boundary, patch['boundary'], 'boundary')
        elif 'boundary' not in patch and row.use_boundary:
            values['boundary'] = boundary
        if row.use_finish or patch.get('finish') is not None:
            finish = importlib.import_module(ui.__package__ + '.finish_nodes')
            current = values_from_rna(row.finish, finish.defaults())
            if 'finish' not in patch:
                values['finish'] = current
            elif patch['finish'] is not None:
                values['finish'] = mapping_patch(current, patch['finish'], 'finish')
    return values


def close_value(expected, actual):
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(close_value(expected[k], actual[k]) for k in expected)
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(map(close_value, expected, actual))
    if type(expected) is float:
        return math.isclose(expected, actual, rel_tol=1e-6, abs_tol=1e-7)
    return expected == actual


def edit(ui, context, patch):
    settings = ui._settings(context)
    require(not settings.receipt, 'CONFLICT', 'Fork the applied author before changing its recipe')
    doc = ui.document(context)
    composite = doc['schema'] == COMPOSITE
    policy = patch.get('line_policy', settings.line_policy)
    require('line_policy' not in patch or (composite and policy in ('CREATE_FROM_BASIC', 'PRESERVE')),
            'INVALID_REQUEST', 'line_policy requires a composite author and a supported policy')
    roles = {row.role: row for row in settings.roles}
    lines = {row.role: row for row in settings.lines}
    role_patches, line_patches = patch.get('roles', {}), patch.get('lines', {})
    require(role_patches.keys() <= roles.keys() and line_patches.keys() <= lines.keys(), 'NOT_FOUND',
            'Patch role names must belong to the current author')
    for key in ('name', 'revision'):
        if key in patch:
            doc[key] = patch[key]
    for role, values in role_patches.items():
        doc['roles'][role] = role_change(ui, roles[role], values, composite)
    doc['lines'] = [mapping_patch(line, line_patches[line['role']], 'line')
                    if line['role'] in line_patches else line for line in doc['lines']]
    for role, values in line_patches.items():
        require(isinstance(values, dict) and values.keys() <= {'width', 'color'}, 'INVALID_REQUEST',
                f'Line role {role} only accepts width and color')
    try:
        desired = ui._core(doc['schema']).normalize(doc)
    except (TypeError, ValueError, RuntimeError, OverflowError) as error:
        raise Failure('INVALID_REQUEST', str(error)) from error

    if 'name' in patch:
        settings.recipe_name = desired['name']
    if 'revision' in patch:
        settings.revision = desired['revision']
    if 'line_policy' in patch:
        settings.line_policy = policy
    for role, values in role_patches.items():
        row, spec = roles[role], desired['roles'][role]
        row.action = spec['action']
        if row.action == 'PRESERVE':
            row.use_boundary = row.use_finish = False
            continue
        if 'action' in values or 'settings' in values:
            ui.surface_ui._load(row.surface, spec['settings'])
        if 'boundary' in values:
            row.use_boundary = 'boundary' in spec
            if row.use_boundary:
                row.boundary_width_low = spec['boundary']['width_low']
                row.boundary_width_high = spec['boundary']['width_high']
        if 'finish' in values:
            row.use_finish = 'finish' in spec
            if row.use_finish:
                for field, value in spec['finish'].items():
                    setattr(row.finish, field, value)
    for spec in desired['lines']:
        role = spec['role']
        if role in line_patches:
            for key in line_patches[role]:
                setattr(lines[role], key, spec[key])
    require(close_value(desired, ui.document(context)) and settings.line_policy == policy,
            'VALIDATION_FAILED', 'RNA did not retain the requested recipe fields')
    return {'status': 'edited'}


def archive_data(bpy, ui, text_name):
    text = bpy.data.texts.get(text_name)
    require(text and not text.library and text.use_fake_user and text.get('toon_cel_author_archive') == ARCHIVE,
            'NOT_FOUND', 'Named native CEL archive is absent')
    record = json.loads(text.as_string())
    require(record.get('schema') == ARCHIVE, 'VALIDATION_FAILED', 'Invalid historical CEL archive schema')
    ui._core(record['document']['schema']).normalize(record['document'])
    return record


def author_state(bpy, ui, context):
    settings = ui._settings(context)
    return {'document': ui.document(context) if settings.recipe_id else None,
            'slots': stored_slots(settings),
            'slot_identity': [{'expected_uid': row.expected_uid, 'binding_epoch': row.binding_epoch,
                               'library': row.target.library.filepath if row.target and row.target.library else None}
                              for row in settings.slots],
            'line_targets': [{'object': row.target.name_full if row.target else None, 'role': row.role}
                             for row in settings.line_targets],
            'line_policy': settings.line_policy,
            'panel': {key: getattr(settings, key) for key in ('bind_role', 'line_role', 'file_path', 'message')},
            'receipt_sha256': fingerprint(json.loads(settings.receipt)) if settings.receipt else None,
            'archives': {row.name_full: fingerprint(archive_data(bpy, ui, row.name_full)) for row in bpy.data.texts
                         if row.get('toon_cel_author_archive') == ARCHIVE}}


def scene_structure(bpy):
    from inspection import identity, key, record

    values = (*bpy.data.objects, *bpy.data.meshes, *bpy.data.armatures, *bpy.data.actions)
    result = {}
    for item in values:
        state = record(item)
        if 'mesh' in state:
            del state['mesh']['materials']
        result[key(identity(item))] = fingerprint(state)
    return result


def protected_before_apply(bpy, ui, allowed):
    from inspection import identity, key

    baseline = protected_graphs(bpy)
    mutable = set()
    for obj in bpy.data.objects:
        for index, slot in enumerate(obj.material_slots):
            mat = slot.material
            if ((obj.name_full, index) in allowed and obj.library is None and mat and mat.library is None
                    and mat.get(ui.surface._PROFILE) == ui.surface.PROFILE
                    and mat.users - int(mat.use_fake_user) == 1):
                mutable.add(mat)
    if not mutable:
        return baseline
    finish = importlib.import_module(ui.__package__ + '.finish_core')
    owners = {mat[ui.surface._OWNER] for mat in mutable}
    scope = ui.surface.materials.SCOPE
    candidates = {group for group in bpy.data.node_groups if not group.library and not group.use_fake_user
                  and (group.get(scope) in owners or group.get(finish._SURFACE) in owners)}
    group_users = bpy.data.user_map(subset=candidates)
    while True:
        private = {group for group in candidates
                   if group_users.get(group) and group_users[group] <= mutable | candidates}
        if private == candidates:
            break
        candidates = private
    for item in mutable | candidates:
        baseline.pop(key(identity(item)), None)
    return baseline


def compare_slots(before, after, allowed):
    require(len(before) == len(after), 'VALIDATION_FAILED', 'Objects changed during CEL authoring')
    for old, new in zip(before, after):
        if old['library'] is None:
            for index, (old_slot, new_slot) in enumerate(zip(old['materials'], new['materials'])):
                if (old['object'], index) in allowed:
                    require(old_slot[0] == new_slot[0], 'VALIDATION_FAILED', 'CEL changed the slot link mode')
                    old['materials'][index] = new_slot
        require(old == new, 'VALIDATION_FAILED', 'CEL changed a non-target slot or active slot state')


def invoke(bpy, ui, context, request):
    op, settings = request['operation'], ui._settings(context)
    if op in ('new', 'new-composite'):
        ui.new_current(context, composite=op == 'new-composite')
    elif op == 'fork-composite':
        return ui.fork_composite(context)
    elif op == 'edit':
        return edit(ui, context, request['patch'])
    elif op == 'bind-slot':
        explicit_bind_slot(ui, context, request['slot'])
    elif op == 'bind-line':
        line = request['line']
        obj = context.view_layer.objects.get(line['object'])
        require(obj and obj.type == 'MESH' and not obj.library and not obj.override_library,
                'NOT_FOUND', 'Named local line mesh is absent from the saved view layer')
        settings.line_role = line['role']
        with context.temp_override(active_object=obj, object=obj):
            ui.bind_line(context)
    elif op == 'remove-binding':
        binding = request['binding']
        require(not settings.receipt and binding['index'] < len(getattr(settings, binding['kind'])),
                'CONFLICT', 'Cannot remove the specified unapplied CEL binding')
        require(bpy.ops.toon_cel.remove_binding(**binding) == {'FINISHED'}, 'CONFLICT', 'Native binding removal failed')
    elif op in ('inspect', 'apply'):
        rebind(ui, context, request.get('rebind_slots'))
        return (ui.apply_current if op == 'apply' else ui.inspect_current)(context)
    elif op == 'import':
        settings.file_path = request['recipe_file']
        ui.import_current(context)
    elif op == 'export':
        previous_path = settings.file_path
        settings.file_path = request['stage']
        try:
            return ui.export_current(context)
        finally:
            settings.file_path = previous_path
    elif op == 'read-archive':
        return {'status': 'read', 'archive': archive_data(bpy, ui, request['archive'])}
    return {'status': {'import': 'imported', 'remove-binding': 'removed'}.get(op, 'created' if op.startswith('new') else 'bound')}


def work(phase, record, job):
    import bpy

    request = record['params']
    sys.pycache_prefix = str(job / (phase + '-no-bytecode'))
    surface = load_modules(bpy, request)
    package = importlib.import_module(surface.__package__.split('.')[0])
    package.register()
    ui = importlib.import_module(surface.__package__ + '.cel_ui')
    op = request['operation']
    verify_blend = phase == 'verify' and op != 'export'
    context = open_saved_context(bpy, {**request, 'file': request['stage']} if verify_blend else request)
    settings = ui._settings(context)
    runtime = {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(), 'pid': os.getpid()}
    state = author_state(bpy, ui, context)
    if phase == 'verify':
        authored = read_json(job / 'author-result.json')['data']
        if op == 'export':
            doc = state['document']
            require(doc == authored['author']['document']
                    and ui._core(doc['schema']).load_document(request['stage']) == doc,
                    'VALIDATION_FAILED', 'Reopened native recipe differs from saved author')
        else:
            proof = authored['verification']
            require(state == authored['author'] and bindings(bpy) == proof['bindings']
                    and dependencies(bpy) == proof['dependencies'] and scene_structure(bpy) == proof['scene']
                    and protected_graphs(bpy, expected=proof['graphs']) == proof['graphs']
                    and fingerprint(ui._pencil_state(context.scene)) == proof['pencil'],
                    'VALIDATION_FAILED', 'Reopened author, protected data or Pencil differs')
            if settings.receipt:
                require(ui.inspect_current(context)['status'] == 'unchanged',
                        'VALIDATION_FAILED', 'Native applied author did not verify after reopening')
        return {'runtime': runtime, 'sha256': digest(request['stage'])}

    require((settings.recipe_id or None) == request['expected_author_id'],
            'CONFLICT', 'Saved CEL author identity differs from the request')
    mutates_blend = op not in READ_ONLY | {'export'}
    if mutates_blend:
        before, external, structure = bindings(bpy), dependencies(bpy), scene_structure(bpy)
        before_pencil = fingerprint(ui._pencil_state(context.scene))
        roles = state['document']['roles'] if state['document'] else {}
        allowed = {(row['object'], row['slot_index']) for row in state['slots'] if roles.get(row['role'], {}).get('action') == 'SURFACE'
                   } if op == 'apply' and not settings.receipt else set()
        graphs = protected_before_apply(bpy, ui, allowed)
    try:
        report = invoke(bpy, ui, context, request)
    except (TypeError, ValueError, RuntimeError, OverflowError) as error:
        if isinstance(error, Failure):
            raise
        raise Failure('CONFLICT', str(error)) from error
    result = {'report': report, 'author': author_state(bpy, ui, context), 'runtime': runtime}
    if not mutates_blend:
        if op == 'export':
            result['sha256'] = digest(request['stage'])
        return result

    after = bindings(bpy)
    compare_slots(before, after, allowed)
    pencil = fingerprint(ui._pencil_state(context.scene))
    may_change_pencil = op == 'apply' and report['status'] == 'applied' and (
        settings.recipe_schema != COMPOSITE or settings.line_policy == 'CREATE_FROM_BASIC')
    require(may_change_pencil or pencil == before_pencil, 'VALIDATION_FAILED', 'CEL changed protected Pencil authoring')
    require(scene_structure(bpy) == structure and protected_graphs(bpy, expected=graphs) == graphs
            and dependencies(bpy) == external, 'VALIDATION_FAILED', 'CEL changed protected geometry, graph or dependency data')
    if op == 'fork-composite':
        archived = archive_data(bpy, ui, report['archive'])
        archive_receipt = fingerprint(json.loads(archived['receipt'])) if archived['receipt'] else None
        archive_slots = [{'object': row['target']['name'] if row['target'] else None, 'slot_index': row['slot'],
                          'material': row['expected_name'] or None, 'link': row['link'], 'role': row['role']}
                         for row in archived['slots']]
        archive_identity = [{'expected_uid': row['expected_uid'], 'binding_epoch': row['binding_epoch'],
                             'library': row['target']['library'] if row['target'] else None}
                            for row in archived['slots']]
        archive_lines = [{'object': row['target']['name'] if row['target'] else None, 'role': row['role']}
                         for row in archived['line_targets']]
        require(archived['document'] == state['document'] and archive_receipt == state['receipt_sha256']
                and archived['line_policy'] == state['line_policy'] and archive_slots == state['slots']
                and archive_identity == state['slot_identity'] and archive_lines == state['line_targets']
                and not settings.slots and not settings.line_targets,
                'VALIDATION_FAILED', 'Fork did not retain the previous author record')
    bpy.context.preferences.filepaths.save_version = 0
    require(bpy.ops.wm.save_as_mainfile(filepath=request['stage'], copy=True, relative_remap=True, check_existing=False)
            == {'FINISHED'}, 'IO_ERROR', 'Cannot save the CEL recipe candidate')
    result.update(sha256=digest(request['stage']), verification={
        'bindings': after, 'graphs': graphs, 'scene': structure, 'dependencies': external, 'pencil': pencil,
        'pencil_preserved': pencil == before_pencil})
    return result


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
            raise Failure('CANCELLED', 'CEL recipe operation cancelled before publication')
        require(time.monotonic() < deadline, 'TIMEOUT', 'CEL recipe operation exceeded its timeout')

    def status(state, phase):
        atomic_json(job / 'status.json', {'job_id': job.name, 'state': state, 'phase': phase,
                    'pid': os.getpid(), 'heartbeat_at': time.time()})

    status('running', 'verify_inputs')
    try:
        with ExitStack() as guards:
            request = request_for(args, guards)
            request['product'] = guard_product(request, guards)
            blender = canonical(args.blender)
            output = None
            if args.output:
                suffix = '.json' if request['operation'] == 'export' else '.blend'
                require(args.output.is_absolute() and args.output.suffix.lower() == suffix,
                        'INVALID_REQUEST', f'Output requires a new absolute {suffix} path')
                parent = canonical(args.output.parent)
                for path in (parent, *parent.parents):
                    if path != Path(path.anchor):
                        guards.enter_context(FileGuard(path, directory=True))
                output = parent / args.output.name
                guards.enter_context(PathLocks([output]))
                require(not os.path.lexists(output), 'CONFLICT', 'Output already exists')
                with tempfile.NamedTemporaryFile(dir=parent, prefix='.toon-cel-', suffix=suffix, delete=False) as file:
                    stage = Path(file.name)
                guards.callback(stage.unlink, missing_ok=True)
                if suffix == '.json':
                    stage.unlink()
                request['stage'] = str(stage)
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
                if request['operation'] == 'export':
                    result['report']['file'] = str(output)
                proof = result.pop('verification', None)
                result.update(candidate={'file': str(output), 'sha256': verified['sha256']}, independent_reopen=verified['runtime'],
                              verification={'status': 'passed', **({
                                  'protected_graphs': len(proof['graphs']), 'protected_geometry': len(proof['scene']),
                                  'pencil_preserved': proof['pencil_preserved']} if proof else {})})
                artifacts.update(candidate=str(output), independent_reopen=str(job / 'verify-result.json'))
            data = {**result, 'operation': request['operation'], 'context': request['context'],
                    'source': {'file': request['file'], 'sha256': request['expected_sha256']},
                    'source_unchanged': True, 'product': request['product']}
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
            print(result['data']['report']['status'])
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
