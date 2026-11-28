"""Bind verified geometry maps and editable effects to an existing Principled material."""
import json
from pathlib import Path

from .procedural_bake import _image_header, _sha
from .procedural_mesh_maps import _mesh_fingerprint
from .projection_bake import CHANNELS, _fail, _geometry_record


PROJECTION_NODE = 'MW Projection Effects'
EFFECT_CONTROLS = {
    'wear': ('Show Wear', 'Wear', 'Wear Range', 'Exposed Color'),
    'dust': ('Show Dust', 'Dust', 'Dust Range', 'Dust Color'),
}


def read_projection(manifest):
    path = Path(manifest).resolve()
    report = json.loads(path.read_text(encoding='utf-8'))
    if report.get('schema') != 'mw-projection-1.0' or report.get('status') != 'complete':
        _fail('A complete projection manifest is required')
    if set(report.get('maps', {})) != set(CHANNELS):
        _fail('The projection must contain Normal, Curvature and AO')
    files = {}
    for channel, entry in report['maps'].items():
        filename = entry.get('file')
        if filename != channel + '.png' or not entry.get('decoded') or entry.get('color_space') != 'Non-Color':
            _fail('Invalid map entry: ' + channel)
        image = path.with_name(filename)
        if image.resolve().parent != path.parent or not image.is_file() or _sha(image) != entry.get('sha256'):
            _fail('Missing or changed projection map: ' + str(image))
        header = _image_header(image)
        if (header['format'] != 'PNG' or header['bit_depth'] != '16'
                or [header['width'], header['height']] != [entry['width'], entry['height']]):
            _fail('Projection map encoding changed: ' + str(image))
        files[channel] = image
    return report, files


def _input(group, name, kind, default, maximum=1):
    socket = group.interface.new_socket(name=name, in_out='INPUT', socket_type=kind)
    socket.default_value = default
    if kind == 'NodeSocketFloat':
        socket.min_value, socket.max_value = 0, maximum


def _effect_group(group, uv_name, images):
    _input(group, 'Base Color', 'NodeSocketColor', (.8, .8, .8, 1))
    _input(group, 'Roughness', 'NodeSocketFloat', .5)
    _input(group, 'Metallic', 'NodeSocketFloat', 0)
    _input(group, 'Show Wear', 'NodeSocketBool', False)
    _input(group, 'Wear', 'NodeSocketFloat', .65)
    _input(group, 'Wear Range', 'NodeSocketFloat', .35)
    _input(group, 'Exposed Color', 'NodeSocketColor', (.48, .50, .53, 1))
    _input(group, 'Show Dust', 'NodeSocketBool', False)
    _input(group, 'Dust', 'NodeSocketFloat', .6)
    _input(group, 'Dust Range', 'NodeSocketFloat', .50)
    _input(group, 'Dust Color', 'NodeSocketColor', (.18, .14, .10, 1))
    for name, kind in [('Base Color', 'Color'), ('Roughness', 'Float'),
                       ('Metallic', 'Float'), ('Normal', 'Vector')]:
        group.interface.new_socket(name=name, in_out='OUTPUT', socket_type='NodeSocket' + kind)
    nodes, links = group.nodes, group.links
    inputs = nodes.new('NodeGroupInput')
    inputs.location = (-900, 220)
    outputs = nodes.new('NodeGroupOutput')
    outputs.location = (900, 220)
    uv = nodes.new('ShaderNodeUVMap')
    uv.uv_map = uv_name
    uv.location = (-940, -450)
    textures = {}
    for index, (channel, image) in enumerate(images.items()):
        node = nodes.new('ShaderNodeTexImage')
        node.name = 'MW ' + channel
        node.label = channel.capitalize()
        node.image = image
        node.location = (-710, 160 - 320 * index)
        links.new(uv.outputs['UV'], node.inputs['Vector'])
        textures[channel] = node
    normal = nodes.new('ShaderNodeNormalMap')
    normal.space = 'TANGENT'
    normal.uv_map = uv_name
    normal.location = (440, -540)
    links.new(textures['normal'].outputs['Color'], normal.inputs['Color'])
    links.new(normal.outputs['Normal'], outputs.inputs['Normal'])

    def math_node(operation, left, right, x, y, label):
        node = nodes.new('ShaderNodeMath')
        node.operation = operation
        node.label = label
        node.location = (x, y)
        for socket, source in zip(node.inputs, (left, right)):
            if isinstance(source, (int, float)):
                socket.default_value = source
            else:
                links.new(source, socket)
        return node.outputs[0]

    def mask(value, range_, strength, y, threshold):
        # Increase range to lower the minimum curvature / occlusion required.
        shift = math_node('MULTIPLY', range_, 0.65, -470, y, 'Coverage range')
        start = math_node('SUBTRACT', threshold, shift, -270, y, 'Minimum signal')
        end = math_node('ADD', start, .23, -60, y - 220, 'Feather end')
        remap = nodes.new('ShaderNodeMapRange')
        remap.interpolation_type = 'SMOOTHSTEP'
        remap.clamp = True
        remap.location = (-70, y)
        for source, name in [(value, 'Value'), (start, 'From Min'), (end, 'From Max')]:
            links.new(source, remap.inputs[name])
        return math_node('MULTIPLY', remap.outputs['Result'], strength, 150, y, 'Layer strength')

    cavity = math_node('SUBTRACT', 1, textures['ao'].outputs['Color'], -480, -450, 'Inverse ambient visibility')
    wear_strength = math_node('MULTIPLY', inputs.outputs['Show Wear'], inputs.outputs['Wear'],
                              -470, 470, 'Wear enabled')
    dust_strength = math_node('MULTIPLY', inputs.outputs['Show Dust'], inputs.outputs['Dust'],
                              -470, -700, 'Dust enabled')
    wear = mask(textures['curvature'].outputs['Color'], inputs.outputs['Wear Range'], wear_strength, 260, .68)
    dust = mask(cavity, inputs.outputs['Dust Range'], dust_strength, -120, .72)

    def mix(factor, original, coating, x, y, is_color=False):
        node = nodes.new('ShaderNodeMix')
        node.data_type = 'RGBA' if is_color else 'FLOAT'
        node.location = (x, y)
        sockets = [socket for socket in node.inputs if socket.enabled]
        for socket, source in zip(sockets, (factor, original, coating)):
            if isinstance(source, (int, float)):
                socket.default_value = source
            else:
                links.new(source, socket)
        return next(socket for socket in node.outputs if socket.enabled)

    for index, (name, bare, dusty) in enumerate([
        ('Base Color', inputs.outputs['Exposed Color'], inputs.outputs['Dust Color']),
        ('Roughness', .27, .86), ('Metallic', .95, 0),
    ]):
        y = 710 - 255 * index
        exposed = mix(wear, inputs.outputs[name], bare, 400, y, index == 0)
        layered = mix(dust, exposed, dusty, 630, y, index == 0)
        links.new(layered, outputs.inputs[name])


def _principled(material):
    if not material or not material.use_nodes:
        _fail('All target slots must have a node-based Principled material')
    tree = material.node_tree
    outputs = [node for node in tree.nodes if node.type == 'OUTPUT_MATERIAL' and node.is_active_output]
    if len(outputs) != 1 or outputs[0].target != 'ALL':
        _fail(f'{material.name}: use one active material output for all engines')
    surface = outputs[0].inputs['Surface'].links
    if len(surface) != 1 or surface[0].from_node.type != 'BSDF_PRINCIPLED':
        _fail(f'{material.name}: the surface must be a directly connected Principled BSDF')
    shader = surface[0].from_node
    effects = tree.nodes.get(PROJECTION_NODE)
    if effects and (effects.type != 'GROUP' or not effects.get('mw_projection_effects')):
        _fail(f'{material.name}: conflicting node name {PROJECTION_NODE}')
    normal = shader.inputs['Normal'].links
    if normal and (not effects or normal[0].from_node != effects):
        _fail(f'{material.name}: the existing Normal connection needs a composition decision')
    return shader, effects


def validate_material_slots(target):
    if not target.material_slots:
        _fail('Target must have a Principled material')
    for slot in target.material_slots:
        _principled(slot.material)


def effect_controls(material):
    if material and material.use_nodes:
        node = material.node_tree.nodes.get(PROJECTION_NODE)
        if node and node.get('mw_projection_effects'):
            return node.inputs
    return None


def verify_projection_binding(target, manifest):
    """Require the delivered manifest to name the maps used by the target."""
    import bpy

    report, files = read_projection(manifest)
    if target is None or target.name != report['target'] or not target.material_slots:
        _fail('The delivery manifest does not match its low mesh')
    for material in {slot.material for slot in target.material_slots}:
        shader, effect = _principled(material)
        if effect is None or effect.node_tree is None:
            _fail(f'{target.name}: 接入已烘焙清单后再另存交付')
        for channel, path in files.items():
            node = effect.node_tree.nodes.get('MW ' + channel)
            image = node.image if node and node.type == 'TEX_IMAGE' else None
            if image is None or Path(bpy.path.abspath(image.filepath, library=image.library)).resolve() != path:
                _fail(f'{target.name}: 材质与已选清单的 {channel} 并不一致，请重新接入')
        for channel in ('Base Color', 'Roughness', 'Metallic', 'Normal'):
            links = shader.inputs[channel].links
            if len(links) != 1 or links[0].from_node != effect or links[0].from_socket.name != channel:
                _fail(f'{target.name}: 检查投射效果与 Principled 的 {channel} 接线')
    return report


def _release_replaced_effects(materials):
    """Release superseded copies when no Blender datablock still uses them."""
    import bpy

    groups = set()
    for material in materials:
        if material.users or not effect_controls(material):
            continue
        group = material.node_tree.nodes[PROJECTION_NODE].node_tree
        if group is not None:
            groups.add(group)
        bpy.data.materials.remove(material)
    images = set()
    for group in groups:
        if group.users:
            continue
        for channel in CHANNELS:
            node = group.nodes.get('MW ' + channel)
            if node and node.type == 'TEX_IMAGE' and node.image:
                images.add(node.image)
        bpy.data.node_groups.remove(group)
    for image in images:
        if not image.users:
            bpy.data.images.remove(image)


def verify_projection_inputs(context, target, report, *, sources=None, cage=None):
    """Reject stale maps against the destination and any explicitly supplied inputs."""
    import bpy

    if target.type != 'MESH' or target.name != report['target']:
        _fail('Projection target differs from the manifest')
    uv_name = report['uv_layer']
    depsgraph = context.evaluated_depsgraph_get()
    evaluated = target.evaluated_get(depsgraph)
    mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=depsgraph)
    try:
        mesh.materials.clear()
        fingerprint = _mesh_fingerprint(mesh, uv_name)
    finally:
        bpy.data.meshes.remove(mesh)
    if (fingerprint['sha256'] != report['identity']['target']['sha256']
            or [list(row) for row in evaluated.matrix_world] != report['identity']['target_transform']):
        _fail('The target geometry, UV, normals or world transform changed. Re-bake its maps.')
    if sources is None:
        return
    identity = report['identity']
    if len(sources) != len(identity['sources']) or {obj.name for obj in sources} != set(identity['sources']):
        _fail('The confirmed contributors changed. Re-bake the projection.')
    expected_cage = identity.get('cage')
    if (cage.name if cage else None) != (expected_cage['name'] if expected_cage else None):
        _fail('The confirmed cage changed. Re-bake the projection.')
    expected = [(obj, identity['sources'][obj.name]) for obj in sources]
    if cage:
        expected.append((cage, {key: value for key, value in expected_cage.items() if key != 'name'}))
    for obj, record in expected:
        evaluated = obj.evaluated_get(depsgraph)
        mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=depsgraph)
        try:
            if _geometry_record(mesh, evaluated.matrix_world) != record:
                _fail(f'{obj.name}: contributor or cage geometry changed. Re-bake the projection.')
        finally:
            bpy.data.meshes.remove(mesh)


def attach_projection(context, target, manifest, *, sources=None, cage=None):
    """Attach independently verified maps without mutating shared source materials."""
    import bpy

    report, files = read_projection(manifest)
    verify_projection_inputs(context, target, report, sources=sources, cage=cage)
    uv_name = report['uv_layer']
    validate_material_slots(target)
    slots = list(target.material_slots)

    images, candidates = {}, {}
    group = None
    created_groups = []
    committed = False
    try:
        for channel, path in files.items():
            image = bpy.data.images.load(str(path), check_existing=False)
            images[channel] = image
            image.colorspace_settings.name = 'Non-Color'
            entry = report['maps'][channel]
            if list(image.size) != [entry['width'], entry['height']] or not image.has_data:
                _fail('Unable to decode ' + str(path))
            if bpy.data.filepath:
                image.filepath = bpy.path.relpath(str(path))
        group = bpy.data.node_groups.new('Projection maps | ' + target.name, 'ShaderNodeTree')
        created_groups.append(group)
        _effect_group(group, uv_name, images)
        for slot in slots:
            old = slot.material
            if old in candidates:
                continue
            material = old.copy()
            candidates[old] = material
            shader, node = _principled(material)
            tree = material.node_tree
            if node is not None:
                # Group sockets from a previous run are kept linked, while
                # only the group's verified map resources are replaced.
                existing = node.node_tree.copy()
                created_groups.append(existing)
                node.node_tree = existing
                for channel, image in images.items():
                    existing.nodes['MW ' + channel].image = image
                for internal in existing.nodes:
                    if internal.type in {'UVMAP', 'NORMAL_MAP'}:
                        internal.uv_map = uv_name
                continue
            material.name = old.name + ' | Projected'
            node = tree.nodes.new('ShaderNodeGroup')
            node.node_tree = group
            node.name = PROJECTION_NODE
            node.label = 'Projection | wear & recess dust'
            node['mw_projection_effects'] = True
            node.location = (shader.location.x - 280, shader.location.y - 330)
            for name in ('Base Color', 'Roughness', 'Metallic'):
                socket = shader.inputs[name]
                node.inputs[name].default_value = socket.default_value
                if socket.is_linked:
                    tree.links.new(socket.links[0].from_socket, node.inputs[name])
                tree.links.new(node.outputs[name], socket)
            tree.links.new(node.outputs['Normal'], shader.inputs['Normal'])
        original_slots = [(slot, slot.link, slot.material) for slot in slots]
        try:
            for slot, _, original in original_slots:
                slot.link = 'OBJECT'
                slot.material = candidates[original]
        except Exception:
            for slot, link, _ in original_slots:
                slot.link = link
            for slot, link, material in original_slots:
                if link == 'OBJECT':
                    slot.material = material
            raise
        committed = True
    finally:
        if not committed:
            for material in candidates.values():
                bpy.data.materials.remove(material)
        for created in created_groups:
            if created.users == 0:
                bpy.data.node_groups.remove(created)
        if not committed:
            for image in images.values():
                bpy.data.images.remove(image)
    _release_replaced_effects(candidates)
    return report
