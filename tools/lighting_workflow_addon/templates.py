"""Versioned, host-safe templates; Blender is imported only by export/instantiate.

Templates capture the current representable state, not the creation recipe.
Positions and sizes use the current target maximum world bounding-box extent.
"""
from copy import deepcopy
from pathlib import Path
import hashlib
import math

from .contract import (WorkflowError, obj, arr, num, NAME, ID, SHA, FILE,
                       MANIFEST_SCHEMA, _validate, _absolute, _unique, normalize_manifest)

BOX = obj({'center': arr(num(), 3, 3), 'size': num(1e-7, 1e9),
           'low': arr(num(), 3, 3), 'high': arr(num(), 3, 3)}, ('center', 'size', 'low', 'high'))
TEMPLATE_SCHEMA = obj({'version': {'const': '1.0'}, 'template_id': ID,
    'revision': {'type': 'integer', 'minimum': 1, 'maximum': 1000000},
    'parent_sha256': SHA, 'source': FILE,
    'reference': obj({'targets': arr(NAME, 1, 64), 'box': BOX,
        'boards': arr(obj({'id': ID, 'object': NAME}, ('id', 'object')), 0, 32)}, ('targets', 'box', 'boards')),
    'manifest': MANIFEST_SCHEMA, 'resources': arr(FILE, 0, 256)},
    ('version', 'template_id', 'revision', 'reference', 'manifest', 'resources'))
TARGET_SCHEMA = obj({'scene': NAME, 'view_layer': NAME, 'rig_id': ID,
    'targets': arr(NAME, 1, 64), 'mode': {'enum': ['relight', 'adapt']},
    'anchor': {'enum': ['world', 'target', 'camera']},
    'power_policy': {'enum': ['native', 'scale_squared']},
    'bindings': {'type': 'object', 'additionalProperties': NAME, 'maxProperties': 256},
    'keep_lights': arr(NAME, 0, 256)},
    ('scene', 'view_layer', 'rig_id', 'targets', 'mode', 'anchor', 'power_policy'))


def _fail(message, code='INVALID_REQUEST'):
    raise WorkflowError(code, message)


def normalize_target(value):
    value = deepcopy(value)
    if not isinstance(value, dict): _fail('target must be an object')
    bindings = value.pop('bindings', {})
    schema = deepcopy(TARGET_SCHEMA)
    schema['properties'].pop('bindings')
    _validate(value, schema, 'target')
    if not isinstance(bindings, dict) or len(bindings) > 256: _fail('target.bindings must be a bounded name map')
    for key, name in bindings.items():
        _validate(key, NAME, 'target.bindings source')
        _validate(name, NAME, 'target.bindings.' + key)
    _unique(value['targets'], 'target targets')
    _unique(value.get('keep_lights', []), 'target keep_lights')
    value['bindings'] = bindings
    return value


def normalize_template(value):
    value = deepcopy(value)
    _validate(value, TEMPLATE_SCHEMA, 'template')
    manifest = value['manifest'] = normalize_manifest(value['manifest'])
    if manifest['action'] != 'create': _fail('template requires create manifest')
    reference = value['reference']
    _unique(reference['targets'], 'reference targets')
    if manifest['targets'] != reference['targets']: _fail('reference and manifest targets disagree')
    boards = reference['boards']
    _unique([b['id'] for b in boards], 'reference board id')
    _unique([b['object'] for b in boards], 'reference board object')
    target_names = set(reference['targets'])
    board_names = {b['object'] for b in boards}
    keep_names = set(manifest.get('keep_lights', []))
    if target_names & board_names:
        _fail('Reference board objects may not alias source targets')
    if keep_names & (target_names | board_names):
        _fail('keep_lights may not alias geometric targets or board objects')
    if set(b['id'] for b in boards) != set(b['id'] for b in manifest.get('boards', [])):
        _fail('reference and manifest boards disagree')
    box = reference['box']
    if any(box['low'][i] > box['high'][i] for i in range(3)):
        _fail('reference box is inverted')
    if not math.isclose(box['size'], max(box['high'][i] - box['low'][i] for i in range(3)), rel_tol=1e-5, abs_tol=1e-6):
        _fail('reference extent does not match box')
    if any(not math.isclose(box['center'][i], (box['low'][i] + box['high'][i]) / 2, rel_tol=1e-5, abs_tol=1e-6) for i in range(3)):
        _fail('reference center does not match box')
    for resource in value['resources']: resource['file'] = _absolute(resource['file'])
    _unique([r['file'] for r in value['resources']], 'resource file')
    world = manifest.get('world', {})
    expected = {world['file']} if world.get('kind') == 'hdri' else set()
    if {r['file'] for r in value['resources']} != expected:
        _fail('template resources must exactly close the HDRI file reference')
    if 'source' in value: value['source']['file'] = _absolute(value['source']['file'])
    return value


def _normalized_position(position, box):
    return [(float(position[i]) - box['center'][i]) / box['size'] for i in range(3)]


def _without_world_fields(record):
    """Mask only fields fully represented by the exported World contract."""
    record = deepcopy(record)
    for node in record.get('nodes', {}).get('nodes', []):
        if node['type'] == 'ShaderNodeBackground':
            for socket in node['inputs']:
                if socket['id'] in ('Strength', 'Color'): socket['value'] = None
        elif node['type'] == 'ShaderNodeMapping':
            for socket in node['inputs']:
                if socket['id'] == 'Rotation': socket['value'][2] = 0
        elif node['type'] == 'ShaderNodeTexSky':
            for field in ('sky_type', 'sun_disc', 'sun_rotation', 'sun_elevation'):
                node['settings'].pop(field, None)
    # Diagnostic provenance is regenerated from the exported spec.
    record.get('custom_properties', {}).pop('lw_sky_support', None)
    return record


def _world_export(scene, state, resource_directory):
    from . import core
    bpy = core._bpy()
    if not state.get('world'): return None, []
    world = bpy.data.worlds.get(state['world'])
    if scene.world != world: _fail('Managed World binding changed', 'CONFLICT')
    actual = core._world_witness(world)
    if not core._close(_without_world_fields(actual), _without_world_fields(state['world_witness'])):
        _fail('World has node/settings edits that cannot be represented by a template', 'CONFLICT')
    nodes = world.node_tree.nodes
    backgrounds = [n for n in nodes if n.bl_idname == 'ShaderNodeBackground']
    if len(backgrounds) != 1: _fail('World Background structure is not representable', 'CONFLICT')
    background = backgrounds[0]
    recipe = state['manifest']['world']
    spec = {'kind': recipe['kind'], 'strength': float(background.inputs['Strength'].default_value)}
    resources = []
    if spec['kind'] == 'constant':
        color = list(background.inputs['Color'].default_value)
        if not core._close(color[3], 1): _fail('World color alpha is not representable', 'CONFLICT')
        spec['color'] = color[:3]
    elif spec['kind'] == 'sky':
        texture = next(n for n in nodes if n.bl_idname == 'ShaderNodeTexSky')
        spec['sky_type'] = texture.sky_type
        if texture.sky_type in ('SINGLE_SCATTERING', 'MULTIPLE_SCATTERING'):
            spec.update(sun_rotation_deg=math.degrees(texture.sun_rotation),
                        sun_elevation_deg=math.degrees(texture.sun_elevation), sun_disc=texture.sun_disc)
    else:
        texture = next(n for n in nodes if n.bl_idname == 'ShaderNodeTexEnvironment')
        mapping = next(n for n in nodes if n.bl_idname == 'ShaderNodeMapping')
        spec['rotation_deg'] = math.degrees(mapping.inputs['Rotation'].default_value[2])
        image = texture.image
        if image is None or image.source != 'FILE': _fail('HDRI image is not a file image', 'CONFLICT')
        packed = list(image.packed_files)
        if packed:
            if len(packed) != 1: _fail('Multiple packed HDRI files are not supported', 'UNSUPPORTED')
            if resource_directory is None: _fail('Packed HDRI requires owned resource_directory')
            directory = Path(_absolute(str(resource_directory)))
            directory.mkdir(parents=True, exist_ok=True)
            raw = bytes(packed[0].packed_file.data)
            digest = hashlib.sha256(raw).hexdigest()
            suffix = Path(image.filepath).suffix.lower()
            if suffix not in ('.hdr', '.exr', '.png', '.jpg', '.jpeg', '.tif', '.tiff'):
                _fail('Packed HDRI extension is unsupported', 'UNSUPPORTED')
            path = directory / (digest + suffix)
            if path.exists():
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest: _fail('Packed resource path is occupied', 'CONFLICT')
            else:
                with path.open('xb') as stream: stream.write(raw)
        else:
            path = Path(bpy.path.abspath(image.filepath)).resolve()
            if not path.is_file(): _fail('HDRI resource is missing: ' + str(path), 'NOT_FOUND')
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        # Verify load semantics before accepting image color-space/alpha hand edits.
        check = bpy.data.images.load(str(path), check_existing=False)
        try:
            if image.colorspace_settings.name != check.colorspace_settings.name or image.alpha_mode != check.alpha_mode:
                _fail('HDRI color space/alpha edits are not representable', 'CONFLICT')
        finally: bpy.data.images.remove(check)
        spec['file'] = str(path)
        resources = [{'file': str(path), 'expected_sha256': digest}]
    return spec, resources


def export(scene_name, rig_id, resource_directory=None):
    from . import core, advanced
    from mathutils import Vector
    scene = core._scene(scene_name)
    state = core._states(scene).get(rig_id)
    if state is None: _fail('Managed rig not found', 'NOT_FOUND')
    _, objects = core._ensure_managed(scene, state)
    advanced.check_links(scene, state)
    with core._context(scene, state['view_layer']):
        scene.view_layers[state['view_layer']].update()
        box = core._box(scene, state['targets'])
        manifest = {'version': '1.0', 'scene': scene.name, 'view_layer': state['view_layer'],
                    'rig_id': rig_id, 'action': 'create', 'targets': deepcopy(state['targets']),
                    'mode': state['mode'], 'anchor': state['anchor'], 'lights': [],
                    'exposure': float(scene.view_settings.exposure)}
        for light_id, row in state['lights'].items():
            obj = objects[row['object']]
            actual, expected = core._light_structure(obj), deepcopy(row['structure'])
            for witness in (actual, expected):
                witness['object']['object'].pop('matrix_basis', None)
                for field in ('shape', 'angle', 'spot_size', 'spot_blend', 'normalize', 'spread', 'diffuse_factor', 'specular_factor'):
                    witness['data'].get('settings', {}).pop(field, None)
            if not core._close(actual, expected): _fail('Light has unrepresentable structure edits: ' + obj.name, 'CONFLICT')
            location, rotation, scale = core.managed_world_matrix(obj, scene=scene, view_layer=state['view_layer']).decompose()
            direction = rotation @ Vector((0, 0, -1))
            rebuilt = direction.to_track_quat('-Z', 'Y')
            if not core._close(list(scale), [1, 1, 1]) or abs(abs(rotation.dot(rebuilt)) - 1) > 1e-6:
                _fail('Light scale/roll cannot be represented: ' + obj.name, 'CONFLICT')
            if obj.hide_render != obj.hide_viewport: _fail('Separate light visibility states cannot be represented', 'CONFLICT')
            data = obj.data
            values = {'id': light_id, 'role': row['role'], 'type': data.type,
                'location': _normalized_position(location, box),
                'aim': _normalized_position(location + direction * box['size'], box),
                'energy': float(data.energy), 'color': list(data.color), 'enabled': not obj.hide_render,
                'use_shadow': bool(getattr(data, 'use_shadow', True)), **advanced.native_controls(obj),
                **advanced.link_values(obj)}
            # RNA stores radians in float32; PI round-trips a few microdegrees
            # above the strict 180 degree contract boundary.
            if 'spread_deg' in values and 180 < values['spread_deg'] < 180.0001:
                values['spread_deg'] = 180
            if data.type == 'AREA':
                values.update(shape=data.shape, size=data.size / box['size'])
                if data.shape in ('RECTANGLE', 'ELLIPSE'): values['size_y'] = data.size_y / box['size']
            elif data.type == 'SUN':
                values['angle_deg'] = math.degrees(data.angle)
                if 180 < values['angle_deg'] < 180.0001: values['angle_deg'] = 180
            else: values['size'] = data.shadow_soft_size / box['size']
            if data.type == 'SPOT': values.update(spot_size_deg=math.degrees(data.spot_size), spot_blend=float(data.spot_blend))
            manifest['lights'].append(values)
        reference_boards = []
        for board_id, row in state.get('boards', {}).items():
            obj = objects[row['object']]
            if not core._close(advanced.board_structure(obj), row['structure']): _fail('Board has unrepresentable structure edits', 'CONFLICT')
            if obj.hide_render != obj.hide_viewport: _fail('Separate board visibility states cannot be represented', 'CONFLICT')
            values = advanced.board_values(obj)
            values['location'] = _normalized_position(core.managed_world_matrix(obj, scene=scene, view_layer=state['view_layer']).translation, box)
            values['size'] = [v / box['size'] for v in values['size']]
            manifest.setdefault('boards', []).append({'id': board_id, 'kind': row['recipe']['kind'], **values})
            reference_boards.append({'id': board_id, 'object': obj.name})
        extras, expected_extras = core._extras_witness(state), deepcopy(state['extras'])
        for board in state.get('boards', {}).values():
            for witness in (extras, expected_extras): witness[board['object']].pop('values', None)
        if not core._close(extras, expected_extras): _fail('Managed camera/background has unrepresentable edits', 'CONFLICT')
        if 'background' in state['manifest']:
            # Extras above verify this unchanged; recompute size at current target extent.
            spec = deepcopy(state['manifest']['background'])
            obj = objects['LW.' + rig_id + '.Background']
            expected = Vector([box['center'][0], box['center'][1], box['low'][2] - box['size'] * .005])
            if (core.managed_world_matrix(obj, scene=scene, view_layer=state['view_layer']).translation - expected).length > max(1e-5, box['size'] * 1e-5):
                _fail('Background placement does not fit current target box', 'CONFLICT')
            spec['size'] = float(obj.dimensions.x) / box['size']
            manifest['background'] = spec
        world, resources = _world_export(scene, state, resource_directory)
        if world is not None: manifest['world'] = world
        if 'use_raytracing' in core._eevee_settings(scene): manifest['eevee'] = core._eevee_settings(scene)
        manifest['keep_lights'] = deepcopy(state['manifest'].get('keep_lights', []))
        return normalize_template({'version': '1.0', 'template_id': rig_id, 'revision': 1,
            'reference': {'targets': deepcopy(state['targets']), 'box': box, 'boards': reference_boards},
            'manifest': manifest, 'resources': resources})


def instantiate(template, target):
    from . import core, advanced
    template, target = normalize_template(template), normalize_target(target)
    scene = core._scene(target['scene'])
    source_targets = template['reference']['targets']
    if len(source_targets) != len(target['targets']): _fail('Target count must match template reference targets')
    with core._context(scene, target['view_layer']):
        scene.view_layers[target['view_layer']].update()
        box = core._box(scene, target['targets'])
    mapping = dict(zip(source_targets, target['targets']))
    mapping.update({row['object']: advanced.board_name(target['rig_id'], row['id']) for row in template['reference']['boards']})
    if set(mapping) & set(target['bindings']): _fail('Explicit bindings may not replace automatic target/board mappings')
    used = set()
    def bind(name):
        if name in mapping: return mapping[name]
        if name not in target['bindings']: _fail('Missing explicit binding for external object: ' + name)
        used.add(name)
        result = target['bindings'][name]
        if not scene.objects.get(result): _fail('Binding target not in scene: ' + result, 'NOT_FOUND')
        return result
    manifest = deepcopy(template['manifest'])
    for key in ('scene', 'view_layer', 'rig_id', 'targets', 'mode', 'anchor'): manifest[key] = deepcopy(target[key])
    ratio = box['size'] / template['reference']['box']['size']
    for row in manifest['lights']:
        if target['power_policy'] == 'scale_squared' and row['type'] != 'SUN': row['energy'] *= ratio ** 2
        for key in ('receivers', 'blockers'):
            if key in row: row[key] = [bind(name) for name in row[key]]
    if 'keep_lights' in target:
        manifest['keep_lights'] = deepcopy(target['keep_lights'])
    else: manifest['keep_lights'] = [bind(name) for name in manifest.get('keep_lights', [])]
    if used != set(target['bindings']): _fail('Unused explicit bindings: ' + ', '.join(sorted(set(target['bindings']) - used)))
    for resource in template['resources']:
        path = Path(resource['file'])
        if not path.is_file(): _fail('Template resource missing: ' + str(path), 'NOT_FOUND')
        if hashlib.sha256(path.read_bytes()).hexdigest() != resource['expected_sha256']:
            _fail('Template resource SHA mismatch: ' + str(path), 'INPUT_CHANGED')
    return normalize_manifest(manifest)
