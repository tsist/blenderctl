"""Select a native Toon 5.3 shadow controller in one attached viewport."""

import importlib
from pathlib import Path
import re

from host_compat import is_supported_host
from material_draft_handoff import fingerprint, require
from protocol import Failure, digest, read_json


CONTROLS = ('toon53_shadow_controller', 'toon53_shadow_anchor',
            'toon53_shadow_handle', 'toon53_eyelid_control')
PARAMS = {
    'shadow-status': {'view_id'},
    'select-shadow-control': {'view_id', 'expected_context', 'control', 'object_id'},
}


def validate(operation, params):
    if operation == 'select-shadow-control':
        require(params['control'] in CONTROLS, 'INVALID_REQUEST', 'Use an exact native Toon shadow control')
        identity = params['object_id']
        require(identity is None or isinstance(identity, str) and re.fullmatch('[0-9a-f]+', identity),
                'INVALID_REQUEST', 'Use the current object_id from shadow-status')


def pointer(value):
    return f'{value.as_pointer():x}' if value is not None else None


def native_operator(bpy, owner):
    require(owner['info']['edition'] == '0.52.1' and is_supported_host(bpy.app.version),
            'UNSUPPORTED', 'Toon shadow control selection requires Blender 5.3.0 and Toon 0.52.1')
    try:
        op = bpy.ops.toon53.select_shadow_control
        rna = op.get_rna_type()
        operator = bpy.types.Operator.bl_rna_get_subclass_py(rna.identifier)
        ui = importlib.import_module(operator.__module__)
        bundle = Path(owner['native'].__file__).resolve().parents[1]
        folder = bundle / 'toon_workflow_addon'
        descriptor = bundle / 'bundle.json'
        require(digest(descriptor) == owner['info']['manifest_sha256'], 'UNSUPPORTED',
                'The enabled Toon bundle manifest differs from this attachment')
        manifest = read_json(descriptor)['files']
        files = {'lighting_53_ui.py': ui, 'lighting_53.py': ui.lighting}
        for filename, module in files.items():
            path = Path(module.__file__).resolve()
            require(path == folder / filename
                    and digest(path) == manifest['toon_workflow_addon/' + filename],
                    'UNSUPPORTED', 'Enabled Toon shadow selection differs from the frozen package')
        require(operator is ui.TOON53_OT_select_control
                and tuple(getattr(ui.lighting, attr) for attr in
                          ('CONTROLLER', 'LOCAL_ANCHOR', 'LOCAL_HANDLE', 'EYELID_CONTROL')) == CONTROLS,
                'UNSUPPORTED', 'The registered Toon shadow selection operator differs')
    except (AttributeError, ImportError, KeyError, OSError, RuntimeError) as exc:
        raise Failure('UNSUPPORTED', 'Enable the matching Toon 0.52.1 native shadow selection operator') from exc
    return op


def selected(layer):
    return {obj for obj in layer.objects if obj.select_get(view_layer=layer)}


def view_state(space):
    return (space.region_3d.view_perspective, space.show_gizmo_object_translate, space.overlay.show_extras)


def observe(bpy, context, owner, view_id):
    scene, layer, space = context.scene, context.view_layer, context.space_data
    selectable = set(context.selectable_objects)
    controls = {}
    for key in CONTROLS:
        obj = scene.get(key)
        if not isinstance(obj, bpy.types.Object) or obj.type != 'EMPTY' or scene.objects.get(obj.name) != obj:
            controls[key] = {'object': None, 'object_id': None, 'selectable': False, 'in_view_layer': False}
            continue
        controls[key] = {'object': obj.name, 'object_id': pointer(obj), 'session_uid': obj.session_uid,
                         'parent_id': pointer(obj.parent), 'matrix_basis': [list(row) for row in obj.matrix_basis],
                         'selectable': obj in selectable, 'in_view_layer': layer.objects.get(obj.name) == obj}
    camera = scene.camera
    active = layer.objects.active
    view_type, gizmo, extras = view_state(space)
    state = {
        'file': bpy.data.filepath, 'scene': scene.name, 'view_layer': layer.name,
        'window_id': owner['info']['window_id'], 'view_id': view_id, 'region_id': pointer(context.region),
        'frame': scene.frame_current, 'subframe': scene.frame_subframe, 'mode': context.mode,
        'camera': camera.name if camera else None, 'camera_id': pointer(camera),
        'camera_matrix': [list(row) for row in camera.matrix_world] if camera else None,
        'local_camera': bool(space.use_local_camera), 'local_camera_id': pointer(space.camera),
        'view_type': view_type, 'translate_gizmo': gizmo, 'show_extras': extras,
        'selected': sorted(obj.name for obj in selected(layer)), 'active': active.name if active else None,
        'active_id': pointer(active), 'controls': controls,
    }
    return {**state, 'expected_context': fingerprint(state)}


def other_scope(bpy, context):
    selection = [(scene.name, layer.name, sorted(pointer(obj) for obj in selected(layer)),
                  pointer(layer.objects.active))
                 for scene in bpy.data.scenes for layer in scene.view_layers if layer != context.view_layer]
    areas = {pointer(area): view_state(area.spaces.active)
             for window in bpy.context.window_manager.windows for area in window.screen.areas
             if area != context.area and area.type == 'VIEW_3D' and area.spaces.active.region_3d is not None}
    return selection, areas


def execute(bpy, owner, operation, params):
    op = native_operator(bpy, owner)
    context = bpy.context
    before = observe(bpy, context, owner, params['view_id'])
    if operation == 'shadow-status':
        return before
    require(before['expected_context'] == params['expected_context'], 'CONFLICT',
            'The shadow control viewport or selection changed after shadow-status')
    control = before['controls'][params['control']]
    require(control['object_id'] is not None, 'NOT_FOUND', 'This native shadow control has not been created')
    require(control['object_id'] == params['object_id'], 'CONFLICT', 'The identified shadow control changed')
    require(before['mode'] == 'OBJECT', 'CONFLICT', 'Switch this viewport to Object Mode before selecting a shadow control')
    overall = context.scene.get(CONTROLS[0])
    camera = context.scene.camera
    require(camera is not None and camera.type == 'CAMERA'
            and before['controls'][CONTROLS[0]]['in_view_layer'] and overall.parent == camera
            and (not before['local_camera'] or before['local_camera_id'] == before['camera_id']),
            'CONFLICT', 'Use the shadow controller camera as the scene and viewport camera')
    require(control['in_view_layer'] and control['selectable'], 'CONFLICT',
            'Make the named shadow control visible and selectable in this viewport and View Layer')
    require(op.poll(), 'CONFLICT', 'The native shadow selection operator is unavailable in this viewport')
    layer, space = context.view_layer, context.space_data
    target = context.scene.get(params['control'])
    original_selection, original_active, original_view = selected(layer), layer.objects.active, view_state(space)
    expected_selection = (original_selection - set(context.selected_objects)) | {target}
    unaffected = other_scope(bpy, context)
    try:
        require(op(control=params['control']) == {'FINISHED'}, 'CONFLICT', 'Native Toon refused the shadow control selection')
        require(selected(layer) == expected_selection and layer.objects.active == target
                and view_state(space) == ('CAMERA', True, True) and other_scope(bpy, context) == unaffected,
                'VALIDATION_FAILED', 'Native selection, camera view, gizmo or selection scope differs')
    except Exception:
        for obj in selected(layer) ^ original_selection:
            obj.select_set(obj in original_selection, view_layer=layer)
        if layer.objects.active != original_active:
            layer.objects.active = original_active
        if view_state(space) != original_view:
            space.region_3d.view_perspective, space.show_gizmo_object_translate, space.overlay.show_extras = original_view
        raise
    return {**observe(bpy, context, owner, params['view_id']), 'native_operator': 'toon53.select_shadow_control',
            'control': params['control']}
