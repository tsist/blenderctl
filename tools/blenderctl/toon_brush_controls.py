"""Saved Pencil chain, dash, map amount and amount-track handoff."""

import copy
import math

from material_draft_handoff import fingerprint, require
from protocol import Failure


READ = 'inspect-controls'
WRITE = 'apply-controls'
TABLES = ('chain_controls', 'dash_patterns', 'map_amounts')
PREPARE = ('PREPARE_CHAINS', 'PREPARE_DASHES', 'PREPARE_MAP_AMOUNTS')
RESTORE = ('RESTORE_CHAIN', 'RESTORE_DASH', 'RESTORE_MAP_AMOUNT')
TRACK = ('SET_AMOUNT_ENABLED', 'KEY_AMOUNT', 'KEY_STAGED_AMOUNT', 'REMOVE_AMOUNT_KEY')
STATIC = {'SET_CHAIN': 'chain_controls', 'SET_DASH': 'dash_patterns', 'SET_MAP_AMOUNT': 'map_amounts'}
CHAIN_BOOL = {'boundary_enabled', 'intersection_enabled', 'split_enabled', 'direction_enabled'}
CHAIN_RANGES = {'max_gap_px': (0, 65536), 'intersection_max_gap_px': (0, 65536),
                'min_length_px': (0, 65536), 'split_angle_degrees': (0, 180),
                'line_direction_degrees': (-360, 360)}
DASH_RANGES = {'dash_length_px': (.25, 65536), 'gap_length_px': (0, 65536)}


def validate_static(action, field, value):
    require(isinstance(field, str), 'INVALID_REQUEST', 'Expected a named brush field')
    if action == 'SET_CHAIN' and field in CHAIN_BOOL:
        valid = type(value) is bool
    elif action == 'SET_CHAIN' and field == 'closed_orientation':
        valid = value in ('clockwise', 'counterclockwise', 'retained')
    elif action == 'SET_DASH' and field == 'mode':
        valid = value in ('SOLID', 'DASHED')
    else:
        ranges = CHAIN_RANGES if action == 'SET_CHAIN' else DASH_RANGES if action == 'SET_DASH' else {'amount': (0, 1)}
        valid = field in ranges and type(value) in (int, float) and ranges[field][0] <= value <= ranges[field][1]
    require(valid, 'INVALID_REQUEST', 'Unknown field or out-of-range value for ' + action + '.' + field)


def frame(value):
    return type(value) is int and -1048574 <= value <= 1048574


def normalize(request, common):
    operation = request['operation']
    if operation == READ:
        require(common | {'actual'} <= request.keys() <= common | {'actual', 'sample_frame'}
                and ('sample_frame' not in request or frame(request['sample_frame'])),
                'INVALID_REQUEST', 'Inspect controls takes the exact actual ID and optional integer sample_frame')
        return
    require(request.keys() == common | {'actual', 'expected_controls_sha256', 'actions'},
            'INVALID_REQUEST', 'Apply controls takes actual, expected_controls_sha256 and actions')
    from toon_viewport_lines_contract import SHA256

    expected = request['expected_controls_sha256']
    require(isinstance(expected, str) and SHA256.fullmatch(expected) is not None,
            'INVALID_REQUEST', 'Expected lowercase saved controls SHA-256')
    edits = request['actions']
    require(isinstance(edits, list) and 1 <= len(edits) <= 32, 'INVALID_REQUEST', 'Expected 1..32 brush controls actions')
    fields = {**{action: {'action'} for action in PREPARE + RESTORE},
              **{action: {'action', 'field', 'value'} for action in STATIC},
              'STAGE_AMOUNT_KEY': {'action', 'expected_value', 'value'}, 'KEY_STAGED_AMOUNT': {'action', 'expected_value'},
              'SET_AMOUNT_ENABLED': {'action', 'enabled'}, 'KEY_AMOUNT': {'action', 'value'}, 'REMOVE_AMOUNT_KEY': {'action'}}
    for edit in edits:
        require(isinstance(edit, dict) and isinstance(edit.get('action'), str) and edit['action'] in fields,
                'INVALID_REQUEST', 'Unknown brush controls action')
        action = edit['action']
        required = fields[action]
        optional = {'frame'} if action in ('KEY_AMOUNT', 'KEY_STAGED_AMOUNT', 'REMOVE_AMOUNT_KEY') else set()
        require(required <= edit.keys() <= required | optional and ('frame' not in edit or frame(edit['frame'])),
                'INVALID_REQUEST', 'Wrong fields or frame for ' + action)
        if 'enabled' in edit:
            require(type(edit['enabled']) is bool, 'INVALID_REQUEST', 'enabled must be boolean')
        if 'expected_value' in edit:
            require(type(edit['expected_value']) in (int, float) and 0 <= edit['expected_value'] <= 1,
                    'INVALID_REQUEST', 'Expected the saved pending slider value within 0..1')
        if action in STATIC:
            validate_static(action, edit['field'], edit['value'])
        elif 'value' in edit:
            value = edit['value']
            require(type(value) in (float,int) and 0 <= value <= 1 and math.isfinite(value),
                    'INVALID_REQUEST', 'Amount key value must be finite within 0..1')


def key_state(scene, amount, actual):
    item = amount.track(scene, actual)
    if item is None:
        return None
    curve = amount.curve(scene, actual)
    return {'enabled': bool(item.enabled), 'path': amount.key_path(actual), 'stored_amount': float(item.amount),
            'keys': [amount._key_state(key) for key in curve.keyframe_points] if curve else []}


def state(scene, actual, file_author, amount):
    captured = file_author.capture_controls(scene)
    return {'tables': {key: captured[key] for key in TABLES}, 'track': key_state(scene, amount, actual)}


def other_animation(scene, actual, amount, animation):
    path = amount.key_path(actual)
    owned = [row for row in amount.signature(scene) if row[0] != actual]
    independent = {'amount': owned, 'parameter': animation.signature(scene)}
    ad = scene.animation_data
    if ad is not None and ad.action is not None:
        action = ad.action
        independent['curves'] = [
            (slot.identifier, layer.name, strip.type, fc.data_path, fc.array_index,
             [amount._key_state(key) for key in fc.keyframe_points])
            for layer in action.layers for strip in layer.strips for bag in strip.channelbags
            for slot in action.slots if slot.handle == bag.slot_handle
            for fc in bag.fcurves if fc.data_path != path or slot != ad.action_slot]
    return {**independent, 'curves': independent.get('curves', [])}


def sample(context, addon, plan, actual, requested):
    scene = context.scene
    current = scene.frame_current
    if requested is None or requested == current:
        return {'frame': current, 'value': addon._map_amounts(scene, plan).get(actual, 1.)}
    scene.frame_set(requested)
    try:
        context.view_layer.update()
        return {'frame': requested, 'value': addon._map_amounts(scene, plan).get(actual, 1.)}
    finally:
        scene.frame_set(current)
        context.view_layer.update()


def restore(scene, plan, actual, action, addon):
    if action == 'RESTORE_CHAIN':
        controls = addon.chain_ui
        bindings, previous = controls.read_controls(scene, plan)
        require(previous is not None, 'CONFLICT', 'Prepare the chain table before restoring it')
        old = previous['brushes'][actual]
        seeded = addon.chain_controls.seed_controls(bindings)['brushes'][actual]
        seeded.update(boundary_enabled=old['boundary_enabled'], intersection_enabled=old['intersection_enabled'])
        previous['brushes'][actual] = seeded
    elif action == 'RESTORE_DASH':
        controls = addon.dash_ui
        bindings, previous = controls.read_controls(scene, plan)
        require(previous is not None, 'CONFLICT', 'Prepare the dash table before restoring it')
        previous['patterns'][actual] = addon.dash_controls.default_pattern()
    else:
        controls = addon.map_amount_ui
        bindings, previous = controls.read_controls(scene, plan)
        require(previous is not None, 'CONFLICT', 'Prepare the map amount table before restoring it')
        previous['amounts'][actual] = controls.controls.native_value(bindings, plan['raw'], actual)
    controls._commit(scene, plan, bindings, previous)


def amount_step(scene, plan, addon, amount, actual, registry, action, edit):
    _, static = addon.map_amount_ui.read_controls(scene, plan)
    require(static is not None, 'CONFLICT', 'Prepare the map amount table before editing its animation')
    if action == 'SET_AMOUNT_ENABLED':
        amount.set_enabled(scene, actual, edit['enabled'], registry, initial_value=static['amounts'][actual])
    elif action in ('KEY_AMOUNT', 'KEY_STAGED_AMOUNT'):
        value = scene.toon_viewport_preview.amount_key_value if action == 'KEY_STAGED_AMOUNT' else edit['value']
        amount.set_key(scene, actual, value, registry, frame=edit.get('frame'))
    else:
        amount.remove_key(scene, actual, registry, frame=edit.get('frame'))


def edit_static(scene, plan, addon, actual, edit):
    action, field, value = edit['action'], edit['field'], edit['value']
    settings = scene.toon_viewport_preview
    modules = {'SET_CHAIN': (addon.chain_ui, settings.chains, 'chain_error'),
               'SET_DASH': (addon.dash_ui, settings.dashes, 'dash_error'),
               'SET_MAP_AMOUNT': (addon.map_amount_ui, settings.map_amounts, 'map_amount_error')}
    ui, items, error_field = modules[action]
    bindings, payload = ui.read_controls(scene, plan)
    require(payload is not None, 'CONFLICT', 'Prepare the native table before editing ' + field)
    native_field = field
    if action == 'SET_CHAIN':
        ui._resync(scene, plan, bindings, payload, rebuild=True)
        item = items[actual]
        if field in ('boundary_enabled', 'max_gap_px'):
            require(item.boundary_available, 'CONFLICT', 'Selected brush has no boundary route')
        if field in ('intersection_enabled', 'intersection_max_gap_px'):
            require(item.intersection_available, 'CONFLICT', 'Selected brush has no intersection route')
        angles = {'split_angle_degrees': ('split_angle', 'split_enabled'),
                  'line_direction_degrees': ('direction_angle', 'direction_enabled')}
        if field in angles:
            native_field, gate = angles[field]
            require(getattr(item, gate), 'CONFLICT', 'Enable the angle before changing its value')
            value = math.radians(value)
    else:
        ui._resync(scene, plan, bindings, payload)
        item = items[actual]
        if action == 'SET_DASH' and field in DASH_RANGES:
            require(item.mode == 'DASHED', 'CONFLICT', 'Set mode to DASHED before editing lengths')
    setattr(settings, error_field, '')
    setattr(item, native_field, value)
    # RNA update callbacks report native rejections on the scene rather than raising.
    require(not getattr(settings, error_field), 'CONFLICT', getattr(settings, error_field))
    _, after = ui.read_controls(scene, plan)
    if action == 'SET_CHAIN':
        row = after['brushes'][actual]
        policy = row['policy']
        if field in ('split_enabled', 'direction_enabled'):
            observed = policy['split_angle_degrees' if field == 'split_enabled' else 'line_direction_degrees'] is not None
        else:
            observed = row[field] if field in row else policy[field]
    elif action == 'SET_DASH':
        observed = after['patterns'][actual][field]
    else:
        observed = after['amounts'][actual]
    expected = edit['value']
    matches = (observed is not None and math.isclose(observed, expected, abs_tol=1e-6, rel_tol=1e-6)
               if type(expected) in (float, int) else observed == expected)
    require(matches, 'CONFLICT', 'Native effective brush value differs from the requested ' + field)


def protection(bpy, scene, document, amount, animation, actual, other_styles, dependencies):
    source = copy.deepcopy(document)
    controls = source['scene_controls']
    for key in TABLES:
        controls.pop(key, None)
    return fingerprint([source, other_animation(scene, actual, amount, animation), other_styles(bpy, scene),
                        dependencies(bpy), bpy.context.object.name if bpy.context.object else None,
                        sorted(obj.name for obj in bpy.context.selected_objects)])


def verify_other_rows(before, after, actual, edit):
    action = edit['action']
    allowed = STATIC.get(action)
    if action in PREPARE + RESTORE:
        allowed = TABLES[(PREPARE if action in PREPARE else RESTORE).index(action)]
    for table, branch in zip(TABLES, ('brushes', 'patterns', 'amounts')):
        previous, current = before['tables'][table], after['tables'][table]
        if table != allowed:
            require(previous == current, 'VALIDATION_FAILED', 'Another saved brush table changed')
        elif previous is not None:
            require(current is not None and all(current[branch].get(key) == value for key, value in previous[branch].items()
                    if key != actual or action in PREPARE), 'VALIDATION_FAILED', 'Another brush author changed')
        else:
            require(current is not None, 'VALIDATION_FAILED', 'Native preparation did not persist a table')
    if action not in TRACK:
        require(before['track'] == after['track'], 'VALIDATION_FAILED', 'Static brush control changed the animation track')


def apply(bpy, context, request, modules, plan, registry, other_styles, dependencies):
    addon, _, _, file_author, _, _, amount, animation = modules
    scene = context.scene
    actual = request['actual']
    current = state(scene, actual, file_author, amount)
    require(fingerprint(current) == request['expected_controls_sha256'], 'CONFLICT', 'Saved brush controls SHA changed')
    baseline = protection(bpy, scene, file_author.read_scene(scene), amount, animation, actual, other_styles, dependencies)
    operations = dict(zip(PREPARE, ('prepare_chains','prepare_dashes','prepare_map_amounts')))
    events = []
    for edit in request['actions']:
        action = edit['action']
        pending = scene.toon_viewport_preview.amount_key_value
        if 'expected_value' in edit:
            require(math.isclose(edit['expected_value'], pending, rel_tol=1e-7), 'CONFLICT',
                    'The saved pending key value differs from expected_value')
        try:
            if action in PREPARE:
                finished = getattr(bpy.ops.toon_viewport, operations[action])()
                require(finished == {'FINISHED'}, 'CONFLICT', 'Native brush control preparation did not finish')
            elif action in RESTORE:
                restore(scene, plan, actual, action, addon)
            elif action in STATIC:
                edit_static(scene, plan, addon, actual, edit)
            elif action == 'STAGE_AMOUNT_KEY':
                scene.toon_viewport_preview.amount_key_value = edit['value']
            else:
                amount_step(scene, plan, addon, amount, actual, registry, action, edit)
                scene.frame_set(scene.frame_current, subframe=scene.frame_subframe)
                context.view_layer.update()
        except (RuntimeError, ValueError) as exc:
            raise Failure('CONFLICT', str(exc)) from exc
        changed = state(scene, actual, file_author, amount)
        verify_other_rows(current, changed, actual, edit)
        events.append({'action': action, 'changed': current != changed or pending != scene.toon_viewport_preview.amount_key_value})
        current = changed
    require(protection(bpy, scene, file_author.read_scene(scene), amount, animation, actual, other_styles, dependencies) == baseline,
            'VALIDATION_FAILED', 'Brush controls modified another saved author or animation channel')
    return current, events
