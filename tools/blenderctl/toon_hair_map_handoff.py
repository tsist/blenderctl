"""Drive the packaged Toon hair-map panel on the slot selected by surface-slot."""

from material_draft_handoff import require


UPDATE_FIELDS = {
    'strength', 'map_strength', 'neutral', 'shadow_bias', 'shadow_softness', 'highlight_strength',
    'highlight_threshold', 'highlight_map_strength', 'highlight_softness',
    'highlight_color', 'shadow_color', 'lit_color',
}
INPUTS = {
    'hair-map-attach': {'image_name', 'sun', 'uv_name'},
    'hair-map-mask-attach': {'highlight_mask_image_name', 'highlight_mask_uv_name', 'mask_black_level', 'mask_white_level'},
    'hair-map-update': UPDATE_FIELDS,
}
OPERATIONS = {*INPUTS, 'hair-map-read', 'hair-map-remove', 'hair-map-mask-remove'}


def guard_images(bpy, material, request, guard):
    # Groups may be nested or shared. Pin the FILE images the shader actually reads.
    pending = [material.node_tree] if material and material.node_tree else []
    seen = set()
    while pending:
        tree = pending.pop()
        if tree in seen:
            continue
        seen.add(tree)
        for node in tree.nodes:
            image = getattr(node, 'image', None)
            if image is not None and image.source == 'FILE':
                guard(bpy, image, request)
            if node.type == 'GROUP' and node.node_tree:
                pending.append(node.node_tree)


def apply(bpy, ui, context, request, image_guard):
    op, values = request['operation'], request.get('settings', {})
    panel = ui._settings(context)
    scene, obj, index = ui._context(context)
    material = obj.material_slots[index].material
    guard_images(bpy, material, request, image_guard)
    present = ui.hair_map.present(material)
    if op == 'hair-map-read' and not present:
        return {'present': False, 'material': material.name_full}
    if op == 'hair-map-attach':
        require(not present, 'CONFLICT', 'Hair map already exists on the named slot')
        image_guard(bpy, bpy.data.images.get(values['image_name']), request)
        sun = scene.objects.get(values['sun'])
        require(sun and sun.type == 'LIGHT' and sun.data.type == 'SUN', 'NOT_FOUND', 'Named Sun is absent')
        panel.image_name, panel.sun, panel.uv_name = values['image_name'], sun, values['uv_name']
        return {'present': True, **ui.attach_current(context)}

    require(present, 'NOT_FOUND', 'Hair map is absent from this slot')
    original = ui.read_current(context)
    if op == 'hair-map-read':
        return {'present': True, **original}
    if op == 'hair-map-remove':
        result = ui.remove_current(context)
        require(result['restored'] and not ui.hair_map.present(material), 'VALIDATION_FAILED',
                'Native hair map was not removed')
        return {'present': False, **result}
    if op == 'hair-map-update':
        colors = {key: [*value, 1.0] for key, value in values.items() if key.endswith('_color')}
        normalized = ui.hair_map_nodes.normalize({**original['settings'], **values, **colors})
        for key in values:
            value = normalized[key]
            setattr(panel, key, value[:3] if key in colors else value)
        result = ui.update_current(context)
    elif op == 'hair-map-mask-attach':
        require('highlight_mask' not in original, 'CONFLICT', 'Remove the existing hair highlight mask first')
        image_guard(bpy, bpy.data.images.get(values['highlight_mask_image_name']), request)
        for key, value in values.items():
            setattr(panel, key, value)
        result = ui.mask_attach_current(context)
    else:
        require(op == 'hair-map-mask-remove' and 'highlight_mask' in original,
                'NOT_FOUND', 'The requested highlight mask is absent')
        result = ui.mask_remove_current(context)
    require(result['owner'] == original['owner'], 'VALIDATION_FAILED', 'Native hair-map owner changed')
    return {'present': True, **result}
