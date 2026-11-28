"""Saved-file adapter for the native Blender 5.3 directional face stage."""

import hashlib
import importlib
import math

from material_draft_handoff import require
from toon_panel_handoff import name


INSPECT = 'directional-face-inspect'
BIND = 'directional-face-bind'
UNBIND = 'directional-face-unbind'
NATIVE_INSPECT = 'native-surface-inspect'
NATIVE_QUALIFY = 'native-surface-qualify'
NATIVE_OPERATIONS = {NATIVE_INSPECT, NATIVE_QUALIFY}
OPERATIONS = {INSPECT, BIND, UNBIND} | NATIVE_OPERATIONS
COMMON = {'schema', 'operation', 'edition', 'file', 'expected_sha256', 'context', 'target', 'resources'}
AXES = {'X', 'Y', 'Z', '-X', '-Y', '-Z'}
CONTROLS = {
    'Enabled': (1, 0., 1.),
    'Lit Color': (4, 0., 1.),
    'Shadow Color': (4, 0., 1.),
    'Key Gain': (1, 0., 20.),
    'Cast Weight': (1, 0., 1.),
    'Fill Gain': (1, 0., 1.),
    'Rim Gain': (1, 0., 1.),
    'UV Scale': (3, None, None),
    'UV Offset': (3, None, None),
    'Base Tint': (4, 0., 1.),
}


def _number(value, low, high):
    return (type(value) in (int, float) and -3.4028234e38 <= value <= 3.4028234e38
            and (low is None or low <= value <= high))


def validate(document):
    """Validate extension fields after the shared saved-file request has been validated."""
    operation = document.get('operation')
    require(isinstance(operation, str) and operation in OPERATIONS and isinstance(document.get('target'), dict)
            and name(document['target'].get('expected_material')), 'INVALID_REQUEST',
            'Directional face requires a named existing material slot')
    allowed = COMMON | ({'binding', 'rig'} if operation == BIND else set())
    require(document.keys() <= allowed, 'INVALID_REQUEST',
            'Unexpected directional face request fields: ' + ', '.join(sorted(document.keys() - allowed)))
    if operation != BIND:
        return document
    binding = document.get('binding')
    required = {'head', 'axes', 'uv', 'field'}
    require(isinstance(binding, dict) and required <= binding.keys() <= required | {'scope', 'valid', 'controls'}
            and all(name(binding[key]) for key in ('head', 'uv', 'field')),
            'INVALID_REQUEST', 'binding needs head, axes, uv, field and optional scope, valid, controls')
    axes = binding['axes']
    require(type(axes) is list and len(axes) == 3 and all(isinstance(axis, str) and axis in AXES for axis in axes)
            and len({axis[-1] for axis in axes}) == 3,
            'INVALID_REQUEST', 'binding.axes needs three distinct signed axes in right, up, front order')
    require(all(binding.get(key, '') == '' or name(binding[key]) for key in ('scope', 'valid')),
            'INVALID_REQUEST', 'binding.scope and binding.valid must be attribute names or empty strings')
    controls = binding.get('controls', {})
    require(type(controls) is dict and controls.keys() <= CONTROLS.keys(), 'INVALID_REQUEST',
            'binding.controls must use existing directional face socket names')
    for socket, value in controls.items():
        size, low, high = CONTROLS[socket]
        values = [value] if size == 1 else value
        require(type(values) is list and len(values) == size and all(_number(item, low, high) for item in values),
                'INVALID_REQUEST', 'Invalid directional face control ' + socket)
    if 'rig' in document:
        rig = document['rig']
        require(type(rig) is dict and rig.keys() == {'key', 'fill', 'rim'} and name(rig['key'])
                and all(rig[key] is None or name(rig[key]) for key in ('fill', 'rim')),
                'INVALID_REQUEST', 'rig needs a named key and explicit nullable fill and rim')
        roles = [value for value in rig.values() if value is not None]
        require(len(roles) == len(set(roles)), 'INVALID_REQUEST', 'Each rig light must be distinct')
    return document


def _directional(lighting):
    return importlib.import_module(lighting.__package__ + '.directional_face_53')


def _surface_target(bpy, scene, request, lighting):
    lighting._available(scene)
    spec = request['target']
    target = bpy.context.view_layer.objects.get(spec['object'])
    require(lighting._present(scene, target, 'MESH') and lighting._local(target.data)
            and target.mode == 'OBJECT' and not target.data.is_editmode,
            'CONFLICT', 'Use a local mesh in the saved Object Mode view layer')
    slot = spec['material_slot']
    require(0 <= slot < len(target.material_slots), 'NOT_FOUND', 'The named material slot is absent')
    material_slot = target.material_slots[slot]
    material = material_slot.material
    require(material is not None and material.name_full == spec['expected_material']
            and material_slot.link == spec['expected_link'], 'CONFLICT',
            'The saved material slot name or link differs from the requested baseline')
    require(lighting._local(material) and lighting._local(material.node_tree)
            and any(face.material_index == slot for face in target.data.polygons),
            'CONFLICT', 'The material must have a local node tree and be assigned to real faces')
    output = material.node_tree.get_output_node('EEVEE')
    require(output is not None and len(output.inputs['Surface'].links) == 1 and not output.inputs['Volume'].is_linked,
            'CONFLICT', 'Expected one unambiguous opaque EEVEE Surface connection')
    surface = output.inputs['Surface']
    require(surface.links[0].is_valid and not surface.links[0].is_muted, 'CONFLICT',
            'The EEVEE Surface connection must be active')
    return target, slot, material, surface


def _upstream(socket):
    return socket.links[0].from_socket if len(socket.links) == 1 else None


def _check_write_scope(bpy, scene, target, material):
    require(tuple(target.users_scene) == (scene,), 'CONFLICT',
            'The target object also belongs to another scene')
    require(not any(owner != target and any(slot.material == material for slot in owner.material_slots)
                    for owner in bpy.data.objects), 'CONFLICT',
            'Another object uses the requested material; save it in an isolated slot first')


def _socket_id(socket):
    return {'node': socket.node.name, 'socket': socket.identifier} if socket is not None else None


def _animation_paths(animation):
    if animation is None:
        return []
    actions = [animation.action] if animation.action else []
    strips = [strip for track in animation.nla_tracks for strip in track.strips]
    while strips:
        strip = strips.pop()
        if strip.action:
            actions.append(strip.action)
        strips.extend(getattr(strip, 'strips', ()))
    paths = {curve.data_path for curve in animation.drivers}
    for action in actions:
        for layer in action.layers:
            for strip in layer.strips:
                for bag in getattr(strip, 'channelbags', ()):
                    paths.update(curve.data_path for curve in bag.fcurves)
    return sorted(paths)


def _packed_images(root, directional):
    directional._validate_images(root)
    pending, seen, images = [root], set(), {}
    while pending:
        group = pending.pop()
        if group in seen:
            continue
        seen.add(group)
        for node in group.nodes:
            if node.bl_idname == 'ShaderNodeGroup' and node.node_tree:
                pending.append(node.node_tree)
            elif node.bl_idname == 'ShaderNodeTexImage' and node.image:
                image = node.image
                packed = sorted(hashlib.sha256(bytes(item.packed_file.data)).hexdigest() for item in image.packed_files)
                images[image.name_full] = {'name': image.name_full, 'colorspace': image.colorspace_settings.name,
                                           'packed_sha256': packed}
    return [images[key] for key in sorted(images)]


def _binding_details(group, directional):
    fields = [node for node in group.nodes if node.bl_idname == 'ShaderNodeGroup' and directional.valid_field(node.node_tree)]
    uv_layers = [node.uv_map for node in group.nodes if node.bl_idname == 'ShaderNodeUVMap']
    require(len(fields) == len(uv_layers) == 1, 'CONFLICT', 'The directional field or projected UV is ambiguous')
    lookup, = fields
    source = _upstream(lookup.inputs['Light'])
    require(source is not None and source.node.bl_idname == 'ShaderNodeCombineXYZ', 'CONFLICT',
            'The directional field lost its authored head-space connection')
    drivers = {curve.data_path: curve.driver for curve in group.animation_data.drivers} if group.animation_data else {}
    axes, heads = [], set()
    for socket in source.node.inputs:
        dot = _upstream(socket)
        require(dot is not None, 'CONFLICT', 'A directional head-space component is disconnected')
        negative = dot.node.bl_idname == 'ShaderNodeMath'
        if negative:
            require(dot.node.operation == 'MULTIPLY' and dot.node.inputs[1].default_value == -1., 'CONFLICT',
                    'Unexpected axis sign transform in the native directional field')
            dot = _upstream(dot.node.inputs[0])
        require(dot is not None and dot.node.bl_idname == 'ShaderNodeVectorMath' and dot.node.operation == 'DOT_PRODUCT',
                'CONFLICT', 'The native directional field lost an axis dot product')
        unit = _upstream(dot.node.inputs[1])
        require(unit is not None and unit.node.bl_idname == 'ShaderNodeVectorMath' and unit.node.operation == 'NORMALIZE',
                'CONFLICT', 'The head axis is not normalized')
        coordinate = _upstream(unit.node.inputs[0])
        require(coordinate is not None and coordinate.node.bl_idname == 'ShaderNodeCombineXYZ', 'CONFLICT',
                'The native directional field lost its axis reference')
        targets = []
        for part in coordinate.node.inputs:
            driver = drivers.get(part.path_from_id('default_value'))
            require(driver is not None and len(driver.variables) == 1 and driver.variables[0].targets[0].id is not None,
                    'CONFLICT', 'The head-space driver has no target')
            targets.append(driver.variables[0].targets[0])
        column = next((col for col in range(3) if [target.data_path for target in targets]
                       == [f'matrix_world[{col}][{row}]' for row in range(3)]), None)
        require(column is not None, 'CONFLICT', 'A head driver targets an unexpected matrix axis')
        heads.update(target.id.name_full for target in targets)
        axes.append(('-' if negative else '') + 'XYZ'[column])
    require(len(heads) == 1 and len({axis[-1] for axis in axes}) == 3, 'CONFLICT',
            'The directional field references more than one head or repeats an axis')
    scope = valid = ''
    for node in group.nodes:
        if node.bl_idname != 'ShaderNodeAttribute' or node.attribute_type != 'GEOMETRY':
            continue
        consumers = {link.to_node.bl_idname for link in node.outputs['Fac'].links}
        if 'ShaderNodeMapRange' in consumers:
            scope = node.attribute_name
        elif 'ShaderNodeMath' in consumers:
            valid = node.attribute_name
    return {'head': next(iter(heads)), 'axes': axes, 'uv': uv_layers[0], 'field': lookup.node_tree.name_full,
            'scope': scope, 'valid': valid, 'images': _packed_images(lookup.node_tree, directional)}


def _rig_record(scene, lighting):
    return {role: value.name_full if value is not None else None for role, value in (
        ('key', scene.get(lighting.KEY_LIGHT)), ('fill', scene.get(lighting.FILL_LIGHT)), ('rim', scene.get(lighting.RIM_LIGHT)))}


def snapshot(bpy, scene, request, lighting):
    """Read actual saved native wiring and editable controls for cold comparison."""
    if request['operation'] in NATIVE_OPERATIONS:
        return _native_snapshot(bpy, scene, request, lighting)
    directional = _directional(lighting)
    target, slot, material, surface = _surface_target(bpy, scene, request, lighting)
    stage = directional.active_stage(material)
    source = _upstream(surface)
    record = {'bound': stage is not None, 'object': target.name_full, 'material_slot': slot,
              'material': material.name_full, 'material_link': target.material_slots[slot].link,
              'rig': _rig_record(scene, lighting), 'surface': _socket_id(source), 'original_surface': None,
              'binding': None, 'controls': {}, 'stage': None, 'stage_animation_paths': []}
    if stage is None:
        require(lighting.DIRECTIONAL_STAGE not in material and source.node.get(lighting.ROLE) != lighting.ROLE_DIRECTIONAL_FACE,
                'CONFLICT', 'The material has a directional wrapper with a missing or stale owner marker')
        return record
    group = stage.node_tree
    owner = lighting._identity(scene)
    original = _upstream(stage.inputs['Original Surface'])
    used_outputs = [socket.identifier for socket in stage.outputs if socket.is_linked]
    require(stage.get(lighting.OWNER) == owner and group is not None and group.get(lighting.OWNER) == owner
            and group.get(lighting.PROJECT_REF) == scene and lighting.is_owned_group(group)
            and scene.get(lighting.FACE_OBJECT) == target and original is not None
            and source == stage.outputs['Surface'], 'CONFLICT', 'The native directional wrapper, owner or Surface ingress changed')
    record['stage'] = {'node': stage.name, 'group': group.name_full, 'signature': group[lighting.SIGNATURE],
                       'used_outputs': used_outputs}
    record['original_surface'] = _socket_id(original)
    record['binding'] = _binding_details(group, directional)
    for label, kind, _ in directional.CONTROLS:
        socket = stage.inputs[label]
        value = float(socket.default_value) if kind == 'NodeSocketFloat' else list(socket.default_value)
        components = value if isinstance(value, list) else [value]
        require(all(math.isfinite(item) for item in components), 'CONFLICT',
                'A saved directional control contains a non-finite value')
        record['controls'][label] = {'value': value, 'link': _socket_id(_upstream(socket))}
    record['stage_animation_paths'] = [path for path in _animation_paths(material.node_tree.animation_data)
                                      if path.startswith(stage.path_from_id() + '.')]
    return record


def _prepare_rig(scene, target, slot, request, lighting):
    stored = _rig_record(scene, lighting)
    asked = request.get('rig')
    if scene.get(lighting.PROJECT):
        require(scene.get(lighting.FACE_OBJECT) == target, 'CONFLICT', 'The scene already owns a different face object')
        require(asked is None or asked == stored, 'CONFLICT', 'Saved light roles differ; keep the existing hand-authored rig')
        require(stored['key'] is not None, 'CONFLICT', 'The saved scene has no usable key light')
        tag = lighting._tag_directional_lights(scene)
        tagged = {light.name_full for light in scene.objects if light.type == 'LIGHT' and (tag in light or tag in light.data)}
        require(tagged == {value for value in stored.values() if value is not None}, 'CONFLICT',
                'The saved directional light tags do not match the registered roles')
        for role, number in (('key', 1.), ('fill', 2.), ('rim', 3.)):
            light = scene.objects.get(stored[role]) if stored[role] else None
            require(stored[role] is None or (lighting._light_available(scene, light)
                    and light.get(tag) == light.data.get(tag) == number
                    and (role != 'key' or light.data.type == 'SUN')), 'CONFLICT',
                    'The saved rig has stale directional light tags. Prepare the matching rig in the 5.3 workbench first.')
        return
    require(asked is not None, 'INVALID_REQUEST', 'The unprepared scene requires an explicit key/fill/rim rig on bind')
    lights = {role: scene.objects.get(value) if value is not None else None for role, value in asked.items()}
    require(all(asked[role] is None or lights[role] is not None for role in asked), 'NOT_FOUND',
            'A requested directional light is not in the saved scene')
    require(all(light is None or tuple(light.users_scene) == (scene,) for light in lights.values()), 'CONFLICT',
            'A requested light is also used by another scene')
    lighting.prepare(scene, lights['key'], target, fill=lights['fill'], rim=lights['rim'], face_slot=slot)


def operate(bpy, scene, request, lighting):
    """Run the same binding core as the GUI, or read the saved state without mutation."""
    action = request['operation']
    require(action in OPERATIONS, 'INVALID_REQUEST', 'Unknown directional face operation')
    if action in NATIVE_OPERATIONS:
        return _operate_native(bpy, scene, request, lighting)
    before = snapshot(bpy, scene, request, lighting)
    if action == INSPECT:
        return before
    target, slot, material, _ = _surface_target(bpy, scene, request, lighting)
    _check_write_scope(bpy, scene, target, material)
    directional = _directional(lighting)
    if action == UNBIND:
        require(before['bound'] and not before['stage_animation_paths'], 'CONFLICT',
                'Unbind needs an intact directional stage without animated controls')
        stage = directional.active_stage(material)
        original, = stage.inputs['Original Surface'].links
        require(original.is_valid and not original.is_muted, 'CONFLICT',
                'The saved original Surface connection must be active')
        field = bpy.data.node_groups[before['binding']['field']]
        preserve_field = field.users == 1 and not field.use_fake_user
        if preserve_field:
            field.use_fake_user = True
        try:
            directional.unbind(scene, target, slot)
        except (ValueError, RuntimeError):
            if preserve_field:
                field.use_fake_user = False
            raise
        result = snapshot(bpy, scene, request, lighting)
        require(not result['bound'] and result['surface'] == before['original_surface'], 'VALIDATION_FAILED',
                'Unbind did not restore the exact original EEVEE Surface connection')
        return result
    require(not before['bound'], 'CONFLICT', 'The material already has a hand-authored directional face stage')
    binding = request['binding']
    head = scene.objects.get(binding['head'])
    field = bpy.data.node_groups.get(binding['field'])
    require(lighting._present(scene, head, 'EMPTY') and directional.valid_field(field)
            and binding['uv'] in target.data.uv_layers, 'CONFLICT',
            'Expected a local head Empty, existing directional field and native face UV')
    directional._validate_images(field)
    expected = {key: binding.get(key, '') for key in ('head', 'axes', 'uv', 'field', 'scope', 'valid')}
    _prepare_rig(scene, target, slot, request, lighting)
    stage = directional.bind(scene, target, slot, head=head, axes=binding['axes'], uv_name=binding['uv'], field=field,
                             scope_name=expected['scope'], valid_name=expected['valid'])
    for label, value in binding.get('controls', {}).items():
        stage.inputs[label].default_value = value
    result = snapshot(bpy, scene, request, lighting)
    require(result['bound'] and result['original_surface'] == before['surface']
            and all(result['binding'][key] == value for key, value in expected.items()), 'VALIDATION_FAILED',
            'The native direction, UV, scope or original EEVEE Surface differs from the requested bind')
    return result


def _native_witness(lighting):
    return importlib.import_module(lighting.__package__ + '.native_surface_witness')


def _native_tree_record(tree):
    return {'nodes': sorted((node.name, node.bl_idname) for node in tree.nodes),
            'links': sorted((link.from_node.name, link.from_socket.identifier, link.to_node.name,
                             link.to_socket.identifier, bool(link.is_muted)) for link in tree.links)}


def _native_snapshot(bpy, scene, request, lighting):
    target, slot, material, surface = _surface_target(bpy, scene, request, lighting)
    witness = _native_witness(lighting)
    tree = material.node_tree
    stage_signature = tree.get(witness.KEY)
    if stage_signature is not None:
        require(isinstance(stage_signature, str), 'CONFLICT', 'The existing native qualification marker is malformed')
    live_signature = None
    failure = None
    try:
        live_signature = witness.signature(tree)
    except (ValueError, TypeError, AttributeError, RuntimeError) as exc:
        failure = str(exc)
    graph = _native_tree_record(tree)
    return {'object': target.name_full, 'material_slot': slot, 'material': material.name_full,
            'material_link': target.material_slots[slot].link, 'surface': _socket_id(_upstream(surface)),
            'native_surface': {'qualified': witness.valid(tree), 'witness_key': witness.KEY,
                               'stored_signature': stage_signature, 'evaluated_signature': live_signature,
                               'qualification_error': failure,
                               'accumulations': [name for name, kind in graph['nodes'] if kind == witness.ACCUMULATION],
                               'node_count': len(graph['nodes']), 'link_count': len(graph['links'])}}


def _operate_native(bpy, scene, request, lighting):
    before = _native_snapshot(bpy, scene, request, lighting)
    if request['operation'] == NATIVE_INSPECT:
        return before
    target, slot, material, _ = _surface_target(bpy, scene, request, lighting)
    _check_write_scope(bpy, scene, target, material)
    lighting._material_context(scene, target, slot)
    require(sum(item.material == material for item in target.material_slots) == 1, 'CONFLICT',
            'The native surface must be exclusive to the requested material slot')
    tree = material.node_tree
    original_graph = _native_tree_record(tree)
    witness = _native_witness(lighting)
    count = witness.qualify(scene, material)
    verified = _native_snapshot(bpy, scene, request, lighting)
    status = verified['native_surface']
    require(status['qualified'] and status['stored_signature'] == status['evaluated_signature']
            == before['native_surface']['evaluated_signature']
            and count == len(status['accumulations']) and _native_tree_record(tree) == original_graph
            and verified['surface'] == before['surface'], 'VALIDATION_FAILED',
            'Native qualification changed the original shader graph or failed to sign it')
    return verified
