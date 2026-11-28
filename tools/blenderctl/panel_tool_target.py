"""Replay the released panel's tool menu and active-target binding in a saved workspace."""
from protocol import Failure
import panel_tool_parameters as parameters

OPERATIONS = {'choose-tool', 'use-active-object', parameters.OPERATION}


def request_fields(operation):
    if operation == parameters.OPERATION:
        return {'tool_id', 'changes'}
    return {'tool_id'} if operation == 'choose-tool' else {'object'}


def validate(request):
    if request['operation'] == parameters.OPERATION:
        parameters.validate_changes(request.get('changes'))
    field = 'object' if request['operation'] == 'use-active-object' else 'tool_id'
    value = request.get(field)
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise Failure('INVALID_REQUEST', f'{request["operation"]} requires a nonempty {field}')


def execute(context, request):
    if request['operation'] == parameters.OPERATION:
        return parameters.execute(context, request)
    import bpy
    from hard_surface_workflow_addon.panel.settings import descriptor

    if request['operation'] == 'choose-tool':
        name = 'choose_tool'
        if descriptor(request['tool_id']) is None:
            raise Failure('INVALID_REQUEST', f'Unknown tool in this add-on edition: {request["tool_id"]}')
        properties = {'tool_id': request['tool_id']}
    else:
        name = 'use_active_object'
        active = context.active_object
        if active is None or active.type != 'MESH' or active.name != request['object']:
            raise Failure('CONFLICT', 'The saved active object must be the named mesh')
        properties = {}
    operator = getattr(bpy.ops.hard_surface, name)
    if not operator.poll():
        raise Failure('CONFLICT', f'hard_surface.{name} is unavailable in the saved context')
    result = sorted(operator(**properties))
    return {'id': 'hard_surface.' + name, 'poll': True, 'result': result, 'arguments': properties,
            'blender_report_type': 'FINISHED' if result == ['FINISHED'] else 'CANCELLED'}


def verify(operation, before, after, request):
    if operation == parameters.OPERATION:
        return parameters.verify(before, after, request)
    old = before['settings']['values']
    new = after['settings']['values']
    if operation == 'choose-tool':
        field, expected = 'tool', request['tool_id']
        allowed = {'tool', 'last_result', 'status', 'status_kind'}
        if new['status_kind'] != 'INFO' or new['last_result']:
            raise Failure('VALIDATION_FAILED', 'The native tool menu did not update its panel status')
    else:
        field, expected = 'target_object', request['object']
        preview = {key for key in new if key.startswith('preview_')}
        allowed = {'target_object', 'status', 'status_kind', 'needs_preview'} | preview
        if new['status_kind'] != 'SUCCESS' or not new['needs_preview'] or any(new[key] for key in preview):
            raise Failure('VALIDATION_FAILED', 'Binding the active mesh did not clear its previous preview')
    if new[field] != expected:
        raise Failure('VALIDATION_FAILED', f'The saved {field} differs from the requested value')
    changed = sorted(key for key in new if new[key] != old[key])
    if set(changed) - allowed:
        raise Failure('VALIDATION_FAILED', f'Unrelated panel settings changed: {sorted(set(changed) - allowed)}')
    if (before['scene_state_sha256'] != after['scene_state_sha256'] or before['profile'] != after['profile']
            or before['settings']['params'] != after['settings']['params']):
        raise Failure('VALIDATION_FAILED', 'Choosing a tool or binding an object changed protected scene, profile or tool parameters')
    return {'kind': operation, 'field': field, 'previous': old[field], 'current': new[field],
            'changed_settings': changed}
