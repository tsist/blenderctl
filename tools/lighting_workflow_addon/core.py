"""Deterministic managed lights. File saving and rendering belong to the CLI."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import math

from .contract import WorkflowError, normalize_manifest, LINK_FIELDS
from . import advanced

META = 'lighting_workflow_v1'
OWNER = 'lw_owner'


def _bpy():
    import bpy
    return bpy


def _fail(code, message):
    raise WorkflowError(code, message)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                         allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def _close(a, b):
    if isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool):
        return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-5)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_close(a[k], b[k]) for k in a)
    return a == b


def _scene(name):
    scene = _bpy().data.scenes.get(name)
    if not scene: _fail('NOT_FOUND', 'Scene not found: ' + name)
    if scene.library or scene.override_library: _fail('UNSUPPORTED', 'Linked/override scene is read-only')
    return scene


@contextmanager
def _context(scene, layer_name):
    bpy = _bpy()
    layer = scene.view_layers.get(layer_name)
    if not layer: _fail('NOT_FOUND', 'View layer not found: ' + layer_name)
    window = bpy.context.window
    old_scene, old_layer = (window.scene, window.view_layer) if window else (None, None)
    try:
        if window:
            window.scene = scene
            window.view_layer = layer
        with bpy.context.temp_override(scene=scene, view_layer=layer):
            yield layer
    finally:
        if window:
            window.scene = old_scene
            window.view_layer = old_layer


def _states(scene):
    raw = scene.get(META, '{}')
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        _fail('CONFLICT', 'Lighting metadata is malformed')
    if not isinstance(value, dict): _fail('CONFLICT', 'Lighting metadata must be an object')
    return value


def _save_states(scene, states):
    if states:
        scene[META] = json.dumps(states, ensure_ascii=False, sort_keys=True, allow_nan=False)
    elif META in scene:
        del scene[META]


def _owned(item, rig_id):
    if not item or item.get(OWNER) != rig_id: _fail('CONFLICT', 'Managed ID missing or ownership changed')
    if item.library or item.override_library: _fail('UNSUPPORTED', 'Managed linked/override ID cannot be edited')
    return item


def _mark(item, rig_id):
    item[OWNER] = rig_id
    if getattr(item, 'node_tree', None): item.node_tree[OWNER] = rig_id
    return item


def _fresh(group, name):
    if group.get(name): _fail('CONFLICT', 'Name already exists: ' + name)


def _record(item):
    bpy = _bpy()
    def identity(value):
        return {'type': value.bl_rna.identifier, 'name': value.name,
                'library': bpy.path.abspath(value.library.filepath) if value.library else None}
    def scalar(value):
        if isinstance(value, bpy.types.ID): return identity(value)
        if value is None or isinstance(value, (str, bool, int, float)): return value
        if isinstance(value, set): return sorted(value)
        if hasattr(value, 'keys'): return {k: scalar(value[k]) for k in value.keys()}
        return [scalar(v) for v in value]
    def rna(value, depth=2):
        result = {}
        for prop in value.bl_rna.properties:
            name = prop.identifier
            if name in ('rna_type', 'id_data', 'tag') or prop.is_readonly: continue
            raw = getattr(value, name)
            if prop.type in ('BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'):
                result[name] = scalar(raw)
            elif prop.type == 'POINTER' and isinstance(raw, bpy.types.ID): result[name] = identity(raw)
            elif depth and prop.type == 'POINTER' and raw is not None: result[name] = rna(raw, depth - 1)
            elif depth and prop.type == 'COLLECTION': result[name] = [rna(v, depth - 1) for v in raw]
        for name in ('color_ramp', 'mapping', 'texture_mapping', 'color_mapping', 'dof', 'stereo'):
            raw = getattr(value, name, None)
            if depth and raw is not None and name not in result: result[name] = rna(raw, depth - 1)
        return result
    row = {**identity(item), 'fake_user': item.use_fake_user,
           'custom_properties': {k: scalar(item[k]) for k in item.keys() if k != '_RNA_UI'}}
    if isinstance(item, bpy.types.Object):
        row['object'] = {'data': identity(item.data) if item.data else None,
            'parent': identity(item.parent) if item.parent else None, 'parent_type': item.parent_type,
            'parent_bone': item.parent_bone, 'matrix_basis': scalar(item.matrix_basis),
            'matrix_parent_inverse': scalar(item.matrix_parent_inverse), 'rotation_mode': item.rotation_mode,
            'hide_render': item.hide_render, 'modifiers': [rna(m) for m in item.modifiers],
            'constraints': [rna(c) for c in item.constraints],
            'vertex_groups': [g.name for g in item.vertex_groups]}
        if item.pose:
            row['pose'] = [{'name': p.name, 'matrix_basis': scalar(p.matrix_basis),
                            'constraints': [rna(c) for c in p.constraints]} for p in item.pose.bones]
    if isinstance(item, bpy.types.Mesh):
        from array import array
        hashes = {}
        def field(name, collection, prop, width, kind):
            values = array(kind, [0]) * (len(collection) * width)
            collection.foreach_get(prop, values)
            hashes[name] = hashlib.sha256(values.tobytes()).hexdigest()
        field('positions', item.vertices, 'co', 3, 'f')
        field('edges', item.edges, 'vertices', 2, 'i')
        field('loops', item.loops, 'vertex_index', 1, 'i')
        for prop in ('loop_start', 'loop_total', 'material_index'): field(prop, item.polygons, prop, 1, 'i')
        field('smooth', item.polygons, 'use_smooth', 1, 'b')
        for uv in item.uv_layers: field('uv:' + uv.name, uv.data, 'uv', 2, 'f')
        if item.shape_keys:
            for block in item.shape_keys.key_blocks: field('shape:' + block.name, block.data, 'co', 3, 'f')
        row['mesh'] = {'hashes': hashes, 'materials': [identity(m) if m else None for m in item.materials],
                       'weights': [[(g.group, g.weight) for g in v.groups] for v in item.vertices],
                       'attributes': [{'name': a.name, 'domain': a.domain, 'type': a.data_type,
                                       'sha256': _digest([rna(v) for v in a.data])} for a in item.attributes]}
    if isinstance(item, (bpy.types.Light, bpy.types.Camera, bpy.types.World, bpy.types.Material, bpy.types.Curve)):
        row['settings'] = rna(item)
    tree = item if isinstance(item, bpy.types.NodeTree) else getattr(item, 'node_tree', None)
    if tree:
        row['nodes'] = {'nodes': [{'name': n.name, 'type': n.bl_idname, 'settings': rna(n),
            'inputs': [{'id': s.identifier, 'value': scalar(s.default_value) if hasattr(s, 'default_value') else None} for s in n.inputs],
            'outputs': [{'id': s.identifier, 'value': scalar(s.default_value) if hasattr(s, 'default_value') else None} for s in n.outputs]}
            for n in tree.nodes], 'links': sorted((l.from_node.name, l.from_socket.identifier,
                l.to_node.name, l.to_socket.identifier) for l in tree.links)}
    def curve(curve):
        return {'path': curve.data_path, 'index': curve.array_index, 'settings': rna(curve),
                'keys': [rna(k) for k in curve.keyframe_points], 'samples': [rna(k) for k in curve.sampled_points],
                'modifiers': [rna(m) for m in curve.modifiers]}
    if isinstance(item, bpy.types.Action):
        row['action'] = {'slots': [rna(s) for s in item.slots], 'layers': [{'name': layer.name,
            'strips': [{'type': strip.type, 'bags': [{'slot': bag.slot_handle,
                'curves': [curve(c) for c in bag.fcurves]} for bag in getattr(strip, 'channelbags', [])]}
                for strip in layer.strips]} for layer in item.layers]}
    animation = getattr(item, 'animation_data', None)
    if animation:
        row['animation'] = {'action': identity(animation.action) if animation.action else None,
            'slot': animation.action_slot.identifier if animation.action_slot else None,
            'drivers': [{**curve(c), 'driver': rna(c.driver)} for c in animation.drivers],
            'nla': [rna(t, 3) for t in animation.nla_tracks]}
    if isinstance(item, bpy.types.Scene):
        row['scene'] = {'camera': identity(item.camera) if item.camera else None,
            'frames': [item.frame_start, item.frame_end], 'fps': item.render.fps,
            'fps_base': item.render.fps_base, 'engine': item.render.engine,
            'resolution': [item.render.resolution_x, item.render.resolution_y, item.render.resolution_percentage]}
    return row


def _light_values(obj, include_links=False):
    data = obj.data
    values = {'energy': float(data.energy), 'color': list(data.color), 'location': list(obj.location),
            'size': float(data.size if data.type == 'AREA' else getattr(data, 'shadow_soft_size', 0)),
            'size_y': float(getattr(data, 'size_y', 0)),
            'enabled': not obj.hide_render and not obj.hide_viewport,
            'use_shadow': bool(getattr(data, 'use_shadow', True))}
    if include_links: values.update(advanced.link_values(obj))
    return values


def _light_structure(obj):
    obj_row, data_row = _record(obj), _record(obj.data)
    # Local translation and supported light fields have independent baselines.
    matrix = obj_row['object']['matrix_basis']
    for i in range(3): matrix[i][3] = 0
    obj_row['object'].pop('hide_render', None)
    for key in ('energy', 'color', 'size', 'size_y', 'shadow_soft_size', 'use_shadow'):
        data_row.get('settings', {}).pop(key, None)
    return {'object': obj_row, 'data': data_row,
            'hide_viewport_model': 'separate_visibility',
            'layer_hidden': {layer.name: obj.hide_get(view_layer=layer) for layer in _scene_for_object(obj).view_layers}}


def _scene_for_object(obj):
    scenes = [s for s in _bpy().data.scenes if obj.name in s.objects]
    if len(scenes) != 1: _fail('CONFLICT', 'Managed object must belong to exactly one scene: ' + obj.name)
    return scenes[0]


def managed_world_matrix(obj, *, scene=None, view_layer=None):
    """Read a rigid managed transform without depending on its visibility cache.

    Hidden objects may never get their own matrix_world evaluated after a cold
    load. Their basis/inverse are persistent RNA; only the parent needs actual
    current dependency-graph evaluation, including its animation/constraints.
    """
    if not obj.get(OWNER): _fail('UNSUPPORTED', 'Rigid transform reader requires a managed object: ' + obj.name)
    if obj.constraints or obj.animation_data or getattr(obj, 'rigid_body', None) or getattr(obj, 'rigid_body_constraint', None):
        _fail('UNSUPPORTED', 'Managed rigid transform has unsupported constraints/animation/physics: ' + obj.name)
    if obj.parent and obj.parent_type != 'OBJECT':
        _fail('UNSUPPORTED', 'Managed rigid transform requires OBJECT parenting: ' + obj.name)
    basis = obj.matrix_basis.copy()
    if not obj.parent: return basis
    parent = obj.parent
    if parent.type == 'CURVE' and getattr(parent.data, 'use_path', False):
        _fail('UNSUPPORTED', 'Curve path parenting is outside the rigid transform reader: ' + obj.name)
    scene = _scene(scene) if isinstance(scene, str) else scene or _scene_for_object(obj)
    if parent.name not in scene.objects: _fail('CONFLICT', 'Managed parent is outside its Scene: ' + obj.name)
    view_layer = view_layer or scene.view_layers[0].name
    with _context(scene, view_layer) as layer:
        layer.update()
        graph = _bpy().context.evaluated_depsgraph_get()
        evaluated_parent = graph.objects.get(parent.name)
        if not evaluated_parent or not evaluated_parent.is_evaluated:
            _fail('UNSUPPORTED', 'Managed parent has no current evaluated transform: ' + parent.name)
        return evaluated_parent.matrix_world @ obj.matrix_parent_inverse @ basis


def _visibility(obj):
    return {'hide_render': bool(obj.hide_render), 'hide_viewport': bool(obj.hide_viewport)}


def _original_structure(obj):
    row = _record(obj)
    row['object'].pop('hide_render', None)
    return {'object': row, 'data': _record(obj.data)}


def _box(scene, target_names):
    from mathutils import Vector
    bpy = _bpy()
    graph = bpy.context.evaluated_depsgraph_get()
    points = []
    for name in target_names:
        obj = scene.objects.get(name)
        if not obj: _fail('NOT_FOUND', 'Target not in scene: ' + name)
        if obj.type not in ('MESH', 'CURVE', 'SURFACE', 'META', 'FONT', 'VOLUME'):
            _fail('UNSUPPORTED', 'Target requires a geometric bounding box: ' + name)
        evaluated = obj.evaluated_get(graph)
        corners = list(evaluated.bound_box)
        if not corners or all(tuple(c) == (-1, -1, -1) for c in corners):
            _fail('UNSUPPORTED', 'Target bounding box is unavailable: ' + name)
        points.extend(evaluated.matrix_world @ Vector(c) for c in corners)
    low = Vector([min(p[i] for p in points) for i in range(3)])
    high = Vector([max(p[i] for p in points) for i in range(3)])
    size = max(high - low)
    if not math.isfinite(size) or size <= 1e-7: _fail('INVALID_REQUEST', 'Target bounding box is degenerate')
    return {'center': list((low + high) / 2), 'size': size, 'low': list(low), 'high': list(high)}


def _position(box, value):
    from mathutils import Vector
    return Vector(box['center']) + Vector(value) * box['size']


def _transform(obj, position, aim):
    from mathutils import Matrix, Vector
    direction = Vector(aim) - Vector(position)
    if direction.length <= 1e-9: _fail('INVALID_REQUEST', 'Light position and aim coincide')
    obj.matrix_world = Matrix.LocRotScale(position, direction.to_track_quat('-Z', 'Y'), Vector((1, 1, 1)))


def _exclusive(obj, scene):
    if obj.library or obj.override_library: _fail('UNSUPPORTED', 'Cannot change linked/override light: ' + obj.name)
    if any(obj.name in other.objects for other in _bpy().data.scenes if other != scene):
        _fail('CONFLICT', 'Light is shared across scenes: ' + obj.name)


def _ensure_managed(scene, state):
    bpy = _bpy()
    rig_id = state['rig_id']
    collection = _owned(bpy.data.collections.get(state['collection']), rig_id)
    if collection.name not in scene.collection.children:
        _fail('CONFLICT', 'Managed collection was moved')
    if any(collection in list(parent.children) for parent in bpy.data.collections if parent != scene.collection):
        _fail('CONFLICT', 'Managed collection acquired an outside parent')
    if any(obj.instance_collection == collection for obj in bpy.data.objects):
        _fail('CONFLICT', 'Managed collection acquired an instance consumer')
    consumers = bpy.data.user_map(subset={collection}).get(collection, set())
    if consumers - {scene, scene.collection}:
        _fail('CONFLICT', 'Managed collection has an outside ID consumer')
    for other in bpy.data.scenes:
        if other != scene and collection in list(other.collection.children):
            _fail('CONFLICT', 'Managed collection is shared across scenes')
    objects = {}
    for name in state['objects']:
        obj = _owned(bpy.data.objects.get(name), rig_id)
        _exclusive(obj, scene)
        auxiliary = {row['collection'] for fields in state.get('links', {}).values()
                     for row in fields.values() if row['collection']}
        if collection not in obj.users_collection or any(c.name != collection.name and c.name not in auxiliary for c in obj.users_collection):
            _fail('CONFLICT', 'Managed object has outside collection consumers: ' + name)
        if obj.data:
            _owned(obj.data, rig_id)
            if obj.data.users != 1: _fail('CONFLICT', 'Managed data has outside consumers: ' + name)
        # user_map includes the owning Scene's object membership cache as well
        # as direct collections (verified in 5.2.1), for every Scene object.
        permitted = {scene, collection, *[bpy.data.collections.get(n) for n in auxiliary]}
        if bpy.data.user_map(subset={obj}).get(obj, set()) - permitted:
            _fail('CONFLICT', 'Managed object has an outside ID consumer: ' + name)
        objects[name] = obj
    if sorted(o.name for o in collection.objects) != sorted(state['objects']) or len(collection.children):
        _fail('CONFLICT', 'Managed collection contains unrecorded members')
    for name in state.get('materials', []):
        material = _owned(bpy.data.materials.get(name), rig_id)
        if material.users != 1: _fail('CONFLICT', 'Managed material has outside consumers')
    if state.get('world'):
        world = _owned(bpy.data.worlds.get(state['world']), rig_id)
        if any(s.world == world and s != scene for s in bpy.data.scenes): _fail('CONFLICT', 'Managed World is shared')
        if bpy.data.user_map(subset={world}).get(world, set()) - {scene}:
            _fail('CONFLICT', 'Managed World has an outside ID consumer')
    return collection, objects


def _world(spec, name, rig_id):
    bpy = _bpy()
    if spec['kind'] == 'sky':
        if 'rotation_deg' in spec: _fail('INVALID_REQUEST', 'Sky rotation_deg is unsupported; use sun_rotation_deg')
        properties = bpy.types.ShaderNodeTexSky.bl_rna.properties
        sky_type = spec.get('sky_type', 'MULTIPLE_SCATTERING')
        prop = properties.get('sky_type')
        if not prop or prop.is_readonly or sky_type not in prop.enum_items:
            _fail('UNSUPPORTED', 'Sky type unavailable in actual Blender: ' + sky_type)
        solar = {'sun_rotation_deg': 'sun_rotation', 'sun_elevation_deg': 'sun_elevation', 'sun_disc': 'sun_disc'}
        if set(spec) & set(solar) and sky_type not in ('SINGLE_SCATTERING', 'MULTIPLE_SCATTERING'):
            _fail('INVALID_REQUEST', 'Sun angle/disc fields require scattering Sky models')
        for field, native in solar.items():
            if field not in spec: continue
            prop = properties.get(native)
            if not prop or prop.is_readonly: _fail('UNSUPPORTED', 'Native Sky property unavailable: ' + native)
            value = spec[field] if field == 'sun_disc' else math.radians(spec[field])
            if prop.type == 'FLOAT' and not prop.hard_min <= value <= prop.hard_max:
                _fail('UNSUPPORTED', 'Sun control outside actual RNA bounds: ' + field)
    _fresh(bpy.data.worlds, name)
    world = _mark(bpy.data.worlds.new(name), rig_id)
    world.use_nodes = True
    _mark(world, rig_id)
    tree = world.node_tree
    tree.nodes.clear()
    output = tree.nodes.new('ShaderNodeOutputWorld')
    background = tree.nodes.new('ShaderNodeBackground')
    background.inputs['Strength'].default_value = spec['strength']
    tree.links.new(background.outputs['Background'], output.inputs['Surface'])
    if spec['kind'] == 'constant':
        background.inputs['Color'].default_value = [*spec.get('color', [.05, .05, .05]), 1]
    else:
        if spec['kind'] == 'sky':
            texture = tree.nodes.new('ShaderNodeTexSky')
            texture.sky_type = sky_type
            for field, native in solar.items():
                if field in spec:
                    value = spec[field] if field == 'sun_disc' else math.radians(spec[field])
                    setattr(texture, native, value)
                    if not _close(getattr(texture, native), value): _fail('VALIDATION_FAILED', 'Native Sky control was clamped: ' + field)
            world['lw_sky_support'] = {'sky_type': sky_type, 'sun_disc': 'Cycles-only; Eevee sun disc is unsupported',
                                       'direct_light': 'Use an explicit SUN light for predictable cross-engine direct lighting'}
            tree.links.new(texture.outputs['Color'], background.inputs['Color'])
        else:
            texture = tree.nodes.new('ShaderNodeTexEnvironment')
            image = _mark(bpy.data.images.load(spec['file'], check_existing=False), rig_id)
            texture.image = image
            coordinates = tree.nodes.new('ShaderNodeTexCoord')
            mapping = tree.nodes.new('ShaderNodeMapping')
            mapping.inputs['Rotation'].default_value[2] = math.radians(spec.get('rotation_deg', 0))
            tree.links.new(coordinates.outputs['Generated'], mapping.inputs['Vector'])
            tree.links.new(mapping.outputs['Vector'], texture.inputs['Vector'])
            tree.links.new(texture.outputs['Color'], background.inputs['Color'])
    return world


def _background(spec, box, collection, rig_id, prefix):
    bpy = _bpy()
    size = spec.get('size', 12) * box['size']
    name = prefix + '.Background'
    mesh = _mark(bpy.data.meshes.new(name + '.Mesh'), rig_id)
    h = size / 2
    mesh.from_pydata([(-h, -h, 0), (h, -h, 0), (h, h, 0), (-h, h, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    material = _mark(bpy.data.materials.new(name + '.Material'), rig_id)
    material.use_nodes = True
    _mark(material, rig_id)
    bsdf = next((n for n in material.node_tree.nodes if n.bl_idname == 'ShaderNodeBsdfPrincipled'), None)
    if bsdf is None: _fail('UNSUPPORTED', 'Native Principled BSDF unavailable')
    bsdf.inputs['Base Color'].default_value = [*spec['color'], 1]
    bsdf.inputs['Roughness'].default_value = spec.get('roughness', .8)
    mesh.materials.append(material)
    obj = _mark(bpy.data.objects.new(name, mesh), rig_id)
    collection.objects.link(obj)
    obj.location = [box['center'][0], box['center'][1], box['low'][2] - box['size'] * .005]
    return obj, material


def _created_camera(scene, collection, box, rig_id, prefix):
    bpy = _bpy()
    data = _mark(bpy.data.cameras.new(prefix + '.Camera.Data'), rig_id)
    obj = _mark(bpy.data.objects.new(prefix + '.Camera', data), rig_id)
    collection.objects.link(obj)
    data.lens = 50
    data.clip_start = max(.00001, box['size'] / 1000)
    data.clip_end = max(100, box['size'] * 100)
    _transform(obj, _position(box, [0, -3.2, 1.3]), box['center'])
    scene.camera = obj
    return obj


def _world_witness(world):
    return _record(world) if world else None


def _eevee_settings(scene):
    settings = getattr(scene, 'eevee', None)
    if settings is None or not hasattr(settings, 'use_raytracing'):
        return {'status': 'unsupported'}
    return {'use_raytracing': bool(settings.use_raytracing)}


def _extras_witness(state):
    bpy = _bpy()
    boards = {row['object'] for row in state.get('boards', {}).values()}
    board_materials = {row['material'] for row in state.get('boards', {}).values()}
    names = set(state['objects']) - {row['object'] for row in state['lights'].values()}
    result = {}
    for name in names:
        obj = bpy.data.objects.get(name)
        if name in boards:
            result[name] = {'structure': advanced.board_structure(obj), 'values': advanced.board_values(obj)} if obj and obj.data else None
        else:
            result[name] = {'object': _record(obj), 'data': _record(obj.data)} if obj and obj.data else None
    for name in state.get('materials', []):
        if name in board_materials: continue
        material = bpy.data.materials.get(name)
        result[name] = _record(material) if material else None
    return result


def _create(scene, manifest):
    bpy = _bpy()
    states = _states(scene)
    if states: _fail('CONFLICT', 'Restore the existing rig before creating another active rig in this scene')
    rig_id = manifest['rig_id']
    if any(item.get(OWNER) == rig_id for item in bpy.data.objects): _fail('CONFLICT', 'rig_id already exists in another scene')
    box = _box(scene, manifest['targets'])
    source_eevee = _eevee_settings(scene)
    if 'eevee' in manifest and 'use_raytracing' not in source_eevee:
        _fail('UNSUPPORTED', 'Scene.eevee.use_raytracing is unavailable in actual Blender')
    for row in manifest['lights']:
        if _close(row['location'], row['aim']): _fail('INVALID_REQUEST', 'Light location and aim coincide: ' + row['id'])
        advanced.validate_native_controls(row)
        advanced.validate_links(scene, row, {advanced.board_name(manifest['rig_id'], b['id']) for b in manifest.get('boards', [])})
    keep = set(manifest.get('keep_lights', []))
    for name in keep:
        if not scene.objects.get(name) or scene.objects[name].type != 'LIGHT': _fail('NOT_FOUND', 'keep_lights entry is not a scene light: ' + name)
    originals = [o for o in scene.objects if o.type == 'LIGHT' and o.name not in keep] if manifest['mode'] == 'relight' else []
    for obj in originals: _exclusive(obj, scene)
    if manifest['anchor'] == 'target':
        parent = scene.objects.get(manifest['targets'][0])
        if abs(parent.matrix_world.determinant()) < 1e-12: _fail('UNSUPPORTED', 'Target anchor transform is singular')
    else: parent = None
    prefix = 'LW.' + rig_id
    prospective = [prefix + '.Lights', *[prefix + '.' + row['id'] for row in manifest['lights']]]
    for name in prospective:
        _fresh(bpy.data.objects, name)
        _fresh(bpy.data.collections, name)
    for row in manifest['lights']: _fresh(bpy.data.lights, prefix + '.' + row['id'] + '.Data')
    if scene.camera is None:
        _fresh(bpy.data.objects, prefix + '.Camera')
        _fresh(bpy.data.cameras, prefix + '.Camera.Data')
    if 'background' in manifest:
        _fresh(bpy.data.objects, prefix + '.Background')
        _fresh(bpy.data.meshes, prefix + '.Background.Mesh')
        _fresh(bpy.data.materials, prefix + '.Background.Material')
    if 'world' in manifest: _fresh(bpy.data.worlds, prefix + '.World')
    for board in manifest.get('boards', []):
        name = advanced.board_name(rig_id, board['id'])
        _fresh(bpy.data.objects, name)
        _fresh(bpy.data.meshes, name + '.Mesh')
        _fresh(bpy.data.materials, name + '.Material')
    for row in manifest['lights']:
        for key in advanced.LINK_PROPERTIES:
            if row.get(key): _fresh(bpy.data.collections, advanced.link_name(rig_id, row['id'], key))
    # Validate writable native properties before creating scene IDs.
    light_type = bpy.types.Light.bl_rna.properties
    if any(not row['use_shadow'] for row in manifest['lights']) and 'use_shadow' not in light_type:
        _fail('UNSUPPORTED', 'use_shadow is unavailable in the actual Blender light API')
    state = {'version': '1.0', 'rig_id': rig_id, 'scene': scene.name, 'view_layer': manifest['view_layer'],
             'mode': manifest['mode'], 'anchor': manifest['anchor'], 'targets': manifest['targets'],
             'box': box, 'collection': prefix + '.Lights', 'objects': [], 'materials': [], 'lights': {},
             'source_world': scene.world.name if scene.world else None,
             'source_world_fake_user': bool(scene.world.use_fake_user) if scene.world else None,
             'source_exposure': float(scene.view_settings.exposure),
             'source_camera': scene.camera.name if scene.camera else None,
             'source_eevee_use_raytracing': source_eevee.get('use_raytracing'),
             'original_lights': {}, 'world': None, 'manifest': deepcopy(manifest)}
    for obj in originals:
        state['original_lights'][obj.name] = {'original': _visibility(obj), 'structure': _original_structure(obj)}
    collection = _mark(bpy.data.collections.new(state['collection']), rig_id)
    scene.collection.children.link(collection)
    if scene.world: scene.world.use_fake_user = True
    if scene.camera is None:
        state['objects'].append(_created_camera(scene, collection, box, rig_id, prefix).name)
    if manifest['anchor'] == 'camera':
        parent = scene.camera
        if abs(parent.matrix_world.determinant()) < 1e-12: _fail('UNSUPPORTED', 'Camera anchor transform is singular')
    for row in manifest['lights']:
        name = prefix + '.' + row['id']
        data = _mark(bpy.data.lights.new(name + '.Data', row['type']), rig_id)
        data.use_nodes = False
        data.energy = row['energy']
        data.color = row['color']
        advanced.apply_native_controls(data, row)
        if hasattr(data, 'use_shadow'): data.use_shadow = row['use_shadow']
        if row['type'] == 'AREA':
            data.shape = row.get('shape', 'DISK')
            data.size = row.get('size', 1) * box['size']
            data.size_y = row.get('size_y', row.get('size', 1)) * box['size']
        elif row['type'] in ('POINT', 'SPOT'):
            data.shadow_soft_size = row.get('size', .1) * box['size']
        if row['type'] == 'SPOT':
            data.spot_size = math.radians(row.get('spot_size_deg', 45))
            data.spot_blend = row.get('spot_blend', .3)
        if row['type'] == 'SUN': data.angle = math.radians(row.get('angle_deg', .526))
        obj = _mark(bpy.data.objects.new(name, data), rig_id)
        obj['lw_id'] = row['id']
        obj['lw_role'] = row['role']
        collection.objects.link(obj)
        if parent:
            obj.parent = parent
            obj.matrix_parent_inverse = parent.matrix_world.inverted()
        _transform(obj, _position(box, row['location']), _position(box, row['aim']))
        obj.hide_render = obj.hide_viewport = not row['enabled']
        state['objects'].append(obj.name)
        state['lights'][row['id']] = {'object': obj.name, 'role': row['role'], 'type': row['type'], 'recipe': row}
    for spec in manifest.get('boards', []):
        board, material = advanced.create_board(spec, box, collection, rig_id)
        state['objects'].append(board.name)
        state['materials'].append(material.name)
        state.setdefault('boards', {})[spec['id']] = {'object': board.name, 'material': material.name,
            'recipe': deepcopy(spec), 'baseline': advanced.board_values(board),
            'structure': advanced.board_structure(board), 'visibility': _visibility(board)}
    for row in manifest['lights']:
        advanced.configure_links(scene, state, row['id'], {
            **{key: row.get(key, []) for key in advanced.LINK_PROPERTIES},
            **{key + '_mode': row.get(key + '_mode', 'INCLUDE') for key in advanced.LINK_PROPERTIES}})
    if 'background' in manifest:
        background, material = _background(manifest['background'], box, collection, rig_id, prefix)
        state['objects'].append(background.name)
        state['materials'].append(material.name)
    if 'world' in manifest:
        world = _world(manifest['world'], prefix + '.World', rig_id)
        scene.world = world
        state['world'] = world.name
        state['images'] = [image.name for image in bpy.data.images if image.get(OWNER) == rig_id]
    if 'exposure' in manifest: scene.view_settings.exposure = manifest['exposure']
    if 'eevee' in manifest:
        scene.eevee.use_raytracing = manifest['eevee']['use_raytracing']
        if _eevee_settings(scene)['use_raytracing'] != manifest['eevee']['use_raytracing']:
            _fail('VALIDATION_FAILED', 'Blender did not apply explicit Eevee ray tracing setting')
    for obj in originals:
        obj.hide_render = obj.hide_viewport = True
        state['original_lights'][obj.name]['applied'] = _visibility(obj)
    scene.view_layers[manifest['view_layer']].update()
    for light_id, row in state['lights'].items():
        obj = bpy.data.objects[row['object']]
        row['baseline'] = _light_values(obj, light_id in state.get('links', {}))
        row['structure'] = _light_structure(obj)
        row['visibility'] = _visibility(obj)
    state['extras'] = _extras_witness(state)
    state['world_witness'] = _world_witness(scene.world)
    state['applied_world'] = scene.world.name if scene.world else None
    state['applied_exposure'] = float(scene.view_settings.exposure)
    state['applied_camera'] = scene.camera.name if scene.camera else None
    state['applied_eevee_use_raytracing'] = _eevee_settings(scene).get('use_raytracing')
    _save_states(scene, {rig_id: state})
    return {'operation': 'create', 'rig_id': rig_id, 'changed_objects': state['objects'],
            'disabled_originals': list(state['original_lights']), 'box': box, 'anchor': state['anchor'],
            'eevee_change': {'source': state['source_eevee_use_raytracing'],
                             'applied': state['applied_eevee_use_raytracing'],
                             'requested': 'eevee' in manifest},
            'source_camera_locked': state['source_camera'] is not None, 'units': 'locations and light sizes use target maximum bounding-box extent; energy is native Blender units'}


def _conflict(actual, expected, message, mode):
    if mode == 'error' and not _close(actual, expected): _fail('CONFLICT', message)


def _check_structure(scene, state):
    collection, objects = _ensure_managed(scene, state)
    advanced.check_links(scene, state)
    for row in state['lights'].values():
        obj = objects[row['object']]
        if not _close(_light_structure(obj), row['structure']):
            _fail('CONFLICT', 'Light nodes/rotation/scale/parent/constraint/animation changed: ' + obj.name)
    actual_extras, expected_extras = _extras_witness(state), deepcopy(state['extras'])
    for board in state.get('boards', {}).values():
        for witness in (actual_extras, expected_extras):
            if witness.get(board['object']): witness[board['object']].pop('values', None)
    if not _close(actual_extras, expected_extras): _fail('CONFLICT', 'Managed camera/background/board structure changed; no safe field merge')
    if (scene.camera.name if scene.camera else None) != state['applied_camera']:
        _fail('CONFLICT', 'Scene camera binding changed')
    return collection, objects


def _patch(scene, manifest, state):
    bpy = _bpy()
    _check_structure(scene, state)
    box = _box(scene, state['targets'])
    operations = []
    for patch in manifest.get('patches', []):
        row = state['lights'].get(patch['id'])
        if row is None: _fail('NOT_FOUND', 'Managed light id not found: ' + patch['id'])
        obj = bpy.data.objects[row['object']]
        current = _light_values(obj, True)
        desired = deepcopy(patch['values'])
        advanced.validate_links(scene, desired)
        for key in advanced.LINK_PROPERTIES:
            if key + '_mode' in desired and key not in desired and not current[key]:
                _fail('INVALID_REQUEST', 'Mode patch requires nonempty existing or requested members: ' + key)
        for key in advanced.LINK_PROPERTIES:
            if desired.get(key) and not state.get('links', {}).get(patch['id'], {}).get(key, {}).get('collection'):
                _fresh(bpy.data.collections, advanced.link_name(state['rig_id'], patch['id'], key))
        if 'size_y' in desired and obj.data.type != 'AREA': _fail('INVALID_REQUEST', 'size_y requires AREA')
        if 'size' in desired and obj.data.type == 'SUN': _fail('INVALID_REQUEST', 'SUN size cannot be patched')
        if 'use_shadow' in desired and not hasattr(obj.data, 'use_shadow'): _fail('UNSUPPORTED', 'Native use_shadow unavailable')
        if 'location' in desired:
            world_position = _position(box, desired['location'])
            local = (obj.parent.matrix_world @ obj.matrix_parent_inverse).inverted() @ world_position if obj.parent else world_position
            desired['location'] = list(local)
        for key in ('size', 'size_y'):
            if key in desired: desired[key] *= box['size']
        for key, value in desired.items():
            expected = row['baseline'].get(key, 'INCLUDE' if key.endswith('_mode') else [] if key in advanced.LINK_PROPERTIES else None)
            if key in advanced.LINK_PROPERTIES: value = desired[key] = sorted(value)
            if not _close(current[key], expected) and not _close(current[key], value):
                if manifest['conflict'] == 'error': _fail('CONFLICT', 'Manual edit conflicts with patch: ' + patch['id'] + '.' + key)
            if key == 'enabled' and _visibility(obj) != row['visibility'] and manifest['conflict'] == 'error':
                _fail('CONFLICT', 'Manual visibility edit conflicts with patch: ' + patch['id'])
        operations.append((patch, row, obj, desired))
    board_operations = []
    for patch in manifest.get('board_patches', []):
        row = state.get('boards', {}).get(patch['id'])
        if row is None: _fail('NOT_FOUND', 'Managed board id not found: ' + patch['id'])
        obj = bpy.data.objects[row['object']]
        current = advanced.board_values(obj)
        desired = advanced.board_patch_desired(obj, patch['values'], box)
        for key, value in desired.items():
            if not _close(current[key], row['baseline'][key]) and not _close(current[key], value):
                if manifest['conflict'] == 'error': _fail('CONFLICT', 'Manual edit conflicts with board patch: ' + patch['id'] + '.' + key)
            if key == 'enabled' and _visibility(obj) != row['visibility'] and manifest['conflict'] == 'error':
                _fail('CONFLICT', 'Manual board visibility edit conflicts with patch: ' + patch['id'])
        board_operations.append((patch, row, obj, desired))
    # All conflicts are checked before any parameter mutation.
    for patch, row, obj, desired in operations:
        for key, value in desired.items():
            if key in LINK_FIELDS: continue
            if key == 'location': obj.location = value
            elif key == 'enabled': obj.hide_render = obj.hide_viewport = not value
            elif key == 'size': setattr(obj.data, 'size' if obj.data.type == 'AREA' else 'shadow_soft_size', value)
            else: setattr(obj.data, key, value)
        scene.view_layers[manifest['view_layer']].update()
        advanced.configure_links(scene, state, patch['id'], desired)
        observed = _light_values(obj, True)
        if any(key in desired for key in LINK_FIELDS):
            for key in LINK_FIELDS:
                row['baseline'].setdefault(key, observed[key])
            for key in advanced.LINK_PROPERTIES:
                if key in desired and not desired[key]:
                    row['baseline'][key + '_mode'] = 'INCLUDE'
                    row['recipe'][key + '_mode'] = 'INCLUDE'
        for key, value in desired.items():
            if not _close(observed[key], value): _fail('VALIDATION_FAILED', 'Blender clamped requested patch: ' + key)
            row['baseline'][key] = observed[key]
            row['recipe'][key] = deepcopy(patch['values'][key])
        if 'enabled' in desired: row['visibility'] = _visibility(obj)
        for original in state['manifest']['lights']:
            if original['id'] == patch['id']:
                original.update(deepcopy(patch['values']))
                for key in advanced.LINK_PROPERTIES:
                    if key in desired and not desired[key]: original[key + '_mode'] = 'INCLUDE'
    for patch, row, obj, desired in board_operations:
        advanced.apply_board_values(obj, desired)
        scene.view_layers[manifest['view_layer']].update()
        observed = advanced.board_values(obj)
        for key, value in desired.items():
            if not _close(observed[key], value): _fail('VALIDATION_FAILED', 'Blender clamped requested board patch: ' + key)
            row['baseline'][key] = observed[key]
            row['recipe'][key] = deepcopy(patch['values'][key])
        if 'enabled' in desired: row['visibility'] = _visibility(obj)
        state['extras'][obj.name]['values'] = deepcopy(row['baseline'])
        for original in state['manifest'].get('boards', []):
            if original['id'] == patch['id']: original.update(deepcopy(patch['values']))
    states = _states(scene)
    states[state['rig_id']] = state
    _save_states(scene, states)
    return {'operation': 'patch', 'rig_id': state['rig_id'], 'patches': deepcopy(manifest.get('patches', [])),
            'board_patches': deepcopy(manifest.get('board_patches', [])),
            'native_controls_edit_policy': 'normalize/spread_deg/diffuse_factor/specular_factor are create-only; patch fails schema validation',
            'manual_nonconflicting_edits': 'preserved; original field baseline retained until explicitly patched'}


def _restore(scene, manifest, state):
    bpy = _bpy()
    collection, objects = _check_structure(scene, state)
    mode = manifest['conflict']
    for light_id, row in state['lights'].items():
        obj = objects[row['object']]
        linked_baseline = any(k in row['baseline'] for k in advanced.LINK_PROPERTIES)
        expected = deepcopy(row['baseline'])
        if linked_baseline:
            for key in advanced.LINK_PROPERTIES: expected.setdefault(key + '_mode', 'INCLUDE')
        _conflict(_light_values(obj, linked_baseline), expected, 'Manual managed-light edits would be removed: ' + obj.name, mode)
        _conflict(_visibility(obj), row['visibility'], 'Manual light visibility would be removed: ' + obj.name, mode)
    for row in state.get('boards', {}).values():
        obj = objects[row['object']]
        _conflict(advanced.board_values(obj), row['baseline'], 'Manual board edits would be removed: ' + obj.name, mode)
        _conflict(_visibility(obj), row['visibility'], 'Manual board visibility would be removed: ' + obj.name, mode)
    _conflict(scene.world.name if scene.world else None, state['applied_world'], 'Scene World binding changed', mode)
    _conflict(_world_witness(scene.world), state['world_witness'], 'World content changed', mode)
    _conflict(float(scene.view_settings.exposure), state['applied_exposure'], 'Exposure changed', mode)
    restore_eevee = 'eevee' in state.get('manifest', {})
    if restore_eevee:
        current_eevee = _eevee_settings(scene)
        if 'use_raytracing' not in current_eevee:
            _fail('UNSUPPORTED', 'Scene.eevee.use_raytracing is unavailable during restore')
        _conflict(current_eevee['use_raytracing'], state['applied_eevee_use_raytracing'],
                  'Eevee ray tracing setting manually changed', mode)
    original_world = bpy.data.worlds.get(state['source_world']) if state['source_world'] else None
    if state['source_world'] and not original_world: _fail('CONFLICT', 'Original World no longer exists')
    original_objects = []
    for name, row in state['original_lights'].items():
        obj = bpy.data.objects.get(name)
        if not obj or obj.name not in scene.objects: _fail('CONFLICT', 'Original light no longer exists in scene: ' + name)
        _exclusive(obj, scene)
        if not _close(_original_structure(obj), row['structure']): _fail('CONFLICT', 'Original light structure changed: ' + name)
        _conflict(_visibility(obj), row['applied'], 'Original light visibility manually changed: ' + name, mode)
        original_objects.append((obj, row))
    for name in state.get('images', []):
        image = _owned(bpy.data.images.get(name), state['rig_id'])
        if image.users != 1: _fail('CONFLICT', 'Managed HDRI has outside consumers: ' + name)
    # No deletion before complete ownership, consumer and conflict checks.
    advanced.cleanup_links(state)
    scene.world = original_world
    if original_world: original_world.use_fake_user = state['source_world_fake_user']
    scene.view_settings.exposure = state['source_exposure']
    if restore_eevee: scene.eevee.use_raytracing = state['source_eevee_use_raytracing']
    scene.camera = bpy.data.objects.get(state['source_camera']) if state['source_camera'] else None
    for obj, row in original_objects:
        for key, value in row['original'].items(): setattr(obj, key, value)
    owned_data = [obj.data for obj in objects.values() if obj.data]
    for obj in objects.values(): bpy.data.objects.remove(obj, do_unlink=True)
    bpy.data.collections.remove(collection)
    for data in owned_data:
        if data.users: _fail('CONFLICT', 'Managed data acquired an outside consumer')
        group = bpy.data.lights if isinstance(data, bpy.types.Light) else bpy.data.cameras if isinstance(data, bpy.types.Camera) else bpy.data.meshes
        group.remove(data)
    for name in state.get('materials', []): bpy.data.materials.remove(bpy.data.materials[name])
    if state.get('world'):
        world = bpy.data.worlds.get(state['world'])
        if world: bpy.data.worlds.remove(world)
    for name in state.get('images', []):
        image = bpy.data.images.get(name)
        if image and image.get(OWNER) == state['rig_id'] and image.users == 0: bpy.data.images.remove(image)
    states = _states(scene)
    del states[state['rig_id']]
    _save_states(scene, states)
    return {'operation': 'restore', 'rig_id': state['rig_id'], 'removed_managed_objects': list(objects),
            'original_lights_restored': list(state['original_lights']), 'world_restored': state['source_world'],
            'exposure_restored': state['source_exposure'], 'conflict_policy': mode,
            'eevee_restored': state.get('source_eevee_use_raytracing') if restore_eevee else 'not_requested'}


def apply(manifest):
    manifest = normalize_manifest(manifest)
    scene = _scene(manifest['scene'])
    with _context(scene, manifest['view_layer']):
        if 'frame' in manifest: scene.frame_set(manifest['frame'])
        excluded_names = protection_exclusions(manifest)
        before = snapshot_protection(scene, excluded_names)
        allow_eevee = manifest['action'] == 'create' and 'eevee' in manifest
        if manifest['action'] == 'create':
            result = _create(scene, manifest)
        else:
            states = _states(scene)
            state = states.get(manifest['rig_id'])
            if not state: _fail('NOT_FOUND', 'Managed rig not found: ' + manifest['rig_id'])
            if state['view_layer'] != manifest['view_layer']: _fail('CONFLICT', 'Rig view layer differs from request')
            allow_eevee = manifest['action'] == 'restore' and 'eevee' in state.get('manifest', {})
            result = _patch(scene, manifest, state) if manifest['action'] == 'patch' else _restore(scene, manifest, state)
        after = snapshot_protection(scene, excluded_names)
        protection = verify_protection(before, after, allowed_eevee_scene=scene.name if allow_eevee else None)
        if protection['status'] != 'pass': _fail('VALIDATION_FAILED', 'Non-target content changed at ' + str(protection['difference']))
        result.update(excluded_names=excluded_names, protection={**protection, 'status': 'passed'}, after_snapshot=after)
        return result


def protection_exclusions(manifest):
    manifest = normalize_manifest(manifest)
    scene = _scene(manifest['scene'])
    if manifest['action'] == 'create':
        keep = set(manifest.get('keep_lights', []))
        return sorted(o.name for o in scene.objects if o.type == 'LIGHT' and o.name not in keep) if manifest['mode'] == 'relight' else []
    state = _states(scene).get(manifest['rig_id'])
    if not state: _fail('NOT_FOUND', 'Managed rig not found: ' + manifest['rig_id'])
    return sorted(state['original_lights'])


def inspect(scene, rig_id=None):
    scene = _scene(scene) if isinstance(scene, str) else scene
    bpy = _bpy()
    states = _states(scene)
    if rig_id is not None and rig_id not in states:
        return {'scene': scene.name, 'status': 'missing', 'rig_id': rig_id, 'rigs': [],
                'eevee': _eevee_settings(scene),
                'camera': scene.camera.name if scene.camera else None,
                'world': scene.world.name if scene.world else None, 'exposure': float(scene.view_settings.exposure)}
    rigs = []
    for key, state in states.items():
        if rig_id is not None and key != rig_id: continue
        lights = []
        for light_id, row in state['lights'].items():
            obj = bpy.data.objects.get(row['object'])
            if not obj: _fail('CONFLICT', 'Managed light missing: ' + row['object'])
            actual = _light_values(obj, True)
            world_matrix = managed_world_matrix(obj, scene=scene, view_layer=state['view_layer'])
            lights.append({'id': light_id, 'object': obj.name, 'object_name': obj.name, 'role': row['role'],
                'type': obj.data.type, **actual, 'world_location': list(world_matrix.translation),
                'world_direction': list(world_matrix.to_quaternion() @ __import__('mathutils').Vector((0, 0, -1))),
                'shape': getattr(obj.data, 'shape', None),
                'angle_deg': math.degrees(obj.data.angle) if obj.data.type == 'SUN' else None,
                'spot_size_deg': math.degrees(obj.data.spot_size) if obj.data.type == 'SPOT' else None,
                'spot_blend': getattr(obj.data, 'spot_blend', None),
                'native_controls': advanced.native_controls(obj),
                'native_controls_edit_policy': 'create-only',
                'link_collections': {key: getattr(obj.light_linking, prop).name if getattr(obj.light_linking, prop) else None for key, prop in advanced.LINK_PROPERTIES.items()},
                'manual_fields': [k for k, v in actual.items() if not _close(v, row['baseline'].get(k, 'INCLUDE' if k.endswith('_mode') else [] if k in advanced.LINK_PROPERTIES else None))]})
        boards = []
        for board_id, row in state.get('boards', {}).items():
            obj = bpy.data.objects.get(row['object'])
            if not obj: _fail('CONFLICT', 'Managed board missing: ' + row['object'])
            values = advanced.board_values(obj)
            world_matrix = managed_world_matrix(obj, scene=scene, view_layer=state['view_layer'])
            boards.append({'id': board_id, 'object': obj.name, 'kind': row['recipe']['kind'], **values,
                           'world_location': list(world_matrix.translation),
                           'manual_fields': [k for k, v in values.items() if not _close(v, row['baseline'].get(k))]})
        rigs.append({'rig_id': key, 'collection': state['collection'], 'anchor': state['anchor'],
                     'mode': state['mode'], 'targets': state['targets'], 'lights': lights, 'boards': boards,
                     'box': deepcopy(state['box']), 'manifest': deepcopy(state['manifest']),
                     'world': state['world'], 'exposure': float(scene.view_settings.exposure),
                     'source_camera': state['source_camera']})
    return {'scene': scene.name, 'rigs': rigs, 'camera': scene.camera.name if scene.camera else None,
            'eevee': _eevee_settings(scene),
            'world': scene.world.name if scene.world else None, 'exposure': float(scene.view_settings.exposure),
            'scene_lights': [{'object': o.name, 'type': o.data.type, 'energy': o.data.energy,
                             'color': list(o.data.color), 'enabled': not o.hide_render and not o.hide_viewport,
                             'managed': o.get(OWNER)} for o in scene.objects if o.type == 'LIGHT']}


def snapshot_protection(scene, excluded_names=()):
    """Content witnesses; only listed original LIGHT visibility may change."""
    scene = _scene(scene) if isinstance(scene, str) else scene
    bpy = _bpy()
    excluded_names = set(excluded_names)
    retained_source_worlds = {scene.world.name} if scene.world else set()
    # Exempt only IDs enumerated by the active target Scene's state. Owned IDs
    # belonging to another Scene or unrecorded IDs keep full protection.
    managed = set()
    managed_collections = set()
    for state in _states(scene).values():
        if state.get('source_world'): retained_source_worlds.add(state['source_world'])
        names = {state['collection'], *[r['collection'] for fields in state.get('links', {}).values() for r in fields.values() if r['collection']]}
        managed_collections.update(names)
        for name in names:
            item = bpy.data.collections.get(name)
            if item and item.get(OWNER) == state['rig_id']: managed.add(item.as_pointer())
        ids = [bpy.data.objects.get(name) for name in state['objects']]
        ids.extend(bpy.data.materials.get(name) for name in state.get('materials', []))
        ids.extend(bpy.data.images.get(name) for name in state.get('images', []))
        if state.get('world'): ids.append(bpy.data.worlds.get(state['world']))
        for item in ids:
            if not item or item.get(OWNER) != state['rig_id']: continue
            managed.add(item.as_pointer())
            data = getattr(item, 'data', None)
            if data and data.get(OWNER) == state['rig_id']: managed.add(data.as_pointer())
            for tree in (getattr(item, 'node_tree', None), getattr(data, 'node_tree', None)):
                if tree and tree.get(OWNER) == state['rig_id']: managed.add(tree.as_pointer())
    def exempt(item):
        return item.as_pointer() in managed
    result = {'version': '1.0', 'scene': scene.name, 'records': {}, 'scene_memberships': {},
              'coverage': 'inspection.record mesh/UV/material/nodes/camera/animation; raw object transforms, collection membership and other-scene World/exposure; not arbitrary plugin side effects'}
    def identity(item):
        return {'type': item.bl_rna.identifier, 'name': item.name,
                'library': bpy.path.abspath(item.library.filepath) if item.library else None}
    items, seen = [], set()
    for prop in bpy.data.bl_rna.properties:
        if prop.type != 'COLLECTION': continue
        for item in getattr(bpy.data, prop.identifier):
            if isinstance(item, bpy.types.ID) and item.as_pointer() not in seen:
                seen.add(item.as_pointer())
                if item.bl_rna.identifier not in ('Screen', 'WorkSpace', 'WindowManager'):
                    items.append(item)
    for item in items:
        if exempt(item): continue
        if isinstance(item, bpy.types.Image) and item.name in ('Render Result', 'Viewer Node'): continue
        row = _record(item)
        if isinstance(item, bpy.types.Scene) and item == scene:
            row['custom_properties'].pop(META, None)
            # Creating a camera is allowed only when the source scene had none.
            if item.camera and exempt(item.camera): row['scene']['camera'] = None
        if isinstance(item, bpy.types.World) and item.name in retained_source_worlds:
            # Retaining the original World is a declared persistence operation.
            row.pop('fake_user', None)
            row.get('settings', {}).pop('use_fake_user', None)
        if isinstance(item, bpy.types.Object):
            row['hide_viewport'] = item.hide_viewport
            row['collections'] = sorted(c.name for c in item.users_collection if not exempt(c))
            if item.name in excluded_names and item.type == 'LIGHT':
                row['object'].pop('hide_render', None)
                row.pop('hide_viewport', None)
        if isinstance(item, bpy.types.Collection):
            row['members'] = sorted(o.name for o in item.objects if not exempt(o))
            row['children'] = sorted(c.name for c in item.children if not exempt(c))
        result['records'][json.dumps(identity(item), sort_keys=True, ensure_ascii=False)] = row
    for other in bpy.data.scenes:
        row = {'objects': sorted(o.name for o in other.objects if not exempt(o)),
               'collections': sorted(c.name for c in other.collection.children if not exempt(c)),
               'eevee': _eevee_settings(other)}
        if other != scene:
            row.update(world=identity(other.world) if other.world else None,
                       exposure=float(other.view_settings.exposure), camera=identity(other.camera) if other.camera else None)
        result['scene_memberships'][other.name] = row
    return result


def verify_protection(before, after, allowed_eevee_scene=None):
    # New unowned IDs are also changes; no subset comparison hides leakage.
    def compare(a, b, path='$'):
        if isinstance(a, dict) and isinstance(b, dict):
            if a.keys() != b.keys(): return path + '/keys'
            for key in a:
                delta = compare(a[key], b[key], path + '/' + key)
                if delta: return delta
        elif isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            if len(a) != len(b): return path + '/length'
            for i, (x, y) in enumerate(zip(a, b)):
                delta = compare(x, y, path + '/' + str(i))
                if delta: return delta
        elif not _close(a, b): return path
        return None
    comparable_before = deepcopy(before)
    declared_changes = []
    if allowed_eevee_scene is not None:
        old = before['scene_memberships'][allowed_eevee_scene]['eevee']
        new = after['scene_memberships'][allowed_eevee_scene]['eevee']
        if set(old) != {'use_raytracing'} or set(new) != {'use_raytracing'}:
            _fail('UNSUPPORTED', 'Explicit Eevee protection exception requires available native field')
        comparable_before['scene_memberships'][allowed_eevee_scene]['eevee'] = deepcopy(new)
        declared_changes.append({'scene': allowed_eevee_scene, 'field': 'eevee.use_raytracing',
                                 'source': old['use_raytracing'], 'applied': new['use_raytracing']})
    difference = compare(comparable_before, after)
    return {'status': 'fail' if difference else 'pass', 'difference': difference,
            'before_sha256': _digest(before), 'after_sha256': _digest(after), 'coverage': before.get('coverage'),
            'declared_changes': declared_changes}
