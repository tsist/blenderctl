"""Native M2 controls, owned link collections and editable control boards."""
from copy import deepcopy
import math

LINK_PROPERTIES = {'receivers': 'receiver_collection', 'blockers': 'blocker_collection'}
NATIVE_PROPERTIES = {'normalize': 'normalize', 'spread_deg': 'spread',
                     'diffuse_factor': 'diffuse_factor', 'specular_factor': 'specular_factor'}


def _core():
    from . import core
    return core


def validate_native_controls(row):
    c = _core()
    bpy = c._bpy()
    native_type = {'AREA': bpy.types.AreaLight, 'POINT': bpy.types.PointLight,
                   'SPOT': bpy.types.SpotLight, 'SUN': bpy.types.SunLight}[row['type']]
    for field, propname in NATIVE_PROPERTIES.items():
        if field not in row: continue
        prop = native_type.bl_rna.properties.get(propname)
        if prop is None or prop.is_readonly:
            c._fail('UNSUPPORTED', 'Native writable property unavailable: ' + propname)
        value = math.radians(row[field]) if field == 'spread_deg' else row[field]
        if prop.type == 'FLOAT' and not prop.hard_min <= value <= prop.hard_max:
            c._fail('UNSUPPORTED', 'Requested value outside actual RNA limits: ' + field)


def native_controls(obj):
    return {field: math.degrees(getattr(obj.data, prop)) if field == 'spread_deg' else getattr(obj.data, prop)
            for field, prop in NATIVE_PROPERTIES.items() if hasattr(obj.data, prop)}


def apply_native_controls(data, row):
    c = _core()
    for field, prop in NATIVE_PROPERTIES.items():
        if field in row:
            value = math.radians(row[field]) if field == 'spread_deg' else row[field]
            setattr(data, prop, value)
            if not c._close(getattr(data, prop), value):
                c._fail('VALIDATION_FAILED', 'Native light control was clamped: ' + field)


def board_name(rig_id, board_id):
    return 'LW.' + rig_id + '.Board.' + board_id


def link_name(rig_id, light_id, key):
    return 'LW.' + rig_id + '.Link.' + light_id + '.' + key


def validate_links(scene, row, new_boards=()):
    c = _core()
    bpy = c._bpy()
    properties = bpy.types.ObjectLightLinking.bl_rna.properties if hasattr(bpy.types, 'ObjectLightLinking') else {}
    for key, prop in LINK_PROPERTIES.items():
        if key + '_members' in row:
            _validate_members(scene, row[key + '_members'], new_boards)
            continue
        if key not in row and key + '_mode' not in row: continue
        if prop not in properties or properties[prop].is_readonly:
            c._fail('UNSUPPORTED', 'Native light linking unavailable: ' + key)
        for name in row.get(key, []):
            obj = scene.objects.get(name)
            if obj is None:
                if name in new_boards: continue
                c._fail('NOT_FOUND', 'Link reference is not a named object in this Scene: ' + name)
            if obj.type not in ('MESH', 'CURVE', 'SURFACE', 'META', 'FONT', 'VOLUME'):
                c._fail('UNSUPPORTED', 'Link reference must be geometry: ' + name)
            if any(obj.name in other.objects for other in bpy.data.scenes if other != scene):
                c._fail('CONFLICT', 'Link reference is shared across Scenes: ' + name)
        state_type = getattr(bpy.types, 'CollectionLightLinking', None)
        state_prop = state_type.bl_rna.properties.get('link_state') if state_type else None
        if not state_prop or state_prop.is_readonly or row.get(key + '_mode', 'INCLUDE') not in state_prop.enum_items:
            c._fail('UNSUPPORTED', 'Native INCLUDE member linking unavailable')


def _validate_members(scene, members, new_boards):
    c = _core()
    bpy = c._bpy()
    scene_collections = {scene.collection, *scene.collection.children_recursive}
    for member in members:
        name = member['name']
        if member['kind'] == 'OBJECT':
            validate_links(scene, {'receivers': [name], 'receivers_mode': member['state']}, new_boards)
            continue
        collection = bpy.data.collections.get(name)
        if collection is None or collection not in scene_collections:
            c._fail('NOT_FOUND', 'Light-link collection must be in this scene: ' + name)
        if collection.get(c.OWNER):
            c._fail('CONFLICT', 'Light-link target collection is owned by another rig: ' + name)
        if any(collection in other.collection.children_recursive for other in bpy.data.scenes if other != scene):
            c._fail('CONFLICT', 'Light-link collection is shared across Scenes: ' + name)


def _member_values(collection):
    rows = []
    for kind, items, metadata in (('OBJECT', collection.objects, collection.collection_objects),
                                  ('COLLECTION', collection.children, collection.collection_children)):
        rows.extend({'name': item.name, 'kind': kind, 'state': settings.light_linking.link_state}
                    for item, settings in zip(items, metadata, strict=True))
    return sorted(rows, key=lambda row: (row['kind'], row['name']))


def link_values(obj):
    c = _core()
    values = {}
    for key, prop in LINK_PROPERTIES.items():
        collection = getattr(obj.light_linking, prop)
        if collection and collection.get('lw_member_links'):
            values[key + '_members'] = _member_values(collection)
            values[key] = []
            values[key + '_mode'] = 'INCLUDE'
            continue
        values[key] = sorted(o.name for o in collection.objects) if collection else []
        modes = {member.light_linking.link_state for member in collection.collection_objects} if collection else set()
        if len(modes) > 1: c._fail('CONFLICT', 'Mixed member link states cannot be represented safely: ' + obj.name + '.' + key)
        values[key + '_mode'] = next(iter(modes)) if modes else 'INCLUDE'
    return values


def _link_witness(collection):
    c = _core()
    witness = {'id': c._record(collection), 'members': sorted([
        {'name': item.name, 'type': item.type,
         'data': item.data.name if item.data else None,
         'library': item.library.filepath if item.library else None,
         'link_state': member.light_linking.link_state}
        for item, member in zip(collection.objects, collection.collection_objects)], key=lambda row: row['name']),
        'children': sorted(child.name for child in collection.children),
        'visibility': [collection.hide_render, collection.hide_viewport, collection.hide_select],
        'offset': list(collection.instance_offset), 'exporters': len(collection.exporters)}
    if collection.get('lw_member_links'):
        witness['explicit_members'] = _member_values(collection)
    return witness


def _check_consumers(collection, emitter):
    c = _core()
    bpy = c._bpy()
    consumers = bpy.data.user_map(subset={collection}).get(collection, set())
    if consumers != {emitter}:
        c._fail('CONFLICT', 'Private link collection has unexpected consumers: ' + collection.name)
    for obj in bpy.data.objects:
        if obj.instance_collection == collection:
            c._fail('CONFLICT', 'Private link collection is instanced: ' + collection.name)
        for prop in LINK_PROPERTIES.values():
            if getattr(obj.light_linking, prop) == collection and obj != emitter:
                c._fail('CONFLICT', 'Link collection is shared with another emitter: ' + collection.name)
    if any(collection in list(parent.children) for parent in bpy.data.collections):
        c._fail('CONFLICT', 'Private link collection was attached to another collection')
    if any(collection in list(scene.collection.children) for scene in bpy.data.scenes):
        c._fail('CONFLICT', 'Private link collection was attached to a Scene')


def check_links(scene, state):
    c = _core()
    bpy = c._bpy()
    for light_id, light in state['lights'].items():
        obj = bpy.data.objects[light['object']]
        for key, prop in LINK_PROPERTIES.items():
            if key not in state.get('links', {}).get(light_id, {}) and getattr(obj.light_linking, prop) is not None:
                c._fail('CONFLICT', 'Unrecorded manual link collection: ' + obj.name + '.' + key)
    for light_id, fields in state.get('links', {}).items():
        obj = bpy.data.objects[state['lights'][light_id]['object']]
        for key, row in fields.items():
            actual = getattr(obj.light_linking, LINK_PROPERTIES[key])
            if row['collection'] is None:
                if actual is not None: c._fail('CONFLICT', 'Manual link collection binding changed: ' + obj.name + '.' + key)
                continue
            collection = c._owned(bpy.data.collections.get(row['collection']), state['rig_id'])
            if actual != collection: c._fail('CONFLICT', 'Manual link collection binding changed: ' + obj.name + '.' + key)
            _check_consumers(collection, obj)
            witness = _link_witness(collection)
            if collection.get('lw_member_links'):
                _validate_members(scene, witness['explicit_members'], ())
                if not c._close(witness, row['witness']):
                    c._fail('CONFLICT', 'Explicit light-link members or settings changed: ' + collection.name)
                continue
            validate_links(scene, {key: [m['name'] for m in witness['members']]})
            expected = deepcopy(row['witness'])
            # Membership is an editable field; settings, INCLUDE state and surviving
            # reference identities remain guarded independently from its baseline.
            old_members = {m['name']: m for m in expected.pop('members')}
            actual_members = witness.pop('members')
            modes = {m['link_state'] for m in actual_members}
            if len(modes) > 1: c._fail('CONFLICT', 'Mixed member link states: ' + collection.name)
            for member in actual_members:
                original = deepcopy(old_members.get(member['name']))
                if original: original['link_state'] = member['link_state']
                if member['link_state'] not in ('INCLUDE', 'EXCLUDE') or (original and member != original):
                    c._fail('CONFLICT', 'Manual link member identity/state changed: ' + member['name'])
            if not c._close(witness, expected):
                c._fail('CONFLICT', 'Manual link members/settings changed: ' + collection.name)


def configure_links(scene, state, light_id, values):
    c = _core()
    bpy = c._bpy()
    obj = bpy.data.objects[state['lights'][light_id]['object']]
    current = link_values(obj)
    fields = state.setdefault('links', {}).setdefault(light_id, {})
    for key, prop in LINK_PROPERTIES.items():
        if key + '_members' in values:
            name = link_name(state['rig_id'], light_id, key)
            c._fresh(bpy.data.collections, name)
            collection = c._mark(bpy.data.collections.new(name), state['rig_id'])
            collection.use_fake_user = True
            collection['lw_member_links'] = True
            requested = values[key + '_members']
            states = {(m['kind'], m['name']): m['state'] for m in requested}
            for member in requested:
                if member['kind'] == 'OBJECT':
                    collection.objects.link(scene.objects[member['name']])
                else:
                    collection.children.link(bpy.data.collections[member['name']])
            for kind, items, metadata in (('OBJECT', collection.objects, collection.collection_objects),
                                          ('COLLECTION', collection.children, collection.collection_children)):
                for item, settings in zip(items, metadata, strict=True):
                    settings.light_linking.link_state = states[kind, item.name]
            setattr(obj.light_linking, prop, collection)
            _check_consumers(collection, obj)
            fields[key] = {'collection': collection.name, 'refs': [], 'mode': 'INCLUDE',
                           'witness': _link_witness(collection)}
            continue
        if key not in values and key + '_mode' not in values: continue
        if key + '_members' in current:
            c._fail('UNSUPPORTED', 'Legacy link patch cannot replace explicit object/collection members: ' + obj.name)
        refs = values.get(key, current[key])
        mode = values.get(key + '_mode', current[key + '_mode'])
        name = link_name(state['rig_id'], light_id, key)
        prior = fields.get(key)
        collection = bpy.data.collections.get(prior['collection']) if prior and prior['collection'] else None
        if refs:
            if collection is None:
                collection = new_link_collection(scene, name, refs, mode, state['rig_id'])
            else:
                _populate_link_collection(scene, collection, refs, mode)
            setattr(obj.light_linking, prop, collection)
            fields[key] = {'collection': collection.name, 'refs': sorted(refs), 'mode': mode,
                           'witness': _link_witness(collection)}
        else:
            setattr(obj.light_linking, prop, None)
            if collection is not None: bpy.data.collections.remove(collection)
            fields[key] = {'collection': None, 'refs': [], 'mode': 'INCLUDE', 'witness': None}


def _populate_link_collection(scene, collection, refs, mode):
    for member in list(collection.objects): collection.objects.unlink(member)
    for ref in refs: collection.objects.link(scene.objects[ref])
    for member in collection.collection_objects: member.light_linking.link_state = mode


def new_link_collection(scene, name, refs, mode, owner=None):
    c = _core()
    collections = c._bpy().data.collections
    c._fresh(collections, name)
    collection = collections.new(name)
    try:
        if owner:
            c._mark(collection, owner)
            collection.use_fake_user = True
        _populate_link_collection(scene, collection, refs, mode)
    except Exception:
        collections.remove(collection)
        raise
    return collection


def cleanup_links(state):
    c = _core()
    bpy = c._bpy()
    names = set()
    for light_id, fields in state.get('links', {}).items():
        obj = bpy.data.objects[state['lights'][light_id]['object']]
        for key, row in fields.items():
            setattr(obj.light_linking, LINK_PROPERTIES[key], None)
            if row['collection']: names.add(row['collection'])
    for name in names: bpy.data.collections.remove(bpy.data.collections[name])


def board_values(obj):
    bsdf = next(n for n in obj.data.materials[0].node_tree.nodes if n.bl_idname == 'ShaderNodeBsdfPrincipled')
    return {'location': list(obj.location), 'rotation_deg': [math.degrees(x) for x in obj.rotation_euler],
            'size': list(obj.scale[:2]), 'color': list(bsdf.inputs['Base Color'].default_value[:3]),
            'roughness': float(bsdf.inputs['Roughness'].default_value),
            'enabled': not obj.hide_render and not obj.hide_viewport}


def board_structure(obj):
    c = _core()
    material = obj.data.materials[0]
    object_row = c._record(obj)
    object_row['object'].pop('matrix_basis', None)
    object_row['object'].pop('hide_render', None)
    # Z scale is not a supported board size field.
    object_row['scale_z'] = float(obj.scale.z)
    mat_row = c._record(material)
    bsdf = next(n for n in material.node_tree.nodes if n.bl_idname == 'ShaderNodeBsdfPrincipled')
    fields = {bsdf.inputs[key].identifier for key in ('Base Color', 'Roughness')}
    for node in mat_row.get('nodes', {}).get('nodes', []):
        if node['name'] == bsdf.name:
            node['inputs'] = [v for v in node['inputs'] if v['id'] not in fields]
    return {'object': object_row, 'mesh': c._record(obj.data), 'material': mat_row,
            'base_color_alpha': float(bsdf.inputs['Base Color'].default_value[3]),
            'layer_hidden': {layer.name: obj.hide_get(view_layer=layer) for layer in c._scene_for_object(obj).view_layers}}


def board_patch_desired(obj, values, box):
    c = _core()
    result = deepcopy(values)
    if 'location' in result: result['location'] = list(c._position(box, result['location']))
    if 'size' in result: result['size'] = [x * box['size'] for x in result['size']]
    return result


def apply_board_values(obj, values):
    bsdf = next(n for n in obj.data.materials[0].node_tree.nodes if n.bl_idname == 'ShaderNodeBsdfPrincipled')
    for field, value in values.items():
        if field == 'location': obj.location = value
        elif field == 'rotation_deg': obj.rotation_euler = [math.radians(x) for x in value]
        elif field == 'size': obj.scale = (*value, 1)
        elif field == 'color': bsdf.inputs['Base Color'].default_value = [*value, 1]
        elif field == 'roughness': bsdf.inputs['Roughness'].default_value = value
        elif field == 'enabled': obj.hide_render = obj.hide_viewport = not value


def create_board(spec, box, collection, rig_id):
    c = _core()
    bpy = c._bpy()
    name = board_name(rig_id, spec['id'])
    mesh = c._mark(bpy.data.meshes.new(name + '.Mesh'), rig_id)
    mesh.from_pydata([(-.5, -.5, 0), (.5, -.5, 0), (.5, .5, 0), (-.5, .5, 0)], [], [(0, 1, 2, 3)])
    mesh.update()
    material = c._mark(bpy.data.materials.new(name + '.Material'), rig_id)
    material.use_nodes = True
    c._mark(material, rig_id)
    mesh.materials.append(material)
    obj = c._mark(bpy.data.objects.new(name, mesh), rig_id)
    obj['lw_board_id'] = spec['id']
    obj['lw_board_kind'] = spec['kind']
    obj.rotation_mode = 'XYZ'
    collection.objects.link(obj)
    apply_board_values(obj, board_patch_desired(obj, {key: spec[key] for key in
        ('location', 'rotation_deg', 'size', 'color', 'roughness', 'enabled')}, box))
    return obj, material
