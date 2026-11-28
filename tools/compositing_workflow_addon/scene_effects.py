"""Named operations on native Blender 5.3 scene compositor effects."""

import re
from collections.abc import Mapping
from pathlib import Path
from struct import pack, unpack

import bpy

from . import core
from .contract import M1_NODE_TYPES, WorkflowError
from .runtime import supported


ESSENTIALS_FILE = Path('assets/nodes/compositing_nodes_essentials.blend')
STAGE_TYPES = frozenset((*M1_NODE_TYPES, 'ShaderNodeGamma'))
CONTROL_TYPES = {
    'VALUE': 'NodeSocketFloat', 'INT': 'NodeSocketInt', 'BOOLEAN': 'NodeSocketBool',
    'VECTOR': 'NodeSocketVector', 'RGBA': 'NodeSocketColor',
}
PARAMETER_TYPES = {'FLOAT': (float, int), 'INT': (int,), 'BOOLEAN': (bool,), 'ENUM': (str,), 'STRING': (str,)}
FLOAT32_MAX = 3.4028234663852886e38
NESTED_EFFECT_PATH = re.compile(r'\.compositor_effects\[')


def _scene(scene_name):
    if tuple(bpy.app.version) < (5, 3, 0) or not supported(bpy.app):
        raise WorkflowError('UNSUPPORTED', 'Scene effects require Blender 5.3.0 or later')
    return core._scene(_name(scene_name))


def _name(value):
    if not isinstance(value, str) or not value.strip() or '\0' in value:
        raise WorkflowError('INVALID_REQUEST', 'Expected a nonempty name')
    return value


def _fields(value, *, required, optional=()):
    if not isinstance(value, Mapping) or not set(required) <= value.keys() <= (set(required) | set(optional)):
        raise WorkflowError('INVALID_REQUEST', 'Unexpected or missing request fields', {'required': list(required), 'optional': list(optional)})


def _effect(scene, name):
    effect = scene.compositor_effects.get(_name(name))
    if effect is None:
        raise WorkflowError('NOT_FOUND', 'Scene effect does not exist', {'scene': scene.name, 'effect': name})
    return effect


def _managed_effect(scene):
    state = core._state(scene)
    if state is None:
        return None
    effect = core._bound_effect(scene, state)
    if effect is None or effect.node_group is None or effect.node_group.name != state['root_group']:
        raise WorkflowError('CONFLICT', 'Resolve the existing managed compositor binding in this scene first')
    return effect


def _editable_effect(scene, name):
    effect = _effect(scene, name)
    if effect == _managed_effect(scene):
        raise WorkflowError('CONFLICT', 'Effect belongs to the existing managed compositor workflow', {'effect': name})
    return effect


def _ports(tree, direction):
    return [socket for socket in tree.interface.items_tree if socket.item_type == 'SOCKET' and socket.in_out == direction]


def _parameters(effect):
    tree = effect.node_group
    inputs = getattr(effect.properties, 'inputs', None)
    if tree is None or inputs is None:
        return
    # Blender binds the first input implicitly to the preceding image.
    for socket in _ports(tree, 'INPUT')[1:]:
        control = getattr(inputs, socket.identifier, None)
        value = control.bl_rna.properties.get('value') if control is not None else None
        yield socket, control, value


def _effect_row(effect):
    controls = {}
    for socket, control, value in _parameters(effect):
        row = {'name': socket.name, 'socket_type': socket.bl_socket_idname, 'type': value.type if value else None,
               'writable': value is not None and not value.is_readonly and value.type in PARAMETER_TYPES}
        if value is not None:
            row['value'] = core._plain(control.value)
        controls[socket.identifier] = row
    return {'name': effect.name, 'group': core._plain(effect.node_group),
            'preview': effect.enable_for_preview, 'render': effect.enable_for_render, 'parameters': controls}


def _inspect(scene):
    active = scene.compositor_effects.active
    stack = [_effect_row(effect) for effect in scene.compositor_effects]
    return {'scene': scene.name, 'enabled_globally': scene.render.use_compositing,
            'active': active.name if active else None, 'effects': stack}


def inspect_stack(scene_name):
    """Read names, order, active effect, flags and stable control identifiers."""
    return _inspect(_scene(scene_name))


def _result(operation, name, before, scene):
    return {'operation': operation, 'effect': name, 'before': before, 'after': _inspect(scene)}


def _local_group(name):
    tree = bpy.data.node_groups.get((_name(name), None))
    if tree is None or not isinstance(tree, bpy.types.CompositorNodeTree):
        raise WorkflowError('NOT_FOUND', 'Local compositor node group does not exist', {'group': name})
    if tree.override_library:
        raise WorkflowError('UNSUPPORTED', 'Select a local group without a library override', {'group': name})
    return tree


def _guard_tree(tree):
    managed_roots = set()
    for scene in bpy.data.scenes:
        if core.STATE in scene:
            state = core._state(scene)
            effect = core._bound_effect(scene, state)
            if effect is not None and effect.node_group is not None:
                managed_roots.add(effect.node_group.as_pointer())

    visited = set()
    pending = [tree]
    while pending:
        current = pending.pop()
        pointer = current.as_pointer()
        if pointer in visited:
            continue
        visited.add(pointer)
        if core.OWNER in current or pointer in managed_roots:
            raise WorkflowError('CONFLICT', 'Managed compositor groups cannot acquire additional consumers', {'group': current.name})
        for node in current.nodes:
            core.guard_output_node(node)
            nested = getattr(node, 'node_tree', None)
            if nested is not None:
                pending.append(nested)


def _validate_image_effect(tree):
    for direction in ('INPUT', 'OUTPUT'):
        ports = _ports(tree, direction)
        if not ports or ports[0].bl_socket_idname != 'NodeSocketColor':
            raise WorkflowError('UNSUPPORTED', 'Scene effects require first Image input and output sockets of Color type', {'group': tree.name, 'direction': direction})
    output_node = next((node for node in tree.nodes if node.bl_idname == 'NodeGroupOutput' and node.is_active_output), None)
    if output_node is None or not core._exact(output_node.inputs, _ports(tree, 'OUTPUT')[0].identifier).is_linked:
        raise WorkflowError('UNSUPPORTED', 'The first Color output must be connected in the active Group Output', {'group': tree.name})
    _guard_tree(tree)


def _essential_asset(name):
    datafiles = bpy.utils.system_resource('DATAFILES')
    path = Path(datafiles) / ESSENTIALS_FILE if datafiles else None
    if path is None or not path.is_file():
        raise WorkflowError('NOT_FOUND', 'Blender installation is missing its compositor Essentials library')
    with bpy.data.libraries.load(str(path), link=False, assets_only=True) as (available, requested):
        if name not in available.node_groups:
            raise WorkflowError('NOT_FOUND', 'No compositor asset with this exact name in Blender Essentials', {'asset': name})
        requested.node_groups = [name]
    tree = requested.node_groups[0]
    if tree is None or not tree.asset_data or not tree.allow_usage_in_scene_compositor_effect:
        raise WorkflowError('UNSUPPORTED', 'Blender Essentials asset is not a native scene effect', {'asset': name})
    return tree


def _position(effects, name, *, index, before, after, default):
    if sum(value is not None for value in (index, before, after)) > 1:
        raise WorkflowError('INVALID_REQUEST', 'Specify only one ordered position: index, before, or after')
    names = [effect.name for effect in effects if effect.name != name]
    if index is not None:
        if type(index) is not int or not 0 <= index <= len(names):
            raise WorkflowError('INVALID_REQUEST', 'Requested final index is outside the stack', {'index': index, 'maximum': len(names)})
        return index
    anchor = before if before is not None else after
    if anchor is None:
        return default
    anchor = _name(anchor)
    if anchor not in names:
        raise WorkflowError('NOT_FOUND', 'Position anchor does not resolve to another effect', {'anchor': anchor})
    return names.index(anchor) + (after is not None)


def _move(scene, name, index):
    if [effect.name for effect in scene.compositor_effects].index(name) == index:
        return
    with bpy.context.temp_override(scene=scene):
        result = bpy.ops.scene.move_compositor_effect_to_index(name=name, index=index)
    if 'FINISHED' not in result or scene.compositor_effects[index].name != name:
        raise WorkflowError('VALIDATION_FAILED', 'Blender did not move the requested effect', {'effect': name, 'index': index})


def _flags(preview, render):
    if any(value is not None and type(value) is not bool for value in (preview, render)):
        raise WorkflowError('INVALID_REQUEST', 'The preview and render switches must be booleans')


def _apply_flags(effect, preview, render):
    if preview is not None and preview != effect.enable_for_preview:
        effect.enable_for_preview = preview
    if render is not None and render != effect.enable_for_render:
        effect.enable_for_render = render


def _value(control, prop, value):
    actual = core._plain(control.value)
    expected_types = PARAMETER_TYPES[prop.type]
    values = value if isinstance(value, (list, tuple)) else None
    if isinstance(actual, list):
        valid = values is not None and len(actual) == len(values) and all(type(item) in expected_types for item in values)
        if valid and prop.type == 'FLOAT':
            valid = all(-FLOAT32_MAX <= item <= FLOAT32_MAX for item in values)
    else:
        valid = type(value) in expected_types and (prop.type != 'FLOAT' or -FLOAT32_MAX <= value <= FLOAT32_MAX)
    if isinstance(actual, dict) or (isinstance(actual, list) and any(isinstance(item, dict) for item in actual)):
        raise WorkflowError('UNSUPPORTED', 'Existing nonfinite control value cannot be safely rolled back')
    if not valid:
        raise WorkflowError('INVALID_REQUEST', 'Control value has the wrong type or size', {'type': prop.type, 'value': actual})
    return actual


def _matches(actual, requested):
    if isinstance(actual, list):
        return len(actual) == len(requested) and all(_matches(a, b) for a, b in zip(actual, requested))
    if isinstance(actual, float):
        # Compare in its native precision so a genuine clamp cannot appear equal.
        return actual == unpack('<f', pack('<f', requested))[0]
    return actual == requested


def _patch_parameters(effect, parameters):
    if parameters is None:
        return
    if not isinstance(parameters, Mapping):
        raise WorkflowError('INVALID_REQUEST', 'Parameters must map socket identifiers to values')
    controls = {socket.identifier: (control, prop) for socket, control, prop in _parameters(effect)}
    edits = []
    for identifier, value in parameters.items():
        if identifier not in controls:
            raise WorkflowError('NOT_FOUND', 'No exposed control with this socket identifier', {'socket': identifier})
        control, prop = controls[identifier]
        if prop is None or prop.is_readonly or prop.type not in PARAMETER_TYPES:
            raise WorkflowError('UNSUPPORTED', 'Control does not expose an editable scalar or vector value', {'socket': identifier})
        old = _value(control, prop, value)
        if not _matches(old, value):
            edits.append((identifier, control, value, old))
    written = []
    try:
        for identifier, control, value, old in edits:
            written.append((control, old))
            control.value = value
            if not _matches(core._plain(control.value), value):
                raise WorkflowError('INVALID_REQUEST', 'Blender changed or clamped the requested control value', {'socket': identifier})
    except Exception as exc:
        for control, old in reversed(written):
            if not _matches(core._plain(control.value), old):
                control.value = old
        if isinstance(exc, WorkflowError):
            raise
        raise WorkflowError('INVALID_REQUEST', 'Could not assign the requested control value', {'socket': identifier, 'error': str(exc)}) from exc


def _animation_paths(scene):
    data = scene.animation_data
    if not data:
        return set()
    paths = {curve.data_path for curve in data.drivers}
    slots = [(data.action, data.action_slot_handle)]
    strips = [strip for track in data.nla_tracks for strip in track.strips]
    while strips:
        strip = strips.pop()
        slots.append((strip.action, strip.action_slot_handle))
        strips.extend(strip.strips)
    for action, handle in slots:
        if action is None:
            continue
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    if bag.slot_handle == handle:
                        paths.update(curve.data_path for curve in bag.fcurves)
    return paths


def _removal_affects_path(path, effect_path, scene, shifted_effects):
    if path == effect_path or path.startswith((effect_path + '.', effect_path + '[')):
        return True
    if not path.startswith('compositor_effects[') or path.startswith('compositor_effects["'):
        return False
    selector, bracket, suffix = path.partition(']')
    if not bracket or (suffix and not suffix.startswith(('.', '['))):
        return False
    try:
        indexed_effect = scene.path_resolve(selector + bracket)
    except ValueError:
        return False
    return indexed_effect.name in shifted_effects


def _scene_reference_path(root, path, scene):
    if root is None:
        return None
    if root == scene and path.startswith('compositor_effects['):
        return path
    for segment in NESTED_EFFECT_PATH.finditer(path):
        try:
            resolved = root.path_resolve(path[:segment.start()])
        except (ValueError, TypeError):
            continue
        if resolved == scene:
            return path[segment.start() + 1:]
    return None


def _driver_references(scene, affected):
    references = []
    for owner in core._id_owners():
        animation = getattr(owner, 'animation_data', None)
        if animation is None:
            continue
        for curve in animation.drivers:
            for variable in curve.driver.variables:
                if variable.type not in ('SINGLE_PROP', 'CONTEXT_PROP'):
                    continue
                target = variable.targets[0]
                if variable.type == 'SINGLE_PROP':
                    path = _scene_reference_path(target.id, target.data_path, scene)
                else:
                    path = target.data_path if target.context_property == 'ACTIVE_SCENE' else None
                if path is not None and affected(path):
                    references.append({'owner': core._plain(owner), 'driver_path': curve.data_path,
                                       'array_index': curve.array_index, 'variable': variable.name,
                                       'target_path': target.data_path, 'variable_type': variable.type})
    return sorted(references, key=core._dump)


def _guard_animation(scene, effect, *, remove=False, parameters=None, preview=None, render=None):
    effect_path = effect.path_from_id()
    paths = _animation_paths(scene)
    if remove:
        index = scene.compositor_effects.find(effect.name)
        shifted_effects = {indexed.name for indexed in list(scene.compositor_effects)[index:]}

        def affected(path):
            return _removal_affects_path(path, effect_path, scene, shifted_effects)

        protected = sorted(path for path in paths if affected(path))
        references = _driver_references(scene, affected)
        if protected or references:
            raise WorkflowError('CONFLICT', 'Removing the effect would invalidate or rebind animation paths',
                                {'paths': protected, 'driver_references': references})
        return

    edited = {f'{effect_path}.properties.inputs.{identifier}.value' for identifier in (parameters or ())}
    if preview is not None:
        edited.add(f'{effect_path}.enable_for_preview')
    if render is not None:
        edited.add(f'{effect_path}.enable_for_render')
    protected = sorted(paths & edited)
    if protected:
        raise WorkflowError('CONFLICT', 'Effect has animated or driven properties targeted by this operation', {'paths': protected})


def add_effect(scene_name, name, *, source, index=None, before=None, after=None, parameters=None, preview=None, render=None):
    """Attach a named local group or a fresh native Essentials asset."""
    scene = _scene(scene_name)
    effects = scene.compositor_effects
    name = _name(name)
    if len(name.encode('utf-8')) > 63:
        raise WorkflowError('INVALID_REQUEST', 'Effect name exceeds Blender’s native 63-byte limit')
    if effects.get(name) is not None:
        raise WorkflowError('CONFLICT', 'An effect already has this exact name', {'effect': name})
    _managed_effect(scene)
    _flags(preview, render)
    _fields(source, required=('kind', 'name'))
    source_name = _name(source['name'])
    kind = source['kind']
    if kind not in ('LOCAL', 'ESSENTIALS'):
        raise WorkflowError('INVALID_REQUEST', 'Effect source kind must be LOCAL or ESSENTIALS')
    position = _position(effects, name, index=index, before=before, after=after, default=len(effects))
    snapshot = _inspect(scene)
    previous_active = effects.active
    imported_before = {tree.as_pointer() for tree in bpy.data.node_groups} if kind == 'ESSENTIALS' else set()
    effect = None
    try:
        tree = _local_group(source_name) if kind == 'LOCAL' else _essential_asset(source_name)
        _validate_image_effect(tree)
        effect = effects.new(name)
        if effect.name != name:
            raise WorkflowError('VALIDATION_FAILED', 'Blender changed the requested effect name', {'requested': name, 'actual': effect.name})
        effect.node_group = tree
        if effect.node_group != tree:
            raise WorkflowError('VALIDATION_FAILED', 'Could not attach the chosen compositor group', {'group': tree.name})
        _patch_parameters(effect, parameters)
        _apply_flags(effect, preview, render)
        _move(scene, name, position)
        if previous_active is not None:
            effects.active = previous_active
        return _result('add', name, snapshot, scene)
    except Exception:
        if effect is not None:
            effects.remove(effect)
            if previous_active is not None and effects.active != previous_active:
                effects.active = previous_active
        if kind == 'ESSENTIALS':
            # A fresh append owns exactly the node trees absent when this operation started.
            imported = [tree for tree in bpy.data.node_groups if tree.as_pointer() not in imported_before]
            bpy.data.batch_remove(ids=imported)
        raise


def move_effect(scene_name, name, *, index=None, before=None, after=None):
    scene = _scene(scene_name)
    _editable_effect(scene, name)
    if all(value is None for value in (index, before, after)):
        raise WorkflowError('INVALID_REQUEST', 'A new position is required')
    position = _position(scene.compositor_effects, name, index=index, before=before, after=after, default=None)
    snapshot = _inspect(scene)
    _move(scene, name, position)
    return _result('move', name, snapshot, scene)


def update_effect(scene_name, name, *, parameters=None, preview=None, render=None):
    scene = _scene(scene_name)
    effect = _editable_effect(scene, name)
    _flags(preview, render)
    if parameters is not None and not isinstance(parameters, Mapping):
        raise WorkflowError('INVALID_REQUEST', 'Parameters must map socket identifiers to values')
    _guard_animation(scene, effect, parameters=parameters, preview=preview, render=render)
    snapshot = _inspect(scene)
    _patch_parameters(effect, parameters)
    _apply_flags(effect, preview, render)
    return _result('update', name, snapshot, scene)


def bypass_effect(scene_name, name):
    """Disable both paths and return the prior flags for explicit restoration."""
    result = update_effect(scene_name, name, preview=False, render=False)
    result['operation'] = 'bypass'
    return result


def remove_effect(scene_name, name):
    scene = _scene(scene_name)
    effect = _editable_effect(scene, name)
    _guard_animation(scene, effect, remove=True)
    snapshot = _inspect(scene)
    scene.compositor_effects.remove(effect)
    return _result('remove', name, snapshot, scene)


def _stage(tree, document):
    _fields(document, required=('id',), optional=('type', 'group', 'image_input', 'image_output', 'properties', 'inputs'))
    identifier = _name(document['id'])
    if ('type' in document) == ('group' in document):
        raise WorkflowError('INVALID_REQUEST', 'Each stage needs one node type or local group', {'stage': identifier})
    if 'group' in document:
        source = _local_group(document['group'])
        _validate_image_effect(source)
        node = tree.nodes.new('CompositorNodeGroup')
        node.node_tree = source
        default_input = _ports(source, 'INPUT')[0].identifier
        default_output = _ports(source, 'OUTPUT')[0].identifier
    else:
        node_type = _name(document['type'])
        if node_type not in STAGE_TYPES:
            raise WorkflowError('UNSUPPORTED', 'Node is outside the reusable scene effect stage types', {'type': node_type})
        node = tree.nodes.new(node_type)
        default_input = default_output = 'Image'
    node.name = identifier
    core._patch(node, document)
    image_in = core._exact(node.inputs, _name(document.get('image_input', default_input)))
    image_out = core._exact(node.outputs, _name(document.get('image_output', default_output)))
    if not all(socket.enabled and socket.type == 'RGBA' for socket in (image_in, image_out)):
        raise WorkflowError('UNSUPPORTED', 'Each stage needs enabled Color input and output', {'stage': identifier})
    return node, image_in, image_out


def _expose_control(tree, group_input, spec, stages, occupied, labels):
    _fields(spec, required=('name', 'stage', 'socket'), optional=('default',))
    name = _name(spec['name'])
    if name in labels:
        raise WorkflowError('CONFLICT', 'Exposed control names must be unique inside a new group', {'control': name})
    node = stages.get(_name(spec['stage']))
    if node is None:
        raise WorkflowError('NOT_FOUND', 'Exposed control stage does not exist', {'stage': spec['stage']})
    target = core._exact(node.inputs, _name(spec['socket']))
    path = (node.name, target.identifier)
    if path in occupied or target.is_linked or not target.enabled or target.type not in CONTROL_TYPES:
        raise WorkflowError('UNSUPPORTED', 'Socket is unavailable, already linked, or not supported as a control', {'stage': node.name, 'socket': target.identifier})
    if 'default' in spec:
        core._set_socket(target, spec['default'])
    control = tree.interface.new_socket(name=name, in_out='INPUT', socket_type=CONTROL_TYPES[target.type])
    control.default_value = core._plain(target.default_value)
    tree.links.new(core._exact(group_input.outputs, control.identifier), target)
    occupied.add(path)
    labels.add(name)
    return {'name': name, 'identifier': control.identifier, 'default': core._plain(control.default_value)}


def create_reusable_group(scene_name, name, *, stages, controls=()):
    """Build an editable Color chain from supported compositor nodes or local groups."""
    _scene(scene_name)
    name = _name(name)
    if bpy.data.node_groups.get((name, None)) is not None:
        raise WorkflowError('CONFLICT', 'A local node group already has this exact name', {'group': name})
    if not isinstance(stages, (list, tuple)) or not stages or not isinstance(controls, (list, tuple)):
        raise WorkflowError('INVALID_REQUEST', 'Supply one or more stages and a list of exposed controls')
    tree = bpy.data.node_groups.new(name, 'CompositorNodeTree')
    try:
        if tree.name != name:
            raise WorkflowError('VALIDATION_FAILED', 'Blender changed the requested group name', {'requested': name, 'actual': tree.name})
        image_input = tree.interface.new_socket(name='Image', in_out='INPUT', socket_type='NodeSocketColor')
        image_output = tree.interface.new_socket(name='Image', in_out='OUTPUT', socket_type='NodeSocketColor')
        incoming = tree.nodes.new('NodeGroupInput')
        outgoing = tree.nodes.new('NodeGroupOutput')
        incoming.location = (-240, 0)
        outgoing.location = (len(stages) * 240, 0)
        previous = core._exact(incoming.outputs, image_input.identifier)
        nodes = {}
        occupied = set()
        for position, document in enumerate(stages):
            node, stage_input, stage_output = _stage(tree, document)
            if node.name != document['id'] or node.name in nodes:
                raise WorkflowError('CONFLICT', 'Stage names must be unique and match their native node names', {'stage': document['id']})
            nodes[node.name] = node
            node.location = (position * 240, 0)
            tree.links.new(previous, stage_input)
            occupied.add((node.name, stage_input.identifier))
            previous = stage_output
        tree.links.new(previous, core._exact(outgoing.inputs, image_output.identifier))
        labels = {'Image'}
        exposed = [_expose_control(tree, incoming, spec, nodes, occupied, labels) for spec in controls]
        _validate_image_effect(tree)
        tree.allow_usage_in_scene_compositor_effect = True
        tree.asset_mark()
        return {'group': tree.name, 'asset': bool(tree.asset_data), 'controls': exposed}
    except Exception:
        bpy.data.node_groups.remove(tree)
        raise


def dispatch(scene_name, request):
    """Execute one native scene-effect operation from a JSON-compatible request."""
    position = ('index', 'before', 'after')
    values = ('parameters', 'preview', 'render')
    operations = {
        'inspect': (inspect_stack, (), ()),
        'add': (add_effect, ('name', 'source'), position + values),
        'move': (move_effect, ('name',), position),
        'update': (update_effect, ('name',), values),
        'bypass': (bypass_effect, ('name',), ()),
        'remove': (remove_effect, ('name',), ()),
        'create_group': (create_reusable_group, ('name', 'stages'), ('controls',)),
    }
    operation = request.get('operation') if isinstance(request, Mapping) else None
    if not isinstance(operation, str) or operation not in operations:
        raise WorkflowError('INVALID_REQUEST', 'Unknown scene effect operation', {'supported': list(operations)})
    method, required, optional = operations[operation]
    _fields(request, required=('operation', *required), optional=optional)
    return method(scene_name, **{key: value for key, value in request.items() if key != 'operation'})
