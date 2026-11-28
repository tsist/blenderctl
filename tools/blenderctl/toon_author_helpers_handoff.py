"""Validate and replay the author panel's editable shape, viewport layout and preview choices."""
from hashlib import sha256

from material_draft_handoff import fingerprint, require
import toon_viewport_lines_contract as contract


def validate_shape(data):
    require(type(data) is dict and data.get('mode') in ('FACES', 'NOISE', 'IMAGE', 'RAYCAST'),
            'INVALID_REQUEST', 'Choose native FACES, NOISE, IMAGE or RAYCAST')
    mode = data['mode']
    needed = {'FACES': {'faces'}, 'NOISE': {'threshold'}, 'IMAGE': {'image', 'threshold'}, 'RAYCAST': {'ray_target'}}[mode]
    require(data.keys() == {'mode', 'object', 'label', 'border_only'} | needed and
            all(contract.name(data[key], 63) for key in {'object', 'label', 'image', 'ray_target'} & data.keys())
            and type(data['border_only']) is bool, 'INVALID_REQUEST', 'Specify the named shape source, output label and mode controls')
    require(len((data['label'] + ' · Field Mask').encode('utf-8')) <= 63, 'INVALID_REQUEST', 'Shape label is too long for its native node group')
    if 'threshold' in data:
        require(contract.finite(data['threshold']) and 0 <= data['threshold'] <= 1,
                'INVALID_REQUEST', 'Expected a finite selection threshold in [0,1]')
    if 'faces' in data:
        faces = data['faces']
        require(type(faces) is list and 1 <= len(faces) <= 65536 and all(type(i) is int and i >= 0 for i in faces)
                and len(faces) == len(set(faces)), 'INVALID_REQUEST', 'Expected distinct saved polygon indices')


def local_mesh(context, name):
    obj = context.view_layer.objects.get(name)
    require(obj is not None and obj.type == 'MESH' and obj.mode == 'OBJECT' and obj.is_editable
            and obj.library is None and obj.override_library is None and obj.data.library is None and obj.data.is_editable,
            'CONFLICT', 'Expected a named local editable mesh in the saved view layer')
    return obj


def shape_signature(context, obj):
    from inspection import record

    return fingerprint([record(obj), record(obj.data), sorted(c.name for c in obj.users_collection),
                        obj.select_get(view_layer=context.view_layer)])


def shape_state(context, obj):
    modifier = obj.modifiers.get('Autonomous Selection Field')
    require(modifier is not None and modifier.node_group is not None, 'CONFLICT', 'Owned shape modifier is missing')
    tree = modifier.node_group
    inputs = {}
    for port in tree.interface.items_tree:
        if port.item_type != 'SOCKET' or port.in_out != 'INPUT' or port.socket_type == 'NodeSocketGeometry':
            continue
        value = getattr(modifier.properties.inputs, port.identifier).value
        if hasattr(value, 'name_full'):
            value = value.name_full
        inputs[port.name] = value
    mask = obj.data.attributes.get('toon_selection')
    evaluated = obj.evaluated_get(context.evaluated_depsgraph_get())
    require(mask is not None, 'CONFLICT', 'Owned shape is missing its selection attribute')
    return {'object': obj.name, 'source': obj.get('blenderctl_author_shape_source'),
            'mode': obj.get('toon_selection_shape_mode'), 'modifier': modifier.name, 'node_group': tree.name,
            'inputs': inputs, 'selected_polygon_indices': [i for i, row in enumerate(mask.data) if row.value],
            'evaluated_sizes': {'vertices': len(evaluated.data.vertices), 'polygons': len(evaluated.data.polygons)}}


def build_shape(bpy, context, data, native):
    obj = local_mesh(context, data['object'])
    label = data['label']
    require(label not in bpy.data.objects and label not in bpy.data.meshes and label + ' · Field Mask' not in bpy.data.node_groups,
            'CONFLICT', 'Shape destination names already exist')
    require(context.collection is not None and context.collection.library is None and context.collection.is_editable,
            'CONFLICT', 'Use a local destination collection in the saved view layer')
    image = None
    target = None
    if 'image' in data:
        image = bpy.data.images.get(data['image'])
        require(image is not None and image.library is None and image.packed_file and obj.data.uv_layers.active,
                'CONFLICT', 'Image selection needs a local packed image and the source mesh active UV')
    if 'ray_target' in data:
        target = local_mesh(context, data['ray_target'])
        require(target != obj, 'CONFLICT', 'Raycast target must differ from the source')
    require(not obj.data.attributes.get('toon_selection') or obj.data.attributes['toon_selection'].domain == 'FACE'
            and obj.data.attributes['toon_selection'].data_type == 'BOOLEAN', 'CONFLICT', 'Existing selection attribute is incompatible')
    face_ids = set(data.get('faces', []))
    require(all(index < len(obj.data.polygons) for index in face_ids), 'CONFLICT', 'Polygon index is outside the saved source mesh')
    if face_ids:
        original = [face.select for face in obj.data.polygons]
        for face in obj.data.polygons:
            face.select = face.index in face_ids
    try:
        with context.temp_override(active_object=obj, object=obj):
            out = native.selection_generator(context, mode=data['mode'], label=label, threshold=data.get('threshold', .5),
                                             image=image, ray_target=target, border_only=data['border_only'])
    finally:
        if face_ids:
            for face, selection in zip(obj.data.polygons, original):
                face.select = selection
    out['blenderctl_author_shape_source'] = obj.name
    context.view_layer.update()
    return shape_state(context, out)


def workspace_state(workspace):
    return {'name': workspace.name, 'origin': workspace.get('toon_workspace_origin'),
            'screens': [{'name': screen.name, 'views': [
                {'ui': area.spaces.active.show_region_ui, 'shading': area.spaces.active.shading.type}
                for area in screen.areas if area.type == 'VIEW_3D'], 'properties': [area.spaces.active.context
                for area in screen.areas if area.type == 'PROPERTIES']} for screen in workspace.screens]}


def create_workspace(bpy, context, action, native):
    require(len(action['label'].encode('utf-8')) <= 63, 'INVALID_REQUEST', 'Workspace name is too long')
    require(action['label'] not in bpy.data.workspaces, 'CONFLICT', 'Workspace name already exists')
    workspace = native.workspace(context, name=action['label'])
    return workspace_state(workspace)


def preview_state(image):
    if image is None:
        return None
    return {'image': image.name, 'actual': image.get('toon_preview_actual'), 'size': list(image.size),
            'packed_sha256': sha256(image.packed_file.data).hexdigest() if image.packed_file else None}


def select_preview(bpy, scene, action):
    name = action['image']
    image = bpy.data.images.get(name) if name else None
    require(name is None or image is not None and image.library is None, 'CONFLICT', 'Preview image is not saved locally')
    scene.toon_author_files.preview_image = image
    return {'preview_image': preview_state(image)}
