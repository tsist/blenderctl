"""Author deterministic Blender 5.3 Toon lighting on a saved, hash-pinned scene.

Uses the selected Toon edition's native lighting and material authoring functions.
"""

import argparse
from contextlib import ExitStack
import importlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
sys.path.insert(0, str(ENTRY.parent))

from filesystem import FileGuard, PathLocks
from material_draft_handoff import (Parser, canonical, guard_file, open_saved_context,
                                   read_document, require)
from material_projection_handoff import error_payload, run_worker
from protocol import (CODES, DEFAULT_BLENDER, DEFAULT_JOBS, SCHEMA, Failure, atomic_json,
                      digest, envelope, read_json, timeout_value)
from toon_panel_handoff import (CURRENT_EDITION, NATIVE_EDITIONS, PRODUCTS, bindings,
                                dependencies, load_modules, name, native_product)
import toon_directional_author as directional


COMMAND = 'toon-workflow.lighting-author'
SCHEMA_ID = 'toon-lighting-author-5.3.v1'
EDITION = '0.52.1'
DIRECTIONAL_EDITION = '0.52.3'
HOST = (5, 3, 0)
from host_compat import is_supported_host
LIGHTING_PRODUCTS = {
    **PRODUCTS,
    DIRECTIONAL_EDITION: (HOST, 'runtime/toon-workflow/0.52.3',
                          '6db937aac6628c50006b92410c09505615191715e8794fea644d35d2edb7b8d0'),
}
REPAIR_NORMAL = 'repair-face-normal'
SET_NORMAL_MIX = 'set-face-normal-mix'
NORMAL_OPERATIONS = {REPAIR_NORMAL, SET_NORMAL_MIX}
OPERATIONS = {'author', 'inspect'} | NORMAL_OPERATIONS | directional.OPERATIONS
READ_OPERATIONS = {'inspect', 'directional-face-inspect', 'native-surface-inspect'}
CONTROL_FIELDS = {
    'controller': ('location', 'scale', 'toon53_density'),
    'anchor': ('location',),
    'handle': ('location', 'toon53_radius', 'toon53_face_bias'),
    'eyelid': ('location', 'toon53_eyelid_width', 'toon53_eyelid_height',
                'toon53_eyelid_relief'),
}
CONTROL_KEYS = {'controller', 'anchor', 'handle', 'eyelid'}
VECTOR_FIELDS = {'location', 'scale'}
IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}\Z')
PALETTE_NAME = re.compile(r'toon53_[a-z_]+\Z')
MAX_FRAMES = 64
MAX_RESOURCES = 100


def parser():
    cli = Parser(description=__doc__)
    cli.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    cli.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS)
    cli.add_argument('--transactions-dir', help=argparse.SUPPRESS)
    cli.add_argument('--timeout', type=float, default=120)
    display = cli.add_mutually_exclusive_group()
    for flag in ('--human', '--compact', '--full'):
        display.add_argument(flag, action='store_true')
    action = cli.add_subparsers(required=True, parser_class=Parser).add_parser(
        'lighting-author', description=__doc__,
        epilog=(
            f'Toon {CURRENT_EDITION} requires Blender 5.3 or later. Use a saved working copy with an active camera and EEVEE scene. '
            'author writes a new .blend and independently reopens it; inspect is '
            'read-only and omits --output. Request: schema, operation, edition, '
            'file, expected_sha256, context {scene,view_layer,frame}, camera, '
            'target {object,material_slot,expected_material,expected_link}, '
            'rig {key,fill}, optional controls, keyframes, bind/eyelid/palette, '
            'scene_palette (scene values) and palette_clear (inherit scene values), '
            'resources [{file,expected_sha256}]. repair-face-normal uses the pinned camera '
            'and existing owned FACE target with a new --output, without rebinding. '
            'set-face-normal-mix adds normal_mix {value, mode: STATIC or REPLACE_KEY}. '
            'Toon 0.52.3 adds directional-face-inspect, directional-face-bind and '
            'directional-face-unbind for a saved target slot. Bind uses binding '
            '{head,axes,uv,field,scope?,valid?,controls?} and optionally rig {key,fill,rim}. '
            'Native light accumulation supports native-surface-inspect and native-surface-qualify.'))
    action.add_argument('--params', type=Path, required=True)
    action.add_argument('--output', type=Path)
    return cli


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def number(value, low=None, high=None, label='value'):
    require(finite(value), 'INVALID_REQUEST', 'Expected a finite number for ' + label)
    if low is not None:
        require(value >= low, 'INVALID_REQUEST', f'{label} must be >= {low}')
    if high is not None:
        require(value <= high, 'INVALID_REQUEST', f'{label} must be <= {high}')
    return float(value)


def vector(value, size=3, label='vector', low=None, high=None):
    require(isinstance(value, (list, tuple)) and len(value) == size,
            'INVALID_REQUEST', f'Expected {size} numbers for {label}')
    return [number(item, low, high, label) for item in value]


def names(value, label):
    require(isinstance(value, str) and name(value), 'INVALID_REQUEST', 'Expected a name for ' + label)
    return value


def validate_controls(value, label='controls', allow_eyelid=True):
    require(isinstance(value, dict) and value.keys() <= CONTROL_KEYS, 'INVALID_REQUEST',
            'Expected control blocks ' + ', '.join(sorted(CONTROL_KEYS)))
    result = {}
    for control, fields in value.items():
        require(isinstance(fields, dict), 'INVALID_REQUEST', f'{label}.{control} must be an object')
        allowed = set(CONTROL_FIELDS[control])
        require(fields.keys() <= allowed, 'INVALID_REQUEST',
                f'{label}.{control} accepts {sorted(allowed)}')
        if control == 'eyelid':
            require(allow_eyelid, 'INVALID_REQUEST', 'eyelid control is not available for this action')
        patch = {}
        for field, item in fields.items():
            if field == 'location':
                patch[field] = vector(item, 3, f'{label}.{control}.location', -1000, 1000)
            elif field == 'scale':
                patch[field] = vector(item, 3, f'{label}.{control}.scale', .001, 100)
            elif field == 'toon53_density':
                patch[field] = number(item, 0, 1, field)
            elif field == 'toon53_radius':
                patch[field] = number(item, .002, .3, field)
            elif field == 'toon53_face_bias':
                patch[field] = number(item, -1, 1, field)
            elif field == 'toon53_eyelid_width':
                patch[field] = number(item, .002, .2, field)
            elif field == 'toon53_eyelid_height':
                patch[field] = number(item, .002, .05, field)
            elif field == 'toon53_eyelid_relief':
                patch[field] = number(item, 0, 1, field)
        require(patch, 'INVALID_REQUEST', f'{label}.{control} is empty')
        result[control] = patch
    return result


def validate_palette(value):
    require(isinstance(value, dict) and 0 < len(value) <= 32, 'INVALID_REQUEST',
            'palette must be a nonempty object up to 32 fields')
    result = {}
    for key, item in value.items():
        require(isinstance(key, str) and PALETTE_NAME.fullmatch(key), 'INVALID_REQUEST',
                'Unknown palette field: ' + str(key))
        if isinstance(item, (list, tuple)):
            require(len(item) in (3, 4), 'INVALID_REQUEST', f'{key} needs 3 or 4 color components')
            result[key] = vector(item, len(item), key, 0, 4)
        else:
            result[key] = number(item, 0, .075 if key.endswith('_shift') else 1, key)
    return result


def validate_request(args, guards):
    document = read_document(guard_file(args.params.absolute(), guards))
    require(isinstance(document, dict), 'INVALID_REQUEST', 'Expected a JSON object')
    common = {'operation', 'edition', 'file', 'expected_sha256', 'context'}
    require(common <= document.keys(), 'INVALID_REQUEST',
            'Missing operation, edition, file, expected_sha256 or context')
    operation = document['operation']
    require(isinstance(operation, str) and operation in OPERATIONS, 'INVALID_REQUEST', 'Unknown lighting-author operation')
    require(document.get('schema', SCHEMA_ID) == SCHEMA_ID, 'UNSUPPORTED',
            'Unknown lighting-author schema')
    is_directional = operation in directional.OPERATIONS
    required_edition = DIRECTIONAL_EDITION if is_directional else EDITION
    editions = {required_edition, *NATIVE_EDITIONS}
    require(isinstance(document['edition'], str) and document['edition'] in editions, 'UNSUPPORTED',
            f'{operation} requires one of these Toon editions: {", ".join(sorted(editions))}')
    allowed = common | {'schema', 'camera', 'target', 'rig', 'controls', 'keyframes',
                        'bind', 'eyelid', 'palette', 'scene_palette', 'palette_clear', 'resources', 'normal_mix'}
    if is_directional:
        allowed |= {'binding'}
    require(document.keys() <= allowed, 'INVALID_REQUEST',
            'Unexpected lighting-author field: ' + ', '.join(sorted(document.keys() - allowed)))
    ctx = document['context']
    require(isinstance(ctx, dict) and ctx.keys() == {'scene', 'view_layer', 'frame'}
            and names(ctx['scene'], 'scene') and names(ctx['view_layer'], 'view_layer')
            and type(ctx['frame']) is int and 1 <= ctx['frame'] <= 100000,
            'INVALID_REQUEST', 'Expected exact scene, view_layer and frame in 1..100000')
    require(isinstance(document['file'], str) and Path(document['file']).suffix.lower() == '.blend'
            and isinstance(document['expected_sha256'], str), 'INVALID_REQUEST',
            'Expected a hash-pinned .blend')
    document['file'] = str(guard_file(document['file'], guards, document['expected_sha256']))

    resources = document.setdefault('resources', [])
    require(isinstance(resources, list) and len(resources) <= MAX_RESOURCES, 'INVALID_REQUEST',
            f'Expected up to {MAX_RESOURCES} hash-pinned resources')
    seen = set()
    for row in resources:
        require(isinstance(row, dict) and row.keys() == {'file', 'expected_sha256'}
                and all(isinstance(item, str) for item in row.values()), 'INVALID_REQUEST',
                'Each resource needs file and expected_sha256')
        row['file'] = str(guard_file(row['file'], guards, row['expected_sha256']))
        key = os.path.normcase(row['file'])
        require(key not in seen, 'INVALID_REQUEST', 'Duplicate resource: ' + row['file'])
        seen.add(key)

    if 'camera' in document:
        document['camera'] = names(document['camera'], 'camera')
    if 'target' in document:
        target = document['target']
        require(isinstance(target, dict)
                and target.keys() == {'object', 'material_slot', 'expected_material', 'expected_link'}
                and names(target['object'], 'target.object')
                and type(target['material_slot']) is int and target['material_slot'] >= 0
                and (target['expected_material'] is None or names(target['expected_material'], 'expected_material'))
                and target['expected_link'] in ('DATA', 'OBJECT'),
                'INVALID_REQUEST', 'Expected target object, slot, material name/null and DATA/OBJECT link')
    if 'rig' in document:
        rig = document['rig']
        expected_rig = {'key', 'fill', 'rim'} if is_directional else {'key', 'fill'}
        require(isinstance(rig, dict) and rig.keys() == expected_rig
                and names(rig['key'], 'rig.key')
                and all(rig[key] is None or names(rig[key], 'rig.' + key)
                        for key in expected_rig - {'key'}),
                'INVALID_REQUEST', 'rig fields must name the key and any declared fill or rim')
    if is_directional:
        directional.validate(document)
        require((args.output is None) == (operation in READ_OPERATIONS), 'INVALID_REQUEST',
                'Directional bind and unbind need a new --output. Directional inspection is read-only.')
    if 'controls' in document:
        document['controls'] = validate_controls(document['controls'])
    if 'bind' in document:
        bind = document['bind']
        require(isinstance(bind, dict) and bind.keys() == {'role', 'mask', 'uv'}
                and bind['role'] in ('FACE', 'HAIR', 'COLOR')
                and (bind['mask'] is None or names(bind['mask'], 'bind.mask'))
                and isinstance(bind['uv'], str),
                'INVALID_REQUEST', 'bind needs role FACE/HAIR/COLOR, optional mask name and uv text')
        if bind['role'] == 'HAIR':
            require(bind['mask'] is not None and names(bind['uv'], 'bind.uv'), 'INVALID_REQUEST',
                    'HAIR bind needs an existing packed non-color mask and UV name')
        else:
            require(bind['mask'] is None and bind['uv'] == '', 'INVALID_REQUEST',
                    'Only HAIR bind accepts mask and uv')
    if 'eyelid' in document:
        eyelid = document['eyelid']
        if eyelid is True:
            document['eyelid'] = {}
        else:
            require(isinstance(eyelid, dict), 'INVALID_REQUEST', 'eyelid must be true or an object')
        require(document.get('bind', {}).get('role') == 'FACE', 'INVALID_REQUEST',
                'eyelid requires a FACE bind')
        if document['eyelid']:
            document['eyelid'] = validate_controls({'eyelid': document['eyelid']})['eyelid']
    if 'palette' in document:
        document['palette'] = validate_palette(document['palette'])
    if 'scene_palette' in document:
        document['scene_palette'] = validate_palette(document['scene_palette'])
    if 'palette_clear' in document:
        clear = document['palette_clear']
        require(isinstance(clear, list) and len(clear) <= 32
                and all(isinstance(field, str) and PALETTE_NAME.fullmatch(field) for field in clear),
                'INVALID_REQUEST', 'palette_clear must contain up to 32 palette field names')
        require(len(set(clear)) == len(clear) and set(clear).isdisjoint(document.get('palette', {})),
                'INVALID_REQUEST', 'Palette fields cannot be cleared twice or set and cleared together')
    require(operation == 'author' or not {'scene_palette', 'palette_clear'} & document.keys(),
            'INVALID_REQUEST', 'Scene palette changes require the author operation')

    require(('normal_mix' in document) == (operation == SET_NORMAL_MIX), 'INVALID_REQUEST',
            'Set-face-normal-mix requires its own normal_mix request')
    if operation in NORMAL_OPERATIONS:
        fields = {'camera', 'target'}
        if operation == SET_NORMAL_MIX:
            fields.add('normal_mix')
            mix = document['normal_mix']
            require(type(mix) is dict and mix.keys() == {'mode', 'value'} and mix['mode'] in ('STATIC', 'REPLACE_KEY')
                    and finite(mix['value']) and 0 <= mix['value'] <= 1, 'INVALID_REQUEST',
                    'normal_mix needs a STATIC or REPLACE_KEY mode and a value in [0,1]')
        require(fields <= document.keys() <= common | fields | {'schema', 'resources'} and args.output is not None,
                'INVALID_REQUEST', 'Normal authoring requires camera, FACE target and a new --output')
    elif operation == 'author':
        require({'camera', 'target', 'rig'} <= document.keys(), 'INVALID_REQUEST',
                'author needs camera, target and rig')
        frames = document.get('keyframes', [])
        require(isinstance(frames, list) and len(frames) <= MAX_FRAMES, 'INVALID_REQUEST',
                f'keyframes accepts up to {MAX_FRAMES} rows')
        previous = None
        normalized = []
        for row in frames:
            require(isinstance(row, dict) and 'frame' in row, 'INVALID_REQUEST',
                    'Each keyframe needs a frame')
            frame = row['frame']
            require(type(frame) is int and 1 <= frame <= 100000, 'INVALID_REQUEST',
                    'Keyframe must be an integer in 1..100000')
            subframe = row.get('subframe', 0)
            require(finite(subframe) and 0 <= subframe < 1, 'INVALID_REQUEST',
                    'Keyframe subframe must be in [0,1)')
            moment = frame + subframe
            require(previous is None or moment > previous, 'INVALID_REQUEST',
                    'Keyframes must be strictly increasing')
            previous = moment
            blocks = {key: value for key, value in row.items() if key not in ('frame', 'subframe')}
            normalized.append({'frame': frame, 'subframe': float(subframe),
                               **validate_controls(blocks, 'keyframe')})
        document['keyframes'] = normalized
        require(args.output is not None, 'INVALID_REQUEST', 'author requires a new --output .blend')
    elif not is_directional:
        require(args.output is None, 'INVALID_REQUEST', 'inspect does not use --output')

    if args.output is not None:
        require(args.output.is_absolute() and args.output.suffix.lower() == '.blend',
                'INVALID_REQUEST', 'Output must be an absolute .blend path')
        parent = canonical(args.output.parent)
        for folder in reversed((parent, *parent.parents)):
            if folder != Path(folder.anchor):
                guards.enter_context(FileGuard(folder, directory=True))
        output = parent / args.output.name
        guards.enter_context(PathLocks([output]))
        require(not os.path.lexists(output), 'CONFLICT', 'Output already exists')
        with tempfile.NamedTemporaryFile(dir=parent, prefix='.lighting-author-',
                                         suffix='.blend', delete=False) as temp:
            stage = Path(temp.name)
        guards.callback(stage.unlink, missing_ok=True)
        stage.unlink()
        document['stage'] = str(stage)
    return document


def serialize(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return 0.0 if value == 0 else round(value, 6)
    if isinstance(value, (list, tuple)):
        return [serialize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): serialize(item) for key, item in value.items()}
    if hasattr(value, '__len__') and hasattr(value, '__getitem__'):
        try:
            return [serialize(value[index]) for index in range(len(value))]
        except (TypeError, IndexError):
            pass
    return str(value)


def vector_value(value):
    return [round(float(item), 6) for item in value]


def check_operator(status, label):
    require(status == {'FINISHED'}, 'CONFLICT', f'Native operator {label} did not finish: {status}')


def layer_curves(action):
    curves = []
    if action is None:
        return curves
    for layer in action.layers:
        for strip in layer.strips:
            for bag in getattr(strip, 'channelbags', ()):
                curves.extend(bag.fcurves)
    return curves


def action_state(action):
    if action is None:
        return None
    curves = []
    for curve in sorted(layer_curves(action), key=lambda item: (item.data_path, item.array_index)):
        curves.append({
            'path': curve.data_path,
            'index': curve.array_index,
            'points': [[round(point.co[0], 6), round(point.co[1], 6),
                        point.interpolation] for point in curve.keyframe_points],
        })
    return {'name': action.name, 'frame_range': [round(value, 6) for value in action.frame_range],
            'curves': curves}


def control_map(scene, lighting):
    return {
        'controller': scene.get(lighting.CONTROLLER),
        'anchor': scene.get(lighting.LOCAL_ANCHOR),
        'handle': scene.get(lighting.LOCAL_HANDLE),
        'eyelid': scene.get(lighting.EYELID_CONTROL),
    }


def native_control_fields(lighting):
    return {
        'controller': lighting.CONTROLLER,
        'anchor': lighting.LOCAL_ANCHOR,
        'handle': lighting.LOCAL_HANDLE,
        'eyelid': lighting.EYELID_CONTROL,
    }


def read_control(obj, fields):
    result = {'object': obj.name_full, 'parent': obj.parent.name_full if obj.parent else None}
    for field in fields:
        if field == 'location':
            result[field] = vector_value(obj.location)
        elif field == 'scale':
            result[field] = vector_value(obj.scale)
        else:
            result[field] = serialize(obj.get(field))
    result['action'] = action_state(obj.animation_data.action if obj.animation_data else None)
    return result


def read_state(bpy, lighting, ui, scene, request):
    project = scene.get(lighting.PROJECT)
    target_name = request.get('target', {}).get('object') or (
        scene[lighting.FACE_OBJECT].name if scene.get(lighting.FACE_OBJECT) else None)
    target = scene.objects.get(target_name) if target_name else None
    target_info = None
    material_info = None
    palette_info = {'scene': {}, 'material': {}}
    if target and target.type == 'MESH':
        slot_index = request.get('target', {}).get('material_slot', target.active_material_index)
        slot = target.material_slots[slot_index] if 0 <= slot_index < len(target.material_slots) else None
        target_info = {
            'object': target.name_full,
            'slot': slot_index,
            'material': slot.material.name_full if slot and slot.material else None,
            'link': slot.link if slot else None,
        }
        material = slot.material if slot else None
        if material:
            tree = material.node_tree
            stage_name = material.get(lighting.STAGE)
            node = tree.nodes.get(stage_name) if tree and stage_name else None
            original = None
            if node is not None and node.bl_idname == 'ShaderNodeGroup' and node.node_tree:
                link = node.inputs['Original Surface'].links[0] if node.inputs.get('Original Surface') else None
                if link:
                    original = {'node': link.from_node.name, 'socket': link.from_socket.identifier}
            material_info = {
                'name': material.name_full,
                'stage': stage_name,
                'node_type': node.bl_idname if node else None,
                'role': node.get(lighting.ROLE) if node else None,
                'owner': node.get(lighting.OWNER) if node else None,
                'profile': node.get(lighting.STAGE) if node else None,
                'group': node.node_tree.name_full if node and node.node_tree else None,
                'group_owner': node.node_tree.get(lighting.OWNER) if node and node.node_tree else None,
                'signature': node.node_tree.get(lighting.SIGNATURE) if node and node.node_tree else None,
                'palette_ref': material.get(lighting.PALETTE_REF),
                'original_surface': original,
                'nodes': sorted(item.name for item in node.node_tree.nodes) if node and node.node_tree else [],
            }
            for field in sorted(ui.LABELS):
                if node is not None and node.node_tree and field in node.node_tree.nodes:
                    path = lighting.palette_path(material[lighting.PALETTE_REF], field)
                    palette_info['material'][field] = {
                        'path': path,
                        'value': serialize(scene.get(path)),
                        'override': bool(scene.get(path + '_override')),
                    }
    for field in sorted(ui.LABELS):
        palette_info['scene'][field] = serialize(scene.get(field))

    controls = {}
    for key, obj in control_map(scene, lighting).items():
        if obj is not None:
            controls[key] = read_control(obj, CONTROL_FIELDS[key])

    lights = {}
    for role, prop in (('key', lighting.KEY_LIGHT), ('fill', lighting.FILL_LIGHT)):
        obj = scene.get(prop)
        if obj is None:
            lights[role] = None
            continue
        light = obj.data
        lights[role] = {
            'object': obj.name_full,
            'type': light.type,
            'energy': round(float(light.energy), 6),
            'color': vector_value(light.color),
            'angle_deg': round(math.degrees(light.angle), 6) if light.type == 'SUN' else None,
            'shape': light.shape if light.type == 'AREA' else None,
            'size': round(float(light.size), 6) if light.type == 'AREA' else None,
            'size_y': round(float(light.size_y), 6) if light.type == 'AREA' else None,
            'rotation_euler': vector_value(obj.rotation_euler),
            'location': vector_value(obj.location),
            'hide_render': bool(obj.hide_render),
            'hide_viewport': bool(obj.hide_viewport),
            'owner': serialize(obj.get('toon53_' + project)) if project else None,
        }

    link_state = {}
    for obj in bpy.data.objects:
        if obj.type != 'LIGHT':
            continue
        collection = obj.light_linking.receiver_collection
        link_state[obj.name_full] = {
            'collection': collection.name_full if collection else None,
            'objects': sorted(item.name_full for item in collection.objects) if collection else [],
            'children': sorted(item.name_full for item in collection.children) if collection else [],
            'owner': serialize(collection.get(lighting.OWNER)) if collection else None,
        }

    return {
        'scene': {
            'name': scene.name,
            'frame': scene.frame_current,
            'subframe': round(float(scene.frame_subframe), 6),
            'camera': scene.camera.name_full if scene.camera else None,
            'engine': scene.render.engine,
            'resolution': [scene.render.resolution_x, scene.render.resolution_y,
                           scene.render.resolution_percentage],
            'fps': scene.render.fps,
            'frame_range': [scene.frame_start, scene.frame_end],
            'world': scene.world.name_full if scene.world else None,
            'objects': sorted(obj.name_full for obj in scene.objects),
            'collections': sorted(collection.name_full for collection in bpy.data.collections),
        },
        'project': project,
        'state': {
            'key': scene.toon53_workbench.key.name_full if scene.toon53_workbench.key else None,
            'fill': scene.toon53_workbench.fill.name_full if scene.toon53_workbench.fill else None,
            'role': scene.toon53_workbench.role,
            'mask': scene.toon53_workbench.mask.name_full if scene.toon53_workbench.mask else None,
            'uv': scene.toon53_workbench.uv,
        } if hasattr(scene, ui.PROP) else None,
        'target': target_info,
        'camera': {
            'name': scene.camera.name_full if scene.camera else None,
            'parent': scene.camera.parent.name_full if scene.camera and scene.camera.parent else None,
            'lens': round(float(scene.camera.data.lens), 6) if scene.camera else None,
            'clip': [round(float(scene.camera.data.clip_start), 6),
                     round(float(scene.camera.data.clip_end), 6)] if scene.camera else None,
        },
        'lights': lights,
        'controls': controls,
        'light_linking': link_state,
        'material': material_info,
        'palette': palette_info,
        'object_slots': bindings(bpy),
    }


def record_inventory(bpy, target_name):
    objects = sorted(
        ({'name': obj.name_full, 'type': obj.type,
          'data': obj.data.name_full if obj.data else None,
          'library': obj.library.filepath if obj.library else None}
         for obj in bpy.data.objects),
        key=lambda row: row['name'])
    collections = {
        collection.name_full: {
            'objects': sorted(obj.name_full for obj in collection.objects),
            'children': sorted(child.name_full for child in collection.children),
        }
        for collection in bpy.data.collections
    }
    active = bpy.context.view_layer.objects.active
    selected = sorted(obj.name_full for obj in bpy.context.scene.objects if obj.select_get())
    target = bpy.context.scene.objects.get(target_name) if target_name else None
    return {
        'objects': objects,
        'collections': collections,
        'object_slots': bindings(bpy),
        'light_linking': {
            obj.name_full: (obj.light_linking.receiver_collection.name_full
                            if obj.light_linking.receiver_collection else None)
            for obj in bpy.data.objects if obj.type == 'LIGHT'
        },
        'actions': sorted(action.name_full for action in bpy.data.actions),
        'node_groups': sorted(group.name_full for group in bpy.data.node_groups),
        'images': sorted(image.name_full for image in bpy.data.images),
        'active_object': active.name_full if active else None,
        'active_material_index': target.active_material_index if target else None,
        'selected': selected,
    }


def data_records(bpy):
    from inspection import record

    result = {}
    for kind in ('scenes', 'collections', 'objects', 'lights', 'cameras', 'materials',
                 'meshes', 'node_groups', 'images', 'actions', 'worlds', 'texts', 'armatures'):
        for item in getattr(bpy.data, kind):
            value = record(item)
            ident = f"{value['type']}|{value['name']}|{value.get('library') or ''}"
            result[ident] = value
    return result


def strip_allowed(value, project, scene_record=False):
    value = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    owner = 'toon53_' + project if project else None

    def walk(node):
        if isinstance(node, dict):
            props = node.get('custom_properties')
            if isinstance(props, dict):
                for key in list(props):
                    if scene_record and (key.startswith('toon53') or key.startswith('toon53m_')):
                        props.pop(key, None)
                    elif key == owner:
                        props.pop(key, None)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return value


def compare_records(before, after, allowed):
    def skip(record_value):
        kind = record_value.get('type')
        name = record_value.get('name')
        if kind == 'Material' and name == allowed['target_material']:
            return True
        if kind == 'Object' and name in allowed['objects']:
            return True
        if kind == 'Collection' and name == allowed['collection']:
            return True
        if kind == 'Action' and name in allowed['actions']:
            return True
        if isinstance(kind, str) and kind.endswith('NodeTree') \
                and record_value.get('custom_properties', {}).get('toon53_owner') == allowed['project']:
            return True
        if kind == 'Image' and isinstance(name, str) and name.startswith('三渲二 5.3'):
            return True
        return False

    before_kept = {ident: value for ident, value in before.items() if not skip(value)}
    after_kept = {ident: value for ident, value in after.items() if not skip(value)}
    for ident, value in before_kept.items():
        require(ident in after_kept, 'VALIDATION_FAILED', 'Existing data block disappeared: ' + ident)
        require(after_kept[ident] == value, 'VALIDATION_FAILED',
                'Non-target data block changed: ' + ident)
    for ident, value in after_kept.items():
        if ident in before_kept:
            continue
        kind = value.get('type')
        allow = ((kind == 'Object' and value.get('name') in allowed['objects'])
                 or (kind == 'Collection' and value.get('name') == allowed['collection'])
                 or (kind == 'Action' and value.get('name') in allowed['actions'])
                 or (isinstance(kind, str) and kind.endswith('NodeTree')
                     and value.get('custom_properties', {}).get('toon53_owner') == allowed['project']))
        require(allow, 'VALIDATION_FAILED', 'Unexpected new data block: ' + ident)


def compare_inventory(before, after, allowed):
    before_slots = {row['object']: row for row in before['object_slots']}
    after_slots = {row['object']: row for row in after['object_slots']}
    for object_name, row in before_slots.items():
        require(after_slots.get(object_name) == row, 'VALIDATION_FAILED',
                'Object/material-slot assignment changed outside the wrapper: ' + object_name)
    before_objects = {row['name'] for row in before['objects']}
    after_objects = {row['name'] for row in after['objects']}
    require(after_objects - before_objects <= allowed['objects'], 'VALIDATION_FAILED',
            'Unexpected new objects: ' + ', '.join(sorted(after_objects - before_objects - allowed['objects'])))
    require(before_objects <= after_objects, 'VALIDATION_FAILED', 'An existing object disappeared')
    for name, members in before['collections'].items():
        require(name in after['collections'], 'VALIDATION_FAILED', 'Existing collection disappeared: ' + name)
        require(after['collections'][name] == members, 'VALIDATION_FAILED',
                'Existing collection membership changed: ' + name)
    for name in set(after['collections']) - set(before['collections']):
        require(name == allowed['collection'], 'VALIDATION_FAILED',
                'Unexpected new collection: ' + name)
    for name, value in before['light_linking'].items():
        if name == allowed['fill']:
            require(after['light_linking'].get(name) in (value, allowed['collection']),
                    'VALIDATION_FAILED', 'Fill light receiver assignment changed unexpectedly')
        else:
            require(after['light_linking'].get(name) == value, 'VALIDATION_FAILED',
                    'Non-fill light receiver assignment changed: ' + name)
    require(set(before['actions']) <= set(after['actions']), 'VALIDATION_FAILED',
            'An existing action disappeared')
    require(set(after['actions']) - set(before['actions']) <= allowed['actions'],
            'VALIDATION_FAILED', 'Unexpected new action appeared')
    require(set(before['node_groups']) <= set(after['node_groups']), 'VALIDATION_FAILED',
            'An existing node group disappeared')
    require(set(after['node_groups']) - set(before['node_groups']) <= allowed['node_groups'],
            'VALIDATION_FAILED', 'Unexpected new node group appeared')
    require(set(before['images']) <= set(after['images']), 'VALIDATION_FAILED',
            'An existing image disappeared')
    require(set(after['images']) - set(before['images']) <= allowed['images'],
            'VALIDATION_FAILED', 'Unexpected new image appeared')
    require(before['active_object'] == after['active_object'], 'VALIDATION_FAILED',
            'Active object was not restored')
    require(before['active_material_index'] == after['active_material_index'], 'VALIDATION_FAILED',
            'Active material slot was not restored')
    require(before['selected'] == after['selected'], 'VALIDATION_FAILED',
            'Object selection was not restored')


def material_output(material):
    tree = material.node_tree
    require(tree is not None, 'CONFLICT', 'Target material has no node tree')
    output = tree.get_output_node('EEVEE')
    require(output is not None and len(output.inputs['Surface'].links) == 1
            and not output.inputs['Volume'].is_linked, 'CONFLICT',
            'Target material needs one opaque EEVEE Surface output')
    return output, output.inputs['Surface'].links[0].from_socket


def light_object(scene, value, label):
    obj = scene.objects.get(value)
    require(obj is not None and obj.type == 'LIGHT', 'NOT_FOUND',
            f'{label} light is absent from the saved scene: {value}')
    require(obj.library is None and obj.override_library is None and obj.data.library is None
            and obj.data.is_editable, 'CONFLICT', f'{label} light must be local and editable')
    require(obj.data.users == 1, 'CONFLICT', f'{label} light data must be independently owned')
    return obj


def target_object(scene, value):
    obj = scene.objects.get(value['object'])
    require(obj is not None and obj.type == 'MESH', 'NOT_FOUND',
            'Target mesh is absent from the saved scene: ' + value['object'])
    require(obj.library is None and obj.override_library is None and obj.data.library is None
            and obj.data.is_editable, 'CONFLICT', 'Target mesh must be local and editable')
    require(0 <= value['material_slot'] < len(obj.material_slots), 'NOT_FOUND',
            'Target material slot is absent')
    slot = obj.material_slots[value['material_slot']]
    actual = slot.material.name_full if slot.material else None
    require(slot.link == value['expected_link'] and actual == value['expected_material'], 'CONFLICT',
            f'Target slot is {slot.link}/{actual}, expected {value["expected_link"]}/{value["expected_material"]}')
    require(any(poly.material_index == value['material_slot'] for poly in obj.data.polygons),
            'CONFLICT', 'Target slot has no polygons')
    require(slot.material is not None and slot.material.node_tree is not None, 'CONFLICT',
            'Target slot needs a material with nodes')
    return obj, slot.material


def camera_object(scene, value):
    camera = scene.camera
    require(camera is not None, 'NOT_FOUND', 'The saved scene has no active camera')
    require(camera.name_full == value, 'CONFLICT',
            f'Active camera is {camera.name_full}, expected {value}')
    require(camera.type == 'CAMERA' and camera.library is None and camera.override_library is None
            and camera.data.library is None and camera.data.is_editable, 'CONFLICT',
            'Active camera must be local and editable')
    require(scene.objects.get(camera.name) == camera, 'CONFLICT', 'Active camera is not linked to the scene')
    return camera


def load_lighting(bpy, request):
    product = request['product']
    edition = request['edition']
    observed = tuple(bpy.app.version)
    require(is_supported_host(observed), 'UNSUPPORTED', 'Lighting author requires Blender 5.3.0 or later')
    require(tuple(product['host']) == HOST and product['edition'] == edition
            and product['manifest_sha256'] == LIGHTING_PRODUCTS[edition][2], 'CONFLICT',
            'Selected Toon product differs from the pinned bundle')
    load_modules(bpy, request)
    package = sys.modules['toon_workflow']
    domain = sys.modules['toon_workflow.toon_workflow_addon']
    lighting = importlib.import_module(domain.__name__ + '.lighting_53')
    ui = importlib.import_module(domain.__name__ + '.lighting_53_ui')
    return package, lighting, ui


def check_light_rig(scene, request, lighting):
    rig = request['rig']
    key = light_object(scene, rig['key'], 'Key')
    require(key.data.type == 'SUN', 'INVALID_REQUEST', 'Key light must be a SUN')
    fill = light_object(scene, rig['fill'], 'Fill') if rig['fill'] else None
    require(fill is None or fill != key, 'INVALID_REQUEST', 'Key and fill must differ')
    return key, fill


def write_palette_value(scene, path, field, value, lighting, *, scene_value=False):
    is_color = isinstance(value, list)
    require((field in lighting.COLORS) == is_color
            and (not is_color or not scene_value or len(value) == 3),
            'INVALID_REQUEST', f'{field} has the wrong value type for the native palette')
    scene[path] = tuple(value) if is_color else value
    require(serialize(scene.get(path)) == serialize(value), 'VALIDATION_FAILED',
            'Palette value readback differs for ' + field)


def set_scene_palette(scene, lighting, ui, values):
    events = []
    for field, value in sorted(values.items()):
        require(field in ui.LABELS, 'INVALID_REQUEST', 'Unknown palette field: ' + field)
        require(field in scene, 'CONFLICT', 'Scene palette field is absent: ' + field)
        before = serialize(scene.get(field))
        write_palette_value(scene, field, field, value, lighting, scene_value=True)
        events.append({'field': field, 'before': before, 'after': serialize(value)})
    if events:
        scene.update_tag()
    return events


def set_palette(bpy, scene, lighting, ui, material, values, clear):
    events = {'set': [], 'cleared': []}
    if not values and not clear:
        return events
    token = material.get(lighting.PALETTE_REF)
    require(isinstance(token, str) and token, 'CONFLICT',
            'palette overrides require a bound material stage')
    require(all(other == material or other.get(lighting.PALETTE_REF) != token
                for other in bpy.data.materials), 'CONFLICT',
            'Another material shares the palette token')
    stage = material.node_tree.nodes.get(material.get(lighting.STAGE, ''))
    require(stage is not None and stage.node_tree is not None
            and stage.get(lighting.OWNER) == scene[lighting.PROJECT], 'CONFLICT',
            'The bound material stage must belong to this scene')
    for field in sorted(set(values) | set(clear)):
        require(field in ui.LABELS, 'INVALID_REQUEST', 'Unknown palette field: ' + field)
        require(field in stage.node_tree.nodes, 'INVALID_REQUEST',
                f'{field} is not used by this material role')
        path = lighting.palette_path(token, field)
        flag = path + '_override'
        require(path in scene and flag in scene, 'CONFLICT', 'Missing saved material property: ' + field)
        enable = field in values
        was_enabled = bool(scene[flag])
        before = serialize(scene[path])
        if was_enabled != enable:
            check_operator(bpy.ops.toon53.toggle_palette_override(field=field),
                           'toggle_palette_override')
        require(bool(scene[flag]) == enable, 'VALIDATION_FAILED', 'Palette override flag differs for ' + field)
        if enable:
            write_palette_value(scene, path, field, values[field], lighting)
            events['set'].append({'field': field, 'value': serialize(values[field]), 'was_enabled': was_enabled})
        else:
            require(serialize(scene[path]) == before, 'VALIDATION_FAILED',
                    'Clearing the override changed the saved value for ' + field)
            events['cleared'].append({'field': field, 'value': before, 'was_enabled': was_enabled})
    scene.update_tag()
    return events


def verify_palette(observed, scene_events, material_events):
    palette = observed['palette']
    for event in scene_events:
        require(palette['scene'][event['field']] == event['after'], 'CONFLICT',
                'Scene palette value changed during evaluation: ' + event['field'])
    for kind, events in material_events.items():
        for event in events:
            readback = palette['material'][event['field']]
            require(readback['value'] == event['value'] and readback['override'] == (kind == 'set'),
                    'CONFLICT', 'Material palette changed during evaluation: ' + event['field'])


def apply_controls(controls, state):
    for key, values in state.items():
        obj = controls.get(key)
        require(obj is not None, 'CONFLICT', 'Control object is absent: ' + key)
        for field, value in values.items():
            if field == 'location':
                obj.location = value
            elif field == 'scale':
                obj.scale = value
            else:
                obj[field] = value


def current_control_values(controls):
    state = {}
    for key, fields in CONTROL_FIELDS.items():
        obj = controls.get(key)
        if obj is None:
            continue
        state[key] = {
            field: (vector_value(obj.location) if field == 'location'
                    else vector_value(obj.scale) if field == 'scale'
                    else serialize(obj.get(field)))
            for field in fields
        }
    return state


def keyframes(bpy, scene, controls, initial_edits, rows):
    edits = {key: dict(fields) for key, fields in initial_edits.items()}
    written = []
    original = (scene.frame_current, scene.frame_subframe)
    try:
        for row in rows:
            frame = row['frame']
            subframe = row['subframe']
            scene.frame_set(frame, subframe=subframe)
            state = current_control_values(controls)
            for key, patch in ((key, value) for key, value in row.items()
                               if key not in ('frame', 'subframe')):
                require(key in state, 'INVALID_REQUEST', f'Control {key} is not available')
                edits.setdefault(key, {}).update(patch)
            for key, patch in edits.items():
                require(key in state, 'INVALID_REQUEST', f'Control {key} is not available')
                state[key].update(patch)
            apply_controls(controls, edits)
            check_operator(bpy.ops.toon53.key_shadow_control(), 'key_shadow_control')
            written.append({'frame': frame, 'subframe': subframe,
                            'values': json.loads(json.dumps(state, ensure_ascii=False, allow_nan=False))})
    finally:
        scene.frame_set(original[0], subframe=original[1])
    return written


def verify_samples(scene, controls, rows):
    original = (scene.frame_current, scene.frame_subframe)
    samples = []
    try:
        for row in rows:
            scene.frame_set(row['frame'], subframe=row['subframe'])
            expected = row['values']
            actual = {}
            for key, values in expected.items():
                obj = controls[key]
                actual[key] = {
                    field: (vector_value(obj.location) if field == 'location'
                            else vector_value(obj.scale) if field == 'scale'
                            else serialize(obj.get(field)))
                    for field in values
                }
            require(actual == expected, 'VALIDATION_FAILED',
                    f'Animation sample differs at frame {row["frame"]}+{row["subframe"]}: '
                    f'expected {expected}, actual {actual}')
            samples.append({'frame': row['frame'], 'subframe': row['subframe'], 'values': actual})
    finally:
        scene.frame_set(original[0], subframe=original[1])
    return samples


def normal_bridge(scene, lighting, material):
    stage, key = lighting._face_groups(scene, material)
    require(key is not None, 'CONFLICT', 'The FACE stage is missing its owned key-light normal reader')
    diffuse = material.node_tree.nodes.get(key.get(lighting.KEY_DIFFUSE, ''))
    if diffuse is None and lighting.KEY_DIFFUSE not in key:
        from toon_workflow.toon_workflow_addon import normal_ui
        diffuse, _ = normal_ui._diffuse(material.node_tree)
    require(diffuse is not None and diffuse.bl_idname == 'ShaderNodeBsdfDiffuse', 'CONFLICT', 'Original face Diffuse is missing')

    def source(socket):
        link = lighting._input_source(socket) if socket is not None else None
        return {"node": link.node.name, "socket": link.identifier} if link else None

    primary, fill = lighting._face_normal_sockets(stage.node_tree)
    return stage, {'diffuse_normal': source(diffuse.inputs['Normal']),
                   'face_normal': source(stage.inputs.get(lighting.FACE_NORMAL)),
                   'key_normal': source(primary), 'fill_factor': source(fill)}


def save_normal_result(bpy, request, observed, records, inventory, resources):
    bpy.context.preferences.filepaths.save_version = 0
    bpy.context.preferences.filepaths.file_preview_type = 'NONE'
    require(bpy.ops.wm.save_as_mainfile(filepath=request['stage'], copy=True, relative_remap=True,
                                      check_existing=False) == {'FINISHED'}, 'IO_ERROR', 'Could not save the FACE candidate')
    return {'status': 'pass', 'runtime': {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(),
             'pid': os.getpid()}, 'observed': observed, 'witness': {'records': records},
            'inventory': inventory, 'resources': resources, 'keyframes': [], 'samples': [],
            'output_sha256': digest(request['stage'])}


def directional_work(bpy, phase, request, scene, lighting):
    from toon_workflow.toon_workflow_addon.joint_workflow import runtime_paths

    target, _ = target_object(scene, request['target'])
    resources = sorted(str(path.resolve()) for path in runtime_paths())
    require(resources == sorted(row['file'] for row in request['resources']), 'CONFLICT',
            'Expected exact pinned runtime resources')
    runtime = {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(), 'pid': os.getpid()}

    def saved_inventory():
        inventory = record_inventory(bpy, target.name_full)
        inventory['images'] = sorted(image.name_full for image in bpy.data.images
                                     if image.users and image.source != 'VIEWER')
        return inventory

    before = saved_inventory()
    if phase == 'verify':
        proof = request['verify']
        observed = directional.snapshot(bpy, scene, request, lighting)
        fields = [key for key in observed.keys() | proof['observed'].keys()
                  if observed.get(key) != proof['observed'].get(key)]
        require(not fields, 'VALIDATION_FAILED', 'Cold reopen author fields differ: ' + ', '.join(sorted(fields)))
        fields = [key for key in before.keys() | proof['inventory'].keys()
                  if before.get(key) != proof['inventory'].get(key)]
        require(not fields, 'VALIDATION_FAILED', 'Cold reopen inventory differs: ' + ', '.join(sorted(fields)))
        require(resources == proof['resources'], 'VALIDATION_FAILED', 'Cold reopen dependencies differ')
        return {'runtime': runtime, 'observed': observed, 'verified': True}
    if request['operation'] in READ_OPERATIONS:
        return {'runtime': runtime, 'observed': directional.snapshot(bpy, scene, request, lighting)}

    creates_rig = 'rig' in request and not scene.get(lighting.PROJECT)
    observed = directional.operate(bpy, scene, request, lighting)
    after = saved_inventory()
    protected_after = after
    if creates_rig:
        controls = [scene.get(key) for key in (lighting.CONTROLLER, lighting.LOCAL_ANCHOR, lighting.LOCAL_HANDLE)]
        owner = scene[lighting.PROJECT]
        require(all(obj and obj.type == 'EMPTY' and obj.hide_render and obj.get(lighting.OWNER) == owner
                    for obj in controls), 'VALIDATION_FAILED', 'Initial preparation did not create its managed controls')
        control_names = {obj.name_full for obj in controls}
        require(len(control_names) == 3, 'VALIDATION_FAILED', 'Initial preparation duplicated a control')
        fill = scene.get(lighting.FILL_LIGHT)
        receiver = fill.light_linking.receiver_collection if fill else None
        expected_collection = {receiver.name_full} if receiver else set()
        require(set(after['collections']) - set(before['collections']) == expected_collection, 'VALIDATION_FAILED',
                'Initial preparation added an unexpected collection')
        if receiver:
            require(receiver.get(lighting.OWNER) == owner and set(receiver.objects) == {target},
                    'VALIDATION_FAILED', 'Initial preparation changed the fill receiver scope')
        protected_after = dict(after,
            objects=[row for row in after['objects'] if row['name'] not in control_names],
            collections={name: value for name, value in after['collections'].items() if name not in expected_collection},
            object_slots=[row for row in after['object_slots'] if row['object'] not in control_names],
            light_linking={**after['light_linking'],
                           **({fill.name_full: before['light_linking'][fill.name_full]} if fill else {})})
    protected = ('object_slots', 'actions', 'images', 'active_object', 'active_material_index', 'selected',
                 'objects', 'collections', 'light_linking')
    require(all(before[key] == protected_after[key] for key in protected), 'VALIDATION_FAILED',
            'Directional authoring changed a protected object, material assignment, action or light link')
    require(resources == sorted(str(path.resolve()) for path in runtime_paths()), 'VALIDATION_FAILED',
            'Directional authoring changed external dependencies')
    return save_normal_result(bpy, request, observed, len(protected), after, resources)


def repair_normal(bpy, scene, request, lighting, ui):
    camera_object(scene, request['camera'])
    target, material = target_object(scene, request['target'])
    resources = dependencies(bpy)
    require(resources == sorted(row['file'] for row in request['resources']), 'CONFLICT', 'Expected exact pinned resources')
    inventory = record_inventory(bpy, target.name_full)
    before_records = data_records(bpy)
    try:
        stage, before = normal_bridge(scene, lighting, material)
        result = lighting.repair_face_normal(scene, target, request['target']['material_slot'])
        _, after = normal_bridge(scene, lighting, material)
    except (ValueError, RuntimeError, KeyError) as error:
        raise Failure('CONFLICT', str(error)) from error
    require(result['status'] == 'repaired', 'CONFLICT', 'No unapplied supported face normal is available')
    require(after['diffuse_normal'] == before['diffuse_normal'] == after['face_normal']
            and after['key_normal'] is not None and after['fill_factor'] != before['fill_factor'],
            'VALIDATION_FAILED', 'The original Diffuse source did not reach the repaired readers')
    after_records = data_records(bpy)
    target_tree = stage.node_tree.name_full
    bridged_ports = {stage.inputs[label].identifier for label in (lighting.FACE_NORMAL, lighting.ARTIST_NORMAL)
                     if stage.inputs.get(label) is not None}
    mix_path = result.get('mix_path')
    if mix_path:
        require(scene.get(mix_path) == .5, 'VALIDATION_FAILED', 'Native Cast normal mix did not initialize correctly')
    for name, prior in before_records.items():
        current = after_records.get(name)
        require(current is not None, 'VALIDATION_FAILED', 'Existing data block disappeared: ' + name)
        if prior['type'] == 'Material' and prior['name'] == material.name_full:
            original_tree = prior['nodes']
            final_tree = current['nodes']
            nodes = [{**node, 'inputs': [port for port in node['inputs'] if port['identifier'] not in bridged_ports]}
                     if node['name'] == stage.name else node for node in final_tree['nodes']]
            require({key: value for key, value in prior.items() if key != 'nodes'} ==
                    {key: value for key, value in current.items() if key != 'nodes'} and
                    original_tree['nodes'] == nodes, 'VALIDATION_FAILED', 'Original face material nodes changed')
            links = [link for link in final_tree['links'] if not (link['to_node'] == stage.name and
                     link['to_socket'] in bridged_ports)]
            require(links == original_tree['links'], 'VALIDATION_FAILED', 'Original face material links changed')
        elif prior['name'] != target_tree or prior['type'] != 'ShaderNodeTree':
            if mix_path and prior['type'] == 'Scene' and prior['name'] == scene.name:
                properties = dict(current.get('custom_properties', {}))
                properties.pop(mix_path, None)
                current = {**current, 'custom_properties': properties}
            require(current == prior, 'VALIDATION_FAILED', 'Repair changed non-target data: ' + name)
    require(before_records.keys() == after_records.keys()
            and record_inventory(bpy, target.name_full) == inventory and dependencies(bpy) == resources,
            'VALIDATION_FAILED', 'Repair changed data inventory or dependencies')
    observed = read_state(bpy, lighting, ui, scene, request)
    observed['normal_bridge'] = after
    return {**save_normal_result(bpy, request, observed, len(after_records), inventory, resources),
            'normal_repair': {'before': before, 'native': result, 'after': after}}


def normal_mix_state(bpy, scene, lighting, material):
    stage, bridge = normal_bridge(scene, lighting, material)
    group = stage.node_tree
    require(group.nodes.get(lighting.NORMAL_MIX) is not None, 'CONFLICT', 'FACE target needs a repaired Cast normal mix')
    path = lighting.face_normal_mix_path(material)
    evaluated = group.evaluated_get(bpy.context.evaluated_depsgraph_get())
    actual = float(evaluated.nodes[lighting.NORMAL_MIX].outputs[0].default_value)
    require(math.isclose(actual, scene[path], abs_tol=1e-6), 'VALIDATION_FAILED', 'Cast mix driver did not consume the scene value')
    return {'path': f'["{path}"]', 'value': round(scene[path], 6), 'evaluated_shader_value': round(actual, 6), 'bridge': bridge}


def set_normal_mix(bpy, scene, request, lighting, ui):
    from inspection import record

    camera_object(scene, request['camera'])
    target, material = target_object(scene, request['target'])
    resources = dependencies(bpy)
    require(resources == sorted(row['file'] for row in request['resources']), 'CONFLICT', 'Expected exact pinned resources')
    try:
        native = lighting.repair_face_normal(scene, target, request['target']['material_slot'])
    except (ValueError, RuntimeError, KeyError) as error:
        raise Failure('CONFLICT', str(error)) from error
    require(native['status'] == 'unchanged' and native.get('mix_path'), 'CONFLICT', 'FACE Cast bridge must already be repaired')
    before = normal_mix_state(bpy, scene, lighting, material)
    inventory = record_inventory(bpy, target.name_full)
    frame = scene.frame_current
    path = before['path']
    ad = scene.animation_data
    action = ad.action if ad else None
    require(not ad or not any(fc.data_path == path for fc in ad.drivers), 'CONFLICT', 'Scene mix is driven')
    require(not ad or not ad.nla_tracks, 'CONFLICT', 'Select a saved scene without NLA mixing for this property')
    curves = [(bag.slot_handle, fc) for layer in action.layers for strip in layer.strips
              for bag in getattr(strip, 'channelbags', ()) for fc in bag.fcurves if fc.data_path == path] if action else []
    wanted = request['normal_mix']
    require(not math.isclose(wanted['value'], before['value'], abs_tol=1e-6), 'CONFLICT', 'Scene mix already has this value')
    if wanted['mode'] == 'STATIC':
        require(not curves, 'CONFLICT', 'Scene mix is animated; replace its key at the saved frame')
        scene[native['mix_path']] = float(wanted['value'])
        scene.update_tag()
    else:
        require(action is not None and action.users == 1 and action.library is None and action.is_editable
                and ad.action_slot is not None and len(curves) == 1 and curves[0][0] == ad.action_slot.handle,
                'CONFLICT', 'Expected a unique local scene action and one assigned mix curve')
        curve = curves[0][1]
        points = [point for point in curve.keyframe_points if point.co.x == frame]
        require(len(points) == 1 and not curve.modifiers and not curve.sampled_points, 'CONFLICT',
                'Expected one authored mix key at the saved frame, without modifiers or cached keys')
        before_action = action_state(action)
        point = points[0]
        delta = wanted['value'] - point.co.y
        point.co.y = wanted['value']
        point.handle_left.y += delta
        point.handle_right.y += delta
        curve.update()
        action.update_tag()
        for row in before_action['curves']:
            if row['path'] == path:
                row['points'] = [[time, round(wanted['value'], 6) if time == frame else value, interpolation]
                                 for time, value, interpolation in row['points']]
        require(action_state(action) == before_action, 'VALIDATION_FAILED', 'A different action key was changed')
    scene.frame_set(frame)
    after = normal_mix_state(bpy, scene, lighting, material)
    require(after['bridge'] == before['bridge'] and math.isclose(after['value'], wanted['value'], abs_tol=1e-6)
            and dependencies(bpy) == resources and record_inventory(bpy, target.name_full) == inventory,
            'VALIDATION_FAILED', 'The mix value did not persist or changed unrelated bindings')
    observed = read_state(bpy, lighting, ui, scene, request)
    observed['normal_mix'] = {**after, 'action': record(action) if action else None}
    return {**save_normal_result(bpy, request, observed, len(inventory), inventory, resources),
            'normal_mix': {'before': before, 'after': after, 'mode': wanted['mode'], 'frame': frame}}


def work(phase, request, job):
    import bpy

    sys.dont_write_bytecode = True
    sys.pycache_prefix = str(Path(job) / (phase + '-no-bytecode'))
    package, lighting, ui = load_lighting(bpy, request)
    source = request['stage'] if phase == 'verify' else request['file']
    context = open_saved_context(bpy, {**request, 'file': source})
    scene = context.scene
    package.register()
    saved_version = tuple(bpy.data.version[:2])
    supported_version = saved_version >= HOST[:2] if request['edition'] == CURRENT_EDITION else saved_version == HOST[:2]
    require(supported_version, 'UNSUPPORTED', 'Saved .blend format does not match the selected Toon edition')
    require(scene.render.engine == 'BLENDER_EEVEE', 'UNSUPPORTED',
            'lighting-author requires an EEVEE scene')
    if request['operation'] in directional.OPERATIONS:
        return directional_work(bpy, phase, request, scene, lighting)

    if phase == 'verify':
        proof = request.get('verify')
        require(isinstance(proof, dict) and {'observed', 'witness', 'inventory', 'resources'} <= proof.keys(),
                'INVALID_REQUEST', 'Verification request is incomplete')
        observed = read_state(bpy, lighting, ui, scene, request)
        if request['operation'] in NORMAL_OPERATIONS:
            _, material = target_object(scene, request['target'])
            if request['operation'] == REPAIR_NORMAL:
                _, observed['normal_bridge'] = normal_bridge(scene, lighting, material)
            else:
                from inspection import record
                observed['normal_mix'] = normal_mix_state(bpy, scene, lighting, material)
                action = scene.animation_data.action if scene.animation_data else None
                observed['normal_mix']['action'] = record(action) if action else None
        require(observed == proof['observed'], 'VALIDATION_FAILED',
                'Cold reopen lighting readback differs from the authored state')
        require(record_inventory(bpy, request.get('target', {}).get('object')) == proof['inventory'],
                'VALIDATION_FAILED', 'Cold reopen inventory differs')
        require(dependencies(bpy) == proof['resources'], 'VALIDATION_FAILED',
                'Cold reopen dependencies differ')
        return {'runtime': {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(),
                            'pid': os.getpid()},
                'observed': observed, 'verified': True}

    if request.get('verify'):
        raise Failure('INVALID_REQUEST', 'verify is reserved for the adapter reopen phase')

    if request['operation'] == 'inspect':
        return {'status': 'read',
                'runtime': {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(),
                            'pid': os.getpid()},
                'observed': read_state(bpy, lighting, ui, scene, request),
                'dependencies': dependencies(bpy)}

    if request['operation'] == REPAIR_NORMAL:
        return repair_normal(bpy, scene, request, lighting, ui)
    if request['operation'] == SET_NORMAL_MIX:
        return set_normal_mix(bpy, scene, request, lighting, ui)

    key, fill = check_light_rig(scene, request, lighting)
    camera = camera_object(scene, request['camera'])
    target, material = target_object(scene, request['target'])
    if request.get('bind') or request.get('palette') or request.get('palette_clear'):
        users = [(obj.name_full, index) for obj in bpy.data.objects for index, slot in enumerate(obj.material_slots)
                 if slot.material == material]
        require(material.library is None and material.override_library is None and material.is_editable
                and users == [(target.name_full, request['target']['material_slot'])],
                'CONFLICT', 'Binding or palette editing requires a local material unique to the specified slot')
    output, original_surface = material_output(material)
    resources = dependencies(bpy)
    require(resources == sorted({row['file'] for row in request['resources']}), 'CONFLICT',
            'Saved scene dependencies differ from the pinned resources list')

    selected = sorted(obj.name_full for obj in scene.objects if obj.select_get())
    active = context.view_layer.objects.active
    active_index = target.active_material_index
    before_records = data_records(bpy)
    before_inventory = record_inventory(bpy, target.name_full)
    before_links = {
        obj.name_full: (obj.light_linking.receiver_collection.name_full
                        if obj.light_linking.receiver_collection else None)
        for obj in bpy.data.objects if obj.type == 'LIGHT'
    }

    for obj in scene.objects:
        obj.select_set(False)
    target.select_set(True)
    context.view_layer.objects.active = target
    target.active_material_index = request['target']['material_slot']

    state = getattr(scene, ui.PROP)
    state.key = key
    state.fill = fill
    if 'bind' in request:
        state.role = request['bind']['role']
        state.mask = bpy.data.images.get(request['bind']['mask']) if request['bind']['mask'] else None
        if request['bind']['mask']:
            require(state.mask is not None and state.mask.library is None
                    and state.mask.source == 'FILE' and state.mask.packed_file
                    and state.mask.colorspace_settings.name == 'Non-Color', 'CONFLICT',
                    'HAIR mask must be a local packed Non-Color image')
        state.uv = request['bind']['uv']

    before_actions = {key: action_state(obj.animation_data.action if obj.animation_data else None)
                      for key, obj in control_map(scene, lighting).items() if obj}
    check_operator(bpy.ops.toon53.prepare(), 'prepare')
    require(scene.get(lighting.PROJECT) and len(scene[lighting.PROJECT]) == 32, 'VALIDATION_FAILED',
            'Native prepare did not create a project owner')
    require(scene.get(lighting.KEY_LIGHT) == key, 'VALIDATION_FAILED', 'Key light identity changed')
    require(scene.get(lighting.FILL_LIGHT) == fill if fill else lighting.FILL_LIGHT not in scene,
            'VALIDATION_FAILED', 'Fill light identity changed')
    controller = scene.get(lighting.CONTROLLER)
    require(controller is not None and controller.parent == camera, 'VALIDATION_FAILED',
            'Shadow controller was not parented to the active camera')

    if 'bind' in request:
        already_bound = bool(material.get(lighting.STAGE))
        if already_bound:
            stage = material.node_tree.nodes.get(material[lighting.STAGE])
            require(stage is not None and stage.get(lighting.ROLE) == request['bind']['role']
                    and stage.get(lighting.OWNER) == scene[lighting.PROJECT], 'CONFLICT',
                    'Existing material stage has a different role or owner')
        else:
            check_operator(bpy.ops.toon53.bind(), 'bind')
        stage = material.node_tree.nodes.get(material.get(lighting.STAGE, ''))
        require(stage is not None and stage.bl_idname == 'ShaderNodeGroup'
                and stage.get(lighting.ROLE) == request['bind']['role']
                and stage.get(lighting.OWNER) == scene[lighting.PROJECT]
                and stage.get(lighting.STAGE) == lighting.PROFILE
                and stage.node_tree is not None, 'VALIDATION_FAILED',
                'Native bind did not create an owned 5.3 stage')
        inp = stage.inputs.get('Original Surface')
        require(inp is not None and len(inp.links) == 1, 'VALIDATION_FAILED',
                'Native bind did not preserve the original Surface source')
        if not already_bound:
            link = inp.links[0]
            require(link.from_node == original_surface.node and link.from_socket == original_surface,
                    'VALIDATION_FAILED', 'Native bind changed the original Surface source')
        require(material.get(lighting.PALETTE_REF), 'VALIDATION_FAILED', 'Native bind missed the palette token')

    if 'eyelid' in request:
        check_operator(bpy.ops.toon53.add_eyelid_control(), 'add_eyelid_control')
        require(scene.get(lighting.EYELID_CONTROL) is not None, 'VALIDATION_FAILED',
                'Eyelid control was not created')
        if request['eyelid']:
            apply_controls({'eyelid': scene[lighting.EYELID_CONTROL]}, {'eyelid': request['eyelid']})

    scene_palette = set_scene_palette(scene, lighting, ui, request.get('scene_palette', {}))
    material_palette = set_palette(bpy, scene, lighting, ui, material, request.get('palette', {}),
                                   request.get('palette_clear', ()))

    controls = control_map(scene, lighting)
    edits = request.get('controls', {})
    for obj in controls.values():
        if request['keyframes'] and obj and obj.animation_data and obj.animation_data.action:
            action = obj.animation_data.action
            require(action.library is None and action.users - int(action.use_fake_user) <= 1, 'CONFLICT',
                    f'Keying requires an independent local action on {obj.name_full}')
    if not request['keyframes']:
        apply_controls(controls, edits)
    written = keyframes(bpy, scene, controls, edits, request['keyframes'])
    samples = verify_samples(scene, controls, written)

    scene.frame_set(request['context']['frame'], subframe=0)
    for obj in scene.objects:
        obj.select_set(False)
    for name_value in selected:
        obj = scene.objects.get(name_value)
        if obj:
            obj.select_set(True)
    context.view_layer.objects.active = active
    target.active_material_index = active_index

    after_records = data_records(bpy)
    after_inventory = record_inventory(bpy, target.name_full)
    control_names = {obj.name_full for obj in controls.values() if obj is not None}
    receiver = fill.light_linking.receiver_collection if fill else None
    receiver_name = receiver.name_full if receiver else None
    control_actions = {
        obj.animation_data.action.name_full for obj in controls.values()
        if obj is not None and obj.animation_data and obj.animation_data.action
    }
    before_groups = {value.get('name') for value in before_records.values()
                     if isinstance(value.get('type'), str) and value['type'].endswith('NodeTree')}
    new_groups = {group.name_full for group in bpy.data.node_groups} - before_groups
    # New node groups are checked by owner in compare_records; inventory only
    # needs the post-author names.
    allowed = {
        'project': scene[lighting.PROJECT],
        'target_material': material.name_full,
        'objects': control_names,
        'collection': receiver_name,
        'fill': fill.name_full if fill else None,
        'actions': control_actions,
        'node_groups': new_groups,
        'images': {image.name_full for image in bpy.data.images
                   if image.name_full.startswith('三渲二 5.3')},
    }
    compare_inventory(before_inventory, after_inventory, allowed)
    compare_records({ident: strip_allowed(value, scene[lighting.PROJECT],
                                           value.get('type') == 'Scene')
                     for ident, value in before_records.items()},
                    {ident: strip_allowed(value, scene[lighting.PROJECT],
                                           value.get('type') == 'Scene')
                     for ident, value in after_records.items()}, allowed)
    before_material = next(
        (value for value in before_records.values()
         if value.get('type') == 'Material' and value.get('name') == material.name_full), None)
    after_material = next(
        (value for value in after_records.values()
         if value.get('type') == 'Material' and value.get('name') == material.name_full), None)
    require(before_material is not None and after_material is not None, 'VALIDATION_FAILED',
            'Target material record is absent from the witness')
    require(before_material.get('material') == after_material.get('material'), 'VALIDATION_FAILED',
            'Target material settings changed outside the wrapper')
    before_nodes = {node['name']: node for node in before_material['nodes']['nodes']}
    after_nodes = {node['name']: node for node in after_material['nodes']['nodes']}
    for node_name, node in before_nodes.items():
        require(after_nodes.get(node_name) == node, 'VALIDATION_FAILED',
                'Existing target-material node changed: ' + node_name)
    require(scene.get(lighting.KEY_LIGHT) == key and scene.get(lighting.FILL_LIGHT) == fill,
            'VALIDATION_FAILED', 'Saved project light references changed')
    require(before_links[fill.name_full] in (None, receiver_name) if fill else True,
            'VALIDATION_FAILED', 'Fill light already had a different receiver collection')

    observed = read_state(bpy, lighting, ui, scene, request)
    verify_palette(observed, scene_palette, material_palette)
    require(len(written) == len(request['keyframes']) == len(samples), 'VALIDATION_FAILED',
            'Keyframe count differs')
    for control, info in observed['controls'].items():
        action = info.get('action')
        previous = before_actions.get(control)
        if not written:
            require(action == previous, 'VALIDATION_FAILED', f'Static edit changed animation on {control}')
            for field, value in edits.get(control, {}).items():
                require(info.get(field) == serialize(value), 'CONFLICT',
                        f'Saved animation overrides the static edit on {control}.{field}; specify a keyframe')
            continue
        require(action is not None, 'VALIDATION_FAILED', 'Control has no keyed action: ' + control)
        previous_curves = {(c['path'], c['index']): c['points'] for c in previous['curves']} if previous else {}
        affected = {(field if field in VECTOR_FIELDS else '["' + field + '"]', index)
                    for field in CONTROL_FIELDS[control] for index in range(3 if field in VECTOR_FIELDS else 1)}
        require({(c['path'], c['index']) for c in action['curves']} == affected | previous_curves.keys(),
                'VALIDATION_FAILED', f'Unexpected keyed channels on {control}')
        times = {round(row['frame'] + row['subframe'], 6) for row in written}
        for curve in action['curves']:
            key = curve['path'], curve['index']
            old_points = previous_curves.get(key, [])
            touched = times if key in affected else set()
            require({p[0] for p in curve['points']} == touched | {p[0] for p in old_points}
                    and [p for p in curve['points'] if p[0] not in touched] == [p for p in old_points if p[0] not in touched],
                    'VALIDATION_FAILED', f'Unselected keys changed on {control}:{key}')

    bpy.context.preferences.filepaths.save_version = 0
    bpy.context.preferences.filepaths.file_preview_type = 'NONE'
    require(bpy.ops.wm.save_as_mainfile(filepath=request['stage'], copy=True,
                                        relative_remap=True, check_existing=False) == {'FINISHED'},
            'IO_ERROR', 'Could not save the lighting candidate')
    output_sha = digest(request['stage'])
    require(output_sha == digest(request['stage']), 'IO_ERROR', 'Candidate SHA is unstable')
    return {
        'status': 'pass',
        'runtime': {'blender': bpy.app.version_string, 'build': bpy.app.build_hash.decode(),
                    'pid': os.getpid()},
        'observed': observed,
        'witness': {'records': len(after_records), 'inventory': after_inventory},
        'inventory': after_inventory,
        'resources': dependencies(bpy),
        'palette_events': {'scene': scene_palette, 'material': material_palette},
        'keyframes': written,
        'samples': samples,
        'output_sha256': output_sha,
    }


def worker(phase, request_path):
    job = Path(request_path).parent
    try:
        record = read_json(request_path)
        request = record.get('params', record)
        result = {'ok': True, 'data': work(phase, request, job)}
    except Exception as error:
        result = {'ok': False, 'error': error_payload(error, 'VALIDATION_FAILED')}
    atomic_json(job / (phase + '-result.json'), result)


def run(args):
    deadline = time.monotonic() + timeout_value(args.timeout)
    job = args.jobs_dir.resolve() / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    result_path = job / 'result.json'
    artifacts = {'job_directory': str(job), 'result': str(result_path)}
    data = error = None

    def checkpoint():
        if (job / 'cancel.request').exists():
            raise Failure('CANCELLED', 'Lighting author cancelled before publication')
        require(time.monotonic() < deadline, 'TIMEOUT', 'Lighting author exceeded its timeout')

    def status(state, phase):
        atomic_json(job / 'status.json', {'job_id': job.name, 'state': state, 'phase': phase,
                                          'pid': os.getpid(), 'heartbeat_at': time.time()})

    status('running', 'verify_inputs')
    try:
        with ExitStack() as guards:
            request = validate_request(args, guards)
            request['product'] = native_product(request, guards, LIGHTING_PRODUCTS)
            require(tuple(request['product']['host']) == HOST
                    and request['product']['edition'] == request['edition'],
                    'UNSUPPORTED', 'Selected product is incompatible with the requested Toon edition')
            blender = canonical(args.blender)
            atomic_json(job / 'request.json', {'schema_version': SCHEMA, 'command': COMMAND,
                                               'params': request})
            status('running', 'author')
            authored = run_worker(blender, job, 'author', deadline, checkpoint, status, entry=ENTRY, private_temp=True)
            if request['operation'] not in READ_OPERATIONS:
                expected = authored.pop('output_sha256')
                stage = Path(request['stage'])
                with FileGuard(stage) as stage_guard:
                    require(stage_guard.sha256() == expected, 'CONFLICT', 'Staged output SHA differs')
                    record = read_json(job / 'request.json')
                    record['params']['verify'] = {
                        'observed': authored['observed'],
                        'witness': authored['witness'],
                        'inventory': authored['inventory'],
                        'resources': authored['resources'],
                    }
                    atomic_json(job / 'request.json', record)
                    verified = run_worker(blender, job, 'verify', deadline, checkpoint, status,
                                          entry=ENTRY, private_temp=True)
                    require(verified['runtime']['pid'] != authored['runtime']['pid'],
                            'VALIDATION_FAILED', 'Expected an independent Blender reopen')
                with FileGuard(stage, renameable=True) as stage_guard:
                    require(stage_guard.sha256() == expected, 'CONFLICT',
                            'Verified candidate changed before publication')
                    checkpoint()
                    output = Path(args.output).resolve()
                    stage_guard.rename_new(output)
                data = {
                    'status': 'pass',
                    'operation': request['operation'],
                    'source': {'file': request['file'], 'sha256': request['expected_sha256']},
                    'source_unchanged': digest(request['file']) == request['expected_sha256'],
                    'product': request['product'],
                    'candidate': {'file': str(output), 'sha256': expected},
                    'independent_reopen': {'process': 'fresh_process', **verified['runtime']},
                    'keyframes': authored['keyframes'],
                    'samples': authored['samples'],
                    'observed': authored['observed'],
                    'verification': {'status': 'passed', 'witness_records': authored['witness']['records'],
                                     'inventory': authored['inventory']},
                    'state_source': 'SAVED_DISK_V1',
                }
                if request.get('scene_palette') or request.get('palette_clear'):
                    data['palette_events'] = authored['palette_events']
                if request['operation'] == REPAIR_NORMAL:
                    data['normal_repair'] = authored['normal_repair']
                if request['operation'] == SET_NORMAL_MIX:
                    data['normal_mix'] = authored['normal_mix']
                require(data['source_unchanged'], 'CONFLICT', 'Source SHA changed during authoring')
                artifacts.update(candidate=str(output), independent_reopen=str(job / 'verify-result.json'))
            else:
                data = {
                    'status': 'read',
                    'operation': request['operation'],
                    'source': {'file': request['file'], 'sha256': request['expected_sha256']},
                    'source_unchanged': digest(request['file']) == request['expected_sha256'],
                    'product': request['product'],
                    'observed': authored['observed'],
                    'keyframes': authored.get('keyframes', []),
                    'samples': authored.get('samples', []),
                    'state_source': 'SAVED_DISK_V1',
                }
    except (Failure, OSError, RuntimeError, ValueError) as exc:
        error = error_payload(exc, 'IO_ERROR')
    for log in sorted(job.glob('*.log')):
        artifacts[log.name] = str(log)
    result = envelope(COMMAND, job.name, data, error, artifacts)
    atomic_json(result_path, result)
    terminal = {'TIMEOUT': 'timed_out', 'CANCELLED': 'cancelled'}
    status(terminal.get(error['code'], 'failed') if error else 'succeeded', 'done')
    return result


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    args = None
    try:
        args = parser().parse_args()
        result = run(args)
    except (Failure, OSError) as exc:
        result = envelope(COMMAND, error=error_payload(exc, 'IO_ERROR'))
    if args and args.human:
        print(result['artifacts']['result'] if result['ok'] else result['error']['message'])
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['ok'] else CODES[result['error']['code']]


if __name__ == '__main__':
    if '--background' in sys.argv and '--' in sys.argv:
        worker(sys.argv[sys.argv.index('--') + 1], sys.argv[sys.argv.index('--') + 2])
    else:
        raise SystemExit(main())
