"""Run the Toon active-slot author on a named material slot in a saved copy."""

import argparse
from contextlib import ExitStack, contextmanager
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid
import math

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
ROOT = ENTRY.parents[2]
sys.path.insert(0, str(ENTRY.parent))

from filesystem import FileGuard, PathLocks
from host_compat import is_supported_host
from material_draft_handoff import Parser, canonical, failure, fingerprint, guard_file, open_saved_context, read_document, require
from processes import ProcessTree
import toon_hair_map_handoff as hair_map
import toon_shot_look as shot_look
from protocol import CODES, DEFAULT_BLENDER, DEFAULT_JOBS, Failure, atomic_json, digest, envelope, read_json, timeout_value


COMMAND = 'toon-workflow.surface-slot'
CURRENT_EDITION = '1.0.1'
NATIVE_EDITIONS = {CURRENT_EDITION}
PRODUCTS = {
    '0.52.1': ((5, 3, 0), 'runtime/toon-workflow/0.52.1',
               'ba9b3d5a91705c6a7c9c0b68fe71f3efd6cdfef888b8a3d004ab60d450cfb336'),
    CURRENT_EDITION: ((5, 3, 0), 'tools/toon_workflow_addon/toon_workflow',
                      'a669eff99f26a67a9ec0f2729c569a7babe19a8f6e0c8feb12ee546369ab90c3'),
}
HAIR_SETTINGS = {
    'hair-shadow-attach': {'hair_image', 'hair_uv_name', 'hair_strength', 'hair_tint'},
    'hair-shadow-update': {'hair_strength', 'hair_tint'},
    'hair-shadow-replace': {'hair_image', 'hair_uv_name'},
}
HAIR_OPERATIONS = {*HAIR_SETTINGS, 'hair-shadow-read', 'hair-shadow-remove'}
OPERATIONS = ('apply', 'update', 'inspect', 'restore', 'boundary-apply',
              'boundary-update', 'boundary-close', 'import-library', 'export-library',
              'eye-create', 'eye-update', 'eye-read', *sorted(HAIR_OPERATIONS), *sorted(hair_map.OPERATIONS))
EYE_OPERATIONS = {'eye-create', 'eye-update', 'eye-read'}
READ_ONLY = {'inspect', 'eye-read', 'hair-shadow-read', 'hair-map-read', 'read-shot-look', 'read-native-media'}
BOUNDARY_WITH_SETTINGS = {'boundary-apply', 'boundary-update'}


def parser():
    cli = Parser(description=__doc__)
    cli.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    cli.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS)
    cli.add_argument('--transactions-dir', help=argparse.SUPPRESS)
    cli.add_argument('--timeout', type=float, default=120)
    display = cli.add_mutually_exclusive_group()
    for flag in ('--human', '--compact', '--full'):
        display.add_argument(flag, action='store_true')
    subcommands = cli.add_subparsers(required=True, dest='route', parser_class=Parser)
    surface = subcommands.add_parser(
        'surface-slot', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f'Required JSON: operation, edition ({", ".join(reversed(PRODUCTS))}), file, expected_sha256, '
            'context {scene, view_layer, frame}, target {object, material_slot, expected_material, expected_link}.\n'
            'apply: candidate CEL3, GAME_REFLECT or INK_FLAT with optional settings, '
            'or explicit settings.mode native_cel / eevee_cel3 / eevee_cel3_reflect.\n'
            'update: settings patch of the currently owned mode.\n'
            'boundary-apply / boundary-update: boundary {width_low, width_high}. boundary-close sets both to 0.\n'
            'import-library: library {file, expected_sha256, optional template_id}. '
            'eye-create: settings.kind SHADOW or GLINT. eye-update: patch the owned eye shader. '
            'SHADOW supports opacity and tint. GLINT supports opacity, glint_color, uv_name, center, radius, feather.\n'
            'hair-shadow-attach: settings.hair_image (saved image name), hair_uv_name, optional hair_strength and hair_tint.\n'
            'hair-shadow-update: patch hair_strength / hair_tint. hair-shadow-replace: patch hair_image / hair_uv_name.\n'
            'hair-shadow-read and hair-shadow-remove need no settings. Fixed shadow external masks require '
            'resources [{file, expected_sha256}].\n'
            'hair-map-attach: settings {image_name,sun,uv_name}. hair-map-update: patch saved HM controls, colors as RGB.\n'
            'hair-map-mask-attach: highlight_mask_image_name, highlight_mask_uv_name, mask_black_level, mask_white_level.\n'
            'hair-map-read / hair-map-mask-remove / hair-map-remove: no settings. Pin external shader images in resources.\n'
            'export-library: new JSON. inspect / eye-read / hair-shadow-read: no output path. Other operations: new .blend.'
        ))
    surface.add_argument('--params', type=Path, required=True, help='UTF-8 JSON with absolute input paths')
    surface.add_argument('--normal', action='store_true',
                         help='Edit and inspect head normals and profiles. Use normal-settings for panel fields, '
                              'normal-write-direction or normal-write-weight with explicit vertex indices, '
                              'and normal-render for a camera comparison.')
    surface.add_argument('--output', type=Path, help='New absolute .blend or exported material library .json')
    look = subcommands.add_parser('shot-look', description=shot_look.__doc__.strip(),
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    look.add_argument('--params', type=Path, required=True, help='UTF-8 shot look or native media request')
    look.add_argument('--output', type=Path, help='New absolute .blend for an edit')
    return cli


def name(value):
    return isinstance(value, str) and 0 < len(value) <= 255 and all(ord(char) >= 32 for char in value)


def read_request(args, guards):
    params = read_document(guard_file(args.params.absolute(), guards))
    if args.route == 'shot-look':
        return shot_look.request(params, guards, PRODUCTS)
    required = {'operation', 'edition', 'file', 'expected_sha256', 'context', 'target'}
    require(isinstance(params, dict) and required <= params.keys(), 'INVALID_REQUEST', 'Missing surface-slot fields')
    operation = params['operation']
    require(operation in OPERATIONS, 'INVALID_REQUEST', 'Unknown surface-slot operation')
    extra = {
        'apply': {'candidate', 'settings'}, 'update': {'settings'},
        'eye-create': {'settings'}, 'eye-update': {'settings'},
        'boundary-apply': {'boundary'}, 'boundary-update': {'boundary'}, 'import-library': {'library'},
    }
    allowed = extra.get(operation, set())
    if operation in HAIR_OPERATIONS:
        allowed = {'resources', 'settings'} if operation in HAIR_SETTINGS else {'resources'}
    if operation in hair_map.OPERATIONS:
        allowed = {'resources', 'settings'} if operation in hair_map.INPUTS else {'resources'}
    require(params.keys() <= required | allowed, 'INVALID_REQUEST',
            f'Unexpected parameters for {operation}')
    edition = params['edition']
    require(isinstance(edition, str) and edition in PRODUCTS, 'UNSUPPORTED', 'Unknown Toon product edition')
    ctx = params['context']
    require(isinstance(ctx, dict) and ctx.keys() == {'scene', 'view_layer', 'frame'}
            and name(ctx['scene']) and name(ctx['view_layer']) and type(ctx['frame']) is int,
            'INVALID_REQUEST', 'Expected exact scene, view_layer and integer frame')
    target = params['target']
    require(isinstance(target, dict) and target.keys() == {'object', 'material_slot', 'expected_material', 'expected_link'}
            and name(target['object']) and type(target['material_slot']) is int and target['material_slot'] >= 0
            and (target['expected_material'] is None or name(target['expected_material']))
            and target['expected_link'] in ('DATA', 'OBJECT'),
            'INVALID_REQUEST', 'Expected object, nonnegative material_slot, expected_material (name or null), and DATA/OBJECT expected_link')
    require(isinstance(params['file'], str) and Path(params['file']).suffix.lower() == '.blend'
            and isinstance(params['expected_sha256'], str), 'INVALID_REQUEST', 'Expected a hash-pinned .blend')
    params['file'] = str(guard_file(params['file'], guards, params['expected_sha256']))
    if operation in ('eye-create', 'eye-update'):
        settings = params.get('settings')
        require(isinstance(settings, dict) and settings, 'INVALID_REQUEST', 'Provide the eye settings to change')
        if operation == 'eye-create':
            require(settings.get('kind') in ('SHADOW', 'GLINT'), 'INVALID_REQUEST', 'Specify SHADOW or GLINT')
        else:
            require('kind' not in settings, 'INVALID_REQUEST', 'Eye updates preserve the existing shader kind')
    if operation in ('apply', 'update'):
        patch = params.get('settings', {})
        require(isinstance(patch, dict), 'INVALID_REQUEST', 'settings must be an object')
        if operation == 'apply':
            candidate = params.get('candidate')
            require((candidate is None and patch.get('mode') in ('native_cel', 'eevee_cel3', 'eevee_cel3_reflect'))
                    or (candidate in ('CEL3', 'GAME_REFLECT', 'INK_FLAT') and 'mode' not in patch),
                    'INVALID_REQUEST', 'Choose an explicit candidate or settings.mode')
        else:
            require(patch and 'mode' not in patch, 'INVALID_REQUEST', 'update needs a nonempty settings patch without mode')
    if operation in BOUNDARY_WITH_SETTINGS:
        values = params.get('boundary')
        require(isinstance(values, dict) and values.keys() == {'width_low', 'width_high'},
                'INVALID_REQUEST', 'Boundary needs exactly width_low and width_high')
    if operation == 'import-library':
        library = params.get('library')
        require(isinstance(library, dict) and {'file', 'expected_sha256'} <= library.keys()
                <= {'file', 'expected_sha256', 'template_id'} and isinstance(library['file'], str)
                and isinstance(library['expected_sha256'], str) and
                ('template_id' not in library or isinstance(library['template_id'], str)),
                'INVALID_REQUEST', 'Import needs a hash-pinned material library and optional template_id')
        library['file'] = str(guard_file(library['file'], guards, library['expected_sha256']))
    if operation in HAIR_OPERATIONS or operation in hair_map.OPERATIONS:
        validate_hair_request(params, guards)
    return params


def validate_hair_request(params, guards):
    operation = params['operation']
    if operation in hair_map.INPUTS:
        values = params.get('settings')
        whitelist = hair_map.INPUTS[operation]
        require(isinstance(values, dict) and values and values.keys() <= whitelist
                and (operation == 'hair-map-update' or values.keys() == whitelist),
                'INVALID_REQUEST', 'Missing or unexpected HM settings')
        for key, value in values.items():
            if key.endswith('_name') or key == 'sun':
                valid = name(value)
            elif key.endswith('_color'):
                valid = type(value) is list and len(value) == 3 and all(type(x) in (int, float) and 0 <= x <= 1 for x in value)
            else:
                valid = type(value) in (int, float) and math.isfinite(value)
            require(valid, 'INVALID_REQUEST', f'Invalid HM setting {key}')
        if operation == 'hair-map-mask-attach':
            require(0 <= values['mask_black_level'] < values['mask_white_level'] <= 1,
                    'INVALID_REQUEST', 'Mask levels must satisfy 0 <= black < white <= 1')
    if operation in HAIR_SETTINGS:
        settings = params.get('settings')
        require(isinstance(settings, dict) and settings and settings.keys() <= HAIR_SETTINGS[operation],
                'INVALID_REQUEST', 'Supply the fixed hair shadow fields allowed by this operation')
        require(operation != 'hair-shadow-attach' or {'hair_image', 'hair_uv_name'} <= settings.keys(),
                'INVALID_REQUEST', 'Attach requires both hair_image and hair_uv_name')
        require(all(name(settings[field]) for field in ('hair_image', 'hair_uv_name') if field in settings),
                'INVALID_REQUEST', 'Image and UV names must be nonempty saved names')
    resources = params.get('resources', [])
    require(isinstance(resources, list) and len(resources) <= 8,
            'INVALID_REQUEST', 'Fixed hair shadow accepts up to 8 hash-pinned resources')
    for row in resources:
        require(isinstance(row, dict) and row.keys() == {'file', 'expected_sha256'}
                and isinstance(row['file'], str), 'INVALID_REQUEST',
                'Each fixed shadow resource needs file and expected_sha256')
        row['file'] = str(guard_file(row['file'], guards, row['expected_sha256']))
    require(len({os.path.normcase(row['file']) for row in resources}) == len(resources),
            'INVALID_REQUEST', 'Duplicate fixed shadow resource')


def native_product(request, guards, products=PRODUCTS):
    host, default, checksum = products[request['edition']]
    override = os.environ.get('BLENDERCTL_TOON_BUNDLE')
    bundle = canonical(override or ROOT / default)
    require(bundle.is_dir(), 'NOT_FOUND', 'Expected the matching Toon package directory')
    descriptor = guard_file(bundle / 'bundle.json', guards, checksum)
    manifest = read_json(descriptor)
    entries = {name: value for name, value in manifest['files'].items()
               if re.fullmatch(r'(__init__\.py|toon_workflow_addon/.+\.py)', name)}
    for relative, expected in entries.items():
        guard = guards.enter_context(FileGuard(bundle / relative))
        require(guard.sha256() == expected, 'CONFLICT', f'Toon package source SHA differs: {relative}')
    return {'host': host, 'edition': manifest['version'], 'directory': str(bundle), 'manifest_sha256': checksum,
            'native_files_verified': len(entries)}


def require_product_host(bpy, product):
    observed = tuple(bpy.app.version)
    minimum = tuple(product['host'])
    require(is_supported_host(observed) and observed >= minimum, 'UNSUPPORTED',
            f'Edition {product["edition"]} requires Blender {".".join(map(str, minimum))} or later')


def load_modules(bpy, request):
    product = request['product']
    require_product_host(bpy, product)
    bundle = Path(product['directory'])
    spec = importlib.util.spec_from_file_location('toon_workflow', bundle / '__init__.py',
                                                 submodule_search_locations=[str(bundle)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
    domain = importlib.import_module('toon_workflow.toon_workflow_addon')
    require(domain.__version__ == product['edition']
            and Path(domain.__file__).resolve().is_relative_to(Path(product['directory'])),
            'CONFLICT', 'A different Toon author package was loaded')
    return importlib.import_module(domain.__name__ + '.surface_ui')


def material_name(slot):
    return slot.material.name_full if slot.material else None


def bindings(bpy):
    rows = []
    for obj in bpy.data.objects:
        rows.append({
            'object': obj.name_full, 'library': obj.library.filepath if obj.library else None,
            'mesh': obj.data.name_full if obj.data else None,
            'active_slot': obj.active_material_index,
            'materials': [[slot.link, material_name(slot)] for slot in obj.material_slots],
        })
    return sorted(rows, key=lambda row: (row['object'], row['library'] or ''))


def dependencies(bpy):
    return sorted({str(Path(path).resolve()) for path in bpy.utils.blend_paths(absolute=True, packed=False)})


def workspace_brush_paths(bpy, paths):
    version = '.'.join(str(part) for part in bpy.app.version[:2])
    directory = Path(bpy.app.binary_path).resolve().parent / version / 'datafiles/assets/brushes'
    linked = {Path(bpy.path.abspath(lib.filepath, library=lib.parent)).resolve() for lib in bpy.data.libraries}
    return [value for value in paths if Path(value).parent == directory
            and Path(value).match('essentials_brushes-*.blend') and Path(value) not in linked]


@contextmanager
def named_slot(context, target):
    obj = context.view_layer.objects.get(target['object'])
    require(obj is not None and obj.type == 'MESH', 'NOT_FOUND', 'Named mesh is absent from the saved view layer')
    require(context.mode == obj.mode == 'OBJECT' and not obj.data.is_editmode
            and all(item.library is None and item.override_library is None and item.is_editable for item in (obj, obj.data)),
            'CONFLICT', 'Use a local editable mesh in saved Object Mode')
    if 'material_slot' not in target:
        with context.temp_override(active_object=obj, object=obj):
            yield obj
        return
    slot = target['material_slot']
    require(slot < len(obj.material_slots), 'NOT_FOUND', 'Named material slot is absent')
    ref = obj.material_slots[slot]
    require(ref.link == target['expected_link'] and material_name(ref) == target['expected_material'],
            'CONFLICT', 'Named slot material or DATA/OBJECT link differs from the requested baseline')
    previous_index = obj.active_material_index
    obj.active_material_index = slot
    try:
        with context.temp_override(active_object=obj, object=obj):
            yield obj
    finally:
        obj.active_material_index = previous_index


def edit_settings(ui, context, request):
    native = ui.surface
    materials = native.materials
    patch = request.get('settings', {})
    settings = ui._settings(context)
    candidate = request.get('candidate')
    if request['operation'] == 'update':
        current = native.inspect(context)
        require(current['status'] == 'valid', 'CONFLICT', '; '.join(current.get('issues', [])))
        baseline = current['declared']
    elif candidate:
        settings.candidate = candidate
        baseline = native.candidate_settings(candidate)
    else:
        mode = patch['mode']
        defaults = {'native_cel': materials.DEFAULTS, 'eevee_cel3': materials.CEL3_DEFAULTS,
                    'eevee_cel3_reflect': materials.CEL3_REFLECT_DEFAULTS}
        baseline = {'mode': mode, **defaults[mode]}
    active = set(materials._active('BLENDER_EEVEE', baseline['mode']))
    require(patch.keys() <= (active | {'mode'}), 'INVALID_REQUEST', 'A supplied key is not used by this surface mode')
    try:
        desired = materials._settings({**baseline, **patch}, 'BLENDER_EEVEE')
    except Exception as exc:
        raise Failure('INVALID_REQUEST', str(exc)) from exc
    ui._load(settings, desired)
    panel = ui.update_current if request['operation'] == 'update' else ui.apply_current
    return panel(context)


def eye_panel(ui, context, request):
    action = request['operation']
    if action == 'eye-read':
        return ui.read_current(context)
    patch = dict(request['settings'])
    settings = ui._settings(context)
    if action == 'eye-create':
        settings.kind = patch.pop('kind')
    else:
        ui.read_current(context)
    values = ui._eye_values(settings)
    if 'color' in patch:
        raise Failure('INVALID_REQUEST', 'Use the panel field glint_color')
    if 'glint_color' in patch:
        patch['color'] = patch.pop('glint_color')
    try:
        desired = ui.eye_nodes._normalize(settings.kind, {**values, **patch})
    except ValueError as exc:
        raise Failure('INVALID_REQUEST', str(exc)) from exc
    for key, value in desired.items():
        setattr(settings, 'glint_color' if key == 'color' else key, value)
    if action == 'eye-create':
        ui.create_current(context)
    else:
        ui.update_current(context)
    return ui.read_current(context)


def guard_shadow_image(bpy, image, request):
    require(image is not None and image.source == 'FILE' and image.library is None
            and image.override_library is None, 'CONFLICT', 'Expected a named local FILE mask image')
    if not image.packed_file:
        path = canonical(bpy.path.abspath(image.filepath))
        require(any(Path(row['file']) == path for row in request.get('resources', [])), 'CONFLICT',
                'Pin the external fixed shadow mask in resources: ' + str(path))


def hair_panel(bpy, ui, context, request):
    action = request['operation']
    core = ui.hair_fixed_shadow
    _, obj, _, material = ui._exclusive_material(context)
    active = core.is_present(material)
    patch = request.get('settings', {})
    if action == 'hair-shadow-read' and not active:
        return {'present': False, 'material': material.name_full}

    if action == 'hair-shadow-attach':
        require(not active, 'CONFLICT', 'Fixed hair shadow already exists in this material slot')
        baseline = {'uv_name': patch['hair_uv_name'], 'strength': 0.0, 'tint': (.65, .55, .65)}
    else:
        require(active, 'NOT_FOUND', 'Fixed hair shadow is absent from this material slot')
        _, _, group, source, destination = core._binding(material)
        guard_shadow_image(bpy, group.get(core._IMAGE), request)
        baseline = ui.read_hair_current(context)
        if action == 'hair-shadow-read':
            return {'present': True, **baseline}
        if action == 'hair-shadow-remove':
            report = ui.remove_hair_current(context)
            require(not core.is_present(material) and len(destination.links) == 1
                    and destination.links[0].from_socket == source, 'VALIDATION_FAILED',
                    'Removing fixed shadow did not restore the exact original shading edge')
            return {'present': False, **report}

    settings = ui._settings(context)
    if 'hair_image' in patch:
        image = bpy.data.images.get(patch['hair_image'])
        guard_shadow_image(bpy, image, request)
        settings.hair_image = image
    uv_name = patch.get('hair_uv_name', baseline['uv_name'])
    require(uv_name in obj.data.uv_layers, 'INVALID_REQUEST', 'Fixed shadow UV is absent from the named mesh: ' + uv_name)
    settings.hair_uv_name = uv_name
    if action != 'hair-shadow-replace':
        require(baseline.get('profile') != core.LEGACY_PROFILE or 'hair_tint' not in patch,
                'UNSUPPORTED', 'Legacy fixed shadow has no tint; remove it before explicitly attaching v2')
        settings.hair_strength = core._strength(patch.get('hair_strength', baseline['strength']))
        if 'tint' in baseline:
            settings.hair_tint = core._tint(patch.get('hair_tint', baseline['tint']))
    panel = {'hair-shadow-attach': ui.attach_hair_current, 'hair-shadow-update': ui.update_hair_current,
             'hair-shadow-replace': ui.replace_hair_source}[action]
    return {'present': True, **panel(context)}


def run_panel(ui, context, request):
    op = request['operation']
    if op in EYE_OPERATIONS:
        return eye_panel(ui, context, request)
    settings = ui._settings(context)
    if op in ('apply', 'update'):
        return edit_settings(ui, context, request)
    if op == 'import-library':
        lib = request['library']
        settings.library_path = lib['file']
        settings.template_id = lib.get('template_id', '')
        return ui.import_current(context)
    if op == 'export-library':
        return ui.surface.export_library(context, request['stage'])
    if op in BOUNDARY_WITH_SETTINGS:
        try:
            values = ui.boundary.normalize(request['boundary'])
        except ValueError as exc:
            raise Failure('INVALID_REQUEST', str(exc)) from exc
        settings.boundary_width_low = values['width_low']
        settings.boundary_width_high = values['width_high']
    actions = {
        'inspect': ui.inspect_current, 'restore': ui.restore_current,
        'boundary-apply': ui.apply_boundary_current, 'boundary-update': ui.update_boundary_current,
        'boundary-close': ui.close_boundary_current,
    }
    return actions[op](context)


def verify_slot_isolation(before, after, target, original_index):
    require(len(before) == len(after), 'VALIDATION_FAILED', 'Object inventory changed during the slot action')
    for baseline, changed in zip(before, after):
        if baseline['object'] == target['object'] and baseline['library'] is None:
            slot = target['material_slot']
            require(changed['active_slot'] == original_index and changed['materials'][slot][0] == target['expected_link'],
                    'VALIDATION_FAILED', 'Selected slot or material link changed')
            baseline['materials'][slot] = changed['materials'][slot]
        require(baseline == changed, 'VALIDATION_FAILED', 'Non-target slots or active slot state changed')


def work(request):
    import bpy

    if request.get('normal'):
        import toon_head_normal_handoff as head_normal
        return head_normal.work(bpy, request)
    ui = load_modules(bpy, request)
    action = request['operation']
    if action in shot_look.OPERATIONS:
        module = '.media_render_ui' if action in shot_look.MEDIA else '.shot_look_ui'
        return shot_look.work(bpy, importlib.import_module(ui.__package__ + module), request)
    hair_action = action in HAIR_OPERATIONS or action in hair_map.OPERATIONS
    if action in hair_map.OPERATIONS:
        ui = importlib.import_module(ui.__package__ + '.hair_map_ui')
    elif action in EYE_OPERATIONS or action in HAIR_OPERATIONS:
        ui = importlib.import_module(ui.__package__ + '.eye_overlay_ui')
    context = open_saved_context(bpy, request)
    ui.register()
    target = request['target']
    before = bindings(bpy)
    external = dependencies(bpy)
    workspace = workspace_brush_paths(bpy, external) if action in hair_map.OPERATIONS else []
    external = [path for path in external if path not in workspace]
    if request.get('verify'):
        require(before == request['verify']['bindings'] and external == request['verify']['dependencies'],
                'VALIDATION_FAILED', 'Saved candidate slot inventory or external dependency paths changed')
    with named_slot(context, target) as obj:
        if action in hair_map.OPERATIONS:
            report = hair_map.apply(bpy, ui, context, request, guard_shadow_image)
        elif hair_action:
            report = hair_panel(bpy, ui, context, request)
        else:
            report = run_panel(ui, context, request)
        actual = material_name(obj.material_slots[target['material_slot']])
        if hair_action:
            from inspection import record

            require(actual == target['expected_material'], 'VALIDATION_FAILED', 'Fixed shadow changed material identity')
            material_signature = fingerprint(record(obj.material_slots[target['material_slot']].material))
        elif action in EYE_OPERATIONS:
            material = obj.material_slots[target['material_slot']].material
            original = material.get('toon_eye_overlay_original_material')
            report['original_material'] = original.name_full if original else None
            if action == 'eye-create':
                require(report['original_material'] == target['expected_material'],
                        'VALIDATION_FAILED', 'Eye shader did not retain its original material')
        elif action not in ('restore', 'inspect', 'export-library'):
            require(report['status'] == 'valid', 'CONFLICT',
                    '; '.join(report.get('issues', [])) or 'Native slot author did not admit this edit')
            report = ui.inspect_current(context)
        if action == 'restore':
            require(actual == report['original_material'], 'VALIDATION_FAILED', 'Native original material did not restore')
            retained = bpy.data.materials.get(report['retained_material'])
            require(retained is not None, 'VALIDATION_FAILED', 'Native retained author material disappeared')
            # The panel keeps this data for recovery. An unused block needs a user
            # to survive saving the candidate with the original slot reattached.
            if not retained.users:
                retained.use_fake_user = True

    result = {'operation': action, 'target': target, 'actual_material': actual, 'report': report,
              'runtime': {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(), 'pid': os.getpid()}}
    if action in hair_map.OPERATIONS:
        result['workspace_brush_paths'] = workspace
    if request.get('verify'):
        expected = request['verify']
        require(report == expected['report'], 'VALIDATION_FAILED', 'Cold slot report differs from the native edit')
        if hair_action:
            require(material_signature == expected['hair_material'], 'VALIDATION_FAILED',
                    'Cold reopen changed the material graph including its shading edges')
        if expected.get('retained'):
            require(expected['retained'] in bpy.data.materials, 'VALIDATION_FAILED', 'Cold open lost the retained author material')
        return result
    if action in READ_ONLY:
        return result
    if action != 'export-library':
        after = bindings(bpy)
        original_index = next(row['active_slot'] for row in before
                              if row['object'] == target['object'] and row['library'] is None)
        verify_slot_isolation(before, after, target, original_index)
        bpy.context.preferences.filepaths.save_version = 0
        require(bpy.ops.wm.save_as_mainfile(filepath=request['stage'], copy=True, relative_remap=True,
                                          check_existing=False) == {'FINISHED'}, 'IO_ERROR', 'Could not save Toon candidate')
        expected = {'bindings': after, 'dependencies': external,
                    'retained': report.get('retained_material'), 'report': report}
        if hair_action:
            expected['hair_material'] = material_signature
            if action in ('hair-shadow-remove', 'hair-map-remove'):
                expected['report'] = {'present': False, 'material': actual}
        if action == 'restore':
            with named_slot(context, {**target, 'expected_material': actual}):
                expected['report'] = ui.inspect_current(context)
        result['verify'] = expected
    result['output_sha256'] = digest(request['stage'])
    return result


def worker(request_path, response_path):
    try:
        response = {'data': work(read_json(request_path))}
    except Exception as exc:
        response = {'error': failure(exc)}
    atomic_json(response_path, response)


def execute(blender, job, request, stage, seconds):
    params, response = job / (stage + '-params.json'), job / (stage + '-result.json')
    atomic_json(params, request)
    profile = job / (stage + '-profile')
    profile.mkdir()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('BLENDER_USER_', 'BLENDER_SYSTEM_', 'PYTHON'))}
    env.update(BLENDER_USER_RESOURCES=str(profile), PYTHONDONTWRITEBYTECODE='1')
    argv = [str(blender), '--background', '--factory-startup', '--offline-mode', '--disable-autoexec',
            '--threads', '2', '--python-exit-code', '23', '--python', str(ENTRY), '--', str(params), str(response)]
    with (job / (stage + '.log')).open('xb') as log:
        process = ProcessTree(argv, cwd=str(job), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            code = process.process.wait(timeout=seconds)
        except BaseException:
            process.terminate()
            raise
        finally:
            process.close()
    require(code == 0 and response.is_file(), 'WORKER_FAILED', f'Toon {stage} worker exited {code}; see {stage}.log')
    result = read_json(response)
    if 'error' in result:
        error = result['error']
        raise Failure(error['code'], error['message'])
    return result['data']


def run(args):
    if args.route == 'surface-slot' and getattr(args, 'normal', False):
        import toon_head_normal_handoff as head_normal
        return head_normal.run(args)
    deadline = time.monotonic() + timeout_value(args.timeout)
    job = args.jobs_dir.resolve() / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    result_path = job / 'result.json'
    artifacts = {'job_directory': str(job), 'result': str(result_path)}
    data = error = None

    def remaining():
        seconds = deadline - time.monotonic()
        require(seconds > 0, 'TIMEOUT', 'Toon surface-slot time budget expired')
        return seconds

    try:
        with ExitStack() as guards:
            request = read_request(args, guards)
            blender = canonical(args.blender)
            request['product'] = native_product(request, guards)
            action = request['operation']
            if action in READ_ONLY:
                require(args.output is None, 'INVALID_REQUEST', f'{action} does not use --output')
            else:
                suffix = '.json' if action == 'export-library' else '.blend'
                require(args.output is not None and args.output.is_absolute() and args.output.suffix.lower() == suffix,
                        'INVALID_REQUEST', f'{action} needs a new absolute {suffix} --output')
                parent = canonical(args.output.parent)
                for folder in reversed((parent, *parent.parents)):
                    if folder != Path(folder.anchor):
                        guards.enter_context(FileGuard(folder, directory=True))
                output = parent / args.output.name
                guards.enter_context(PathLocks([output]))
                require(not os.path.lexists(output), 'CONFLICT', 'Output already exists')
                # Stage at the final parent so Blender can remap relative
                # dependencies correctly before independent reopen.
                if suffix == '.blend':
                    with tempfile.NamedTemporaryFile(dir=parent, prefix='.toon-surface-', suffix=suffix,
                                                     delete=False) as temp:
                        staged = Path(temp.name)
                else:
                    folder = guards.enter_context(tempfile.TemporaryDirectory(dir=parent, prefix='.toon-surface-'))
                    staged = Path(folder) / 'surface.json'
                guards.callback(staged.unlink, missing_ok=True)
                request['stage'] = str(staged)

            result = execute(blender, job, request, 'author', remaining())
            if action not in READ_ONLY:
                expected = result.pop('output_sha256')
                with FileGuard(staged) as stage_guard:
                    require(stage_guard.sha256() == expected, 'CONFLICT', 'Staged output SHA differs')
                    if suffix == '.blend':
                        read_op = ('read-native-media' if action in shot_look.MEDIA else
                                   'read-shot-look' if action in shot_look.OPERATIONS else
                                   'hair-map-read' if action in hair_map.OPERATIONS else
                                   'hair-shadow-read' if action in HAIR_OPERATIONS else
                                   'eye-read' if action in EYE_OPERATIONS else 'inspect')
                        cold = {**request, 'file': str(staged), 'operation': read_op, 'verify': result.pop('verify')}
                        if action not in shot_look.OPERATIONS:
                            cold['target'] = {**request['target'], 'expected_material': result['actual_material']}
                        verified = execute(blender, job, cold, 'reopen', remaining())
                        require(verified['runtime']['pid'] != result['runtime']['pid'],
                                'VALIDATION_FAILED', 'Independent Blender reopen was not observed')
                        result['independent_reopen'] = verified['runtime']
                    else:
                        template = read_json(staged)
                        require(template.get('templates', [{}])[0].get('template_id') == result['report']['template_id'],
                                'VALIDATION_FAILED', 'Exported slot library could not be read back')
                        result['report']['file'] = str(output)
                with FileGuard(staged, renameable=True) as stage_guard:
                    require(stage_guard.sha256() == expected, 'CONFLICT', 'Verified output SHA changed before publication')
                    remaining()
                    stage_guard.rename_new(output)
                key = 'candidate' if suffix == '.blend' else 'library'
                result[key] = {'file': str(output), 'sha256': expected}
                artifacts[key] = str(output)
            data = {**result, 'source': {'file': request['file'], 'sha256': request['expected_sha256']},
                    'source_unchanged': True, 'product': request['product']}
    except subprocess.TimeoutExpired:
        error = {'code': 'TIMEOUT', 'message': 'Toon slot author or independent reopen timed out'}
    except (Failure, ValueError, OSError, RuntimeError) as exc:
        error = failure(exc, 'IO_ERROR')
    for stage in ('author', 'reopen'):
        for name in (stage + '.log', stage + '-result.json'):
            if (job / name).is_file():
                artifacts[name] = str(job / name)
    result = envelope('toon-workflow.' + args.route, job.name, data, error, artifacts)
    atomic_json(result_path, result)
    return result


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    args = None
    try:
        args = parser().parse_args()
        result = run(args)
    except (Failure, OSError) as exc:
        command = 'toon-workflow.' + args.route if args else (
            'toon-workflow.shot-look' if 'shot-look' in sys.argv else COMMAND)
        result = envelope(command, error=failure(exc, 'IO_ERROR'))
    if args and args.human:
        print(result['artifacts']['result'] if result['ok'] else result['error']['message'])
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['ok'] else CODES[result['error']['code']]


if __name__ == '__main__':
    if '--background' in sys.argv and '--' in sys.argv:
        worker(*(Path(value) for value in sys.argv[sys.argv.index('--') + 1:]))
    else:
        sys.exit(main())
