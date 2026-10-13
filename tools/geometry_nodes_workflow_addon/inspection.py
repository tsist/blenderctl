"""Bounded real geometry inspection and isolated intermediate observations."""
import hashlib
import json
import uuid
import bpy
from mathutils import Vector
from .contract import WorkflowError
from . import core


def _context():
    return {'scene': bpy.context.scene.name, 'view_layer': bpy.context.view_layer.name,
            'frame': bpy.context.scene.frame_current, 'evaluation_mode': 'VIEWPORT'}


def _bounds(points):
    points = list(points)
    if not points: return None
    lo = [min(p[i] for p in points) for i in range(3)]; hi = [max(p[i] for p in points) for i in range(3)]
    return {'min': lo, 'max': hi, 'size': [hi[i] - lo[i] for i in range(3)], 'space': 'WORLD'}


def _attributes(data, limit=8, skip_names=()):
    result = []
    for attr in getattr(data, 'attributes', []):
        if attr.name in skip_names: continue
        samples = []
        for value in list(attr.data[:limit]):
            name = next((n for n in ('value', 'vector', 'color', 'matrix') if hasattr(value, n)), None)
            samples.append(core.plain(getattr(value, name)) if name else None)
        result.append({'name': attr.name, 'type': attr.data_type, 'domain': attr.domain,
                       'count': len(attr.data), 'samples': samples, 'sample_limit': limit})
    return sorted(result, key=lambda row: (row['name'], row['domain'], row['type']))


def _positions(data):
    if isinstance(data, bpy.types.Mesh): return [v.co.copy() for v in data.vertices]
    attrs = getattr(data, 'attributes', None)
    position = attrs.get('position') if attrs is not None else None
    if position: return [v.vector.copy() for v in position.data]
    if isinstance(data, bpy.types.Curve):
        return [Vector(p.co[:3]) for s in data.splines for p in (s.bezier_points if s.type == 'BEZIER' else s.points)]
    return []


def _component(data, matrix, limit=8, instance_component=False):
    if data is None: return None
    if instance_component:
        # Blender 5.2.1's synthetic instance PointCloud exposes a position
        # attribute without usable backing RNA values. The real transforms
        # are readable and carry the instance origins; never sample that view.
        transforms = data.attributes.get('instance_transform')
        origins = [v.value.translation.copy() for v in transforms.data] if transforms else []
        attrs = _attributes(data, limit, skip_names={'position'})
        attrs.append({'name': 'position', 'type': 'FLOAT_VECTOR', 'domain': 'POINT',
                      'count': len(origins), 'sample_limit': limit, 'status': 'unsupported', 'samples': [],
                      'reason': 'Synthetic instance position RNA view is not readable in this build; origins are derived from instance_transform'})
        result = {'type': data.bl_rna.identifier, 'attributes': attrs,
                  'bounds': _bounds(matrix @ p for p in origins),
                  'origins': {'source': 'instance_transform.translation', 'samples': [list(v) for v in origins[:limit]], 'count': len(origins)}}
    else:
        result = {'type': data.bl_rna.identifier, 'attributes': _attributes(data, limit),
                  'bounds': _bounds(matrix @ p for p in _positions(data))}
    if isinstance(data, bpy.types.Mesh):
        result.update(vertices=len(data.vertices), edges=len(data.edges), faces=len(data.polygons),
                      loops=len(data.loops), samples=[list(v.co) for v in data.vertices[:limit]])
    elif hasattr(data, 'points'): result['points'] = len(data.points)
    elif hasattr(data, 'curves'): result['curves'] = len(data.curves)
    elif isinstance(data, bpy.types.Curve): result['splines'] = len(data.splines)
    return result


def _evaluated(obj, limit=8):
    deps = bpy.context.evaluated_depsgraph_get(); evaluated = obj.evaluated_get(deps)
    geometry = evaluated.evaluated_geometry()
    components = {}
    for name in ('mesh', 'curves', 'pointcloud', 'instances_pointcloud', 'volume', 'grease_pencil'):
        value = getattr(geometry, name, None)
        if name == 'instances_pointcloud': value = value() if callable(value) else value
        if value is not None: components[name] = _component(value, evaluated.matrix_world, limit, name == 'instances_pointcloud')
    mesh = components.get('mesh')
    pointcloud = components.get('instances_pointcloud')
    instance_rows = []; all_bounds = []
    for instance in deps.object_instances:
        if not instance.is_instance or not instance.parent or instance.parent.original != obj: continue
        matrix = instance.matrix_world.copy(); instance_obj = instance.object
        bounds = _bounds(matrix @ Vector(p) for p in instance_obj.bound_box)
        if bounds: all_bounds.extend((bounds['min'], bounds['max']))
        if len(instance_rows) < limit:
            instance_rows.append({'object': instance_obj.original.name, 'matrix_world': [list(r) for r in matrix], 'bounds': bounds})
    if mesh and mesh['bounds']: all_bounds.extend((mesh['bounds']['min'], mesh['bounds']['max']))
    if not all_bounds:
        for comp in components.values():
            if comp and comp.get('bounds'): all_bounds.extend((comp['bounds']['min'], comp['bounds']['max']))
    instances = {'count': pointcloud.get('points', 0) if pointcloud else 0, 'samples': instance_rows,
                 'references': len(geometry.instance_references()), 'sample_limit': limit,
                 'semantics': 'top-level instance component count; depsgraph samples may include nested instances'}
    return {'vertices': mesh['vertices'] if mesh else 0, 'faces': mesh['faces'] if mesh else 0,
            'edges': mesh['edges'] if mesh else 0, 'bounds': _bounds(all_bounds),
            'attributes': mesh['attributes'] if mesh else [], 'components': components,
            'instances': instances, 'context': _context(),
            'limitations': ['VIEWPORT dependency graph; RENDER result requires separate render validation',
                            'Counts are component-specific; instances are not realized for inspection']}


def _consumers(tree):
    return {'modifiers': [{'object': o.name, 'modifier': m.name} for o in bpy.data.objects for m in o.modifiers
                          if m.type == 'NODES' and m.node_group == tree],
            'groups': [{'group': g.name, 'node': n.name} for g in bpy.data.node_groups for n in g.nodes
                       if getattr(n, 'node_tree', None) == tree]}


def inspect_target(object_name, modifier_name=None, observations=None):
    obj = bpy.data.objects.get(object_name)
    if obj is None: raise WorkflowError('NOT_FOUND', 'Object missing: ' + object_name)
    bpy.context.view_layer.update()
    modifiers = [m for m in obj.modifiers if m.type == 'NODES' and m.node_group]
    if modifier_name is not None:
        modifiers = [m for m in modifiers if m.name == modifier_name]
        if not modifiers: raise WorkflowError('NOT_FOUND', 'Geometry modifier missing: ' + modifier_name)
    bindings = [core.inspect_binding(obj.name, m.name) for m in modifiers]
    consumers = {}
    dependencies = []
    for modifier in modifiers:
        tree = modifier.node_group; consumers[tree.name] = _consumers(tree)
        if tree.get(core.ROLE) == 'wrapper':
            managed = core._core_node(tree).node_tree; consumers[managed.name] = _consumers(managed)
        seen = set()
        def visit(group):
            if group.as_pointer() in seen: return
            seen.add(group.as_pointer())
            for node in group.nodes:
                for prop in node.bl_rna.properties:
                    if prop.type != 'POINTER' or prop.identifier in ('rna_type', 'parent'): continue
                    value = getattr(node, prop.identifier)
                    if isinstance(value, bpy.types.ID):
                        dependencies.append({'group': group.name, 'node': node.name, 'property': prop.identifier, **core.plain(value)})
                        if isinstance(value, bpy.types.NodeTree): visit(value)
        visit(tree)
    result = {'object': obj.name, 'binding': bindings[0] if len(bindings) == 1 else None, 'bindings': bindings,
              'base': _component(obj.data, obj.matrix_world) if obj.data else None,
              'evaluated': _evaluated(obj), 'consumers': consumers, 'dependencies': dependencies,
              'context': _context(), 'observations': []}
    result['instances'] = result['evaluated']['instances']
    for observation in observations or []: result['observations'].append(observe(object_name, observation))
    return result


def discover_native_nodes():
    tree = bpy.data.node_groups.new('_GNW_Discovery_' + uuid.uuid4().hex, 'GeometryNodeTree')
    rows = []
    try:
        for name in sorted(dir(bpy.types)):
            cls = getattr(bpy.types, name)
            if not isinstance(cls, type) or not issubclass(cls, bpy.types.Node) or cls.__module__ != 'bpy.types' or getattr(cls.bl_rna, 'is_runtime', False): continue
            if cls in (bpy.types.Node, bpy.types.NodeInternal): continue
            try:
                # Built-in NodeInternal.poll RNA functions and Python class
                # poll methods have different binding signatures in 5.2.
                # nodes.new performs the authoritative tree-context poll.
                node = tree.nodes.new(name)
            except (AttributeError, RuntimeError, TypeError): continue
            deferred = any(s in name for s in ('Simulation', 'Bake'))
            rows.append({'type': node.bl_idname, 'label': node.bl_label, 'native': True,
                         'phase': 'later' if deferred else 'current', 'effect_validation': 'not_run',
                         'inputs': [{'identifier': s.identifier, 'name': s.name, 'type': s.bl_idname,
                                     'enabled': s.enabled, 'multi_input': s.is_multi_input} for s in node.inputs],
                         'outputs': [{'identifier': s.identifier, 'name': s.name, 'type': s.bl_idname} for s in node.outputs],
                         'writable_properties': [{'name': p.identifier, 'type': p.type,
                                                   'enum': [i.identifier for i in p.enum_items] if p.type == 'ENUM' else None}
                                                  for p in node.bl_rna.properties if not p.is_readonly and p.identifier != 'rna_type']})
            tree.nodes.remove(node)
    finally: bpy.data.node_groups.remove(tree, do_unlink=True)
    return {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(), 'tree_type': 'GeometryNodeTree',
            'nodes': rows, 'count': len(rows), 'limitations': ['Native registration/default poll only, not an effect certification',
                                                             'Dynamic sockets are dependent on actual node configuration']}


def _rna_state(owner, persistence_exceptions=()):
    result = {p.identifier: core.plain(getattr(owner, p.identifier)) for p in owner.bl_rna.properties
              if not p.is_readonly and p.type not in ('COLLECTION',) and p.identifier not in ('rna_type',)}
    if isinstance(owner, bpy.types.ID):
        matches = []
        for record in persistence_exceptions:
            collection = getattr(bpy.data, record.get('collection', ''), None)
            if record.get('name') == owner.name and collection is not None and hasattr(collection, 'get') and collection.get(owner.name) == owner:
                matches.append(record)
        if matches:
            if len(matches) != 1 or owner.library or owner.override_library or not owner.use_fake_user:
                raise WorkflowError('VALIDATION_FAILED', 'Persistence exception does not match current local ID: ' + owner.name)
            record = matches[0]
            if record.get('use_fake_user_before') is not False or record.get('use_fake_user_after') is not True:
                raise WorkflowError('VALIDATION_FAILED', 'Invalid persistence transition: ' + owner.name)
            result['use_fake_user'] = False
    return result


def _data_state(data, persistence_exceptions=()):
    if data is None: return None
    result = {'identity': core.plain(data), 'properties': _rna_state(data, persistence_exceptions)}
    if isinstance(data, bpy.types.Mesh):
        result.update(vertices=[list(v.co) for v in data.vertices], edges=[list(e.vertices) for e in data.edges],
                      polygons=[{'vertices': list(p.vertices), 'material': p.material_index, 'smooth': p.use_smooth} for p in data.polygons],
                      weights=[[{'group': g.group, 'weight': g.weight} for g in v.groups] for v in data.vertices],
                      shapes=[{'name': k.name, 'value': k.value, 'points': [list(p.co) for p in k.data]} for k in data.shape_keys.key_blocks] if data.shape_keys else [])
    if isinstance(data, bpy.types.Curve):
        result['splines'] = [{'type': s.type, 'cyclic': s.use_cyclic_u,
                              'points': [{'co': core.plain(p.co), 'radius': p.radius, 'tilt': p.tilt,
                                          'handle_left': core.plain(p.handle_left) if hasattr(p, 'handle_left') else None,
                                          'handle_right': core.plain(p.handle_right) if hasattr(p, 'handle_right') else None}
                                         for p in (s.bezier_points if s.type == 'BEZIER' else s.points)]} for s in data.splines]
    result['attributes'] = _attributes(data, limit=0)
    # Full attribute content is hashed, while public observations remain bounded.
    result['attribute_content'] = []
    for attr in getattr(data, 'attributes', []):
        values = []
        for value in attr.data:
            name = next((n for n in ('value', 'vector', 'color', 'matrix') if hasattr(value, n)), None)
            values.append(core.plain(getattr(value, name)) if name else None)
        result['attribute_content'].append({'name': attr.name, 'values': values})
    return result


def scene_fingerprint(exclude_objects=(), persistence_exceptions=()):
    excluded = set(exclude_objects)
    objects = []
    for obj in sorted(bpy.data.objects, key=lambda o: o.name):
        if obj.name in excluded: continue
        modifiers = []
        for modifier in obj.modifiers:
            row = _rna_state(modifier, persistence_exceptions)
            if modifier.type == 'NODES' and modifier.node_group:
                row['graph_sha256'] = core.tree_fingerprint(modifier.node_group)
                row['parameters'] = core.parameter_values(modifier)
            modifiers.append(row)
        objects.append({'object': obj.name, 'properties': _rna_state(obj, persistence_exceptions), 'data': _data_state(obj.data, persistence_exceptions),
                        'modifiers': modifiers, 'constraints': [_rna_state(c, persistence_exceptions) for c in obj.constraints],
                        'custom_properties': {k: core.plain(v) for k, v in obj.items() if k != '_RNA_UI'}})
    materials = [{'name': m.name, 'properties': _rna_state(m, persistence_exceptions),
                  'graph': core.tree_fingerprint(m.node_tree) if m.node_tree else None} for m in sorted(bpy.data.materials, key=lambda m: m.name)]
    worlds = [{'name': w.name, 'properties': _rna_state(w, persistence_exceptions), 'graph': core.tree_fingerprint(w.node_tree) if w.node_tree else None} for w in sorted(bpy.data.worlds, key=lambda w: w.name)]
    collections = [{'name': c.name, 'objects': sorted(o.name for o in c.objects if o.name not in excluded),
                    'children': sorted(child.name for child in c.children)} for c in sorted(bpy.data.collections, key=lambda c: c.name)]
    value = {'objects': objects, 'materials': materials, 'worlds': worlds, 'collections': collections}
    return hashlib.sha256(core._dump(value).encode()).hexdigest()


def _node(tree, selector):
    matches = [n for n in tree.nodes if n.get(core.NODE_ID, n.name) == selector or n.name == selector]
    if len(matches) != 1: raise WorkflowError('UNSUPPORTED', 'Node must resolve uniquely in selected group: ' + selector)
    return matches[0]


def _output(tree):
    outputs = [n for n in tree.nodes if n.bl_idname == 'NodeGroupOutput' and n.is_active_output]
    if len(outputs) != 1: raise WorkflowError('UNSUPPORTED', 'One active group output required')
    return outputs[0]


def _route_capture(root, leaf, chain, source, token):
    """Add temporary output interfaces through real parent group instances."""
    current = leaf
    for parent, group_node in reversed(chain):
        socket = current.interface.new_socket(name=token, in_out='OUTPUT', socket_type='NodeSocketGeometry')
        current.links.new(source, core.socket_find(_output(current).inputs, socket.identifier))
        current.update_tag()
        source = core.socket_find(group_node.outputs, socket.identifier)
        current = parent
    output = next((s for s in _output(root).inputs if s.type == 'GEOMETRY'), None)
    if output is None: raise WorkflowError('UNSUPPORTED', 'Root Geometry output missing')
    root.links.new(source, output)


def _repeat_capture(tree, source, observation, token):
    zone = _node(tree, observation['repeat_zone'])
    if zone.bl_idname != 'GeometryNodeRepeatOutput': raise WorkflowError('UNSUPPORTED', 'repeat_zone must identify RepeatOutput')
    inputs = [n for n in tree.nodes if n.bl_idname == 'GeometryNodeRepeatInput' and n.paired_output == zone]
    if len(inputs) != 1: raise WorkflowError('UNSUPPORTED', 'RepeatInput pairing must resolve uniquely')
    inp = inputs[0]
    if 'iteration' not in observation: raise WorkflowError('UNSUPPORTED', 'Repeat observation requires a zero-based iteration')
    iterations = core.socket_find(inp.inputs, 'Iterations')
    for link in list(iterations.links): tree.links.remove(link)
    iterations.default_value = observation['iteration'] + 1
    if _node(tree, observation['node']) == zone:
        # The paired output is already outside the zone. Feeding it back into
        # a capture state would make a cycle; route its bounded result directly.
        tree.update_tag()
        return source
    zone.repeat_items.new('GEOMETRY', token)
    # RepeatItem has no identifier; obtain real sockets after the native update.
    capture_input = core.socket_find(zone.inputs, token)
    capture_output = core.socket_find(zone.outputs, token)
    tree.links.new(source, capture_input)
    tree.update_tag()
    return capture_output


def _repeat_iterations(root, tree, chain, temporary, selector):
    """Evaluate the original single-valued bound through its parent context."""
    zone = _node(tree, selector)
    if zone.bl_idname != 'GeometryNodeRepeatOutput': raise WorkflowError('UNSUPPORTED', 'repeat_zone must identify RepeatOutput')
    inputs = [n for n in tree.nodes if n.bl_idname == 'GeometryNodeRepeatInput' and n.paired_output == zone]
    if len(inputs) != 1: raise WorkflowError('UNSUPPORTED', 'RepeatInput pairing must resolve uniquely')
    socket = core.socket_find(inputs[0].inputs, 'Iterations')
    if not socket.is_linked: return max(0, int(socket.default_value))
    token = '_gnw_original_iterations_' + uuid.uuid4().hex
    # Iterations accepts a single value, not a geometry-dependent field. A
    # one-point capture tests its evaluated value without changing public N.
    point = tree.nodes.new('GeometryNodeMeshLine'); point.inputs['Count'].default_value = 1
    store = tree.nodes.new('GeometryNodeStoreNamedAttribute'); store.data_type = 'INT'; store.domain = 'POINT'; store.inputs['Name'].default_value = token
    tree.links.new(point.outputs['Mesh'], store.inputs['Geometry'])
    tree.links.new(socket.links[0].from_socket, core.socket_find(store.inputs, 'Value'))
    _route_capture(root, tree, chain, store.outputs['Geometry'], token)
    temporary.update_tag(); tree.update_tag(); bpy.context.view_layer.update()
    result = _evaluated(temporary, 1)
    matches = [a for component in result['components'].values() for a in component.get('attributes', []) if a['name'] == token]
    if len(matches) != 1 or not matches[0]['samples'] or type(matches[0]['samples'][0]) is not int:
        raise WorkflowError('UNSUPPORTED', 'Original Repeat iteration count could not be observed reliably')
    count = max(0, matches[0]['samples'][0])
    tree.nodes.remove(store); tree.nodes.remove(point)
    return count


def observe(object_name, observation):
    """Evaluate real nested/Repeat captures in copies, retaining parent inputs."""
    row = {'id': observation.get('id'), 'object': object_name, 'status': 'not_run', 'context': _context()}
    original = bpy.data.objects.get(object_name)
    if original is None: return {**row, 'status': 'unsupported', 'reason': 'Object not found'}
    modifiers = [m for m in original.modifiers if m.type == 'NODES' and m.node_group and
                 (not observation.get('modifier') or m.name == observation['modifier'])]
    if len(modifiers) != 1: return {**row, 'status': 'unsupported', 'reason': 'Specify one geometry modifier'}
    if not observation.get('node'):
        try:
            result = _evaluated(original, observation.get('limit', 8))
            component = observation.get('component')
            if component and component not in result['components']:
                return {**row, 'status': 'unsupported', 'reason': 'Requested geometry component absent', 'result': result}
            if observation.get('attribute'):
                attrs = [a for name, comp in result['components'].items() if not component or name == component
                         for a in comp.get('attributes', []) if a['name'] == observation['attribute'] and
                         (not observation.get('domain') or a['domain'] == observation['domain'])]
                if not attrs: return {**row, 'status': 'unsupported', 'reason': 'Requested attribute/domain absent', 'result': result}
                if any(a.get('status') == 'unsupported' for a in attrs):
                    return {**row, 'status': 'unsupported', 'reason': 'Requested attribute RNA view unavailable; see component origins', 'result': result}
                row['capture'] = attrs
            return {**row, 'status': 'pass', 'result': result, 'scope': 'final_output'}
        except (WorkflowError, RuntimeError, TypeError, ValueError) as error:
            return {**row, 'status': 'unsupported', 'reason': str(error)}
    def has_temporal(tree, seen):
        if tree.as_pointer() in seen: return False
        seen.add(tree.as_pointer())
        return any(node.bl_idname in ('GeometryNodeSimulationInput', 'GeometryNodeSimulationOutput', 'GeometryNodeBake') or
                   (getattr(node, 'node_tree', None) and has_temporal(node.node_tree, seen)) for node in tree.nodes)
    if has_temporal(modifiers[0].node_group, set()):
        return {**row, 'status': 'unsupported',
                'reason': 'Temporal internal/group/Repeat capture cannot preserve original simulation/cache state; store the requested field explicitly on the final output and observe that attribute'}
    before = core.tree_fingerprint(modifiers[0].node_group); groups_before = set(bpy.data.node_groups)
    temporary = None; copied_data = None
    try:
        temporary = original.copy()
        if original.data: copied_data = original.data.copy(); temporary.data = copied_data
        bpy.context.collection.objects.link(temporary)
        temporary.name = '_GNW_Observation_' + uuid.uuid4().hex
        modifier = temporary.modifiers.get(modifiers[0].name)
        modifier.node_group = core.copy_tree_isolated(modifiers[0].node_group)
        # Preserve modifier parameters explicitly across the copied binding.
        bpy.context.view_layer.update()
        parameter_state = core._parameter_state(modifiers[0])
        core._restore_parameter_state(modifier, parameter_state)
        tree = modifier.node_group
        if tree.get(core.ROLE) == 'wrapper': tree = core._core_node(tree).node_tree
        node_id = observation.get('node')
        # Observe the core socket directly, excluding the user's downstream
        # wrapper chain and later object modifiers from the capture result.
        values = core.parameter_values(modifiers[0])
        modifier.node_group = tree
        bpy.context.view_layer.update()
        core._restore_parameter_state(modifier, {k: v for k, v in parameter_state.items() if k in core.interface_map(tree)})
        past_target = False
        for item in temporary.modifiers:
            if past_target: item.show_viewport = False
            if item == modifier: past_target = True
        root = tree; chain = []
        for selector in observation.get('group_path', []):
            group_node = _node(tree, selector)
            if not getattr(group_node, 'node_tree', None): raise WorkflowError('UNSUPPORTED', 'group_path must identify real group instances')
            chain.append((tree, group_node)); tree = group_node.node_tree
        node = _node(tree, node_id); socket = core.socket_find(node.outputs, observation.get('socket', 'Geometry'))
        row.update(node_path=[*observation.get('group_path', []), node_id],
                   geometry_component=observation.get('component'), domain=observation.get('domain'),
                   evaluation_mode='VIEWPORT', parent_parameters=values)
        if observation.get('repeat_zone'):
            if type(observation.get('iteration')) is not int or observation['iteration'] < 0:
                raise WorkflowError('UNSUPPORTED', 'Repeat observation requires nonnegative zero-based iteration')
            original_count = _repeat_iterations(root, tree, chain, temporary, observation['repeat_zone'])
            if observation['iteration'] >= original_count:
                raise WorkflowError('UNSUPPORTED', 'Requested iteration is outside original Repeat count ' + str(original_count))
            row['original_iterations'] = original_count
        if node.bl_idname in ('GeometryNodeRepeatInput', 'GeometryNodeRepeatOutput') and not observation.get('repeat_zone'):
            raise WorkflowError('UNSUPPORTED', 'Repeat zone boundary/interior cannot be rerouted out of scope')
        if socket.type == 'GEOMETRY':
            source = socket
            capture_name = None
        else:
            attribute_type = observation.get('type') or {'VALUE': 'FLOAT', 'INT': 'INT', 'BOOLEAN': 'BOOLEAN', 'VECTOR': 'FLOAT_VECTOR', 'RGBA': 'FLOAT_COLOR'}.get(socket.type)
            if not attribute_type: raise WorkflowError('UNSUPPORTED', 'Socket type cannot be captured as named attribute')
            geometry_node = observation.get('geometry_node', node_id)
            geometry_socket = core.socket_find(_node(tree, geometry_node).outputs, observation.get('geometry_socket', 'Geometry'))
            if geometry_socket.type != 'GEOMETRY': raise WorkflowError('UNSUPPORTED', 'Field requires Geometry capture context')
            store = tree.nodes.new('GeometryNodeStoreNamedAttribute'); store.data_type = attribute_type; store.domain = observation.get('domain', 'POINT')
            capture_name = observation.get('attribute') or '_gnw_capture_' + hashlib.sha256(json.dumps(observation, sort_keys=True).encode()).hexdigest()[:16]
            store.inputs['Name'].default_value = capture_name
            tree.links.new(geometry_socket, store.inputs['Geometry']); tree.links.new(socket, core.socket_find(store.inputs, 'Value'))
            source = store.outputs['Geometry']
        token = '_gnw_geometry_' + uuid.uuid4().hex
        if observation.get('repeat_zone'):
            source = _repeat_capture(tree, source, observation, token)
            row.update(repeat_zone=observation['repeat_zone'], iteration=observation['iteration'],
                       repeat_semantics='selected prefix iteration, zero-based; parent public parameters unchanged')
        elif 'iteration' in observation:
            raise WorkflowError('UNSUPPORTED', 'iteration requires repeat_zone')
        _route_capture(root, tree, chain, source, token)
        tree.update_tag(); temporary.update_tag(); bpy.context.view_layer.update()
        result = _evaluated(temporary, observation.get('limit', 8))
        component = observation.get('component')
        if component and component not in result['components']:
            return {**row, 'status': 'unsupported', 'reason': 'Requested geometry component absent', 'result': result}
        if capture_name:
            attributes = [a for comp in result['components'].values() for a in comp.get('attributes', []) if a['name'] == capture_name]
            if not attributes: return {**row, 'status': 'unsupported', 'reason': 'Capture produced no observable named attribute on evaluated components', 'result': result}
            row['capture'] = attributes
        return {**row, 'status': 'pass', 'scope': 'isolated_repeat_iteration' if observation.get('repeat_zone') else 'isolated_nested_socket' if chain else 'isolated_root_core_socket', 'node': node_id, 'socket': socket.identifier, 'result': result}
    except (WorkflowError, RuntimeError, TypeError, ValueError) as error:
        return {**row, 'status': 'unsupported', 'reason': str(error)}
    finally:
        if temporary: bpy.data.objects.remove(temporary, do_unlink=True)
        if copied_data and copied_data.users == 0:
            collection = getattr(bpy.data, {'Mesh': 'meshes', 'Curve': 'curves', 'Curves': 'hair_curves', 'PointCloud': 'pointclouds'}.get(copied_data.bl_rna.identifier, ''), None)
            if collection is not None: collection.remove(copied_data)
        for tree in list(bpy.data.node_groups):
            if tree not in groups_before: bpy.data.node_groups.remove(tree, do_unlink=True)
        bpy.context.view_layer.update()
        if core.tree_fingerprint(modifiers[0].node_group) != before:
            raise WorkflowError('VALIDATION_FAILED', 'Observation modified original graph')
