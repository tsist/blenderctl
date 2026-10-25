"""Versioned, editable signal graphs for ordinary Principled PBR surfaces.

This backend deliberately does not reinterpret the legacy v1/v2 BSDF stacks.
Geometry-dependent masks are baked before assignment so Cycles and Eevee use
the same input. Graph edits are protected; exposed parameter edits merge only
when the incoming recipe has not changed that same parameter.
"""
import copy
import hashlib
import json
import math
import struct
from pathlib import Path

from .contract import WorkflowError

KEY = 'mwp_id'
STATE = 'mwp_state'
MANIFEST = 'mwp_manifest'
CHANNEL_SOCKETS = {
    'base_color': 'Base Color', 'roughness': 'Roughness', 'metallic': 'Metallic',
    'height': 'Height', 'alpha': 'Alpha', 'transmission': 'Transmission Weight',
    'ior': 'IOR', 'coat_weight': 'Coat Weight', 'coat_roughness': 'Coat Roughness',
}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _plain(value):
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    try:
        return [_plain(v) for v in value]
    except TypeError:
        return str(value)


def _sha(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _socket(collection, name):
    matches = [s for s in collection if s.enabled and (s.name == name or s.identifier == name)]
    if len(matches) != 1:
        raise WorkflowError('unsupported_rna', 'Expected one enabled socket: ' + name)
    return matches[0]


def _image_identity(image):
    import array
    pixels = array.array('f', [0]) * len(image.pixels)
    image.pixels.foreach_get(pixels)
    return {'size': list(image.size), 'color_space': image.colorspace_settings.name,
            'pixels_sha256': hashlib.sha256(pixels.tobytes()).hexdigest()}


def _tree_snapshot(tree, mutable=None, stack=()):
    import bpy
    if tree in stack or len(stack) >= 12:
        raise WorkflowError('managed_conflict', 'Recursive shader graph')
    if tree.library or tree.override_library or tree.animation_data:
        raise WorkflowError('protected_material', 'Only local static shader graphs are supported')
    nodes = []
    index = {node: i for i, node in enumerate(tree.nodes)}
    ignored = {'name', 'label', 'location', 'width', 'height', 'dimensions', 'select',
               'show_options', 'show_preview', 'show_texture', 'hide', 'use_custom_color', 'color'}
    for node in tree.nodes:
        row = {'type': node.bl_idname, 'id': node.get('mwp_node'), 'parent': index.get(node.parent),
               'inputs': {}, 'outputs': {}, 'properties': {}}
        for socket in node.inputs:
            if hasattr(socket, 'default_value') and not (mutable and node.name == 'MWP Controls' and socket.name in mutable):
                row['inputs'][socket.identifier] = _plain(socket.default_value)
        for socket in node.outputs:
            if hasattr(socket, 'default_value'):
                row['outputs'][socket.identifier] = _plain(socket.default_value)
        for prop in node.bl_rna.properties:
            if prop.is_readonly or prop.identifier in ignored:
                continue
            if prop.type in {'BOOLEAN', 'INT', 'FLOAT', 'ENUM', 'STRING'}:
                row['properties'][prop.identifier] = _plain(getattr(node, prop.identifier))
            elif prop.type == 'POINTER' and prop.identifier not in {'rna_type', 'parent', 'image', 'node_tree', 'color_ramp'}:
                pointer = getattr(node, prop.identifier, None)
                if isinstance(pointer, bpy.types.ID):
                    row['properties'][prop.identifier] = {'type': pointer.bl_rna.identifier, 'name': pointer.name_full,
                        'library': pointer.library.filepath if pointer.library else None,
                        'matrix_world': _plain(pointer.matrix_world) if isinstance(pointer, bpy.types.Object) else None}
                elif pointer is not None and hasattr(pointer, 'bl_rna'):
                    row['properties'][prop.identifier] = {p.identifier: _plain(getattr(pointer, p.identifier))
                        for p in pointer.bl_rna.properties if not p.is_readonly and p.type in {'BOOLEAN', 'INT', 'FLOAT', 'ENUM', 'STRING'}}
                else:
                    row['properties'][prop.identifier] = None
        # Structural texture/map modes remain protected even when sockets expose
        # the numeric controls. Never whitelist these as mutable parameters.
        for name in ('feature', 'distance', 'noise_type', 'normalize', 'wave_profile',
                     'noise_dimensions', 'voronoi_dimensions', 'interpolation_type', 'data_type'):
            if hasattr(node, name):
                row['properties'][name] = _plain(getattr(node, name))
        if node.type == 'GROUP':
            row['group'] = _tree_snapshot(node.node_tree, stack=stack + (tree,))
        if node.type == 'TEX_IMAGE' and node.image:
            row['image'] = _image_identity(node.image)
        if hasattr(node, 'color_ramp'):
            ramp = node.color_ramp
            row['ramp'] = {'mode': ramp.color_mode, 'interpolation': ramp.interpolation,
                           'hue': ramp.hue_interpolation,
                           'elements': [(e.position, list(e.color)) for e in ramp.elements]}
        nodes.append(row)
    interface = []
    for item in tree.interface.items_tree:
        if item.item_type == 'SOCKET':
            interface.append({'name': item.name, 'direction': item.in_out, 'type': item.socket_type,
                              'default': _plain(item.default_value) if hasattr(item, 'default_value') else None})
    return {'nodes': nodes, 'interface': interface,
            'links': sorted((index[l.from_node], l.from_socket.identifier, index[l.to_node], l.to_socket.identifier)
                            for l in tree.links)}


def _snapshot(material):
    controls = json.loads(material.get('mwp_controls', '{}'))
    snapshot = _tree_snapshot(material.node_tree, controls)
    # Opt in only for the new contract. Old saved snapshot identities remain
    # byte/structure compatible; never rewrite an old snapshot to admit edits.
    if 'surface' in json.loads(material.get(MANIFEST, '{}')):
        snapshot['surface'] = {'thickness_mode': material.thickness_mode}
    return json.loads(_json(snapshot))


def verify_managed_state(material):
    if STATE not in material or MANIFEST not in material:
        return {'ok': False, 'conflicts': ['missing_procedural_state']}
    expected = json.loads(material[STATE])
    current = _snapshot(material)
    return {'ok': current == expected, 'conflicts': [] if current == expected else ['shader_graph_or_image_edited']}


def _get_path(value, path):
    for key in path:
        value = value[key]
    return value


def _set_path(value, path, item):
    parent = value
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = item


def _same_control(a, b):
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same_control(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        from .procedural_contract import _float32
        return _float32(a, 'control') == _float32(b, 'control')
    return a == b


def get_stored_manifest(material):
    from .procedural_contract import normalize_manifest
    if not verify_managed_state(material)['ok']:
        raise WorkflowError('managed_conflict', 'Graph edits cannot be represented by the stored recipe')
    manifest = json.loads(material[MANIFEST])
    # Earlier unpublished 2.0 candidates registered Mapping Scale and the
    # texture's scalar Scale with the same key. Their recipe was not a faithful
    # description of the effective graph. Do not silently migrate or export it.
    for name, path in json.loads(material['mwp_controls']).items():
        if (len(path) == 5 and path[0] == 'signals' and path[-2:] == ['coordinate', 'scale']
                and manifest['signals'][path[1]]['type'] in ('noise', 'wood')
                and name == 'S/' + manifest['signals'][path[1]]['id'] + '/scale'):
            raise WorkflowError('managed_conflict',
                'Unpublished procedural 2.0 coordinate/texture Scale collision; inspect the old graph read-only and rebuild with a new material ID')
    control_node = material.node_tree.nodes.get('MWP Controls')
    for name, path in json.loads(material['mwp_controls']).items():
        value = _plain(control_node.inputs[name].default_value)
        if path[0] == 'signals' and path[-1] == 'seed':
            if value != round(value):
                raise WorkflowError('invalid_parameter', name + ' must remain an integer')
            value = int(round(value))
        _set_path(manifest, path, value)
    return normalize_manifest(manifest)


class Builder:
    def __init__(self, manifest, resource_paths, mesh_maps):
        import bpy
        self.spec = manifest
        self.resources = resource_paths or {}
        self.mesh_maps = mesh_maps
        self.images = []
        self.controls = {}
        self.tree = bpy.data.node_groups.new('MWP/' + manifest['material']['id'], 'ShaderNodeTree')
        self.tree[KEY] = manifest['material']['id']
        self.nodes = self.tree.nodes
        self.links = self.tree.links
        self.inputs = self.new('NodeGroupInput', 'parameters', None, (-1200, 300))
        self.output = self.new('NodeGroupOutput', 'output', None, (1200, 300))
        self.signals = {}

    def new(self, typ, identity, parent=None, location=(0, 0)):
        node = self.nodes.new(typ)
        node.name = 'MWP/' + identity
        node['mwp_node'] = identity
        node.label = identity.split('/')[-1]
        node.parent = parent
        node.location = location
        return node

    def link(self, output, node, socket):
        self.links.new(output, _socket(node.inputs, socket))

    def control(self, key, path, kind=None):
        if key in self.controls and self.controls[key] != path:
            raise WorkflowError('managed_conflict', 'Control key aliases distinct recipe paths: ' + key)
        value = _get_path(self.spec, path)
        if key not in self.controls:
            typ = kind or ('NodeSocketColor' if isinstance(value, list) and len(value) == 4 else
                           'NodeSocketVector' if isinstance(value, list) else 'NodeSocketFloat')
            socket = self.tree.interface.new_socket(name=key, in_out='INPUT', socket_type=typ)
            socket.default_value = value
            self.controls[key] = path
        return self.inputs.outputs[key]

    def constant(self, value, identity, frame):
        node = self.new('ShaderNodeRGB' if isinstance(value, (list, tuple)) else 'ShaderNodeValue', identity, frame)
        node.outputs[0].default_value = value
        return node.outputs[0]

    def coordinate(self, signal, i, frame):
        """Choose vector input or space source, then apply this signal's mapping.

        Shared warped coordinates can therefore feed independently scaled pore,
        ring and fiber textures without losing the per-signal transforms.
        """
        p = signal['parameters'].get('coordinate', {})
        if 'vector' in signal.get('inputs', {}):
            output = self.signals[signal['inputs']['vector']]
        elif p.get('space', 'OBJECT') == 'UV':
            node = self.new('ShaderNodeUVMap', signal['id'] + '/uv', frame, (-650, 0))
            node.uv_map = self.spec['target']['uv_layer']
            output = node.outputs['UV']
        else:
            node = self.new('ShaderNodeTexCoord', signal['id'] + '/object', frame, (-650, 0))
            output = node.outputs['Object']
        mapping = self.new('ShaderNodeMapping', signal['id'] + '/mapping', frame, (-400, 0))
        mapping.vector_type = 'POINT'
        self.link(output, mapping, 'Vector')
        for key, socket, default in [('scale', 'Scale', [1, 1, 1]), ('rotation', 'Rotation', [0, 0, 0]), ('translation', 'Location', [0, 0, 0])]:
            if key in p:
                self.link(self.control('S/' + signal['id'] + '/coordinate/' + key, ['signals', i, 'parameters', 'coordinate', key]), mapping, socket)
            else:
                mapping.inputs[socket].default_value = default
        return mapping.outputs['Vector']

    def image(self, record, identity, frame, color_space):
        import bpy
        path = self.resources.get(record['file'], record['file'])
        if not Path(path).is_file() or _sha(path) != record.get('expected_sha256', record.get('sha256')):
            raise WorkflowError('resource_sha_mismatch', 'Image changed or missing: ' + path)
        image = bpy.data.images.load(str(path), check_existing=False)
        image.colorspace_settings.name = color_space
        image.pack()
        image[KEY] = self.spec['material']['id']
        self.images.append(image)
        node = self.new('ShaderNodeTexImage', identity + '/image', frame)
        node.image = image
        node.interpolation = 'Linear'
        node.extension = 'EXTEND'
        uv = self.new('ShaderNodeUVMap', identity + '/uv', frame, (-200, 0))
        uv.uv_map = self.spec['target']['uv_layer']
        self.link(uv.outputs['UV'], node, 'Vector')
        return node.outputs['Color']

    def math(self, operation, a, b, identity, frame, clamp=False):
        node = self.new('ShaderNodeMath', identity, frame)
        node.operation = operation
        node.use_clamp = clamp
        # Math's repeated Value display names have stable RNA identifiers.
        self.links.new(a, next(s for s in node.inputs if s.identifier == 'Value'))
        self.links.new(b, next(s for s in node.inputs if s.identifier == 'Value_001'))
        return node.outputs['Value']

    def vector_math(self, operation, a, b, identity, frame):
        node = self.new('ShaderNodeVectorMath', identity, frame)
        node.operation = operation
        self.links.new(a, next(s for s in node.inputs if s.identifier == 'Vector'))
        self.links.new(b, next(s for s in node.inputs if s.identifier == 'Vector_001'))
        return node.outputs['Vector']

    def vector_constant(self, value, identity, frame):
        node = self.new('ShaderNodeCombineXYZ', identity, frame)
        for axis, component in zip(('X', 'Y', 'Z'), value):
            node.inputs[axis].default_value = component
        return node.outputs['Vector']

    def noise(self, signal, i, frame, vector, identity, warp=False):
        n = self.new('ShaderNodeTexNoise', identity, frame)
        n.noise_dimensions = '4D'
        n.noise_type = 'FBM'
        n.normalize = True
        keys = [('scale', 'Scale'), ('detail', 'Detail'), ('roughness', 'Roughness'),
                ('lacunarity', 'Lacunarity'), ('seed', 'W')]
        if not warp:
            keys.append(('distortion', 'Distortion'))
        for key, socket in keys:
            self.link(self.control('S/' + signal['id'] + '/' + key, ['signals', i, 'parameters', key]), n, socket)
        self.link(vector, n, 'Vector')
        return n

    def mix(self, factor, a, b, color, identity, frame):
        node = self.new('ShaderNodeMix', identity, frame)
        node.data_type = 'RGBA' if color else 'FLOAT'
        node.clamp_factor = True
        if color:
            node.blend_type = 'MIX'
            node.clamp_result = False
        for name, output in [('Factor', factor), ('A', a), ('B', b)]:
            self.link(output, node, name)
        return _socket(node.outputs, 'Result')

    def build_signal(self, signal, i):
        p, kind, sid = signal['parameters'], signal['type'], signal['id']
        frame = self.new('NodeFrame', 'signal/' + sid, None, ((i % 4) * 1000 - 4000, -(i // 4) * 800))
        frame.label = sid + ' — ' + kind
        refs = signal.get('inputs', {})
        if kind == 'constant':
            output = self.control('S/' + sid + '/value', ['signals', i, 'parameters', 'value'])
        elif kind == 'image':
            output = self.image(p['image'], sid, frame, p['image']['color_space'])
            if p['output'] == 'VALUE':
                n = self.new('ShaderNodeSeparateColor', sid + '/red', frame)
                n.mode = 'RGB'; self.link(output, n, 'Color'); output = n.outputs['Red']
        elif kind == 'coordinate':
            output = self.coordinate(signal, i, frame)
        elif kind == 'warp':
            vector = self.coordinate(signal, i, frame)
            sample_scale = self.control('S/' + sid + '/sample_scale', ['signals', i, 'parameters', 'sample_scale'])
            sample = self.vector_math('MULTIPLY', vector, sample_scale, sid + '/sample', frame)
            noise = self.noise(signal, i, frame, sample, sid + '/noise', warp=True)
            centered = self.vector_math('SUBTRACT', noise.outputs['Color'],
                self.vector_constant([0.5, 0.5, 0.5], sid + '/center', frame), sid + '/centered', frame)
            strength = self.control('S/' + sid + '/strength', ['signals', i, 'parameters', 'strength'])
            displacement = self.vector_math('MULTIPLY', centered, strength, sid + '/displacement', frame)
            output = self.vector_math('ADD', vector, displacement, sid + '/warped', frame)
        elif kind in ('noise', 'wood'):
            vector = self.coordinate(signal, i, frame)
            if kind == 'noise':
                n = self.noise(signal, i, frame, vector, sid + '/noise')
            else:
                n = self.new('ShaderNodeTexWave', sid + '/wood', frame)
                n.wave_type = 'RINGS' if p['rings'] else 'BANDS'
                n.rings_direction = p['direction']; n.bands_direction = p['direction']
                n.wave_profile = p['profile']
                for key, socket in [('scale', 'Scale'), ('detail', 'Detail'), ('detail_scale', 'Detail Scale'), ('roughness', 'Detail Roughness'), ('distortion', 'Distortion'), ('seed', 'Phase Offset')]:
                    self.link(self.control('S/' + sid + '/' + key, ['signals', i, 'parameters', key]), n, socket)
                self.link(vector, n, 'Vector')
            output = _socket(n.outputs, 'Factor')
        elif kind == 'voronoi':
            n = self.new('ShaderNodeTexVoronoi', sid + '/voronoi', frame)
            n.voronoi_dimensions = '3D'; n.feature = p['feature']; n.distance = 'EUCLIDEAN'
            n.normalize = False
            self.link(self.coordinate(signal, i, frame), n, 'Vector')
            for key, socket in [('scale', 'Scale'), ('randomness', 'Randomness')]:
                self.link(self.control('S/' + sid + '/' + key, ['signals', i, 'parameters', key]), n, socket)
            output = n.outputs['Distance']
        elif kind in ('edge', 'cavity'):
            output = self.image(self.mesh_maps[sid], sid, frame, 'Non-Color')
        elif kind == 'height_band':
            vector = self.coordinate(signal, i, frame)
            n = self.new('ShaderNodeSeparateXYZ', sid + '/position', frame)
            self.link(vector, n, 'Vector')
            remap = self.new('ShaderNodeMapRange', sid + '/range', frame)
            remap.clamp = True
            self.link(n.outputs[p['axis']], remap, 'Value')
            for key, socket in [('low', 'From Min'), ('high', 'From Max')]:
                self.link(self.control('S/' + sid + '/' + key, ['signals', i, 'parameters', key]), remap, socket)
            output = remap.outputs['Result']
        elif kind == 'normal_direction':
            geo = self.new('ShaderNodeNewGeometry', sid + '/geometry', frame)
            trans = self.new('ShaderNodeVectorTransform', sid + '/local-normal', frame)
            trans.vector_type = 'NORMAL'; trans.convert_from = 'WORLD'; trans.convert_to = 'OBJECT'
            self.link(geo.outputs['Normal'], trans, 'Vector')
            separate = self.new('ShaderNodeSeparateXYZ', sid + '/direction', frame)
            self.link(trans.outputs['Vector'], separate, 'Vector')
            output = self.math('MULTIPLY', separate.outputs[p['axis']], self.constant(-1 if p['invert'] else 1, sid + '/sign', frame), sid + '/positive', frame, True)
        elif kind == 'math':
            output = self.math(p['operation'], self.signals[refs['a']], self.signals[refs['b']], sid + '/math', frame, p['clamp'])
        elif kind == 'remap':
            n = self.new('ShaderNodeMapRange', sid + '/remap', frame)
            n.data_type = 'FLOAT'; n.interpolation_type = p['interpolation']; n.clamp = p['clamp']
            self.link(self.signals[refs['value']], n, 'Value')
            for key, socket in [('from_min', 'From Min'), ('from_max', 'From Max'), ('to_min', 'To Min'), ('to_max', 'To Max')]:
                self.link(self.control('S/' + sid + '/' + key, ['signals', i, 'parameters', key]), n, socket)
            output = n.outputs['Result']
        elif kind == 'ramp':
            n = self.new('ShaderNodeValToRGB', sid + '/ramp', frame)
            ramp = n.color_ramp; ramp.interpolation = 'LINEAR'
            stops = p['stops']
            for element, stop in zip(ramp.elements, (stops[0], stops[-1])):
                element.position = stop['position']; element.color = stop['color']
            for stop in stops[1:-1]:
                element = ramp.elements.new(stop['position']); element.color = stop['color']
            self.link(self.signals[refs['value']], n, 'Fac'); output = n.outputs['Color']
        elif kind == 'mix':
            from .procedural_contract import signal_types
            output = self.mix(self.signals[refs['factor']], self.signals[refs['a']], self.signals[refs['b']], signal_types(self.spec)[sid] == 'COLOR', sid + '/mix', frame)
        else:
            raise WorkflowError('unsupported_source', kind)
        self.signals[sid] = output

    def build(self):
        import bpy
        from .procedural_contract import signal_order
        lookup = {signal['id']: (i, signal) for i, signal in enumerate(self.spec['signals'])}
        for identity in signal_order(self.spec):
            # The contract returns IDs; accept normalized records for discovery helpers.
            i, signal = lookup[identity if isinstance(identity, str) else identity['id']]
            self.build_signal(signal, i)
        final = {}
        for index, layer in enumerate(self.spec['layers']):
            frame = self.new('NodeFrame', 'layer/' + layer['id'], None, (index * 1200, 800))
            frame.label = layer['name']
            if not layer['enabled']:
                continue
            channels = {}
            for channel in CHANNEL_SOCKETS:
                if channel in layer['channels']:
                    channels[channel] = self.signals[layer['channels'][channel]]
                else:
                    channels[channel] = self.control('L/' + layer['id'] + '/' + channel, ['layers', index, 'values', channel])
            if not final:
                final = channels
            else:
                factor = self.control('L/' + layer['id'] + '/opacity', ['layers', index, 'opacity'])
                if layer['mask']:
                    factor = self.math('MULTIPLY', factor, self.signals[layer['mask']], layer['id'] + '/mask', frame, True)
                for channel in CHANNEL_SOCKETS:
                    final[channel] = self.mix(factor, final[channel], channels[channel], channel == 'base_color', layer['id'] + '/' + channel, frame)
        for channel, socket in CHANNEL_SOCKETS.items():
            out = self.tree.interface.new_socket(name=channel, in_out='OUTPUT', socket_type='NodeSocketColor' if channel == 'base_color' else 'NodeSocketFloat')
            self.links.new(final[channel], self.output.inputs[out.name])
        if 'thickness' in self.spec.get('surface', {}):
            control = self.control('Surface/thickness', ['surface', 'thickness'])
            item = next(item for item in self.tree.interface.items_tree
                        if item.item_type == 'SOCKET' and item.in_out == 'INPUT'
                        and item.name == 'Surface/thickness')
            item.min_value = 0; item.max_value = 1000
            out = self.tree.interface.new_socket(name='surface_thickness', in_out='OUTPUT', socket_type='NodeSocketFloat')
            self.links.new(control, self.output.inputs[out.name])
        return self.tree


def _target(manifest):
    import bpy
    context, target = manifest['context'], manifest['target']
    scene = bpy.data.scenes.get(context['scene'])
    if not scene or context['view_layer'] not in scene.view_layers:
        raise WorkflowError('invalid_context', 'Explicit scene/view layer is missing')
    obj = scene.objects.get(target['object'])
    if obj is None or obj.type != 'MESH' or obj.name not in scene.view_layers[context['view_layer']].objects:
        raise WorkflowError('invalid_target', 'Explicit mesh target is missing from the view layer')
    if obj.library or obj.override_library or obj.data.library or obj.data.override_library or obj.mode != 'OBJECT':
        raise WorkflowError('protected_target', 'Local Object Mode mesh required')
    if obj.animation_data or obj.data.animation_data or obj.data.shape_keys:
        raise WorkflowError('unsupported_target', 'Static mesh without shape keys required')
    if any(m.type in {'CLOTH', 'FLUID', 'SOFT_BODY', 'PARTICLE_SYSTEM', 'DYNAMIC_PAINT'} for m in obj.modifiers):
        raise WorkflowError('unsupported_target', 'Simulation modifiers require a static modeling handoff')
    if not obj.data.polygons or target['uv_layer'] not in obj.data.uv_layers:
        raise WorkflowError('missing_uv', 'Nonempty mesh and explicit UV layer required')
    if any(not math.isfinite(v) or abs(v) < 1e-8 for v in obj.scale):
        raise WorkflowError('invalid_scale', 'Finite nonzero object scale required')
    if target['material_slot'] >= len(obj.material_slots) and obj.data.users > 1:
        raise WorkflowError('shared_slot', 'Cannot add a material slot to shared mesh data')
    return obj


def apply_material(manifest, resource_paths=None, work_dir=None):
    import bpy
    from .procedural_contract import normalize_manifest
    manifest = normalize_manifest(manifest)
    obj = _target(manifest)
    slot = manifest['target']['material_slot']
    previous = [(s.link, s.material) for s in obj.material_slots]
    existing = [m for m in bpy.data.materials if m.get(KEY) == manifest['material']['id']]
    if len(existing) > 1:
        raise WorkflowError('managed_conflict', 'Duplicate procedural material identity')
    old = existing[0] if existing else None
    if old:
        if old.use_fake_user:
            raise WorkflowError('protected_material', 'Retained material has Fake User; use a new recipe material ID for a new version')
        if old.library or old.override_library or old.animation_data:
            raise WorkflowError('protected_material', 'Static local material required')
        if slot >= len(obj.material_slots) or obj.material_slots[slot].material != old or obj.material_slots[slot].link != 'OBJECT':
            raise WorkflowError('managed_conflict', 'Material binding was changed')
        if any(s.material == old and (other != obj or i != slot) for other in bpy.data.objects for i, s in enumerate(other.material_slots)):
            raise WorkflowError('shared_material', 'Shared managed material must first be isolated')
        live = get_stored_manifest(old)
        baseline = json.loads(old[MANIFEST])
        if baseline['target'] != manifest['target']:
            raise WorkflowError('managed_conflict', 'Binding migration requires a new material ID')
        for name, path in json.loads(old['mwp_controls']).items():
            base_value, live_value = _get_path(baseline, path), _get_path(live, path)
            if _same_control(live_value, base_value):
                continue
            try:
                incoming = _get_path(manifest, path)
                # Reordering/removing a signal or layer must not transplant a manual edit.
                if len(path) > 2 and path[0] in ('signals', 'layers') and manifest[path[0]][path[1]]['id'] != baseline[path[0]][path[1]]['id']:
                    raise KeyError(name)
            except (KeyError, IndexError):
                raise WorkflowError('managed_conflict', 'Removing or reordering edited control: ' + name)
            if not _same_control(incoming, base_value) and not _same_control(incoming, live_value):
                raise WorkflowError('managed_conflict', 'Manual parameter overlaps incoming recipe: ' + name)
            _set_path(manifest, path, live_value)
        manifest = normalize_manifest(manifest)
    requests = [{k: s[k] for k in ('id', 'type', 'parameters')} for s in manifest['signals'] if s['type'] in ('edge', 'cavity')]
    maps = {}
    map_report = None
    if requests:
        if not work_dir:
            raise WorkflowError('missing_output', 'Geometry signals need an owned work directory')
        from .procedural_mesh_maps import bake_mesh_maps
        map_report = bake_mesh_maps(obj, manifest['target']['uv_layer'], requests, str(Path(work_dir) / 'mesh_maps'), size=manifest['mesh_map_size'])
        maps = map_report.get('maps', map_report)
    builder = Builder(manifest, resource_paths, maps)
    material = None
    committed = False
    try:
        group = builder.build()
        material = bpy.data.materials.new(manifest['material']['name'])
        material.use_nodes = True
        material.node_tree.nodes.clear()
        material[KEY] = manifest['material']['id']
        material['mwp_group_name'] = group.name
        material['mwp_height_distance'] = manifest['height_distance']
        material['mwp_controls'] = _json(builder.controls)
        material[MANIFEST] = _json(manifest)
        node = material.node_tree.nodes.new('ShaderNodeGroup')
        node.node_tree = group; node.name = 'MWP Controls'; node.label = 'Surface controls'; node.width = 320
        node['mwp_node'] = 'controls'
        output = material.node_tree.nodes.new('ShaderNodeOutputMaterial')
        output['mwp_node'] = 'output'; output.location = (1100, 100)
        shader = material.node_tree.nodes.new('ShaderNodeBsdfPrincipled')
        shader['mwp_node'] = 'surface'; shader.location = (750, 100)
        material.node_tree.links.new(shader.outputs['BSDF'], output.inputs['Surface'])
        if 'surface' in manifest:
            material.thickness_mode = manifest['surface']['thickness_mode']
            if 'thickness' in manifest['surface']:
                # A connected zero is deliberately different from AUTO.
                material.node_tree.links.new(node.outputs['surface_thickness'], output.inputs['Thickness'])
        bump = material.node_tree.nodes.new('ShaderNodeBump')
        bump['mwp_node'] = 'height'; bump.location = (500, -500)
        bump.inputs['Distance'].default_value = manifest['height_distance']
        bump.inputs['Strength'].default_value = 1
        for normal_socket in ('Normal', 'Coat Normal'):
            material.node_tree.links.new(bump.outputs['Normal'], shader.inputs[normal_socket])
        for i, channel in enumerate(CHANNEL_SOCKETS):
            reroute = material.node_tree.nodes.new('NodeReroute')
            reroute.name = 'MWP/' + channel; reroute.label = channel
            reroute['mwp_channel'] = channel; reroute['mwp_node'] = 'channel/' + channel
            reroute.location = (450, -100 - i * 45)
            material.node_tree.links.new(node.outputs[channel], reroute.inputs[0])
            if channel == 'height':
                material.node_tree.links.new(reroute.outputs[0], bump.inputs['Height'])
            else:
                material.node_tree.links.new(reroute.outputs[0], shader.inputs[CHANNEL_SOCKETS[channel]])
        if hasattr(material, 'use_raytrace_refraction'):
            material.use_raytrace_refraction = True
        while len(obj.material_slots) <= slot:
            obj.data.materials.append(None)
        obj.material_slots[slot].link = 'OBJECT'
        obj.material_slots[slot].material = material
        material[STATE] = _json(_snapshot(material))
        report = {'material_name': material.name, 'material_id': manifest['material']['id'],
                  'target': copy.deepcopy(manifest['target']), 'group_name': group.name,
                  'generated_images': [{'name': image.name, 'file': bpy.path.abspath(image.filepath)} for image in builder.images],
                  'mesh_maps': map_report, 'reused': bool(old),
                  'counts': {'nodes': len(group.nodes), 'signals': len(manifest['signals']), 'layers': len(manifest['layers']), 'images': len(builder.images)},
                  'limitations': ['Static single-Principled surface-channel compositing, not arbitrary BSDF-stack equivalence',
                                 'Object coordinates are local units; specify scale for differently sized models',
                                 'Geometry maps are regenerated per invocation; no stale cross-asset cache reuse']}
        committed = True
        if old and old.users == 0:
            old_control = old.node_tree.nodes.get('MWP Controls')
            old_group = old_control.node_tree if old_control and old_control.type == 'GROUP' else None
            bpy.data.materials.remove(old)
            if old_group and old_group.users == 0 and old_group.get(KEY) == manifest['material']['id']:
                bpy.data.node_groups.remove(old_group)
        return report
    finally:
        if not committed:
            while len(obj.material_slots) > len(previous):
                obj.data.materials.pop(index=len(obj.material_slots) - 1)
            for i, (link, bound) in enumerate(previous):
                obj.material_slots[i].link = link; obj.material_slots[i].material = bound
            if material:
                bpy.data.materials.remove(material)
            if builder.tree.users == 0:
                bpy.data.node_groups.remove(builder.tree)
            for image in builder.images:
                if image.users == 0:
                    bpy.data.images.remove(image)


def save_template(material, path):
    """Save a reusable recipe, including representable live control edits."""
    manifest = get_stored_manifest(material)
    destination = Path(path)
    with destination.open('x', encoding='utf8') as stream:
        json.dump({'schema': 'material-procedural-template-1.0', 'manifest': manifest}, stream, ensure_ascii=False, indent=2)
    return {'file': str(destination.resolve()), 'sha256': _sha(destination)}
