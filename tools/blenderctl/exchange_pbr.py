"""Portable Principled PBR graph validation and scalar texture adaptation."""
from pathlib import Path

import bpy
import numpy as np

from protocol import Failure


COLOR = {'Base Color', 'Emission Color', 'Coat Tint'}
SCALAR = {'Roughness', 'Metallic', 'Alpha', 'Emission Strength', 'Transmission Weight',
          'Coat Weight', 'Coat Roughness', 'IOR', 'Specular IOR Level',
          'Sheen Weight', 'Subsurface Weight', 'Anisotropic'}
NORMAL = {'Normal', 'Coat Normal'}
TEXTURE_INPUTS = COLOR | SCALAR | NORMAL
DIRECT_ONLY = {'FBX': {'Base Color', 'Roughness', 'Metallic', 'Normal', 'Emission Color',
                       'Emission Strength', 'Alpha'},
               'USD': {'Base Color', 'Roughness', 'Metallic', 'Normal', 'Emission Color',
                       'Alpha', 'IOR', 'Coat Weight', 'Coat Roughness'}}


def _unsupported(message):
    raise Failure('UNSUPPORTED', message)


def _source(socket):
    seen = set()
    while socket.node.type == 'REROUTE':
        node = socket.node
        if node.as_pointer() in seen or len(node.inputs[0].links) != 1:
            _unsupported('Unconnected or cyclic material reroute')
        seen.add(node.as_pointer())
        socket = node.inputs[0].links[0].from_socket
    return socket


def _texture(socket, channel):
    socket = _source(socket)
    node = socket.node
    if channel in NORMAL:
        if node.type != 'NORMAL_MAP' or node.space != 'TANGENT' or node.inputs['Strength'].is_linked:
            _unsupported(f'{channel} requires a tangent-space Normal Map with constant strength')
        links = node.inputs['Color'].links
        if len(links) != 1:
            _unsupported(f'{channel} requires a linked normal texture')
        socket = _source(links[0].from_socket)
        node = socket.node
    component = None
    if node.type in ('SEPARATE_COLOR', 'SEPRGB'):
        if channel not in SCALAR or getattr(node, 'mode', 'RGB') != 'RGB':
            _unsupported(f'{channel} cannot use a split image channel')
        component = {'Red': 0, 'Green': 1, 'Blue': 2, 'R': 0, 'G': 1, 'B': 2}.get(socket.name)
        links = node.inputs[0].links
        if component is None or len(links) != 1:
            _unsupported(f'{channel} requires RGB components from one image')
        socket = _source(links[0].from_socket)
        node = socket.node
    if node.type != 'TEX_IMAGE' or not node.image or socket.name not in ('Color', 'Alpha'):
        _unsupported(f'{channel} requires a raster image texture')
    image = node.image
    if image.source == 'TILED':
        _unsupported('Flatten UDIMs or split them into separate materials before portable PBR export')
    if image.source not in ('FILE', 'GENERATED') or node.projection != 'FLAT':
        _unsupported(f'{channel} requires a single flat raster image')
    if node.extension not in ('REPEAT', 'EXTEND') or node.interpolation not in ('Linear', 'Closest'):
        _unsupported(f'{channel} uses an unsupported texture sampler')
    if node.inputs['Vector'].is_linked:
        uv = _source(node.inputs['Vector'].links[0].from_socket)
        if uv.node.type == 'MAPPING':
            mapping = uv.node
            if (mapping.vector_type != 'POINT' or not mapping.inputs['Vector'].is_linked
                    or any(mapping.inputs[key].is_linked for key in ('Scale','Rotation','Location'))):
                _unsupported(f'{channel} requires a constant UV mapping transform')
            uv = _source(mapping.inputs['Vector'].links[0].from_socket)
        if uv.node.type != 'UVMAP' and not (uv.node.type == 'TEX_COORD' and uv.name == 'UV'):
            _unsupported(f'{channel} texture mapping needs an explicit UV map or the default UVs')
    if channel in NORMAL and socket.name != 'Color':
        _unsupported(f'{channel} requires an RGB normal texture')
    return node, component


def profile(objects, strict=True):
    rows = []
    seen = set()
    for obj in objects:
        if obj.type != 'MESH':
            continue
        for slot in obj.material_slots:
            material = slot.material
            if not material or material.as_pointer() in seen:
                continue
            seen.add(material.as_pointer())
            tree = material.node_tree
            if not material.use_nodes or not tree:
                _unsupported(f'{material.name} requires a node-based Principled material')
            outputs = [node for node in tree.nodes if node.type == 'OUTPUT_MATERIAL' and node.is_active_output]
            if len(outputs) != 1 or len(outputs[0].inputs['Surface'].links) != 1:
                _unsupported(f'{material.name} needs one active material output with a linked surface')
            output = outputs[0]
            shader = _source(output.inputs['Surface'].links[0].from_socket).node
            if shader.type != 'BSDF_PRINCIPLED' or output.inputs['Volume'].is_linked or output.inputs['Displacement'].is_linked:
                _unsupported(f'{material.name} needs a direct Principled surface without volume or displacement')
            channels = {}
            for socket in shader.inputs:
                if not socket.is_linked:
                    continue
                if socket.name not in TEXTURE_INPUTS and strict:
                    _unsupported(f'{material.name} uses unsupported linked input {socket.name}')
                if socket.name in TEXTURE_INPUTS:
                    channels[socket.name] = (socket, *_texture(socket.links[0].from_socket, socket.name))
            occlusion = [node for node in tree.nodes if node.type == 'GROUP' and node.node_tree
                         and node.node_tree.name in ('glTF Material Output', 'glTF Settings')
                         and node.inputs.get('Occlusion') and node.inputs['Occlusion'].is_linked]
            if occlusion:
                socket = occlusion[0].inputs['Occlusion']
                channels['Occlusion'] = (socket, *_texture(socket.links[0].from_socket, 'Roughness'))
            rows.append((material, shader, channels))
    return rows


def describe(rows):
    summary = []
    for material, shader, channels in rows:
        description = {}
        for name, (_, node, component) in channels.items():
            image = node.image
            width, height = image.size
            if width <= 0 or height <= 0:
                raise Failure('VALIDATION_FAILED', f'{material.name} {name} has no decodable image')
            description[name] = {'image': image.name, 'size': [width, height],
                                 'color_space': image.colorspace_settings.name,
                                 'component': component, 'packed': bool(image.packed_file)}
            if name in NORMAL:
                description[name]['normal_convention'] = _source(channels[name][0].links[0].from_socket).node.convention
            if name in NORMAL or name in {'Occlusion', 'Roughness', 'Metallic'}:
                pixels = image.pixels
                samples = []
                for x, y in ((.125,.25), (.5,.5), (.875,.75)):
                    start = (int((height-1)*y) * width + int((width-1)*x)) * image.channels
                    pixel = list(pixels[start:start + min(3, image.channels)])
                    samples.append(pixel if name in NORMAL else [pixel[component or 0]])
                description[name]['samples'] = samples
        summary.append({'name': material.name, 'base_color': list(shader.inputs['Base Color'].default_value),
                        'secondary_lobes': {name: shader.inputs[name].default_value for name in
                                            ('Sheen Weight', 'Subsurface Weight', 'Anisotropic')
                                            if shader.inputs.get(name) and not shader.inputs[name].is_linked},
                        'roughness': shader.inputs['Roughness'].default_value,
                        'metallic': shader.inputs['Metallic'].default_value, 'channels': description,
                        'textures': sorted({entry['image'] for entry in description.values()})})
    return summary


def compare_textures(source, imported, target):
    supported = set(DIRECT_ONLY.get(target, TEXTURE_INPUTS | {'Occlusion'}))
    if target == 'GLB':
        supported.discard('Subsurface Weight')
    existing = {row['name']: row for row in imported}
    checks = []
    for row in source:
        recovered = existing.get(row['name'])
        if not recovered:
            raise Failure('VALIDATION_FAILED', f'{target} cold import is missing material {row["name"]}')
        expected = {name: channel for name, channel in row['channels'].items() if name in supported}
        sample_errors = {}
        for name, sent in expected.items():
            returned = recovered['channels'].get(name)
            if not returned or not returned['packed'] or returned['size'] != sent['size']:
                raise Failure('VALIDATION_FAILED', f'{target} cold import lost texture dimensions or packing on {row["name"]}/{name}')
            if sent.get('samples'):
                incoming = sent['samples']
                if sent.get('normal_convention') == 'DIRECTX':
                    incoming = [[red, 1 - green, blue] for red, green, blue in incoming]
                sample_errors[name] = max(abs(a-b) for sent_sample, returned_sample in
                    zip(incoming, returned['samples'], strict=True) for a,b in zip(sent_sample, returned_sample, strict=True))
        checks.append({'material': row['name'], 'retained_textures': sorted(expected),
                       'out_of_scope_channels': sorted(row['channels'].keys() - supported),
                       'sample_max_absolute_difference': sample_errors})
    return checks


def _pack_image(image, job, ordinal):
    # FBX hashes embedded files by the path and USD names the sidecars by the path.
    # Unique, local basenames avoid combining two different images of the same name.
    suffix = Path(image.filepath).suffix.lower()
    if image.file_format == 'JPEG':
        suffix = '.jpg'
    elif image.file_format == 'PNG':
        suffix = '.png'
    if suffix not in ('.png', '.jpg', '.jpeg', '.tif', '.tiff', '.exr'):
        _unsupported(f'Unsupported exchange raster format for {image.name}')
    path = Path(job) / f'pbr-{ordinal:03d}{suffix}'
    if not image.packed_file:
        image.pack()
    image.filepath = str(path)
    for packed in image.packed_files:
        packed.filepath = str(path)


def adapt(rows, target, job):
    variants = {}
    images = {}
    conversion = []
    for material, shader, channels in rows:
        links = material.node_tree.links
        # Resolve transparent edges for native exporters that inspect only direct links.
        for edge in list(links):
            if edge.from_node.type == 'REROUTE' and edge.to_node.type != 'REROUTE':
                links.new(_source(edge.from_socket), edge.to_socket)
        unsupported_lobes = {'Subsurface Weight'} if target == 'GLB' else {'Sheen Weight', 'Subsurface Weight', 'Anisotropic'}
        for name in sorted(unsupported_lobes):
            port = shader.inputs.get(name)
            if port and not port.is_linked and abs(port.default_value) > 1e-6:
                conversion.append({'material': material.name, 'channel': name, 'value': port.default_value,
                                   'status': 'not_represented_by_native_adapter'})
        for name, (socket, source, component) in channels.items():
            if (target in DIRECT_ONLY and name not in DIRECT_ONLY[target]) or name in unsupported_lobes:
                conversion.append({'material': material.name, 'channel': name,
                                   'status': 'not_represented_by_native_adapter'})
                continue
            image = source.image
            normal_node = socket.links[0].from_node if name in NORMAL else None
            flip = normal_node is not None and normal_node.convention == 'DIRECTX'
            split = component is not None and target in DIRECT_ONLY
            if flip or split:
                key = image.as_pointer(), component, flip
                if key not in variants:
                    w, h = image.size
                    if not w or not h or w * h > 16777216 or image.channels != 4:
                        _unsupported('Packed scalar textures must decode to RGBA within 16 megapixels')
                    pixels = np.empty(w * h * 4, dtype=np.float32)
                    image.pixels.foreach_get(pixels)
                    rgba = pixels.reshape((-1, 4))
                    if flip:
                        values = rgba.copy()
                        values[:, 1] = 1 - values[:, 1]
                        suffix = 'OpenGL'
                    else:
                        values = np.ones_like(rgba)
                        values[:, :3] = rgba[:, component:component + 1]
                        suffix = str(component)
                    variant = bpy.data.images.new(f'{image.name}.{suffix}', w, h, alpha=True, is_data=True)
                    variant.file_format = 'PNG'
                    variant.pixels.foreach_set(values.ravel())
                    variant.update()
                    variants[key] = variant
                texture = material.node_tree.nodes.new('ShaderNodeTexImage')
                texture.image = image = variants[key]
                texture.extension, texture.interpolation = source.extension, source.interpolation
                if source.inputs['Vector'].is_linked:
                    links.new(source.inputs['Vector'].links[0].from_socket, texture.inputs['Vector'])
                if flip:
                    links.new(texture.outputs['Color'], normal_node.inputs['Color'])
                    normal_node.convention = 'OPENGL'
                else:
                    links.new(texture.outputs['Color'], socket)
                conversion.append({'material': material.name, 'channel': name,
                                   'status': 'directx_to_opengl' if flip else 'unpacked_scalar'})
            images[image.as_pointer()] = image
    if target in DIRECT_ONLY:
        for index, image in enumerate(images.values()):
            _pack_image(image, job, index)
    return conversion
