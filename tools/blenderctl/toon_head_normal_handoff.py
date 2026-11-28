"""Author and verify Toon normal fields and head profiles in saved Blender copies."""

from contextlib import ExitStack
import hashlib
import importlib
import importlib.util
import json
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import uuid

import toon_panel_handoff as panel
from filesystem import FileGuard, PathLocks
from protocol import Failure, atomic_json, digest, envelope


SETTINGS = (
    'reference', 'weight_attribute', 'strength', 'mapping_attribute',
    'direction', 'blend', 'weight_value', 'weight_blend',
)
NORMAL_OPERATIONS = (
    'normal-inspect', 'normal-settings', 'normal-zero-weight',
    'normal-create-field', 'normal-apply-strength', 'normal-bind-diffuse',
    'normal-disconnect-diffuse', 'normal-initialize-mapping',
    'normal-enable-mapping', 'normal-restore-ellipsoid',
    'normal-write-direction', 'normal-write-weight', 'normal-render',
)
HEAD_OPERATIONS = ('head-add', 'head-apply', 'head-inspect', 'head-controls',
                   'head-role', 'head-strength', 'head-remove', 'head-clear')
OPERATIONS = (*NORMAL_OPERATIONS, *HEAD_OPERATIONS)
READ_ONLY = {'normal-inspect', 'head-inspect', 'head-controls'}
RENDER = 'normal-render'
ROLES = {'FACE', 'LASH', 'BROW', 'HAIR', 'EYE', 'MOUTH', 'ACCESSORY'}


def require(condition, code, detail):
    if not condition:
        raise Failure(code, detail)


def name(value):
    return isinstance(value, str) and 0 < len(value) <= 255 and all(ord(char) >= 32 for char in value)


def setting_name(value):
    limit = 63
    return (isinstance(value, str) and 0 < len(value.encode('utf8')) <= limit
            and '\x00' not in value)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def unit_float(value):
    return finite(value) and 0 <= value <= 1


def vector(value):
    return (isinstance(value, (list, tuple)) and len(value) == 3
            and all(finite(item) for item in value))


def validate_settings(value):
    require(isinstance(value, dict) and value.keys() <= set(SETTINGS),
            'INVALID_REQUEST', 'settings must be an object using only normal_ui fields')
    for key, item in value.items():
        if key == 'reference':
            require(item is None or name(item), 'INVALID_REQUEST', 'reference must be an Object name or null')
        elif key in ('weight_attribute',):
            require(setting_name(item), 'INVALID_REQUEST', 'weight_attribute must be 1..63 UTF-8 bytes')
        elif key == 'mapping_attribute':
            require(setting_name(item), 'INVALID_REQUEST',
                    'mapping_attribute must be 1..63 UTF-8 bytes without NUL')
        elif key == 'direction':
            require(vector(item), 'INVALID_REQUEST', 'direction must be three finite numbers')
        else:
            require(unit_float(item), 'INVALID_REQUEST', f'{key} must be a finite number in [0,1]')


def validate_indices(value):
    require(isinstance(value, list) and value, 'INVALID_REQUEST', 'indices must be a nonempty list')
    require(all(type(item) is int and item >= 0 for item in value), 'INVALID_REQUEST',
            'indices must be nonnegative integers, never bool')
    require(len(set(value)) == len(value), 'INVALID_REQUEST', 'indices must be unique')


def read_request(args, guards):
    path = panel.guard_file(args.params.absolute(), guards)
    params = panel.read_document(path)
    require(isinstance(params, dict), 'INVALID_REQUEST', 'Head-normal params must be a JSON object')
    required = {'operation', 'edition', 'file', 'expected_sha256', 'context', 'target'}
    action = params.get('operation')
    require(action in OPERATIONS, 'INVALID_REQUEST', 'Unknown head-normal operation')
    allowed_extra = {
        'normal-inspect': {'indices'},
        'normal-settings': {'settings'},
        'normal-zero-weight': {'settings'},
        'normal-create-field': {'settings'},
        'normal-apply-strength': {'settings'},
        'normal-bind-diffuse': set(),
        'normal-disconnect-diffuse': set(),
        'normal-initialize-mapping': {'settings'},
        'normal-enable-mapping': {'settings'},
        'normal-restore-ellipsoid': set(),
        'normal-write-direction': {'settings', 'indices'},
        'normal-write-weight': {'settings', 'indices'},
        'normal-render': {'render'},
        'head-add': {'head'},
        'head-apply': set(),
        'head-inspect': set(),
        'head-controls': set(),
        'head-role': {'head'},
        'head-strength': {'head'},
        'head-remove': {'head'},
        'head-clear': set(),
    }[action]
    require(required <= params.keys() <= required | allowed_extra, 'INVALID_REQUEST',
            f'Unexpected or missing names for {action}')
    require(isinstance(params['edition'], str) and params['edition'] in panel.PRODUCTS,
            'UNSUPPORTED', 'Unknown Toon product edition')
    ctx = params['context']
    require(isinstance(ctx, dict) and ctx.keys() == {'scene', 'view_layer', 'frame'}
            and name(ctx['scene']) and name(ctx['view_layer']) and type(ctx['frame']) is int,
            'INVALID_REQUEST', 'Expected exact scene, view_layer and integer frame')
    target = params['target']
    require(isinstance(target, dict) and name(target.get('object')), 'INVALID_REQUEST', 'Expected a named mesh')
    slot_required = action in ('normal-bind-diffuse','normal-disconnect-diffuse','normal-render')
    if slot_required or target.keys() != {'object'}:
        require(target.keys() == {'object', 'material_slot', 'expected_material', 'expected_link'}
                and type(target['material_slot']) is int and target['material_slot'] >= 0
                and (target['expected_material'] is None or name(target['expected_material']))
                and target['expected_link'] in ('DATA', 'OBJECT'), 'INVALID_REQUEST',
                'Material operations need a pinned slot, material and DATA/OBJECT link')
    require(isinstance(params['file'], str) and Path(params['file']).suffix.lower() == '.blend'
            and isinstance(params['expected_sha256'], str), 'INVALID_REQUEST',
            'Expected a hash-pinned .blend')
    params['file'] = str(panel.guard_file(params['file'], guards, params['expected_sha256']))
    params['normal'] = True

    settings = params.get('settings', {})
    validate_settings(settings)
    if action in ('normal-settings', 'normal-zero-weight', 'normal-create-field',
                  'normal-apply-strength', 'normal-initialize-mapping',
                  'normal-enable-mapping', 'normal-write-direction', 'normal-write-weight'):
        require(settings, 'INVALID_REQUEST', f'{action} needs an explicit settings patch')
    if action == 'normal-zero-weight':
        require('weight_attribute' in settings, 'INVALID_REQUEST', 'zero-weight needs settings.weight_attribute')
    if action == 'normal-create-field':
        require({'reference', 'weight_attribute'} <= settings.keys(), 'INVALID_REQUEST',
                'create-field needs settings.reference and settings.weight_attribute')
    if action == 'normal-apply-strength':
        require('strength' in settings, 'INVALID_REQUEST', 'apply-strength needs settings.strength')
    if action in ('normal-initialize-mapping', 'normal-enable-mapping'):
        require('mapping_attribute' in settings, 'INVALID_REQUEST',
                f'{action} needs settings.mapping_attribute')
    if action == 'normal-write-direction':
        require({'mapping_attribute', 'direction', 'blend'} <= settings.keys(), 'INVALID_REQUEST',
                'write-direction needs mapping_attribute, direction and blend')
        require(math.hypot(*[float(item) for item in settings['direction']]) > 1e-9,
                'INVALID_REQUEST', 'write-direction needs a nonzero direction')
    if action == 'normal-write-weight':
        require({'weight_value', 'weight_blend'} <= settings.keys(), 'INVALID_REQUEST',
                'write-weight needs weight_value and weight_blend')
    if action in ('normal-write-direction', 'normal-write-weight'):
        require('indices' in params, 'INVALID_REQUEST', f'{action} needs explicit indices')
        validate_indices(params['indices'])
    if action == 'normal-inspect' and 'indices' in params:
        validate_indices(params['indices'])
    if action == 'head-add':
        head = params.get('head')
        require(isinstance(head, dict) and {'reference', 'groups'} <= head.keys() <= {'reference', 'groups', 'bindings'}
                and name(head['reference']) and isinstance(head['groups'], list) and 1 <= len(head['groups']) <= 8,
                'INVALID_REQUEST', 'head-add needs reference and 1..8 explicit groups')
        seen = set()
        for group in head['groups']:
            require(isinstance(group, dict) and {'role', 'targets'} <= group.keys()
                    <= {'role', 'targets', 'weight_attribute'} and group['role'] in ROLES
                    and isinstance(group['targets'], list) and group['targets']
                    and all(name(item) for item in group['targets'])
                    and len(set(group['targets'])) == len(group['targets'])
                    and ('weight_attribute' not in group or setting_name(group['weight_attribute'])),
                    'INVALID_REQUEST', 'Each head group needs a known role and explicit target names')
            require(not (seen & set(group['targets'])), 'INVALID_REQUEST', 'A head target appears in two groups')
            seen.update(group['targets'])
        if 'bindings' in head:
            require(isinstance(head['bindings'], list) and head['bindings'], 'INVALID_REQUEST',
                    'head.bindings must be a nonempty list')
            for binding in head['bindings']:
                require(isinstance(binding, dict)
                        and binding.keys() == {'target', 'role', 'strength'}
                        and name(binding['target']) and binding['role'] in ROLES
                        and unit_float(binding['strength']),
                        'INVALID_REQUEST',
                        'Each head binding needs target, known role and finite strength in [0,1]')
            names = [binding['target'] for binding in head['bindings']]
            require(len(names) == len(set(names)) and set(names) <= seen, 'INVALID_REQUEST',
                    'Bindings must uniquely name members in the supplied groups')
    if action in ('head-remove', 'head-role'):
        head = params.get('head')
        keys = {'targets', 'role'} if action == 'head-role' else {'targets'}
        require(isinstance(head, dict) and head.keys() == keys
                and isinstance(head['targets'], list) and head['targets']
                and all(name(item) for item in head['targets'])
                and len(set(head['targets'])) == len(head['targets']),
                'INVALID_REQUEST', f'{action} needs explicit unique target names')
        if action == 'head-role':
            require(isinstance(head['role'], str) and head['role'] in ROLES,
                    'INVALID_REQUEST', 'Unknown head role')
    if action == 'head-strength':
        head = params.get('head')
        require(isinstance(head, dict) and head.keys() == {'target', 'strength'}
                and name(head['target']) and unit_float(head['strength']),
                'INVALID_REQUEST', 'head-strength needs a registered target and strength in [0,1]')
    if action == 'normal-render':
        render = params.get('render')
        require(isinstance(render, dict)
                and {'mode', 'resolution', 'samples'} <= render.keys()
                <= {'mode', 'resolution', 'samples', 'expected_camera_sha256'}
                and render['mode'] in ('shaded', 'normal')
                and type(render['resolution']) is int and 64 <= render['resolution'] <= 1024
                and type(render['samples']) is int and 1 <= render['samples'] <= 64,
                'INVALID_REQUEST', 'render needs mode shaded/normal, resolution 64..1024 and samples 1..64')
        if 'expected_camera_sha256' in render:
            require(isinstance(render['expected_camera_sha256'], str)
                    and len(render['expected_camera_sha256']) == 64
                    and all(item in '0123456789abcdef' for item in render['expected_camera_sha256']),
                    'INVALID_REQUEST', 'expected_camera_sha256 must be lowercase SHA-256')
        require('settings' not in params and 'indices' not in params and 'head' not in params,
                'INVALID_REQUEST', 'normal-render accepts only target and render')
    if action in READ_ONLY or action == 'head-clear':
        require(not params.get('settings') and 'render' not in params,
                'INVALID_REQUEST', f'{action} does not accept settings or render')
        if action != 'normal-inspect':
            require('indices' not in params, 'INVALID_REQUEST', f'{action} does not accept indices')
    if action in ('head-apply', 'head-inspect', 'head-controls', 'head-clear'):
        require('head' not in params, 'INVALID_REQUEST', f'{action} does not accept head input')
    if action in HEAD_OPERATIONS:
        require('settings' not in params and 'indices' not in params and 'render' not in params,
                'INVALID_REQUEST', f'{action} accepts only head fields')
    return params


def load_modules(bpy, product):
    if product['edition'] == panel.CURRENT_EDITION:
        panel.require_product_host(bpy, product)
    else:
        require(tuple(bpy.app.version) == tuple(product['host']), 'UNSUPPORTED',
                f'Edition {product["edition"]} requires Blender {".".join(map(str, product["host"]))}')
    bundle = Path(product['directory'])
    spec = importlib.util.spec_from_file_location(
        'toon_workflow', bundle / '__init__.py', submodule_search_locations=[str(bundle)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
    domain = importlib.import_module('toon_workflow.toon_workflow_addon')
    require(domain.__version__ == product['edition']
            and Path(domain.__file__).resolve().is_relative_to(bundle.resolve()),
            'CONFLICT', 'A different Toon author package was loaded')
    prefix = domain.__name__
    return {
        'domain': domain,
        'normal_ui': importlib.import_module(prefix + '.normal_ui'),
        'head_ui': importlib.import_module(prefix + '.head_ui'),
        'normal': importlib.import_module(prefix + '.artistic_normals'),
        'head': importlib.import_module(prefix + '.head_normals'),
        'authoring': importlib.import_module(prefix + '.normal_authoring'),
    }


def obj_inventory(bpy):
    rows = []
    for obj in sorted(bpy.data.objects, key=lambda item: item.name_full):
        modifiers = []
        for modifier in obj.modifiers:
            row = {'name': modifier.name, 'type': modifier.type,
                   'show_viewport': bool(modifier.show_viewport),
                   'show_render': bool(modifier.show_render)}
            if modifier.type == 'NODES' and modifier.node_group is not None:
                row['node_group'] = modifier.node_group.name_full
                row['inputs'] = {}
                for item in modifier.node_group.interface.items_tree:
                    if item.item_type != 'SOCKET' or item.in_out != 'INPUT':
                        continue
                    value = getattr(modifier.properties.inputs, item.identifier, None)
                    for _ in range(3):
                        if hasattr(value, 'value') and not isinstance(value, (str, bytes)):
                            value = value.value
                        else:
                            break
                    if hasattr(value, 'name_full'):
                        value = value.name_full
                    if isinstance(value, float):
                        value = round(value, 7)
                    if value is None or isinstance(value, (str, bool, int, float)):
                        row['inputs'][item.name] = value
            modifiers.append(row)
        rows.append({
            'object': obj.name_full, 'type': obj.type,
            'library': obj.library.filepath if obj.library else None,
            'data': obj.data.name_full if obj.data else None,
            'data_users': obj.data.users if obj.data else None,
            'vertices': len(obj.data.vertices) if obj.type == 'MESH' else None,
            'polygons': len(obj.data.polygons) if obj.type == 'MESH' else None,
            'slots': [[slot.link, slot.material.name_full if slot.material else None]
                      for slot in getattr(obj, 'material_slots', ())],
            'active_slot': getattr(obj, 'active_material_index', None),
            'modifiers': modifiers,
        })
    meshes = []
    for mesh in sorted(bpy.data.meshes, key=lambda item: item.name_full):
        attributes = []
        for attribute in sorted(mesh.attributes, key=lambda item: item.name):
            values = []
            if attribute.data_type == 'FLOAT':
                values = [round(float(item.value), 7) for item in attribute.data]
            elif attribute.data_type == 'FLOAT_VECTOR':
                values = [[round(float(v), 7) for v in item.vector] for item in attribute.data]
            elif attribute.data_type == 'INT':
                values = [int(item.value) for item in attribute.data]
            elif attribute.data_type == 'BOOLEAN':
                values = [bool(item.value) for item in attribute.data]
            digest = hashlib.sha256(json.dumps(values, separators=(',', ':')).encode()).hexdigest()
            attributes.append({'name': attribute.name, 'domain': attribute.domain,
                               'data_type': attribute.data_type, 'count': len(attribute.data),
                               'digest': digest})
        meshes.append({'mesh': mesh.name_full, 'users': mesh.users, 'library': bool(mesh.library),
                       'vertices': len(mesh.vertices), 'polygons': len(mesh.polygons),
                       'attributes': attributes})
    materials = []
    for material in sorted(bpy.data.materials, key=lambda item: item.name_full):
        nodes = []
        links = []
        if material.use_nodes and material.node_tree is not None:
            for node in sorted(material.node_tree.nodes, key=lambda item: item.name):
                nodes.append([node.name, node.bl_idname, node.label])
            for link in material.node_tree.links:
                links.append([link.from_node.name, link.from_socket.identifier,
                              link.to_node.name, link.to_socket.identifier])
        materials.append({'material': material.name_full, 'users': material.users,
                          'nodes': nodes, 'links': sorted(links)})
    payload = {'objects': rows, 'meshes': meshes, 'materials': materials}
    return payload, hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def camera_snapshot(context):
    scene = context.scene
    camera = scene.camera
    if camera is None:
        return None
    matrix = [[round(float(value), 6) for value in row] for row in camera.matrix_world]
    return {
        'name': camera.name_full,
        'matrix_world': matrix,
        'lens': round(float(camera.data.lens), 6),
        'sensor_width': round(float(camera.data.sensor_width), 6),
        'resolution': [int(scene.render.resolution_x), int(scene.render.resolution_y)],
        'resolution_percentage': int(scene.render.resolution_percentage),
        'frame': int(scene.frame_current),
        'scene': scene.name,
    }


def camera_digest(value):
    if value is None:
        return None
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def attribute_values(mesh, name):
    if mesh is None or not isinstance(name, str) or not name:
        return None
    attribute = mesh.attributes.get(name)
    if attribute is None:
        return None
    if attribute.data_type == 'FLOAT':
        return [round(float(item.value), 7) for item in attribute.data]
    if attribute.data_type == 'FLOAT_VECTOR':
        return [[round(float(value), 7) for value in item.vector] for item in attribute.data]
    if attribute.data_type == 'BOOLEAN':
        return [bool(item.value) for item in attribute.data]
    return None


def values_digest(values):
    return hashlib.sha256(
        json.dumps(values, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def shader_binding(material):
    if material is None:
        return {'present': False}
    bpy = __import__('bpy')
    consumers = sorted(obj.name_full for obj in bpy.data.objects
                       if any(slot.material == material for slot in getattr(obj, 'material_slots', ())))
    user_map = bpy.data.user_map(subset={material}).get(material, set())
    nodes = []
    links = []
    if material.use_nodes and material.node_tree is not None:
        for node in sorted(material.node_tree.nodes, key=lambda item: item.name):
            nodes.append([node.name, node.bl_idname, node.label])
        for link in material.node_tree.links:
            links.append([link.from_node.name, link.from_socket.identifier,
                          link.to_node.name, link.to_socket.identifier])
    diffuse = [node for node in material.node_tree.nodes
               if node.bl_idname == 'ShaderNodeBsdfDiffuse'] if material.use_nodes else []
    normal_links = []
    if len(diffuse) == 1:
        sockets = [socket for socket in diffuse[0].inputs if socket.identifier == 'Normal']
        if len(sockets) == 1:
            normal_links = [[link.from_node.name, link.from_socket.identifier]
                            for link in sockets[0].links]
    ui_nodes = []
    if material.use_nodes and material.node_tree is not None:
        ui_nodes = sorted(node.name for node in material.node_tree.nodes
                          if 'toon_art_normal_ui_binding' in node)
    return {
        'present': True,
        'material': material.name_full,
        'library': material.library.filepath if material.library else None,
        'consumers': consumers,
        'user_map_consumers': sorted(getattr(item, 'name_full', str(item)) for item in user_map
                                     if item != material),
        'diffuse_count': len(diffuse),
        'normal_links': normal_links,
        'ui_nodes': ui_nodes,
        'nodes': nodes,
        'links': sorted(links),
    }


def normalized_field(core, obj, modifier, depsgraph):
    if modifier is None:
        return None
    report = core.inspect(obj, modifier, depsgraph=depsgraph)
    keep = ('profile', 'status', 'space', 'owner', 'target', 'modifier_uid',
            'reference', 'weight_attribute', 'strength', 'attributes',
            'mapping_attribute', 'mapping_space', 'invalid_points', 'vertices', 'issues')
    result = {key: report.get(key) for key in keep if key in report}
    if 'strength' in result:
        result['strength'] = round(float(result['strength']), 7)
    # Reader groups can become zero-user orphans after an explicit disconnect.
    # Their transient users count is not part of the frozen native state; the
    # material binding report below witnesses the exact UI node/link instead.
    return result


def head_view(head, scene):
    report = head.inspect(scene)
    if report is None:
        return {'present': False}
    bindings = []
    for row in report['bindings']:
        gn = row.get('gn')
        if isinstance(gn, dict):
            gn = {key: gn.get(key) for key in
                  ('profile', 'status', 'owner', 'modifier_uid', 'reference',
                   'weight_attribute', 'strength', 'attributes', 'mapping_attribute',
                   'invalid_points', 'vertices', 'issues') if key in gn}
        bindings.append({key: row.get(key) for key in
                         ('binding_id', 'role', 'target', 'owner', 'modifier_uid',
                          'reference', 'requested_reference', 'requested_strength',
                          'strength', 'weight_attribute', 'attributes') if key in row}
                        | {'gn': gn})
    return {'present': True, **{key: report.get(key) for key in
                                ('profile', 'status', 'owner', 'config_id',
                                 'reference', 'binding_count', 'roles', 'scope')},
            'bindings': bindings}


def evaluated_fields(obj, context, core):
    depsgraph = context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    modifier = next((item for item in obj.modifiers if core.is_managed(item)), None)
    if modifier is None:
        return depsgraph, None, None
    report = core.inspect(obj, modifier, depsgraph=depsgraph)
    fields = report.get('attributes')
    if not fields:
        return depsgraph, report, None
    mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
    try:
        values = {key: attribute_values(mesh, fields[key]) for key in ('normal', 'mix', 'valid')}
        values['vertices'] = len(mesh.vertices)
        return depsgraph, report, values
    finally:
        evaluated.to_mesh_clear()


def oracle_report(obj, context, mods, field, evaluated):
    if field is None or evaluated is None:
        return {'available': False}
    normal = mods['normal']
    profile = field.get('profile')
    strength = float(field.get('strength', 0.0))
    weight_name = field.get('weight_attribute')
    weights = attribute_values(obj.data, weight_name)
    directions = attribute_values(obj.data, field.get('mapping_attribute'))
    normals = evaluated.get('normal')
    mixes = evaluated.get('mix')
    valid = evaluated.get('valid')
    require(normals is not None and mixes is not None and valid is not None,
            'VALIDATION_FAILED', 'Evaluated artistic normal fields are missing')
    require(len(normals) == len(obj.data.vertices) and len(mixes) == len(obj.data.vertices)
            and len(valid) == len(obj.data.vertices),
            'VALIDATION_FAILED', 'Evaluated field counts do not match source vertices')
    candidates = [index for index in range(len(valid))
                  if valid[index] and weights is not None and weights[index] > 1e-6]
    if not candidates:
        candidates = list(range(min(12, len(valid))))
    if len(candidates) > 24:
        step = max(1, len(candidates) // 24)
        candidates = candidates[::step][:24]
    reference = context.view_layer.objects.get(field.get('reference') or '')
    require(reference is not None, 'CONFLICT', 'Field reference Object is absent')
    from mathutils import Matrix, Vector
    b = reference.matrix_world.to_3x3().inverted().transposed()
    angle_error = 0.0
    mix_error = 0.0
    samples = []
    for index in candidates:
        actual = Vector(normals[index])
        if profile == 'toon-artistic-normal.v2':
            require(directions is not None, 'VALIDATION_FAILED', 'Mapped direction attribute is missing')
            raw = b @ Vector(directions[index])
            expected = raw.normalized()
        else:
            expected = Vector(normal.reference_normal(
                tuple(float(item) for item in obj.data.vertices[index].co),
                obj.matrix_world, reference.matrix_world))
        dot = max(-1.0, min(1.0, actual.normalized().dot(expected.normalized())))
        angle = math.degrees(math.acos(dot))
        expected_mix = float(struct.unpack('f', struct.pack('f', strength))[0]) * float(weights[index])
        error = abs(float(mixes[index]) - expected_mix)
        angle_error = max(angle_error, angle)
        mix_error = max(mix_error, error)
        if len(samples) < 8:
            samples.append({'index': index, 'weight': round(float(weights[index]), 7),
                            'angle_deg': round(angle, 7), 'mix_error': round(error, 9),
                            'normal': [round(float(item), 7) for item in actual],
                            'expected': [round(float(item), 7) for item in expected]})
    return {
        'available': True,
        'profile': profile,
        'space': 'WORLD_PRE_CAMERA_CORRECTION' if profile else None,
        'sample_count': len(candidates),
        'max_angle_deg': round(angle_error, 7),
        'max_mix_error': round(mix_error, 9),
        'angle_ok': angle_error <= 0.05,
        'mix_ok': mix_error <= 1e-6,
        'samples': samples,
    }


def target_state(context, obj, mods):
    core = mods['normal']
    field = normalized_field(core, obj, next((item for item in obj.modifiers if core.is_managed(item)), None),
                             context.evaluated_depsgraph_get())
    depsgraph, evaluated_report, evaluated = evaluated_fields(obj, context, core)
    oracles = oracle_report(obj, context, mods, field, evaluated)
    ui_settings = getattr(context.scene, mods['normal_ui'].PROP, None)
    weight_hint = ui_settings.weight_attribute if ui_settings is not None else None
    weights = attribute_values(obj.data, (field or {}).get('weight_attribute') or weight_hint)
    directions = attribute_values(obj.data, field.get('mapping_attribute')) if field else None
    return {
        'object': obj.name_full,
        'mesh': obj.data.name_full,
        'mesh_users': obj.data.users,
        'vertices': len(obj.data.vertices),
        'polygons': len(obj.data.polygons),
        'field': field,
        'evaluated': {
            'normal_digest': values_digest(evaluated['normal']) if evaluated else None,
            'mix_digest': values_digest(evaluated['mix']) if evaluated else None,
            'valid_digest': values_digest(evaluated['valid']) if evaluated else None,
            'invalid_points': sum(not item for item in evaluated['valid']) if evaluated else None,
        },
        'weights': {
            'present': weights is not None,
            'digest': values_digest(weights) if weights is not None else None,
            'count': len(weights) if weights is not None else 0,
            'min': round(min(weights), 7) if weights else None,
            'max': round(max(weights), 7) if weights else None,
        },
        'directions': {
            'present': directions is not None,
            'digest': values_digest(directions) if directions is not None else None,
            'count': len(directions) if directions is not None else 0,
        },
        'oracles': oracles,
        'binding': shader_binding(obj.active_material),
    }


def index_readback(obj, field, indices):
    require(all(index < len(obj.data.vertices) for index in indices), 'INVALID_REQUEST',
            'An explicit index is outside the target mesh')
    weights = attribute_values(obj.data, field.get('weight_attribute')) if field else None
    directions = attribute_values(obj.data, field.get('mapping_attribute')) if field else None
    return {
        'weights': [{'index': index, 'value': weights[index]} for index in indices]
        if weights is not None else [],
        'directions': [{'index': index, 'value': directions[index]} for index in indices]
        if directions is not None else [],
    }


def changed_indices(before, after):
    if before is None or after is None or len(before) != len(after):
        return None
    return [index for index, (left, right) in enumerate(zip(before, after)) if left != right]


def state_digest(target, obj):
    payload = {
        'weights': target.get('weights', {}).get('digest'),
        'directions': target.get('directions', {}).get('digest'),
        'normal': target.get('evaluated', {}).get('normal_digest'),
        'mix': target.get('evaluated', {}).get('mix_digest'),
        'valid': target.get('evaluated', {}).get('valid_digest'),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def settings_snapshot(context, mods):
    scene = context.scene
    require(hasattr(scene, mods['normal_ui'].PROP), 'CONFLICT',
            'normal_ui Scene RNA was not registered')
    value = getattr(scene, mods['normal_ui'].PROP)
    return {
        'reference': value.reference.name_full if value.reference else None,
        'weight_attribute': value.weight_attribute,
        'strength': round(float(value.strength), 7),
        'mapping_attribute': value.mapping_attribute,
        'direction': [round(float(item), 7) for item in value.direction],
        'blend': round(float(value.blend), 7),
        'weight_value': round(float(value.weight_value), 7),
        'weight_blend': round(float(value.weight_blend), 7),
    }


def apply_settings(context, mods, patch):
    scene = context.scene
    ui = mods['normal_ui']
    require(hasattr(scene, ui.PROP), 'CONFLICT', 'normal_ui Scene RNA was not registered')
    value = getattr(scene, ui.PROP)
    before = settings_snapshot(context, mods)
    for key, item in patch.items():
        if key == 'reference':
            if item is None:
                value.reference = None
            else:
                target = context.view_layer.objects.get(item)
                require(target is not None and target.library is None and target.override_library is None
                        and target.is_editable, 'NOT_FOUND',
                        'settings.reference must name a local editable Object in the saved view layer')
                value.reference = target
        elif key == 'direction':
            value.direction = [float(entry) for entry in item]
        else:
            setattr(value, key, float(item) if key in
                    ('strength', 'blend', 'weight_value', 'weight_blend') else item)
    after = settings_snapshot(context, mods)
    changed = {key: {'before': before.get(key), 'after': after.get(key)}
               for key in SETTINGS if before.get(key) != after.get(key)}
    return after, changed


def select_only(context, objects):
    bpy = __import__('bpy')
    for obj in list(context.selected_objects):
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    require(objects, 'INVALID_REQUEST', 'No explicit target Object')
    context.view_layer.objects.active = objects[0]
    context.view_layer.update()
    return [obj.name_full for obj in objects]


def write_indices(context, obj, mods, indices, action):
    require(obj.data.users == 1 and obj.data.library is None and obj.data.override_library is None,
            'CONFLICT', 'Vertex-index authoring requires an exclusive local Mesh')
    require(max(indices) < len(obj.data.vertices), 'INVALID_REQUEST', 'Vertex index outside the original source Mesh')
    ui = mods['normal_ui']
    settings = getattr(context.scene, ui.PROP)
    modifier = ui._managed(obj)
    authoring = mods['authoring']
    if action == 'normal-write-direction':
        return authoring.write_direction(obj, settings.mapping_attribute, indices,
                                        tuple(settings.direction), blend=settings.blend)
    return authoring.write_weight(obj, modifier, indices, settings.weight_value, blend=settings.weight_blend)


def head_add(context, mods, head_request):
    bpy = __import__('bpy')
    scene = context.scene
    head_ui = mods['head_ui']
    head = mods['head']
    require(hasattr(scene, head_ui.PROP), 'CONFLICT', 'head_ui Scene RNA was not registered')
    settings = getattr(scene, head_ui.PROP)
    reference = context.view_layer.objects.get(head_request['reference'])
    require(reference is not None and reference.library is None and reference.override_library is None
            and reference.is_editable, 'NOT_FOUND',
            'head.reference must name a local editable Object in the saved view layer')
    groups = []
    previous_active = context.view_layer.objects.active
    previous_selection = list(context.selected_objects)
    try:
        for group in head_request['groups']:
            targets = [context.view_layer.objects.get(name) for name in group['targets']]
            require(all(item is not None for item in targets), 'NOT_FOUND',
                    'A head target is absent from the saved view layer')
            settings.reference = reference
            settings.role = group['role']
            settings.weight_attribute = group.get('weight_attribute', 'toon_head_weight')
            select_only(context, targets)
            report = head_ui.add_selected(context)
            groups.append({'role': group['role'], 'targets': group['targets'],
                           'weight_attribute': settings.weight_attribute, 'result': report})
        if head_request.get('bindings'):
            overrides = {row['target']: row for row in head_request['bindings']}
            bindings = []
            for row in head.controls(scene)['bindings']:
                target = row['target']
                desired = overrides.get(target.name_full, row)
                bindings.append({'target': target, 'role': desired['role'],
                                 'strength': float(desired['strength'])})
            receipt = head.define(scene, reference, bindings, replace=True)
        else:
            receipt = head.inspect(scene)
        return {'status': 'defined', 'groups': groups, 'receipt': receipt}
    finally:
        context.view_layer.objects.active = previous_active
        for item in list(context.selected_objects):
            item.select_set(False)
        for item in previous_selection:
            item.select_set(True)
        context.view_layer.update()


def edit_head_members(context, mods, names, role=None):
    head_ui = mods['head_ui']
    previous_active = context.view_layer.objects.active
    previous_selection = list(context.selected_objects)
    try:
        targets = [context.view_layer.objects.get(name) for name in names]
        require(all(item is not None for item in targets), 'NOT_FOUND',
                'A head member is absent from the saved view layer')
        select_only(context, targets)
        if role is not None:
            getattr(context.scene, head_ui.PROP).role = role
            return head_ui.update_selected_role(context)
        return head_ui.remove_selected(context)
    finally:
        context.view_layer.objects.active = previous_active
        for item in list(context.selected_objects):
            item.select_set(False)
        for item in previous_selection:
            item.select_set(True)
        context.view_layer.update()


def render_technical(context, obj, mods, request, stage):
    import bpy
    scene = context.scene
    mode = request['render']['mode']
    resolution = request['render']['resolution']
    samples = request['render']['samples']
    camera = camera_snapshot(context)
    expected_camera = request['render'].get('expected_camera_sha256')
    if expected_camera is not None:
        require(camera_digest(camera) == expected_camera, 'CONFLICT',
                'Saved camera is not at the requested same-camera baseline')
    require(scene.camera is not None, 'CONFLICT', 'Saved Scene has no active camera')
    previous = {
        'engine': scene.render.engine,
        'resolution_x': scene.render.resolution_x,
        'resolution_y': scene.render.resolution_y,
        'resolution_percentage': scene.render.resolution_percentage,
        'filepath': scene.render.filepath,
        'format': scene.render.image_settings.file_format,
        'color_mode': scene.render.image_settings.color_mode,
        'view_transform': scene.view_settings.view_transform,
        'look': scene.view_settings.look,
        'exposure': scene.view_settings.exposure,
        'gamma': scene.view_settings.gamma,
    }
    slot = obj.material_slots[request['target']['material_slot']]
    original_link = slot.link
    original_material = slot.material
    original_obj_hide = obj.hide_render
    hidden = [(item, item.hide_render) for item in bpy.data.objects
              if item.type == 'MESH' and item != obj]
    temporary = None
    try:
        scene.render.engine = 'CYCLES'
        scene.cycles.device = 'CPU'
        scene.cycles.samples = samples
        scene.cycles.use_denoising = False
        scene.render.resolution_x = resolution
        scene.render.resolution_y = resolution
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = 'PNG'
        scene.render.image_settings.color_mode = 'RGB'
        scene.render.filepath = str(stage)
        scene.view_settings.view_transform = 'Standard' if mode == 'normal' else 'AgX'
        scene.view_settings.look = 'None'
        scene.view_settings.exposure = 0.0
        scene.view_settings.gamma = 1.0
        if mode == 'normal':
            for item, state in hidden:
                item.hide_render = True
            obj.hide_render = False
            temporary = bpy.data.materials.new('__toon_head_normal_debug__')
            temporary.use_nodes = True
            tree = temporary.node_tree
            tree.nodes.clear()
            output = tree.nodes.new('ShaderNodeOutputMaterial')
            emission = tree.nodes.new('ShaderNodeEmission')
            emission.inputs['Strength'].default_value = 1.0
            reader = None
            if original_material and original_material.use_nodes:
                reader = next((node for node in original_material.node_tree.nodes
                               if 'toon_art_normal_ui_binding' in node), None)
            if reader is not None:
                group = tree.nodes.new('ShaderNodeGroup')
                group.node_tree = reader.node_tree
                socket = next((item for item in group.outputs if item.identifier
                               == reader.get('toon_art_normal_ui_output')), None)
                require(socket is not None, 'VALIDATION_FAILED',
                        'Bound normal reader output socket is missing')
                tree.links.new(socket, emission.inputs['Color'])
            else:
                geometry = tree.nodes.new('ShaderNodeNewGeometry')
                tree.links.new(geometry.outputs['Normal'], emission.inputs['Color'])
            tree.links.new(emission.outputs['Emission'], output.inputs['Surface'])
            slot.link = 'OBJECT'
            slot.material = temporary
        bpy.ops.render.render(write_still=True)
        sha = digest(stage)
        return {
            'mode': mode,
            'resolution': resolution,
            'samples': samples,
            'engine': 'CYCLES',
            'device': 'CPU',
            'view_transform': scene.view_settings.view_transform,
            'file': str(stage),
            'sha256': sha,
            'bytes': Path(stage).stat().st_size,
            'camera': camera,
            'hidden_others': [item.name_full for item, _ in hidden] if mode == 'normal' else [],
        }
    finally:
        if temporary is not None:
            if obj.material_slots[request['target']['material_slot']].material == temporary:
                slot.material = original_material
            slot.link = original_link
            if temporary.users == 0:
                bpy.data.materials.remove(temporary)
        for item, state in hidden:
            item.hide_render = state
        obj.hide_render = original_obj_hide
        scene.render.engine = previous['engine']
        scene.render.resolution_x = previous['resolution_x']
        scene.render.resolution_y = previous['resolution_y']
        scene.render.resolution_percentage = previous['resolution_percentage']
        scene.render.filepath = previous['filepath']
        scene.render.image_settings.file_format = previous['format']
        scene.render.image_settings.color_mode = previous['color_mode']
        scene.view_settings.view_transform = previous['view_transform']
        scene.view_settings.look = previous['look']
        scene.view_settings.exposure = previous['exposure']
        scene.view_settings.gamma = previous['gamma']


def dispatch(context, obj, mods, request, action):
    ui = mods['normal_ui']
    head = mods['head']
    scene = context.scene
    patch = request.get('settings', {})
    if action == 'normal-settings':
        return {'status': 'settings', 'settings': settings_snapshot(context, mods)}
    if action == 'normal-zero-weight':
        apply_settings(context, mods, patch)
        return ui.create_zero_weight(context)
    if action == 'normal-create-field':
        apply_settings(context, mods, patch)
        modifier = ui.create_field(context)
        return {'status': 'created', 'modifier': modifier.name,
                'modifier_uid': modifier.persistent_uid,
                'node_group': modifier.node_group.name_full if modifier.node_group else None}
    if action == 'normal-apply-strength':
        apply_settings(context, mods, patch)
        return ui.apply_strength(context)
    if action == 'normal-bind-diffuse':
        return ui.bind_diffuse(context)
    if action == 'normal-disconnect-diffuse':
        return ui.disconnect_diffuse(context)
    if action == 'normal-initialize-mapping':
        apply_settings(context, mods, patch)
        return ui.initialize_mapping(context)
    if action == 'normal-enable-mapping':
        apply_settings(context, mods, patch)
        return ui.enable_mapping(context)
    if action == 'normal-restore-ellipsoid':
        return ui.restore_ellipsoid(context)
    if action in ('normal-write-direction', 'normal-write-weight'):
        return write_indices(context, obj, mods, request['indices'], action)
    if action == 'head-add':
        return head_add(context, mods, request['head'])
    if action == 'head-apply':
        return head.apply(scene)
    if action == 'head-inspect':
        return head_view(head, scene)
    if action == 'head-controls':
        value = head.controls(scene)
        if value is None:
            return {'present': False}
        return {'present': True, 'reference': value['reference'].name_full,
                'bindings': [{'target': row['target'].name_full, 'role': row['role'],
                              'strength': round(float(row['strength']), 7),
                              'control': row['control'].name_full}
                             for row in value['bindings']]}
    if action in ('head-remove', 'head-role'):
        return edit_head_members(context, mods, request['head']['targets'], request['head'].get('role'))
    if action == 'head-strength':
        registry = head.controls(scene)
        target = request['head']['target']
        require(registry is not None, 'NOT_FOUND', 'No registered head configuration')
        row = next((row for row in registry['bindings'] if row['target'].name_full == target), None)
        require(row is not None, 'NOT_FOUND', 'No registered head control for the named target')
        control, key = row['control'], row['strength_property']
        before = float(control[key])
        control[key] = request['head']['strength']
        context.view_layer.update()
        return {'target': target, 'previous': before, 'strength': float(control[key])}
    if action == 'head-clear':
        return head.clear(scene)
    raise Failure('INVALID_REQUEST', 'Unknown normal/head operation')


def cold_view(context, mods, state):
    return {
        'field': state.get('field'),
        'binding': state.get('binding'),
        'oracles': state.get('oracles'),
        'head': head_view(mods['head'], context.scene),
    }


def build_result(context, obj, mods, request, action, before_arrays,
                 before_settings, settings_changed, report):
    after = target_state(context, obj, mods)
    settings = settings_snapshot(context, mods)
    indices = request.get('indices')
    field = after.get('field') or {}
    readback = index_readback(obj, field, indices) if indices else {'weights': [], 'directions': []}
    changed = {'weights': None, 'directions': None}
    unexpected = {'weights': [], 'directions': []}
    if action in ('normal-write-direction', 'normal-write-weight', 'normal-restore-ellipsoid',
                  'normal-enable-mapping', 'normal-initialize-mapping'):
        weights = attribute_values(obj.data, field.get('weight_attribute')) if field else None
        directions = attribute_values(obj.data, field.get('mapping_attribute')) if field else None
        changed['weights'] = changed_indices(before_arrays.get('weights'), weights)
        changed['directions'] = changed_indices(before_arrays.get('directions'), directions)
        allowed = set(indices or [])
        if changed['weights'] is not None:
            unexpected['weights'] = sorted(set(changed['weights']) - allowed)
        if changed['directions'] is not None:
            unexpected['directions'] = sorted(set(changed['directions']) - allowed)
        if action in ('normal-write-direction', 'normal-write-weight'):
            require(not unexpected['weights'] and not unexpected['directions'], 'VALIDATION_FAILED',
                    'A vertex outside the explicit selection changed')
    inventory, inventory_sha = obj_inventory(__import__('bpy'))
    camera = camera_snapshot(context)
    result = {
        'operation': action,
        'action': request['operation'],
        'target': request['target'],
        'settings': settings,
        'settings_changed': settings_changed,
        'before_settings': before_settings,
        'report': report,
        'cold_report': cold_view(context, mods, after),
        'target_state': after,
        'indices': indices,
        'readback': readback,
        'changed_indices': changed,
        'unexpected_changed': unexpected,
        'inventory_sha256': inventory_sha,
        'camera': camera,
        'camera_sha256': camera_digest(camera),
        'state_digest': state_digest(after, obj),
        'mesh_state': {
            'field': field,
            'weights': after['weights'],
            'directions': after['directions'],
            'evaluated': after['evaluated'],
            'oracles': after['oracles'],
            'binding': after['binding'],
        },
        'runtime': {'blender': __import__('bpy').app.version_string,
                    'build': __import__('bpy').app.build_hash.decode(),
                    'pid': os.getpid()},
    }
    return result


def work(bpy, request):
    product = request['product']
    mods = load_modules(bpy, product)
    mods['normal_ui'].register(include_panel=False)
    mods['head_ui'].register()
    context = panel.open_saved_context(bpy, request)
    require(context.mode == 'OBJECT', 'CONFLICT', 'Saved source must open in Object Mode')
    target = request['target']
    action = request['operation']
    with panel.named_slot(context, target) as obj:
        before_settings = settings_snapshot(context, mods)
        before = target_state(context, obj, mods)
        before_arrays = {
            'weights': attribute_values(
                obj.data, (before.get('field') or {}).get('weight_attribute')),
            'directions': attribute_values(
                obj.data, (before.get('field') or {}).get('mapping_attribute')),
        }
        if action in READ_ONLY:
            if action == 'normal-inspect':
                report = cold_view(context, mods, before)
                if request.get('indices'):
                    report['readback'] = index_readback(
                        obj, before.get('field') or {}, request['indices'])
            else:
                report = dispatch(context, obj, mods, request, action)
            inventory_sha = obj_inventory(bpy)[1]
            camera = camera_snapshot(context)
            result = {
                'operation': action,
                'target': request['target'],
                'report': report,
                'target_state': before,
                'settings': before_settings,
                'inventory_sha256': inventory_sha,
                'camera': camera,
                'state_digest': state_digest(before, obj),
                'runtime': {'blender': bpy.app.version_string,
                            'build': bpy.app.build_hash.decode(), 'pid': os.getpid()},
            }
            result['camera_sha256'] = camera_digest(camera)
            expected = request.get('verify')
            if expected:
                for key in ('report', 'settings', 'inventory_sha256', 'camera_sha256', 'state_digest'):
                    require(result.get(key) == expected.get(key), 'VALIDATION_FAILED',
                            f'Independent reopen differs at {key}')
                result['verified'] = True
            return result
        if action == RENDER:
            require(not request.get('verify'), 'INVALID_REQUEST', 'Render worker does not accept verify')
            report = cold_view(context, mods, before)
            inventory_sha = obj_inventory(bpy)[1]
            camera = camera_snapshot(context)
            render = render_technical(context, obj, mods, request, request['stage'])
            result = {
                'operation': action,
                'target': request['target'],
                'render': render,
                'report': report,
                'target_state': before,
                'settings': before_settings,
                'inventory_sha256': inventory_sha,
                'camera': camera,
                'camera_sha256': camera_digest(camera),
                'state_digest': state_digest(before, obj),
                'output_sha256': render['sha256'],
                'verify_source': {
                    'report': report,
                    'settings': before_settings,
                    'inventory_sha256': inventory_sha,
                    'camera_sha256': camera_digest(camera),
                    'state_digest': state_digest(before, obj),
                },
                'runtime': {'blender': bpy.app.version_string,
                            'build': bpy.app.build_hash.decode(), 'pid': os.getpid()},
            }
            return result
        _, settings_changed = apply_settings(context, mods, request.get('settings', {}))
        report = dispatch(context, obj, mods, request, action)
        result = build_result(context, obj, mods, request, action,
                              before_arrays, before_settings, settings_changed, report)
        result['verify'] = {
            'report': result['cold_report'],
            'settings': result['settings'],
            'inventory_sha256': result['inventory_sha256'],
            'camera_sha256': result['camera_sha256'],
            'state_digest': result['state_digest'],
        }
    bpy.context.preferences.filepaths.save_version = 0
    require(bpy.ops.wm.save_as_mainfile(
        filepath=request['stage'], copy=True, relative_remap=True,
        check_existing=False) == {'FINISHED'}, 'IO_ERROR', 'Could not save head-normal candidate')
    result['output_sha256'] = digest(request['stage'])
    return result


def run(args):
    deadline = time.monotonic() + panel.timeout_value(args.timeout)
    job = args.jobs_dir.resolve() / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    result_path = job / 'result.json'
    artifacts = {'job_directory': str(job), 'result': str(result_path)}
    data = error = None

    def remaining():
        seconds = deadline - time.monotonic()
        require(seconds > 0, 'TIMEOUT', 'Head-normal time budget expired')
        return seconds

    try:
        with ExitStack() as guards:
            request = read_request(args, guards)
            blender = panel.canonical(args.blender)
            request['product'] = panel.native_product(request, guards)
            action = request['operation']
            source_sha_before = digest(request['file'])
            if action in READ_ONLY:
                require(args.output is None, 'INVALID_REQUEST', f'{action} does not use --output')
            else:
                suffix = '.png' if action == RENDER else '.blend'
                require(args.output is not None and args.output.is_absolute()
                        and args.output.suffix.lower() == suffix, 'INVALID_REQUEST',
                        f'{action} needs a new absolute {suffix} --output')
                parent = panel.canonical(args.output.parent)
                for folder in reversed((parent, *parent.parents)):
                    if folder != Path(folder.anchor):
                        guards.enter_context(FileGuard(folder, directory=True))
                output = parent / args.output.name
                guards.enter_context(PathLocks([output]))
                require(not os.path.lexists(output), 'CONFLICT', 'Output already exists')
                with tempfile.NamedTemporaryFile(dir=parent, prefix='.toon-head-normal-',
                                                 suffix=suffix, delete=False) as temporary:
                    staged = Path(temporary.name)
                guards.callback(staged.unlink, missing_ok=True)
                request['stage'] = str(staged)
            result = panel.execute(blender, job, request, 'normal-author', remaining())
            if action not in READ_ONLY:
                expected = result.pop('output_sha256')
                with FileGuard(staged) as stage_guard:
                    require(stage_guard.sha256() == expected, 'CONFLICT', 'Staged output SHA differs')
                    if action == RENDER:
                        cold = {**request, 'file': request['file'], 'operation': 'normal-inspect',
                                'verify': result.pop('verify_source')}
                        cold.pop('stage', None)
                        verified = panel.execute(blender, job, cold, 'normal-source-reopen', remaining())
                    else:
                        cold = {**request, 'file': str(staged), 'operation': 'normal-inspect',
                                'verify': result.pop('verify')}
                        cold.pop('stage', None)
                        cold.pop('indices', None)
                        verified = panel.execute(blender, job, cold, 'normal-reopen', remaining())
                    require(verified['runtime']['pid'] != result['runtime']['pid'],
                            'VALIDATION_FAILED', 'Independent Blender reopen was not observed')
                    result['independent_reopen'] = verified['runtime']
                with FileGuard(staged, renameable=True) as stage_guard:
                    require(stage_guard.sha256() == expected, 'CONFLICT',
                            'Verified output SHA changed before publication')
                    remaining()
                    stage_guard.rename_new(output)
                key = 'image' if action == RENDER else 'candidate'
                result[key] = {'file': str(output), 'sha256': expected}
                if action == RENDER:
                    result['render']['file'] = str(output)
                artifacts[key] = str(output)
            source_sha_after = digest(request['file'])
            require(source_sha_before == source_sha_after, 'CONFLICT',
                    'Input .blend SHA changed during the head-normal action')
            data = {**result, 'source': {'file': request['file'],
                                         'sha256': request['expected_sha256']},
                    'source_sha256_before': source_sha_before,
                    'source_sha256_after': source_sha_after,
                    'source_unchanged': True,
                    'product': request['product']}
    except Exception as exc:
        error = panel.failure(exc, 'IO_ERROR')
    for stage in ('normal-author', 'normal-reopen', 'normal-source-reopen'):
        for suffix in ('.log', '-result.json'):
            path = job / (stage + suffix)
            if path.is_file():
                artifacts[path.name] = str(path)
    result = envelope('toon-workflow.surface-slot.head-normal', job.name, data, error, artifacts)
    atomic_json(result_path, result)
    return result
