"""Blender 5.3 lighting controls for managed rigs."""

from hashlib import sha256
import math
from pathlib import Path

from . import core


def validate(scene, manifest):
    bpy = core._bpy()
    prefix = 'LW.' + manifest['rig_id']
    if 'fog' in manifest:
        for name, group in (('.Fog', bpy.data.objects), ('.Fog.Mesh', bpy.data.meshes),
                            ('.Fog.Material', bpy.data.materials)):
            core._fresh(group, prefix + name)
    for row in manifest['lights']:
        name = prefix + '.' + row['id']
        if 'follow_bone' in row:
            spec = row['follow_bone']
            armature = scene.objects.get(spec['armature'])
            if armature is None or armature.type != 'ARMATURE' or spec['bone'] not in armature.pose.bones:
                core._fail('NOT_FOUND', 'Bone anchor must be in the target scene: ' + str(spec))
        for key, suffix, group, maximum in (
            ('ies', '.IES', bpy.data.texts, 1024 * 1024),
            ('gobo', '.Gobo', bpy.data.images, 128 * 1024 * 1024),
        ):
            if key not in row:
                continue
            core._fresh(group, name + suffix)
            path = Path(row[key]['file'])
            if not path.is_file() or not 1 <= path.stat().st_size <= maximum:
                core._fail('NOT_FOUND', 'Lighting profile file unavailable or outside size limit: ' + str(path))
            if key == 'ies':
                _ies_text(row[key])
            elif sha256(path.read_bytes()).hexdigest() != row[key]['expected_sha256']:
                core._fail('CONFLICT', 'Gobo SHA mismatch: ' + str(path))


def _ies_text(spec):
    raw = Path(spec['file']).read_bytes()
    if sha256(raw).hexdigest() != spec['expected_sha256']:
        core._fail('CONFLICT', 'IES SHA mismatch: ' + spec['file'])
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = raw.decode('latin-1')
    lines = text.splitlines()
    if not lines or not lines[0].startswith('IES') or not any(line.strip() == 'TILT=NONE' for line in lines):
        core._fail('UNSUPPORTED', 'IES LM-63 with a self-contained TILT=NONE profile is required')
    return text


def light_graph(obj, row, state):
    bpy = core._bpy()
    data = obj.data
    if 'kelvin' in row:
        data.use_temperature = True
        data.temperature = row['kelvin']
    if 'ies' not in row and 'gobo' not in row:
        return
    data.use_nodes = True
    tree = data.node_tree
    tree.nodes.clear()
    emission = tree.nodes.new('ShaderNodeEmission')
    output = tree.nodes.new('ShaderNodeOutputLight')
    tree.links.new(emission.outputs['Emission'], output.inputs['Surface'])
    if 'ies' in row:
        text = core._mark(bpy.data.texts.new(obj.name + '.IES'), state['rig_id'])
        text.write(_ies_text(row['ies']))
        node = tree.nodes.new('ShaderNodeTexIES')
        node.mode = 'INTERNAL'
        node.ies = text
        tree.links.new(node.outputs['Factor'], emission.inputs['Strength'])
        state.setdefault('texts', []).append(text.name)
    if 'gobo' in row:
        spec = row['gobo']
        file = Path(spec['file'])
        if sha256(file.read_bytes()).hexdigest() != spec['expected_sha256']:
            core._fail('CONFLICT', 'Gobo SHA mismatch: ' + spec['file'])
        image = core._mark(bpy.data.images.load(str(file), check_existing=False), state['rig_id'])
        image.name = obj.name + '.Gobo'
        image.pack()
        if sha256(image.packed_file.data).hexdigest() != spec['expected_sha256']:
            core._fail('CONFLICT', 'Gobo changed during packing: ' + spec['file'])
        texture = tree.nodes.new('ShaderNodeTexImage')
        texture.image, texture.extension = image, 'CLIP'
        _spot_uv(tree, texture, data.spot_size)
        tree.links.new(texture.outputs['Color'], emission.inputs['Color'])
        state.setdefault('images', []).append(image.name)


def _spot_uv(tree, texture, spread):
    """Project the light shader's local ray onto the spot cone cross section."""
    def scalar(operation, source, value):
        node = tree.nodes.new('ShaderNodeMath')
        node.operation = operation
        tree.links.new(source, node.inputs[0])
        if isinstance(value, (int, float)):
            node.inputs[1].default_value = value
        else:
            tree.links.new(value, node.inputs[1])
        return node.outputs[0]

    geometry = tree.nodes.new('ShaderNodeNewGeometry')
    local = tree.nodes.new('ShaderNodeVectorTransform')
    local.vector_type, local.convert_from, local.convert_to = 'VECTOR', 'WORLD', 'OBJECT'
    tree.links.new(geometry.outputs['Incoming'], local.inputs['Vector'])
    xyz = tree.nodes.new('ShaderNodeSeparateXYZ')
    tree.links.new(local.outputs['Vector'], xyz.inputs[0])
    uv = tree.nodes.new('ShaderNodeCombineXYZ')
    depth = scalar('ABSOLUTE', xyz.outputs['Z'], 0)
    depth = scalar('MAXIMUM', depth, 1e-6)
    scale = .5 / math.tan(spread / 2)
    for axis in ('X', 'Y'):
        value = scalar('DIVIDE', xyz.outputs[axis], depth)
        pixel = scalar('MULTIPLY', value, -scale)
        tree.links.new(scalar('ADD', pixel, .5), uv.inputs[axis])
    tree.links.new(uv.outputs[0], texture.inputs['Vector'])


def bone_follow(scene, obj, spec):
    constraint = obj.constraints.new('COPY_LOCATION')
    constraint.name = 'Lighting Workflow Bone Follow'
    constraint.target = scene.objects[spec['armature']]
    constraint.subtarget = spec['bone']
    constraint.owner_space = constraint.target_space = 'WORLD'
    constraint.use_offset = True
    anchor = constraint.target.matrix_world @ constraint.target.pose.bones[spec['bone']].head
    obj.location -= anchor


def volume(tree, output, spec):
    scatter = tree.nodes.new('ShaderNodeVolumeScatter')
    scatter.inputs['Color'].default_value = (*spec.get('color', [1, 1, 1]), 1)
    scatter.inputs['Density'].default_value = spec['density']
    scatter.inputs['Anisotropy'].default_value = spec.get('anisotropy', 0)
    tree.links.new(scatter.outputs['Volume'], output.inputs['Volume'])


def local_fog(spec, box, collection, state):
    bpy = core._bpy()
    owner = state['rig_id']
    name = 'LW.' + owner + '.Fog'
    mesh = core._mark(bpy.data.meshes.new(name + '.Mesh'), owner)
    x, y, z = [value * box['size'] / 2 for value in spec['size']]
    points = [(-x, -y, -z), (x, -y, -z), (x, y, -z), (-x, y, -z),
              (-x, -y, z), (x, -y, z), (x, y, z), (-x, y, z)]
    mesh.from_pydata(points, [], [(3, 2, 1, 0), (4, 5, 6, 7), (0, 1, 5, 4),
                                  (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)])
    mesh.update()
    obj = core._mark(bpy.data.objects.new(name, mesh), owner)
    collection.objects.link(obj)
    obj.location = core._position(box, spec['location'])
    material = core._mark(bpy.data.materials.new(name + '.Material'), owner)
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    output = tree.nodes.new('ShaderNodeOutputMaterial')
    volume(tree, output, spec)
    mesh.materials.append(material)
    state['objects'].append(obj.name)
    state['materials'].append(material.name)


def hdri_direction(world, rotation_deg):
    """Use the luminance centroid of the brightest ten-degree HDRI lobe."""
    import numpy as np
    from mathutils import Vector

    image = next(n.image for n in world.node_tree.nodes if n.bl_idname == 'ShaderNodeTexEnvironment')
    width, height = image.size
    if not image.is_float or not 8 <= width <= 16384 or not 4 <= height <= 8192:
        core._fail('UNSUPPORTED', 'HDRI alignment requires a float equirectangular environment up to 16K')
    buffer = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(buffer)
    rgb = buffer.reshape(height, width, 4)[:, :, :3]
    luminance = rgb @ np.array([.2126, .7152, .0722], dtype=np.float32)
    if not np.isfinite(luminance).all():
        core._fail('UNMEASURABLE', 'HDRI contains invalid scene-linear samples')
    row, col = np.unravel_index(luminance.argmax(), luminance.shape)
    peak = luminance[row, col]
    if peak <= max(0, luminance.mean() * 1.5):
        core._fail('UNMEASURABLE', 'HDRI has no distinct bright lobe to align')
    y, x = np.where(luminance >= .5 * peak)
    azimuth = math.tau * (.5 - (x + .5) / width) - math.radians(rotation_deg)
    elevation = math.pi * ((y + .5) / height - .5)
    directions = np.stack((np.cos(elevation) * np.cos(azimuth),
                           np.cos(elevation) * np.sin(azimuth), np.sin(elevation)), axis=1)
    center = directions[(y == row) & (x == col)][0]
    cluster = directions @ center >= math.cos(math.radians(10))
    weights = luminance[y[cluster], x[cluster]] * np.cos(elevation[cluster])
    return Vector(weights @ directions[cluster]).normalized()
