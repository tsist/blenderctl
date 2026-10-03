# SPDX-License-Identifier: GPL-3.0-or-later
"""Read uniform simulation states through temporary native POINT attributes.

The caller must reopen its immutable source after capture and configure the
actual bake again. This module never reads cache metadata or imports zone_cache.
"""
import math
from pathlib import Path
import uuid

import bpy

import node_zones
from protocol import Failure, atomic_json


def _read_state(mesh, attribute_name, item):
    attribute = mesh.attributes.get(attribute_name)
    data_type = 'FLOAT' if item['type'] == 'FLOAT' else 'FLOAT_VECTOR'
    if not attribute or attribute.domain != 'POINT' or attribute.data_type != data_type or len(attribute.data) != len(mesh.vertices) or not mesh.vertices:
        raise Failure('VALIDATION_FAILED', 'Native state oracle requires a nonempty POINT state attribute')
    values = [float(row.value) if item['type'] == 'FLOAT' else [float(v) for v in row.vector] for row in attribute.data]
    if any(not math.isfinite(v) for value in values for v in ([value] if item['type'] == 'FLOAT' else value)):
        raise Failure('VALIDATION_FAILED', 'Nonfinite native simulation state: ' + item['name'])
    if any(value != values[0] for value in values[1:]):
        raise Failure('VALIDATION_FAILED', 'Field-valued simulation state needs a separate adapter: ' + item['name'])
    return values[0]


def capture(resolved, spec, job):
    """Return native states in object / original bake ID / frame / item order."""
    job = Path(job).resolve()
    frames = list(range(spec['frame_start'], spec['frame_end'] + 1))
    if not 2 <= len(frames) <= 32:
        raise Failure('UNSUPPORTED', 'Native state oracle requires 2..32 frames')
    prepared = []
    temporary_groups = []
    temporary_modifiers = []
    originals = []
    try:
        for obj, modifier in sorted(resolved, key=lambda pair: pair[0].name):
            tree = modifier.node_group
            if modifier.type != 'NODES' or not tree:
                raise Failure('UNSUPPORTED', 'Native state oracle requires geometry node modifiers')
            cache_path = Path(bpy.path.abspath(modifier.bake_directory)).resolve()
            if modifier.bake_target != 'DISK' or not cache_path.is_relative_to(job) or cache_path == job or (cache_path.exists() and (not cache_path.is_dir() or any(cache_path.iterdir()))):
                raise Failure('CONFLICT', 'Native state oracle requires a new empty DISK cache directory inside its job')
            simulations = [node for node in tree.nodes if node.bl_idname == 'GeometryNodeSimulationOutput']
            if len(simulations) != 1 or len(modifier.bakes) != 1:
                raise Failure('UNSUPPORTED', 'Native state oracle requires one root simulation per modifier')
            bake = modifier.bakes[0]
            if not bake.node or bake.node != simulations[0] or bake.use_custom_path or bake.bake_target != 'INHERIT':
                raise Failure('CONFLICT', 'Native state oracle bake identity/path differs from its root simulation')
            node_zones.validate_tree(tree)
            items = node_zones.item_records(simulations[0])
            if any(item['type'] not in ('GEOMETRY', 'FLOAT', 'VECTOR') for item in items):
                raise Failure('UNSUPPORTED', 'Native state oracle supports geometry, float and vector states')
            original = {'object': obj.name, 'modifier': modifier.name, 'node': bake.node.name, 'bake_id': bake.bake_id}
            interface_values = []
            for socket in tree.interface.items_tree:
                if socket.item_type != 'SOCKET' or socket.in_out != 'INPUT':
                    continue
                source_input = getattr(modifier.properties.inputs, socket.identifier, None)
                if source_input is None or not hasattr(source_input, 'value'):
                    continue
                if getattr(source_input, 'type', 'VALUE') != 'VALUE':
                    raise Failure('UNSUPPORTED', 'Native state oracle requires VALUE modifier interface inputs: ' + socket.name)
                interface_values.append((socket.identifier, source_input.value))
            copied = tree.copy()
            temporary_groups.append(copied)
            originals.append((modifier, modifier.show_viewport, modifier.show_render))
            index = list(obj.modifiers).index(modifier)
            modifier.show_viewport = False
            modifier.show_render = False
            temporary = obj.modifiers.new('__StateOracle_' + uuid.uuid4().hex, 'NODES')
            temporary_modifiers.append((obj, temporary))
            temporary.node_group = copied
            temporary.bake_target = 'DISK'
            temporary.bake_directory = str(cache_path)
            for identifier, value in interface_values:
                target_input = getattr(temporary.properties.inputs, identifier, None)
                if target_input is None or not hasattr(target_input, 'value'):
                    raise Failure('VALIDATION_FAILED', 'Copied modifier interface identifier differs from original')
                if hasattr(target_input, 'type'):
                    target_input.type = 'VALUE'
                target_input.value = value
            for temporary_bake in temporary.bakes:
                temporary_bake.use_custom_path = False
                temporary_bake.directory = ''
                temporary_bake.bake_target = 'INHERIT'
                temporary_bake.bake_mode = 'ANIMATION'
                temporary_bake.use_custom_simulation_frame_range = True
                temporary_bake.frame_start = spec['frame_start']
                temporary_bake.frame_end = spec['frame_end']
            obj.modifiers.move(len(obj.modifiers) - 1, index)
            output = copied.nodes.get(original['node'])
            terminals = [node for node in copied.nodes if node.bl_idname == 'NodeGroupOutput']
            geometry_outputs = [socket for state, socket in node_zones.state_items(output) if state.socket_type == 'GEOMETRY']
            if len(terminals) != 1 or len(geometry_outputs) != 1:
                raise Failure('UNSUPPORTED', 'Native state oracle requires one Group Output and one geometry state')
            terminal_sockets = [socket for socket in terminals[0].inputs if socket.type == 'GEOMETRY']
            if len(terminal_sockets) != 1:
                raise Failure('UNSUPPORTED', 'Native state oracle requires one group geometry output')
            terminal = terminal_sockets[0]
            for link in list(terminal.links):
                copied.links.remove(link)
            previous = geometry_outputs[0]
            observed = []
            token = uuid.uuid4().hex
            for index, item in enumerate(items):
                if item['type'] == 'GEOMETRY':
                    continue
                sockets = [socket for socket in output.outputs if socket.identifier == item['identifier']]
                if len(sockets) != 1:
                    raise Failure('VALIDATION_FAILED', 'Copied state socket identifier differs from original')
                attribute_name = '__state_oracle_' + token + '_' + str(index)
                store = copied.nodes.new('GeometryNodeStoreNamedAttribute')
                store.data_type = 'FLOAT' if item['type'] == 'FLOAT' else 'FLOAT_VECTOR'
                store.domain = 'POINT'
                store.inputs['Name'].default_value = attribute_name
                store.inputs['Selection'].default_value = True
                copied.links.new(previous, store.inputs['Geometry'])
                copied.links.new(sockets[0], store.inputs['Value'])
                previous = store.outputs['Geometry']
                observed.append((attribute_name, {key: item[key] for key in ('identifier', 'name', 'type')}))
            copied.links.new(previous, terminal)
            copied.update_tag()
            obj.update_tag()
            prepared.append({'object': obj, 'identity': original, 'attributes': observed, 'states': []})
        # Packed state belongs to the modifier, so each temporary group also
        # needs a fresh modifier. Advance once per frame for native history.
        for frame in frames:
            bpy.context.scene.frame_set(frame)
            bpy.context.view_layer.update()
            graph = bpy.context.evaluated_depsgraph_get()
            for row in prepared:
                evaluated = row['object'].evaluated_get(graph)
                mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=graph)
                try:
                    values = [{**item, 'value': _read_state(mesh, name, item)} for name, item in row['attributes']]
                    row['states'].append({**row['identity'], 'frame': frame, 'items': values})
                finally:
                    evaluated.to_mesh_clear()
        states = [state for row in prepared for state in row['states']]
        atomic_json(job / 'zone-state-oracle.json', states)
        return states
    finally:
        for obj, modifier in reversed(temporary_modifiers):
            obj.modifiers.remove(modifier)
        for modifier, viewport, render in reversed(originals):
            modifier.show_viewport = viewport
            modifier.show_render = render
            modifier.id_data.update_tag()
        for tree in reversed(temporary_groups):
            bpy.data.node_groups.remove(tree)
