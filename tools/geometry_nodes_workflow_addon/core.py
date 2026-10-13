"""Geometry graph ownership and isolated target commits; caller owns persistence."""
import copy
import hashlib
import json
import math
import uuid
import bpy
from .contract import WorkflowError, normalize_blueprint

KEY = 'gnw_id'
STATE = 'gnw_state'
ROLE = 'gnw_role'
NODE_ID = 'gnw_node_id'
CORE_NODE_ID = 'gnw_core'
INTERFACE = 'gnw_interface'
BASELINE = 'gnw_baseline'
GENERATED = 'gnw_generated_state'


def plain(value):
    if isinstance(value, bpy.types.ID):
        kind = {'Object': 'OBJECT', 'Collection': 'COLLECTION', 'Material': 'MATERIAL', 'Image': 'IMAGE', 'GeometryNodeTree': 'GROUP'}.get(value.bl_rna.identifier)
        if kind: return {'kind': kind, 'name': value.name}
        return {'id_type': value.bl_rna.identifier, 'name': value.name,
                'library': value.library.filepath if value.library else None}
    if value is None or type(value) in (str, bool, int): return value
    if type(value) is float:
        if not math.isfinite(value): raise WorkflowError('VALIDATION_FAILED', 'Non-finite RNA value')
        return value
    if isinstance(value, bpy.types.bpy_struct):
        return {'rna_type': value.bl_rna.identifier,
                'fields': {p.identifier: plain(getattr(value, p.identifier)) for p in value.bl_rna.properties
                           if p.identifier not in ('rna_type', 'id_data') and p.type in ('BOOLEAN', 'INT', 'FLOAT', 'ENUM', 'STRING')}}
    try: return [plain(v) for v in value]
    except TypeError: return str(value)


def _dump(value): return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _local(tree, allow_dynamic=False):
    if tree.library or tree.override_library:
        raise WorkflowError('UNSUPPORTED', 'Linked/override node group is protected: ' + tree.name)
    if tree.bl_idname != 'GeometryNodeTree': raise WorkflowError('INVALID_REQUEST', 'GeometryNodeTree required')
    if tree.animation_data:
        raise WorkflowError('UNSUPPORTED', 'Animated/driven node groups require the later animation workflow')
    if not allow_dynamic and any(any(term in node.bl_idname for term in ('Simulation', 'Bake')) for node in tree.nodes):
        raise WorkflowError('UNSUPPORTED', 'Simulation/Bake groups are a later phase')


def socket_find(sockets, selector):
    matches = [s for s in sockets if s.identifier == selector]
    if not matches: matches = [s for s in sockets if s.name == selector]
    if len(matches) != 1: raise WorkflowError('CONFLICT' if matches else 'NOT_FOUND', 'Socket must resolve uniquely: ' + selector)
    socket = matches[0]
    if socket.is_unavailable or not socket.enabled: raise WorkflowError('UNSUPPORTED', 'Socket unavailable: ' + selector)
    return socket


def _set(owner, key, value):
    prop = owner.bl_rna.properties.get(key)
    if prop is None or prop.is_readonly or prop.type in ('COLLECTION', 'POINTER'):
        raise WorkflowError('INVALID_REQUEST', 'Writable scalar/vector RNA property required: ' + key)
    if getattr(prop, 'is_array', False):
        if not isinstance(value, (list, tuple)) or len(value) != prop.array_length:
            raise WorkflowError('INVALID_REQUEST', 'Wrong RNA array length: ' + key)
        values = value
    else: values = [value]
    for val in values:
        if prop.type == 'BOOLEAN' and type(val) is not bool: raise WorkflowError('INVALID_REQUEST', 'Boolean required: ' + key)
        if prop.type == 'INT' and type(val) is not int: raise WorkflowError('INVALID_REQUEST', 'Integer required: ' + key)
        if prop.type == 'FLOAT' and type(val) not in (int, float): raise WorkflowError('INVALID_REQUEST', 'Number required: ' + key)
        if prop.type in ('FLOAT', 'INT') and (not math.isfinite(val) or not prop.hard_min <= val <= prop.hard_max):
            raise WorkflowError('INVALID_REQUEST', 'RNA hard range exceeded: ' + key)
        if prop.type == 'ENUM' and val not in {i.identifier for i in prop.enum_items}:
            raise WorkflowError('INVALID_REQUEST', 'Unknown RNA enum: ' + key + '=' + str(val))
        if prop.type == 'STRING' and not isinstance(val, str): raise WorkflowError('INVALID_REQUEST', 'String required: ' + key)
    try: setattr(owner, key, value)
    except (TypeError, ValueError, RuntimeError) as exc: raise WorkflowError('INVALID_REQUEST', key + ': ' + str(exc)) from exc


def _socket_value(socket, value):
    prop = socket.bl_rna.properties.get('default_value')
    if prop and prop.type == 'POINTER':
        if value is not None and (not isinstance(value, dict) or set(value) != {'kind', 'name'}):
            raise WorkflowError('INVALID_REQUEST', 'Pointer input requires explicit kind/name')
        setattr(socket, 'default_value', _resource(value) if value is not None else None)
    else: _set(socket, 'default_value', value)


def _order_links(links):
    """Preserve declared top-to-bottom order using native read-only IDs/swaps."""
    slots = sorted((l.multi_input_sort_id for l in links), reverse=True)
    for link, sort_id in zip(links, slots):
        if link.multi_input_sort_id == sort_id: continue
        other = next(l for l in links if l.multi_input_sort_id == sort_id)
        link.swap_multi_input_sort_id(other)


def _resource(spec, groups=None):
    if spec['kind'] == 'GROUP' and groups and spec['name'] in groups: return groups[spec['name']]
    collection = {'OBJECT': bpy.data.objects, 'COLLECTION': bpy.data.collections, 'MATERIAL': bpy.data.materials,
                  'IMAGE': bpy.data.images, 'GROUP': bpy.data.node_groups}[spec['kind']]
    item = collection.get(spec['name'])
    if item is None: raise WorkflowError('NOT_FOUND', 'Missing resource: ' + spec['name'])
    if item.library or item.override_library: raise WorkflowError('UNSUPPORTED', 'Linked/override resource is not supported')
    return item


def _interface(tree, specs):
    mapping = {}
    for spec in specs:
        socket = tree.interface.new_socket(name=spec['name'], in_out=spec['direction'], socket_type=spec['type'])
        if socket.bl_socket_idname != spec['type']:
            # Subtype types may be normalized by Blender; contract records the real type.
            if socket.socket_type != spec['type']: raise WorkflowError('UNSUPPORTED', 'Blender normalized socket type: ' + spec['type'])
        if 'default' in spec: _socket_value(socket, spec['default'])
        if 'min' in spec: _set(socket, 'min_value', spec['min'])
        if 'max' in spec: _set(socket, 'max_value', spec['max'])
        mapping[spec['key']] = {'identifier': socket.identifier, **copy.deepcopy(spec), 'type': socket.bl_socket_idname}
    tree[INTERFACE] = _dump(mapping)
    return mapping


def interface_map(tree):
    result = json.loads(tree[INTERFACE]) if INTERFACE in tree else {}
    actual = {s.identifier: s for s in tree.interface.items_tree if s.item_type == 'SOCKET'}
    for key, row in result.items():
        socket = actual.get(row['identifier'])
        if socket is None or socket.in_out != row['direction'] or socket.bl_socket_idname != row['type']:
            raise WorkflowError('CONFLICT', 'Managed public interface manually changed or removed: ' + key)
    mapped = {r['identifier'] for r in result.values()}
    for socket in tree.interface.items_tree:
        if socket.item_type != 'SOCKET': continue
        if socket.identifier in mapped: continue
        key = ('user:' if INTERFACE in tree else '') + socket.identifier
        spec = {'key': key, 'identifier': socket.identifier, 'name': socket.name,
                'direction': socket.in_out, 'type': socket.bl_socket_idname, 'semantic': '', 'unit': ''}
        if hasattr(socket, 'default_value'): spec['default'] = plain(socket.default_value)
        result[key] = spec
    return result


def _tree_state(tree, trail=()):
    if tree.as_pointer() in trail: raise WorkflowError('CONFLICT', 'Recursive group graph')
    trail = (*trail, tree.as_pointer())
    rows = []
    for node in tree.nodes:
        props = {}
        if node.bl_idname == 'GeometryNodeSimulationInput':
            paired = node.paired_output
            props['paired_output'] = paired.get(NODE_ID, paired.name) if paired else None
        for prop in node.bl_rna.properties:
            key = prop.identifier
            if prop.is_readonly or key in ('rna_type', 'name', 'location', 'location_absolute', 'width', 'width_hidden', 'height', 'dimensions', 'select', 'parent', 'color', 'use_custom_color', 'label', 'hide', 'show_options', 'show_preview', 'show_texture'):
                continue
            if prop.type == 'COLLECTION':
                # Dynamic zone items are semantic, unlike visual node collections.
                if key.endswith('_items'):
                    props[key] = [{p.identifier: plain(getattr(v, p.identifier)) for p in v.bl_rna.properties
                                   if p.identifier != 'rna_type' and p.type not in ('COLLECTION', 'POINTER')} for v in getattr(node, key)]
                continue
            if key == 'node_tree' and node.node_tree: props[key] = _tree_state(node.node_tree, trail)
            elif prop.type in ('BOOLEAN', 'INT', 'FLOAT', 'ENUM', 'STRING', 'POINTER'):
                props[key] = plain(getattr(node, key))
        rows.append({'id': node.get(NODE_ID, node.name), 'name': node.name, 'type': node.bl_idname,
                     'properties': props, 'inputs': [{'identifier': s.identifier, 'name': s.name,
                                                       'type': s.bl_idname, 'available': not s.is_unavailable,
                                                       'default': plain(s.default_value) if hasattr(s, 'default_value') else None} for s in node.inputs],
                     'outputs': [{'identifier': s.identifier, 'name': s.name, 'type': s.bl_idname,
                                  'default': plain(s.default_value) if hasattr(s, 'default_value') else None} for s in node.outputs],
                     'custom_properties': {k: plain(v) for k, v in node.items() if not k.startswith('gnw_')}})
    links = [{'from': [l.from_node.get(NODE_ID, l.from_node.name), l.from_socket.identifier],
              'to': [l.to_node.get(NODE_ID, l.to_node.name), l.to_socket.identifier],
              'order': l.multi_input_sort_id, 'muted': l.is_muted} for l in tree.links]
    interfaces = []
    for item in tree.interface.items_tree:
        interfaces.append({p.identifier: plain(getattr(item, p.identifier)) for p in item.bl_rna.properties
                           if p.identifier not in ('rna_type', 'select') and p.type not in ('COLLECTION', 'POINTER')})
    return {'nodes': sorted(rows, key=lambda n: n['id']), 'links': sorted(links, key=_dump), 'interface': interfaces}


def tree_fingerprint(tree): return hashlib.sha256(_dump(_tree_state(tree)).encode()).hexdigest()


def copy_tree_isolated(tree, allow_dynamic=False):
    """Recursively copy the requested core graph; no original group is changed."""
    clones = {}
    def clone(source, trail=()):
        _local(source, allow_dynamic=allow_dynamic)
        if source.as_pointer() in trail: raise WorkflowError('CONFLICT', 'Recursive core group')
        if source.as_pointer() in clones: return clones[source.as_pointer()]
        result = source.copy(); clones[source.as_pointer()] = result
        for node in result.nodes:
            if getattr(node, 'node_tree', None): node.node_tree = clone(node.node_tree, (*trail, source.as_pointer()))
        return result
    return clone(tree)


def build_graph(blueprint, name=None):
    blueprint = normalize_blueprint(blueprint)
    existing = set(bpy.data.node_groups)
    groups = {}; active = set()
    def build(spec, requested=None):
        if spec['id'] in active: raise WorkflowError('CONFLICT', 'Recursive blueprint group')
        if spec['id'] in groups: raise WorkflowError('CONFLICT', 'Duplicate blueprint group id')
        active.add(spec['id'])
        nested = {}
        for child in spec.get('groups', []): nested[child['id']] = build(child)
        tree = bpy.data.node_groups.new(requested or 'GNW_Core_' + spec['id'], 'GeometryNodeTree')
        tree[ROLE] = 'core'; tree[KEY] = spec['id']; groups[spec['id']] = tree
        _interface(tree, spec['interface'])
        nodes = {}
        for row in spec['nodes']:
            if any(s in row['type'] for s in ('Simulation', 'Bake')) and row['type'] not in (
                    'GeometryNodeSimulationInput', 'GeometryNodeSimulationOutput', 'GeometryNodeBake'):
                raise WorkflowError('UNSUPPORTED', 'Simulation/Bake is a later phase')
            # Built-in node RNA lookup via Node.bl_rna_get_subclass_py returns
            # None on this 5.2 runtime, including MeshCube/GroupInput. The
            # registered bpy.types class is present and nodes.new polls it.
            cls = getattr(bpy.types, row['type'], None)
            if cls is None or not isinstance(cls, type) or not issubclass(cls, bpy.types.Node) or cls.__module__ != 'bpy.types' or getattr(cls.bl_rna, 'is_runtime', False):
                raise WorkflowError('UNSUPPORTED', 'Unknown/non-native node type: ' + row['type'])
            try: node = tree.nodes.new(row['type'])
            except RuntimeError as exc: raise WorkflowError('UNSUPPORTED', str(exc)) from exc
            node[NODE_ID] = row['id']; node.name = row['id']; nodes[row['id']] = node
            for key, value in row.get('properties', {}).items(): _set(node, key, value)
            for key, resource in row.get('resources', {}).items():
                prop = node.bl_rna.properties.get(key)
                if prop is None or prop.type != 'POINTER' or prop.is_readonly:
                    raise WorkflowError('INVALID_REQUEST', 'Writable RNA pointer required: ' + key)
                value = _resource(resource, {**groups, **nested})
                if key == 'node_tree':
                    _local(value, allow_dynamic=value in groups.values() or value in nested.values())
                    if value == tree or value.contains_tree(tree): raise WorkflowError('CONFLICT', 'Recursive node group')
                    # Explicit local external group references are isolated as part of core.
                    if value not in groups.values(): value = copy_tree_isolated(value)
                try: setattr(node, key, value)
                except TypeError as exc: raise WorkflowError('INVALID_REQUEST', str(exc)) from exc
            if 'location' in row: node.location = row['location']
            if 'label' in row: node.label = row['label']
        for zone in spec.get('repeat_zones', []):
            inp, out = nodes[zone['input']], nodes[zone['output']]
            if inp.bl_idname != 'GeometryNodeRepeatInput' or out.bl_idname != 'GeometryNodeRepeatOutput':
                raise WorkflowError('INVALID_REQUEST', 'Repeat zone requires RepeatInput/Output')
            out.repeat_items.clear()
            for item in zone['items']:
                try: out.repeat_items.new(item['type'], item['name'])
                except (TypeError, RuntimeError) as exc: raise WorkflowError('INVALID_REQUEST', str(exc)) from exc
            inp.pair_with_output(out)
        for zone in spec.get('simulation_zones', []):
            inp, out = nodes[zone['input']], nodes[zone['output']]
            out.state_items.clear()
            for spec_item in zone['items']:
                try: item = out.state_items.new(spec_item['type'], spec_item['name'])
                except (TypeError, RuntimeError) as exc: raise WorkflowError('INVALID_REQUEST', str(exc)) from exc
                if 'attribute_domain' in spec_item: _set(item, 'attribute_domain', spec_item['attribute_domain'])
            paired = inp.pair_with_output(out)
            if paired is False or inp.paired_output != out:
                raise WorkflowError('VALIDATION_FAILED', 'Simulation pairing failed')
        for bake in spec.get('bake_nodes', []):
            node = nodes[bake['node']]
            node.bake_items.clear()
            for spec_item in bake['items']:
                try: item = node.bake_items.new(spec_item['type'], spec_item['name'])
                except (TypeError, RuntimeError) as exc: raise WorkflowError('INVALID_REQUEST', str(exc)) from exc
                for key in ('attribute_domain', 'is_attribute'):
                    if key in spec_item: _set(item, key, spec_item[key])
        for row in spec['nodes']:
            for selector, value in row.get('inputs', {}).items(): _socket_value(socket_find(nodes[row['id']].inputs, selector), value)
        ordered_links = {}
        for link in spec['links']:
            output = socket_find(nodes[link['from'][0]].outputs, link['from'][1])
            inp = socket_find(nodes[link['to'][0]].inputs, link['to'][1])
            if inp.is_linked and not inp.is_multi_input:
                raise WorkflowError('CONFLICT', 'Multiple links to single input: ' + str(link['to']))
            created = tree.links.new(output, inp, handle_dynamic_sockets=True)
            if inp.is_multi_input: ordered_links.setdefault(inp.as_pointer(), []).append(created)
        for links in ordered_links.values(): _order_links(links)
        tree.update_tag(); active.remove(spec['id'])
        tree[BASELINE] = tree_fingerprint(tree)
        tree[GENERATED] = _dump(_tree_state(tree))
        return tree
    try: return build(blueprint, name)
    except Exception:
        for tree in list(bpy.data.node_groups):
            if tree not in existing: bpy.data.node_groups.remove(tree, do_unlink=True)
        raise


def _core_node(wrapper):
    matches = [n for n in wrapper.nodes if n.get(NODE_ID) == CORE_NODE_ID]
    if len(matches) != 1 or not getattr(matches[0], 'node_tree', None):
        raise WorkflowError('CONFLICT', 'Managed core node must resolve uniquely')
    return matches[0]


def _typed(modifier, identifier):
    try: return getattr(modifier.properties.inputs, identifier)
    except AttributeError:
        try: return modifier.properties.inputs[identifier]
        except (KeyError, TypeError) as exc: raise WorkflowError('NOT_FOUND', 'Modifier parameter missing: ' + identifier) from exc


def parameter_values(modifier):
    if not modifier.node_group: return {}
    result = {}
    for key, row in interface_map(modifier.node_group).items():
        if row['direction'] != 'INPUT' or row['type'] == 'NodeSocketGeometry': continue
        value = _typed(modifier, row['identifier'])
        if hasattr(value, 'value'): result[key] = plain(value.value)
        else: result[key] = plain(value.get('value'))
    return result


def _parameter_state(modifier):
    result = {}
    for key, row in interface_map(modifier.node_group).items():
        if row['direction'] != 'INPUT' or row['type'] == 'NodeSocketGeometry': continue
        item = _typed(modifier, row['identifier'])
        result[key] = {p.identifier: plain(getattr(item, p.identifier)) for p in item.bl_rna.properties
                       if p.identifier not in ('rna_type', 'name') and not p.is_readonly and p.type != 'COLLECTION'}
    return result


def _restore_parameter_state(modifier, state):
    rows = interface_map(modifier.node_group)
    for key, values in state.items():
        item = _typed(modifier, rows[key]['identifier'])
        for field, value in values.items():
            prop = item.bl_rna.properties.get(field)
            if prop and prop.type == 'POINTER': setattr(item, field, _resource(value) if value else None)
            else: _set(item, field, value)


def _write_parameter(modifier, key, value):
    rows = interface_map(modifier.node_group)
    if key not in rows or rows[key]['direction'] != 'INPUT' or rows[key]['type'] == 'NodeSocketGeometry':
        raise WorkflowError('INVALID_REQUEST', 'Unknown writable public parameter: ' + key)
    row = rows[key]
    if type(value) in (int, float) and not row.get('min', -math.inf) <= value <= row.get('max', math.inf):
        raise WorkflowError('INVALID_REQUEST', 'Public parameter outside declared range: ' + key)
    item = _typed(modifier, row['identifier'])
    mode = getattr(item, 'type', None)
    if mode is not None and mode != 'VALUE': raise WorkflowError('UNSUPPORTED', 'Attribute-bound input requires explicit field workflow: ' + key)
    if isinstance(value, dict): value = _resource(value)
    if isinstance(value, bpy.types.Object) and value == modifier.id_data:
        raise WorkflowError('CONFLICT', 'Public object input cannot reference its own target')
    if hasattr(item, 'value'):
        prop = item.bl_rna.properties.get('value')
        if prop and prop.type == 'POINTER': item.value = value
        else: _set(item, 'value', value)
    else:
        item['value'] = value


def _restore_values(modifier, values):
    for key, value in values.items():
        if isinstance(value, dict) and 'id_type' in value:
            kind = {'Object': 'OBJECT', 'Collection': 'COLLECTION', 'Material': 'MATERIAL', 'Image': 'IMAGE'}.get(value['id_type'])
            if not kind: raise WorkflowError('UNSUPPORTED', 'Unsupported public pointer parameter')
            value = {'kind': kind, 'name': value['name']}
        _write_parameter(modifier, key, value)


def _compatibility(old, new):
    conflicts = []
    for key, row in old.items():
        if key not in new: conflicts.append({'key': key, 'reason': 'removed'})
        elif any(row.get(k, '') != new[key].get(k, '') for k in ('direction', 'type', 'unit', 'semantic')):
            conflicts.append({'key': key, 'reason': 'type_direction_unit_or_semantic_changed'})
    if conflicts:
        error = WorkflowError('CONFLICT', 'Public interface incompatible; keep previous state and migrate explicitly: ' + _dump(conflicts))
        error.migration = conflicts
        raise error


def _wrapper(core, identity):
    wrapper = bpy.data.node_groups.new('GNW_Wrapper_' + identity, 'GeometryNodeTree')
    wrapper[ROLE] = 'wrapper'; wrapper[KEY] = identity
    rows = interface_map(core); _interface(wrapper, [{k: v for k, v in row.items() if k != 'identifier'} for row in rows.values()])
    inp = wrapper.nodes.new('NodeGroupInput'); inp.name = 'Public Input'; inp[NODE_ID] = 'gnw_public_input'
    out = wrapper.nodes.new('NodeGroupOutput'); out.name = 'Public Output'; out[NODE_ID] = 'gnw_public_output'
    node = wrapper.nodes.new('GeometryNodeGroup'); node.node_tree = core; node[NODE_ID] = CORE_NODE_ID; node.name = 'Plugin Core'
    inp.location = (-250, 0); out.location = (250, 0)
    for key, row in rows.items():
        outer = interface_map(wrapper)[key]
        if row['direction'] == 'INPUT': wrapper.links.new(socket_find(inp.outputs, outer['identifier']), socket_find(node.inputs, row['identifier']))
        else: wrapper.links.new(socket_find(node.outputs, row['identifier']), socket_find(out.inputs, outer['identifier']))
    return wrapper


def _replace_core(wrapper, newcore):
    oldnode = _core_node(wrapper); oldrows = interface_map(oldnode.node_tree); newrows = interface_map(newcore)
    _compatibility(oldrows, newrows)
    outer = interface_map(wrapper)
    # Wrapper-only user sockets are outside core ownership and stay intact.
    _compatibility({k: r for k, r in outer.items() if k in oldrows}, newrows)
    # Add only genuinely new public keys; all existing identifiers stay intact.
    for key, row in newrows.items():
        if key in outer: continue
        socket = wrapper.interface.new_socket(name=row['name'], in_out=row['direction'], socket_type=row['type'])
        if 'default' in row: _socket_value(socket, row['default'])
        outer[key] = {**row, 'identifier': socket.identifier}
    wrapper[INTERFACE] = _dump(outer)
    incoming, outgoing = [], []
    outgoing_orders = {}
    for link in wrapper.links:
        if link.to_node == oldnode:
            key = next((k for k, r in oldrows.items() if r['direction'] == 'INPUT' and r['identifier'] == link.to_socket.identifier), None)
            if key is None: raise WorkflowError('CONFLICT', 'Core link not mapped by public key')
            incoming.append((link.from_socket, key))
        if link.from_node == oldnode:
            key = next((k for k, r in oldrows.items() if r['direction'] == 'OUTPUT' and r['identifier'] == link.from_socket.identifier), None)
            if key is None: raise WorkflowError('CONFLICT', 'Core link not mapped by public key')
            outgoing.append((key, link.to_socket))
            if link.to_socket.is_multi_input and link.to_socket.as_pointer() not in outgoing_orders:
                outgoing_orders[link.to_socket.as_pointer()] = (link.to_socket, [
                    ('core', next(k for k, r in oldrows.items() if r['direction'] == 'OUTPUT' and r['identifier'] == old.from_socket.identifier))
                    if old.from_node == oldnode else ('user', old.from_socket)
                    for old in sorted(link.to_socket.links, key=lambda l: l.multi_input_sort_id, reverse=True)])
    location, label = tuple(oldnode.location), oldnode.label
    wrapper.nodes.remove(oldnode)
    node = wrapper.nodes.new('GeometryNodeGroup'); node.node_tree = newcore; node[NODE_ID] = CORE_NODE_ID; node.name = 'Plugin Core'; node.location = location; node.label = label
    for source, key in incoming: wrapper.links.new(source, socket_find(node.inputs, newrows[key]['identifier']))
    for key, target in outgoing: wrapper.links.new(socket_find(node.outputs, newrows[key]['identifier']), target)
    for target, order in outgoing_orders.values():
        links = []
        for kind, value in order:
            source = socket_find(node.outputs, newrows[value]['identifier']) if kind == 'core' else value
            links.append(next(l for l in target.links if l.from_socket == source))
        _order_links(links)
    inp = next((n for n in wrapper.nodes if n.get(NODE_ID) == 'gnw_public_input'), None)
    out = next((n for n in wrapper.nodes if n.get(NODE_ID) == 'gnw_public_output'), None)
    for key in set(newrows) - set(oldrows):
        row = newrows[key]
        if row['direction'] == 'INPUT' and inp: wrapper.links.new(socket_find(inp.outputs, outer[key]['identifier']), socket_find(node.inputs, row['identifier']))
        elif row['direction'] == 'OUTPUT' and out: wrapper.links.new(socket_find(node.outputs, row['identifier']), socket_find(out.inputs, outer[key]['identifier']))
    wrapper.update_tag()


def inspect_binding(object_name, modifier_name):
    obj = bpy.data.objects.get(object_name)
    if obj is None: raise WorkflowError('NOT_FOUND', 'Missing object: ' + object_name)
    modifier = obj.modifiers.get(modifier_name)
    if modifier is None or modifier.type != 'NODES' or not modifier.node_group:
        raise WorkflowError('NOT_FOUND', 'Geometry modifier binding missing')
    wrapper = modifier.node_group
    result = {'object': obj.name, 'modifier': modifier.name, 'wrapper': wrapper.name,
              'wrapper_sha256': tree_fingerprint(wrapper), 'interface': interface_map(wrapper), 'parameters': parameter_values(modifier),
              'parameter_state': _parameter_state(modifier),
              'managed': wrapper.get(ROLE) == 'wrapper', 'target_id': wrapper.get(KEY), 'core': None}
    result['interfaces'] = result['interface']
    def temporal(tree, seen):
        if tree.as_pointer() in seen: return False
        seen.add(tree.as_pointer())
        return any(node.bl_idname in ('GeometryNodeSimulationInput', 'GeometryNodeSimulationOutput', 'GeometryNodeBake') or
                   (getattr(node, 'node_tree', None) and temporal(node.node_tree, seen)) for node in tree.nodes)
    result['has_temporal_nodes'] = bool(temporal(wrapper, set()))
    if result['managed']:
        core = _core_node(wrapper).node_tree
        result.update(core=core.name, core_sha256=tree_fingerprint(core), core_baseline_sha256=core.get(BASELINE),
                      core_modified=tree_fingerprint(core) != core.get(BASELINE))
        result['core_dirty'] = result['core_modified']
    return result


def has_temporal_nodes(tree, trail=()):
    if tree is None: return False
    pointer = tree.as_pointer()
    if pointer in trail: raise WorkflowError('CONFLICT', 'Recursive temporal node graph')
    return any(node.bl_idname in ('GeometryNodeSimulationInput', 'GeometryNodeSimulationOutput', 'GeometryNodeBake') or
               (getattr(node, 'node_tree', None) and has_temporal_nodes(node.node_tree, (*trail, pointer))) for node in tree.nodes)


def _native_copy(value):
    if isinstance(value, bpy.types.ID): return value
    if hasattr(value, 'to_dict'): return copy.deepcopy(value.to_dict())
    if hasattr(value, 'to_list'): return copy.deepcopy(value.to_list())
    if hasattr(value, 'to_tuple'): return value.to_tuple()
    return copy.deepcopy(value)


def _guard_modifier_animation_paths(obj, old):
    """Reject unimplemented modifier-path migration before names/cache change."""
    prefix = old.path_from_id()
    def refers(path):
        return isinstance(path, str) and (path == prefix or path.startswith(prefix + '.') or path.startswith(prefix + '['))
    def action_curves(action, slot=None):
        if action is None: return
        for curve in getattr(action, 'fcurves', ()): yield curve
        for layer in getattr(action, 'layers', ()):
            for strip in layer.strips:
                for bag in getattr(strip, 'channelbags', ()):
                    if slot is None or bag.slot_handle == slot.handle:
                        yield from bag.fcurves
    ad = obj.animation_data
    if ad:
        if any(refers(curve.data_path) for curve in ad.drivers):
            raise WorkflowError('UNSUPPORTED', 'Fresh modifier cannot migrate its input/control driver paths')
        if any(refers(curve.data_path) for curve in action_curves(ad.action, getattr(ad, 'action_slot', None))):
            raise WorkflowError('UNSUPPORTED', 'Fresh modifier cannot migrate its Action slot paths')
        def nla(strips):
            for strip in strips:
                if any(refers(curve.data_path) for curve in action_curves(getattr(strip, 'action', None), getattr(strip, 'action_slot', None))):
                    raise WorkflowError('UNSUPPORTED', 'Fresh modifier cannot migrate its NLA Action paths')
                nla(getattr(strip, 'strips', ()))
        for track in ad.nla_tracks: nla(track.strips)
    seen = set(); owners = []
    for prop in bpy.data.bl_rna.properties:
        if prop.type != 'COLLECTION': continue
        owners.extend(item for item in getattr(bpy.data, prop.identifier) if isinstance(item, bpy.types.ID))
    while owners:
        owner = owners.pop()
        if owner.as_pointer() in seen: continue
        seen.add(owner.as_pointer())
        embedded = getattr(owner, 'node_tree', None)
        if isinstance(embedded, bpy.types.ID): owners.append(embedded)
        animation = getattr(owner, 'animation_data', None)
        if not animation: continue
        for curve in animation.drivers:
            for variable in curve.driver.variables:
                for target in variable.targets:
                    if getattr(target, 'id', None) == obj and refers(getattr(target, 'data_path', '')):
                        raise WorkflowError('UNSUPPORTED', 'Fresh modifier cannot migrate external driver target paths: ' + owner.name)


def stage_fresh_modifier(obj, old, node_group):
    """Stage a fresh cache owner while retaining the exact old modifier for rollback.

    No update/evaluation or cache deletion. Unknown writable non-scalar RNA is
    rejected before mutation; native typed inputs and supported user IDProps are
    copied explicitly, never .bakes or global cache control/history.
    """
    if old.type != 'NODES' or old not in obj.modifiers[:] or obj.library or obj.override_library:
        raise WorkflowError('UNSUPPORTED', 'Fresh modifier requires a local owned NODES target')
    if not hasattr(obj.modifiers, 'move'):
        raise WorkflowError('UNSUPPORTED', 'Object.modifiers.move is required for exact stack order')
    _guard_modifier_animation_paths(obj, old)
    scalar = {}
    excluded = {'name', 'node_group', 'bake_target', 'bake_directory', 'bakes', 'is_active'}
    for prop in old.bl_rna.properties:
        if prop.is_readonly or prop.identifier in excluded: continue
        if prop.type not in ('BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'):
            raise WorkflowError('UNSUPPORTED', 'Unmapped writable modifier RNA: ' + prop.identifier)
        scalar[prop.identifier] = _native_copy(getattr(old, prop.identifier))
    try: user_keys = list(old.keys())
    except TypeError: user_keys = []  # Native 5.2 NodesModifier has no IDProperty storage.
    user = {key: _native_copy(old[key]) for key in user_keys}
    state = _parameter_state(old) if old.node_group else {}
    active = [modifier for modifier in obj.modifiers if modifier.is_active]
    if len(active) != 1: raise WorkflowError('UNSUPPORTED', 'Fresh modifier requires one unambiguous active stack entry')
    token = {'object': obj, 'old': old, 'fresh': None, 'name': old.name,
             'index': list(obj.modifiers).index(old), 'flags': {'show_viewport': old.show_viewport, 'show_render': old.show_render},
             'active': active[0], 'active_was_old': active[0] == old,
             'scalar_fields': sorted(scalar), 'user_property_keys': sorted(user), 'status': 'staging'}
    try:
        fresh = obj.modifiers.new('__GNW_fresh_' + uuid.uuid4().hex, 'NODES'); token['fresh'] = fresh
        for name, value in scalar.items(): setattr(fresh, name, value)
        fresh.node_group = node_group
        # Preserve every matching native input/control field. New/removed graph
        # interface keys retain the established _restore_parameter_state policy.
        if state: _restore_parameter_state(fresh, state)
        for key, value in user.items(): fresh[key] = value
        obj.modifiers.move(len(obj.modifiers) - 1, token['index'])
        old.name = '__GNW_retained_' + uuid.uuid4().hex
        old.show_viewport = False; old.show_render = False
        fresh.name = token['name']
        (fresh if token['active_was_old'] else token['active']).is_active = True
        if fresh.name != token['name']: raise WorkflowError('CONFLICT', 'Stable modifier name could not be preserved')
        token['status'] = 'staged'
        return token
    except Exception:
        rollback_fresh_modifier(token)
        raise


def rollback_fresh_modifier(token):
    """Restore the retained old cache owner without reassigning its node group."""
    if token.get('status') == 'committed': raise WorkflowError('CONFLICT', 'Committed modifier cannot be rolled back in memory')
    obj, old, fresh = token['object'], token['old'], token.get('fresh')
    if fresh is not None and fresh in obj.modifiers[:]: obj.modifiers.remove(fresh)
    if old not in obj.modifiers[:]: raise WorkflowError('CONFLICT', 'Retained modifier disappeared before rollback')
    old.name = token['name']
    for name, value in token['flags'].items(): setattr(old, name, value)
    index = list(obj.modifiers).index(old)
    if index != token['index']: obj.modifiers.move(index, token['index'])
    token['active'].is_active = True
    token['status'] = 'rolled_back'
    return old


def commit_fresh_modifier(token):
    """Remove only the disabled in-memory old modifier after validation."""
    if token.get('status') != 'staged': raise WorkflowError('CONFLICT', 'Staged modifier required for commit')
    obj, old, fresh = token['object'], token['old'], token['fresh']
    if old not in obj.modifiers[:] or fresh not in obj.modifiers[:]:
        raise WorkflowError('CONFLICT', 'Staged modifier identity disappeared')
    if fresh.name != token['name'] or list(obj.modifiers).index(fresh) != token['index']:
        raise WorkflowError('CONFLICT', 'Staged modifier name/order changed')
    next_active = fresh if token['active_was_old'] else token['active']
    obj.modifiers.remove(old)
    next_active.is_active = True
    token['status'] = 'committed'
    return fresh


def apply_target(target, before_evaluate=None, before_first_evaluation=None):
    """Prepare isolated groups, then commit a single modifier binding atomically."""
    groups_before = set(bpy.data.node_groups); obj = bpy.data.objects.get(target['object']); created_object = False
    modifier = None; created_modifier = False; oldgroup = None; oldvalues = {}; oldparameterstate = {}; diff = None; fresh_token = None
    try:
        if obj is None:
            if not target.get('create'): raise WorkflowError('NOT_FOUND', 'Target object missing')
            mesh = bpy.data.meshes.new(target['object'] + '_Mesh'); obj = bpy.data.objects.new(target['object'], mesh)
            bpy.context.collection.objects.link(obj); created_object = True
        if obj.library or obj.override_library: raise WorkflowError('UNSUPPORTED', 'Target object is linked/override')
        modifier = obj.modifiers.get(target['modifier'])
        if modifier and modifier.type != 'NODES': raise WorkflowError('CONFLICT', 'Target modifier has another type')
        oldgroup = modifier.node_group if modifier else None
        action = target['action']
        if oldgroup:
            _local(oldgroup)
            if oldgroup.animation_data: raise WorkflowError('UNSUPPORTED', 'Animated/driven wrapper requires explicit migration')
            oldvalues = parameter_values(modifier)
            oldparameterstate = _parameter_state(modifier)
        if action in ('update', 'parameters') and (not oldgroup or oldgroup.get(ROLE) != 'wrapper'):
            raise WorkflowError('CONFLICT', 'Managed target required for update/parameters')
        if action == 'build' and oldgroup: raise WorkflowError('CONFLICT', 'Build cannot overwrite an existing binding; use update/adopt')
        if oldgroup and oldgroup.get(ROLE) == 'wrapper':
            oldcore = _core_node(oldgroup).node_tree; current_sha = tree_fingerprint(oldcore)
            if action in ('update', 'parameters'):
                try:
                    state = json.loads(oldgroup[STATE])
                    generated = json.loads(oldcore[GENERATED])
                except (KeyError, ValueError, TypeError) as exc:
                    raise WorkflowError('CONFLICT', 'Managed update requires valid core ownership metadata') from exc
                if (oldcore.get(ROLE) != 'core' or not oldcore.get(BASELINE) or
                        hashlib.sha256(_dump(generated).encode()).hexdigest() != oldcore.get(BASELINE) or
                        state.get('target_id') != oldgroup.get(KEY) or not state.get('core_sha256')):
                    raise WorkflowError('CONFLICT', 'Managed core ownership identity is incomplete or inconsistent')
                _local(oldcore, allow_dynamic=True)
            if action != 'parameters' and current_sha != oldcore.get(BASELINE):
                state = _tree_state(oldcore)
                baseline_state = json.loads(oldcore[GENERATED]) if GENERATED in oldcore else None
                diff = {'baseline_sha256': oldcore.get(BASELINE), 'current_sha256': current_sha,
                        'replacement': 'managed_core_contents', 'external_user_zone': 'preserved',
                        'replacement_nodes': [{'id': n['id'], 'name': n['name'], 'type': n['type']} for n in state['nodes']],
                        'replacement_link_count': len(state['links']),
                        'changed_sections': [key for key in ('nodes', 'links', 'interface')
                                             if baseline_state is None or state[key] != baseline_state.get(key)]}
                if not target.get('overwrite_core') or target.get('expected_core_sha256') != current_sha:
                    err = WorkflowError('CONFLICT', 'Core manually changed; review core_diff and explicitly confirm current SHA: ' + _dump(diff)); err.core_diff = diff; raise err
        if action == 'parameters':
            wrapper = oldgroup.copy(); _core_node(wrapper).node_tree = copy_tree_isolated(_core_node(oldgroup).node_tree, allow_dynamic=True)
        else:
            if target.get('share_with'):
                matches = [m.node_group for o in bpy.data.objects for m in o.modifiers if m.type == 'NODES' and m.node_group and m.node_group.get(KEY) == target['share_with'] and m.node_group.get(ROLE) == 'wrapper']
                if len(matches) != 1: raise WorkflowError('CONFLICT', 'share_with target must resolve uniquely')
                core = _core_node(matches[0]).node_tree
            elif action == 'adopt':
                source = bpy.data.node_groups.get(target['source_group'])
                if source is None: raise WorkflowError('NOT_FOUND', 'Adopt group missing')
                _local(source); core = copy_tree_isolated(source); core[ROLE] = 'core'; core[INTERFACE] = _dump(interface_map(source))
                for direction in ('INPUT', 'OUTPUT'):
                    if len([s for s in interface_map(core).values() if s['direction'] == direction and s['type'] == 'NodeSocketGeometry']) != 1:
                        raise WorkflowError('UNSUPPORTED', 'Adopt requires exactly one Geometry input/output')
                core[BASELINE] = tree_fingerprint(core)
                core[GENERATED] = _dump(_tree_state(core))
            else:
                if 'graph' in target: graph = target['graph']
                else:
                    from .recipes import build_recipe
                    graph = build_recipe(target['recipe'])
                core = build_graph(graph)
            if action == 'update': wrapper = oldgroup.copy(); _replace_core(wrapper, core)
            else: wrapper = _wrapper(core, target['id'])
        wrapper[KEY] = target['id']; wrapper[ROLE] = 'wrapper'
        if before_evaluate is not None: before_evaluate()
        if modifier is None: modifier = obj.modifiers.new(target['modifier'], 'NODES'); created_modifier = True
        if not created_modifier and (has_temporal_nodes(wrapper) or has_temporal_nodes(oldgroup)):
            fresh_token = stage_fresh_modifier(obj, modifier, wrapper)
            modifier = fresh_token['fresh']
        else: modifier.node_group = wrapper
        if action in ('update', 'parameters'): _restore_parameter_state(modifier, oldparameterstate)
        if action == 'adopt' and oldgroup and oldgroup.name == target['source_group']:
            _restore_parameter_state(modifier, oldparameterstate)
        for key, value in target.get('parameters', {}).items(): _write_parameter(modifier, key, value)
        effective_recipe = target.get('recipe')
        if action == 'parameters' and oldgroup and STATE in oldgroup:
            effective_recipe = json.loads(oldgroup[STATE]).get('recipe')
        if effective_recipe == 'curve_fence':
            current_parameters = parameter_values(modifier)
            if current_parameters.get('use_external_path'):
                path = current_parameters.get('path_object')
                path_object = _resource(path) if path else None
                if path_object is None or path_object.type not in ('CURVE', 'CURVES'):
                    raise WorkflowError('INVALID_REQUEST', 'External curve_fence path requires a local CURVE/CURVES object')
        wrapper[STATE] = _dump({'schema_version': '1.0', 'target_id': target['id'], 'action': action,
                                'interface': interface_map(wrapper), 'source_group': target.get('source_group'),
                                'blueprint': target.get('graph') if action != 'parameters' else json.loads(oldgroup.get(STATE, '{}')).get('blueprint'),
                                'recipe': effective_recipe, 'core_sha256': tree_fingerprint(_core_node(wrapper).node_tree)})
        if before_evaluate is not None: before_evaluate()
        if before_first_evaluation is not None: before_first_evaluation(modifier)
        obj.update_tag(); bpy.context.view_layer.update()
        result = inspect_binding(obj.name, modifier.name)
        result.update(id=target['id'], action=action, status='pass', core_diff=diff,
                      protection={'source_group_preserved': True, 'user_zone_preserved': action in ('update', 'parameters'),
                                  'default_isolated': not bool(target.get('share_with')), 'explicit_shared_core': bool(target.get('share_with'))})
        if fresh_token:
            result['fresh_modifier'] = {'status': 'committed', 'index': fresh_token['index'],
                'scalar_fields': fresh_token['scalar_fields'], 'user_property_keys': fresh_token['user_property_keys']}
            commit_fresh_modifier(fresh_token)
        return result
    except Exception:
        if modifier is not None:
            if fresh_token:
                modifier = rollback_fresh_modifier(fresh_token)
            elif created_modifier: obj.modifiers.remove(modifier)
            elif modifier.node_group != oldgroup:
                modifier.node_group = oldgroup
                if oldgroup: _restore_parameter_state(modifier, oldparameterstate)
                obj.update_tag(); bpy.context.view_layer.update()
        if created_object and obj:
            mesh = obj.data; bpy.data.objects.remove(obj, do_unlink=True)
            if mesh.users == 0: bpy.data.meshes.remove(mesh)
        for tree in list(bpy.data.node_groups):
            if tree not in groups_before: bpy.data.node_groups.remove(tree, do_unlink=True)
        raise
