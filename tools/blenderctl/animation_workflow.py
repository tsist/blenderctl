"""Thin protected adapter for clip_event review/execute/inspect.

The public CLI never edits F-curves itself. Capture and retime both go through
the fixed add-on's review/execute transaction. ``run`` saves only
``<job>/animation-candidate.blend`` after execute succeeds, then asks a fresh
Blender process (``animation_verifier.py``) to reopen and inspect that
candidate. ``inspect`` never saves and never creates an event record.
"""
import copy
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import subprocess
import time
import uuid
from contextlib import ExitStack

from protocol import CODES, Failure, atomic_json, digest, read_json
from animation_workflow_contract import (implementation_sha256, normalize_inspect_params,
                                         normalize_params, signature)


SCANNED_COLLECTIONS = ('images', 'fonts', 'libraries', 'volumes', 'sounds',
                       'movieclips', 'cache_files')
RECORD_PROPERTY = 'acw_clip_event_state'
RECORD_SCHEMA = 'event-panel-state-v2'
RECORD_POLICY = 'clip-window-v1'
RECORD_MAX_BYTES = 8 * 1024 * 1024
EPSILON = 1e-6


def descriptor(path):
    path = Path(path).resolve()
    return {'file': str(path), 'sha256': digest(path),
            'bytes': path.stat().st_size}


def _fixed_module(name):
    addon = Path(__file__).resolve().parent.parent / 'animation_curves_workflow_addon'
    if not addon.is_dir() or not (addon / (name + '.py')).is_file():
        raise Failure('NOT_FOUND',
                      'Fixed animation add-on ' + name + '.py is missing: ' + str(addon))
    try:
        return importlib.import_module('animation_curves_workflow_addon.' + name)
    except Exception as exc:
        raise Failure('NOT_FOUND',
                      'Fixed animation add-on ' + name + ' import failed: '
                      + str(exc)) from exc


def _clip_event():
    return _fixed_module('clip_event')


def _call(fn, *args, default_code='VALIDATION_FAILED', **kwargs):
    try:
        return fn(*args, **kwargs)
    except Failure:
        raise
    except Exception as exc:
        code = getattr(exc, 'code', None)
        raise Failure(code if code in CODES else default_code, str(exc)) from exc


def _plain(value, label):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise Failure('VALIDATION_FAILED',
                      label + ' is not JSON-safe: ' + str(exc)) from exc


def _dict(value, label):
    value = _plain(value, label)
    if not isinstance(value, dict):
        raise Failure('VALIDATION_FAILED', label + ' must be an object')
    return value


def _runtime_descriptor():
    import bpy
    return {'version': bpy.app.version_string,
            'build': bpy.app.build_hash.decode(),
            'binary_sha256': digest(bpy.app.binary_path)}


def _check_identity(params):
    if implementation_sha256() != params['implementation_sha256']:
        raise Failure('CONFLICT',
                      'Animation implementation changed after request normalization')


def _guard_inputs(params, stack):
    """Hold deny-write handles on the source and every explicit resource."""
    from filesystem import FileGuard
    source = Path(params['file'])
    if not source.is_file():
        raise Failure('NOT_FOUND', 'Animation source not found: ' + str(source))
    source_guard = stack.enter_context(FileGuard(source))
    if source_guard.sha256() != params['expected_sha256']:
        raise Failure('CONFLICT', 'Animation source SHA mismatch')
    for row in params['resources']:
        path = Path(row['file'])
        if not path.is_file():
            raise Failure('NOT_FOUND', 'Animation resource not found: ' + str(path))
        guard = stack.enter_context(FileGuard(path))
        if guard.sha256() != row['expected_sha256']:
            raise Failure('CONFLICT', 'Animation resource SHA mismatch: ' + str(path))
    return source_guard


def _open_source(params):
    import bpy
    bpy.ops.wm.open_mainfile(filepath=params['file'], load_ui=False,
                             use_scripts=False)
    if tuple(bpy.data.version[:2]) != tuple(bpy.app.version[:2]):
        raise Failure('UNSUPPORTED',
                      'Animation adapter requires matching Blender major/minor')


def _validate_context(manifest):
    import bpy
    context = manifest['context']
    scene = bpy.data.scenes.get(context['scene'])
    if scene is None:
        raise Failure('NOT_FOUND', 'Animation scene not found: ' + context['scene'])
    if scene.view_layers.get(context['view_layer']) is None:
        raise Failure('NOT_FOUND',
                      'Animation view layer not found: ' + context['view_layer'])
    bpy.context.window.scene = scene
    bpy.context.window.view_layer = scene.view_layers[context['view_layer']]


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(',', ':'))


def _canonical_sha(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _workflow_call(fn, *args, default_code='CONFLICT', **kwargs):
    try:
        return fn(*args, **kwargs)
    except Failure:
        raise
    except Exception as exc:
        code = getattr(exc, 'code', None)
        raise Failure(code if code in CODES else default_code, str(exc)) from exc


def _ui_state():
    return _fixed_module('event_ui_state')


def _read_scene_record(scene, required=False):
    """Read only the request Scene's acw_clip_event_state JSON string."""
    if RECORD_PROPERTY not in scene:
        if required:
            raise Failure('NOT_FOUND',
                          'No clip event record exists in the requested scene')
        return None
    raw = scene.get(RECORD_PROPERTY)
    if not isinstance(raw, str) or not raw:
        raise Failure('CONFLICT',
                      'acw_clip_event_state must be a nonempty JSON string')
    if len(raw.encode('utf-8')) > RECORD_MAX_BYTES:
        raise Failure('UNSUPPORTED', 'Clip event record exceeds its byte budget')

    def pairs(rows):
        out = {}
        for key, value in rows:
            if key in out:
                raise Failure('CONFLICT',
                              'Clip event record has duplicate JSON fields: ' + key)
            out[key] = value
        return out

    try:
        record = json.loads(raw, object_pairs_hook=pairs)
    except Failure:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise Failure('CONFLICT',
                      'Clip event record is not valid JSON') from exc
    if not isinstance(record, dict):
        raise Failure('CONFLICT', 'Clip event record must be a JSON object')
    return record


def _record_event(record):
    events = record.get('events')
    if not isinstance(events, dict) or len(events) != 1:
        raise Failure('CONFLICT', 'Clip record must contain exactly one event')
    event_id, event = next(iter(events.items()))
    if not isinstance(event, dict) or event.get('id') != event_id:
        raise Failure('CONFLICT', 'Clip event id differs from its map key')
    return event_id, event


def _same_names(record_value, request_value, label, *, allow_empty=False):
    if not isinstance(record_value, list) or (not record_value and not allow_empty):
        raise Failure('CONFLICT', 'Clip record ' + label + ' must be a nonempty list')
    if len(record_value) != len(set(record_value)):
        raise Failure('CONFLICT', 'Clip record ' + label + ' contains duplicates')
    if sorted(record_value) != sorted(request_value):
        raise Failure('CONFLICT',
                      'Clip record ' + label + ' differs from the current request')


def _validate_record_request(record, manifest):
    if record.get('schema') != RECORD_SCHEMA or record.get('policy') != RECORD_POLICY:
        raise Failure('CONFLICT',
                      'Clip record schema/policy must be '
                      + RECORD_SCHEMA + '/' + RECORD_POLICY)
    _workflow_call(_ui_state().check_clip_state, record)
    if record.get('context') != manifest['context']:
        raise Failure('CONFLICT',
                      'Clip record context differs from the current request')
    participants = record.get('participants')
    expected_fields = set(manifest['participants'])
    if (not isinstance(participants, dict)
            or expected_fields not in ({'rigs', 'roots', 'lights', 'materials'},
                                       {'rigs', 'roots', 'lights', 'materials', 'objects'})
            or set(participants) != expected_fields):
        raise Failure('CONFLICT', 'Clip record participants are malformed')
    object_mode = 'objects' in participants
    for field in sorted(expected_fields):
        _same_names(participants[field], manifest['participants'][field],
                    'participants.' + field,
                    allow_empty=object_mode and field != 'objects')
    locks = record.get('locks')
    if not isinstance(locks, list):
        raise Failure('CONFLICT', 'Clip record locks must be a list')
    try:
        record_locks = sorted(float(frame) for frame in locks)
        request_locks = sorted(float(frame) for frame in manifest['locks'])
    except (TypeError, ValueError, OverflowError) as exc:
        raise Failure('CONFLICT', 'Clip record locks are not finite') from exc
    if record_locks != request_locks:
        raise Failure('CONFLICT',
                      'Clip record locks differ from the current request')
    event_id, event = _record_event(record)
    if event_id != manifest['event']['id']:
        raise Failure('CONFLICT',
                      'Clip record event id differs from the current request')
    for recorded_field, request_field in (('frame', 'start'), ('end', 'end')):
        if abs(float(event[recorded_field])
               - float(manifest['event'][request_field])) > EPSILON:
            raise Failure('CONFLICT',
                          'Clip record event ' + request_field
                          + ' differs from the current request')
    return event


def _participant_resolution(manifest, scene, layer):
    import bpy
    participants = manifest['participants']
    rigs = set(participants['rigs'])
    roots = set(participants['roots'])
    objects = set(participants.get('objects', []))
    for name in objects:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type not in {'MESH', 'EMPTY'}:
            raise Failure('NOT_FOUND', 'Requested native object does not exist: ' + name)
        if scene.objects.get(name) != obj or layer.objects.get(name) != obj:
            raise Failure('CONFLICT', 'Requested object is outside the scene/view layer: ' + name)
        if obj.library is not None or obj.override_library is not None:
            raise Failure('UNSUPPORTED', 'Linked/overridden object clip is unsupported: ' + name)
    light_data = {}
    for name in participants['lights']:
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != 'LIGHT':
            raise Failure('NOT_FOUND', 'Requested light does not exist: ' + name)
        if scene.objects.get(name) != obj or layer.objects.get(name) != obj:
            raise Failure('CONFLICT',
                          'Requested light is outside the requested scene/view layer: '
                          + name)
        if obj.data is None:
            raise Failure('CONFLICT', 'Requested light has no data: ' + name)
        light_data[obj.data.name] = name
    materials = {}
    for name in participants['materials']:
        material = bpy.data.materials.get(name)
        if material is None:
            raise Failure('NOT_FOUND', 'Requested material does not exist: ' + name)
        if not material.use_nodes or material.node_tree is None:
            raise Failure('CONFLICT',
                          'Requested material has no node tree: ' + name)
        materials[name] = material
    return {'rigs': rigs, 'roots': roots, 'objects': objects, 'light_data': light_data,
            'materials': materials}


def _resolve_target_owner(target, scene, layer):
    import bpy
    owner = target.get('owner')
    if not isinstance(owner, dict) or set(owner) != {'type', 'name'}:
        raise Failure('CONFLICT', 'Clip target owner must contain type and name')
    kind, name = owner['type'], owner['name']
    if not isinstance(name, str) or not name:
        raise Failure('CONFLICT', 'Clip target owner name is empty')
    if kind == 'OBJECT':
        item = bpy.data.objects.get(name)
        if item is None:
            raise Failure('NOT_FOUND', 'Clip target object does not exist: ' + name)
        if scene.objects.get(name) != item or layer.objects.get(name) != item:
            raise Failure('CONFLICT',
                          'Clip target object is outside the requested scene/view layer: '
                          + name)
        if item.library is not None:
            raise Failure('UNSUPPORTED', 'Linked clip target is unsupported: ' + name)
        return item
    if kind == 'LIGHT':
        item = bpy.data.lights.get(name)
        if item is None:
            raise Failure('NOT_FOUND', 'Clip target light data does not exist: ' + name)
        if item.library is not None:
            raise Failure('UNSUPPORTED', 'Linked clip light data is unsupported: ' + name)
        return item
    if kind == 'MATERIAL_NODETREE':
        material = bpy.data.materials.get(name)
        if material is None or not material.use_nodes or material.node_tree is None:
            raise Failure('NOT_FOUND',
                          'Clip target material node tree does not exist: ' + name)
        if material.library is not None or material.node_tree.library is not None:
            raise Failure('UNSUPPORTED',
                          'Linked clip material node tree is unsupported: ' + name)
        return material.node_tree
    raise Failure('UNSUPPORTED', 'Unsupported clip target owner type: ' + str(kind))


def _target_domain_membership(target, resolved):
    domain = target.get('domain')
    owner = target.get('owner') or {}
    kind, name = owner.get('type'), owner.get('name')
    if domain == 'body' and kind == 'OBJECT' and name in resolved['rigs']:
        return
    if domain == 'root' and kind == 'OBJECT' and name in resolved['roots']:
        return
    if domain == 'object' and kind == 'OBJECT' and name in resolved['objects']:
        if (target.get('data_path') not in {'location', 'rotation_euler', 'scale'}
                or type(target.get('array_index')) is not int
                or target['array_index'] not in (0, 1, 2)):
            raise Failure('CONFLICT', 'Native object clip target is outside XYZ TRS')
        return
    if domain == 'light' and kind == 'LIGHT' and name in resolved['light_data']:
        return
    if (domain == 'material' and kind == 'MATERIAL_NODETREE'
            and name in resolved['materials']):
        return
    raise Failure('CONFLICT',
                  'Clip target domain/owner is outside explicit participants',
                  )


def _active_curve(owner, target):
    action_data = owner.animation_data
    action = action_data.action if action_data is not None else None
    slot = action_data.action_slot if action_data is not None else None
    if action is None or slot is None:
        raise Failure('CONFLICT',
                      'Clip target has no active Action/slot: ' + target['id'])
    if action.library is not None:
        raise Failure('UNSUPPORTED',
                      'Linked clip Action is unsupported: ' + action.name)
    if (action.name != target['action_name']
            or slot.identifier != target['slot_identifier']
            or slot.target_id_type != target['slot_type']):
        raise Failure('CONFLICT',
                      'Clip target active Action/slot differs from its record',
                      )
    layers = list(action.layers)
    if not layers:
        fcurves = action.fcurves
        curve = fcurves.find(target['data_path'],
                             index=int(target['array_index']))
    else:
        bags = []
        for layer in layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    if bag.slot_handle == slot.handle:
                        bags.append(bag)
        if len(bags) != 1:
            raise Failure('CONFLICT',
                          'Clip target must resolve to exactly one active channelbag: '
                          + target['id'])
        curve = bags[0].fcurves.find(target['data_path'],
                                     index=int(target['array_index']))
    if curve is None:
        raise Failure('CONFLICT',
                      'Clip target channel is missing from the active Action: '
                      + target['id'])
    return curve


def _key_semantic(key):
    raw = {
        'co': [float(key.co[0]), float(key.co[1])],
        'handle_left': [float(key.handle_left[0]), float(key.handle_left[1])],
        'handle_right': [float(key.handle_right[0]), float(key.handle_right[1])],
        'handle_left_type': key.handle_left_type,
        'handle_right_type': key.handle_right_type,
        'interpolation': key.interpolation,
        'easing': key.easing,
        'back': float(key.back),
        'amplitude': float(key.amplitude),
        'period': float(key.period),
        'type': key.type,
    }
    return _workflow_call(_ui_state().semantic_key, raw,
                          default_code='INVALID_REQUEST')


def clip_record_evidence(manifest, expected_contact, required=True):
    """Validate the exact v2 clip record in the request Scene.

    The only contact source is event.contact. ``expected_contact`` is an
    explicit actual value chosen by the caller (capture.contact, or
    retime.target_contact after commit); request aliases are never read.
    """
    import bpy
    expected_contact = float(expected_contact)
    scene = bpy.data.scenes.get(manifest['context']['scene'])
    if scene is None:
        raise Failure('NOT_FOUND',
                      'Animation scene not found: '
                      + manifest['context']['scene'])
    layer = scene.view_layers.get(manifest['context']['view_layer'])
    if layer is None:
        raise Failure('NOT_FOUND',
                      'Animation view layer not found: '
                      + manifest['context']['view_layer'])
    record = _read_scene_record(scene, required=required)
    if record is None:
        return {'schema': 'animation-clip-record-evidence-v1',
                'status': 'absent', 'record_property': RECORD_PROPERTY,
                'scene': scene.name, 'view_layer': layer.name}
    event = _validate_record_request(record, manifest)
    actual_contact = float(event['contact'])
    if abs(actual_contact - float(expected_contact)) > EPSILON:
        raise Failure('CONFLICT',
                      'Clip record event.contact differs from the required actual contact',
                      )
    resolved = _participant_resolution(manifest, scene, layer)
    targets = record.get('targets')
    bindings = record.get('bindings')
    if not isinstance(targets, dict) or not targets:
        raise Failure('CONFLICT', 'Clip record targets must be a nonempty object')
    if not isinstance(bindings, list) or not bindings:
        raise Failure('CONFLICT', 'Clip record bindings must be a nonempty list')
    if len(targets) > 256 or len(bindings) > 4096:
        raise Failure('UNSUPPORTED', 'Clip record exceeds channel/binding budget')
    by_target = {ident: [] for ident in targets}
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != {
                'event', 'target', 'offset', 'expected_key'}:
            raise Failure('CONFLICT', 'Clip binding fields are malformed')
        ident = binding['target']
        if ident not in by_target:
            raise Failure('CONFLICT', 'Clip binding references an unknown target')
        by_target[ident].append(binding)
    frame = float(event['frame'])
    end = float(event['end'])
    contact_offset = actual_contact - frame
    evidence_targets = []
    binding_rows = {}
    total_window_keys = 0
    actual_contacts = []
    for ident, target in targets.items():
        if not isinstance(target, dict) or target.get('id') != ident:
            raise Failure('CONFLICT', 'Clip target id differs from its map key')
        for field in ('owner', 'data_path', 'array_index', 'sharing', 'domain',
                      'action_name', 'slot_identifier', 'slot_type'):
            if field not in target:
                raise Failure('CONFLICT',
                              'Clip target is missing field: ' + field)
        if not isinstance(target['data_path'], str) or not target['data_path']:
            raise Failure('CONFLICT', 'Clip target data_path is empty')
        if (isinstance(target['array_index'], bool)
                or not isinstance(target['array_index'], int)
                or target['array_index'] < 0):
            raise Failure('CONFLICT', 'Clip target array_index must be a nonnegative int')
        if target['sharing'] != 'reject':
            raise Failure('CONFLICT', 'Clip target sharing must be reject')
        for field in ('action_name', 'slot_identifier', 'slot_type'):
            if not isinstance(target[field], str) or not target[field]:
                raise Failure('CONFLICT',
                              'Clip target ' + field + ' must be a nonempty string')
        spec = {'owner': target['owner'], 'data_path': target['data_path'],
                'array_index': target['array_index']}
        if _workflow_call(_ui_state().target_id, spec) != ident:
            raise Failure('CONFLICT', 'Clip target id does not match native address')
        _target_domain_membership(target, resolved)
        owner = _resolve_target_owner(target, scene, layer)
        curve = _active_curve(owner, target)
        expected = by_target[ident]
        expected_by_offset = {}
        for binding in expected:
            offset = float(binding['offset'])
            if not math.isfinite(offset):
                raise Failure('CONFLICT', 'Clip binding offset is not finite')
            if round(offset, 6) in expected_by_offset:
                raise Failure('CONFLICT', 'Duplicate clip binding offset')
            key = _workflow_call(_ui_state().semantic_key,
                                 binding['expected_key'],
                                 default_code='INVALID_REQUEST')
            if abs(float(key['co'][0]) - (frame + offset)) > EPSILON:
                raise Failure('CONFLICT',
                              'Clip binding expected key time differs from event.frame+offset')
            expected_by_offset[round(offset, 6)] = key
        if round(contact_offset, 6) not in expected_by_offset:
            raise Failure('CONFLICT',
                          'Every clip target requires a record binding at contact')
        actual_by_offset = {}
        for key in curve.keyframe_points:
            key_time = float(key.co[0])
            if frame - EPSILON <= key_time <= end + EPSILON:
                allowed = ({'BEZIER', 'LINEAR'}
                           if target['domain'] == 'object' else {'BEZIER'})
                if key.interpolation not in allowed:
                    if target['domain'] != 'object':
                        raise Failure('UNSUPPORTED',
                                      'Clip event window keys must remain native BEZIER')
                    raise Failure('UNSUPPORTED',
                                  'Clip event window interpolation is unsupported for its domain')
                offset = round(key_time - frame, 6)
                if offset in actual_by_offset:
                    raise Failure('CONFLICT',
                                  'Clip target has duplicate key time in event window')
                actual_by_offset[offset] = _key_semantic(key)
        if set(actual_by_offset) != set(expected_by_offset):
            raise Failure('CONFLICT',
                          'Clip actual event-window key set differs from record',
                          )
        for offset, expected_key in expected_by_offset.items():
            if actual_by_offset.get(offset) != expected_key:
                raise Failure('CONFLICT',
                              'Clip actual semantic key differs from binding',
                              )
        if round(contact_offset, 6) not in actual_by_offset:
            raise Failure('CONFLICT',
                          'Clip target actual contact key is missing')
        actual_contacts.append(actual_contact)
        rows = []
        for offset in sorted(expected_by_offset):
            rows.append({'offset': offset,
                         # Offset is a semantic bucket; time is observed RNA.
                         'time': float(actual_by_offset[offset]['co'][0]),
                         'key': actual_by_offset[offset]})
        binding_rows[ident] = rows
        total_window_keys += len(rows)
        evidence_targets.append({
            'id': ident, 'domain': target['domain'],
            'owner': copy.deepcopy(target['owner']),
            'data_path': target['data_path'],
            'array_index': target['array_index'],
            'action_name': target['action_name'],
            'slot_identifier': target['slot_identifier'],
            'slot_type': target['slot_type'],
            'window_key_count': len(rows),
            'contact_key': True,
        })
    for group, names in (('rigs', resolved['rigs']), ('roots', resolved['roots']),
                         ('objects', resolved['objects']),
                         ('lights', set(resolved['light_data'])),
                         ('materials', set(resolved['materials']))):
        covered = {row['owner']['name'] for row in evidence_targets
                   if (group == 'rigs' and row['domain'] == 'body')
                   or (group == 'roots' and row['domain'] == 'root')
                   or (group == 'objects' and row['domain'] == 'object')
                   or (group == 'lights' and row['domain'] == 'light')
                   or (group == 'materials' and row['domain'] == 'material')}
        if names - covered:
            raise Failure('CONFLICT',
                          'Clip record lacks targets for participants.' + group)
    return {
        'schema': 'animation-clip-record-evidence-v1',
        'status': 'verified',
        'record_property': RECORD_PROPERTY,
        'scene': scene.name,
        'view_layer': layer.name,
        'record_schema': record['schema'],
        'record_policy': record['policy'],
        'record_id': record['id'],
        'revision': record['revision'],
        'context': copy.deepcopy(record['context']),
        'participants': copy.deepcopy(record['participants']),
        'locks': copy.deepcopy(record['locks']),
        'event': {'id': event['id'], 'frame': event['frame'],
                  'contact': event['contact'], 'end': event['end'],
                  'locked': event['locked']},
        'expected_contact': expected_contact,
        'actual_contact': actual_contact,
        'actual_contacts': sorted(set(actual_contacts)),
        'record_sha256': _canonical_sha(record),
        'target_count': len(evidence_targets),
        'binding_count': total_window_keys,
        'window_key_count': total_window_keys,
        'targets': evidence_targets,
        'bindings_by_target': binding_rows,
        'source': 'request-scene ID property only; no request-history scan',
    }


def _inspect_expected_contact(manifest, record):
    event = _validate_record_request(record, manifest)
    actual = float(event['contact'])
    allowed = {float(manifest['event']['contact'])}
    if manifest['action'] == 'retime':
        allowed.add(float(manifest['event']['target_contact']))
    if not any(abs(actual - value) <= EPSILON for value in allowed):
        raise Failure('CONFLICT',
                      'Clip record event.contact is unrelated to this request',
                      )
    return actual


def _property_value(value, depth=0):
    if depth > 8:
        return repr(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if hasattr(value, 'to_dict'):
        return _property_value(value.to_dict(), depth + 1)
    if hasattr(value, 'to_list'):
        return _property_value(value.to_list(), depth + 1)
    if isinstance(value, (list, tuple)):
        return [_property_value(item, depth + 1) for item in list(value)[:4096]]
    if isinstance(value, dict):
        return {str(key): _property_value(child, depth + 1)
                for key, child in sorted(value.items(), key=lambda item: str(item[0]))}
    return repr(value)


def _action_rows():
    import bpy
    from animation_curves_workflow_addon.pose_channel import action_witness
    rows = []
    for action in sorted(bpy.data.actions, key=lambda item: item.name):
        witness = action_witness(action)
        count = sum(len(curve['keys']) for layer in witness['layers']
                    for strip in layer['strips']
                    for _slot, bag in strip['bags'] for curve in bag)
        rows.append({'name': action.name, 'users': action.users,
                     'fake_user': bool(action.use_fake_user),
                     'frame_range': [float(value) for value in action.frame_range],
                     'keyframes': count, 'witness': witness})
    return rows


def _scene_rows():
    import bpy
    rows = []
    for scene in sorted(bpy.data.scenes, key=lambda item: item.name):
        properties = {str(key): _property_value(scene[key]) for key in scene.keys()}
        rows.append({'name': scene.name, 'properties': properties})
    return rows


def _object_action_rows():
    import bpy
    rows = []
    owners = []
    for group in ('objects', 'lights', 'cameras', 'materials', 'node_groups'):
        owners.extend((group, obj) for obj in getattr(bpy.data, group))
    owners.extend(('material_node_tree', mat.node_tree) for mat in bpy.data.materials
                  if mat.node_tree is not None)
    owners.extend(('light_node_tree', light.node_tree) for light in bpy.data.lights
                  if getattr(light, 'node_tree', None) is not None)
    for group, obj in sorted(owners, key=lambda row: (row[0], row[1].name)):
        animation = obj.animation_data
        action = animation.action if animation is not None else None
        slot = animation.action_slot if animation is not None else None
        rows.append({'kind': group, 'name': obj.name,
                     'action': action.name if action is not None else None,
                     'slot': slot.identifier if slot else None,
                     'properties': {str(key): _property_value(obj[key])
                                    for key in obj.keys()}})
    return rows


def state_fingerprint():
    """Detect review/inspect writes to actions, bindings or scene records."""
    import bpy
    actions = _action_rows()
    body = {'actions': actions, 'scenes': _scene_rows(),
            'objects': _object_action_rows(),
            'blender_version': bpy.app.version_string}
    encoded = json.dumps(body, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, allow_nan=False).encode('utf-8')
    return {'sha256': hashlib.sha256(encoded).hexdigest(),
            'actions': len(actions),
            'keyframes': sum(action['keyframes'] for action in actions),
            'scenes': len(body['scenes']),
            'objects': len(body['objects'])}


def animation_fingerprint():
    """Native animation-only witness used for capture and cold reopen."""
    import bpy
    all_rows = _action_rows()
    # Zero-user, non-fake-user Actions are not guaranteed to survive Blender's
    # save/reopen purge. They are not part of the persisted animation state.
    rows = [row for row in all_rows
            if row['users'] > 0 or row['fake_user']]
    actions = [{'name': row['name'], 'witness': row['witness']}
               for row in rows]
    bindings = [{key: row[key] for key in ('kind', 'name', 'action', 'slot')}
                for row in _object_action_rows()]
    body = {'actions': actions, 'bindings': bindings,
            'blender_version': bpy.app.version_string}
    encoded = json.dumps(body, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False, allow_nan=False).encode('utf-8')
    return {'sha256': hashlib.sha256(encoded).hexdigest(),
            'actions': len(actions),
            'keyframes': sum(row['keyframes'] for row in rows),
            'bindings': len(bindings)}


def _static_sound_members(scene):
    """Preserve plain static native SOUND strips, never interpret general VSE."""
    editor = getattr(scene, 'sequence_editor', None)
    if editor is None:
        return []
    if not hasattr(editor, 'strips_all') and not hasattr(editor, 'sequences_all'):
        raise Failure('UNSUPPORTED', 'Native sequence collection is unavailable')
    # Blender 5.x renamed sequences_all. Missing the new collection must not
    # silently turn an existing media timeline into an empty dependency set.
    strips = list(getattr(editor, 'strips_all',
                          getattr(editor, 'sequences_all', ())))
    if not strips:
        return []
    animation = scene.animation_data
    if animation is not None and (animation.action is not None
                                  or len(animation.drivers)
                                  or len(animation.nla_tracks)):
        raise Failure('UNSUPPORTED',
                      'Static animation audio members do not support Scene animation: '
                      + scene.name)
    members = []
    for strip in strips:
        if strip.type != 'SOUND' or getattr(strip, 'sound', None) is None:
            raise Failure('UNSUPPORTED',
                          'Only plain static SOUND strips are supported by animation: '
                          + scene.name + '/' + strip.name)
        if getattr(strip, 'animation_data', None) is not None:
            raise Failure('UNSUPPORTED', 'Animated SOUND strips need a separate audio workflow')
        if (len(getattr(strip, 'modifiers', ()))
                or len(getattr(strip, 'retiming_keys', ()))):
            raise Failure('UNSUPPORTED',
                          'Animation SOUND members must have no modifiers or retiming keys: '
                          + strip.name)
        if (getattr(strip.sound, 'library', None) is not None
                or bool(getattr(strip.sound, 'packed_file', None))
                or bool(getattr(strip.sound, 'packed_files', ()))):
            raise Failure('UNSUPPORTED',
                          'Animation SOUND members must use local unpacked declared audio')
        raw = getattr(strip.sound, 'filepath', '') or ''
        if (not raw or raw == '<builtin>' or raw.startswith('//')
                or not Path(raw).is_absolute()):
            raise Failure('UNSUPPORTED',
                          'Animation SOUND members require a nonempty absolute declared audio path')
        sound_animation = getattr(strip.sound, 'animation_data', None)
        if sound_animation is not None:
            raise Failure('UNSUPPORTED',
                          'Animated Sound datablocks require a separate audio workflow')
        fields = ('name', 'type', 'channel', 'mute', 'lock', 'frame_start',
                  'frame_duration', 'frame_offset_start', 'frame_offset_end',
                  'frame_final_start', 'frame_final_end', 'volume', 'pan',
                  'sound_offset', 'pitch_correction',
                  'animation_offset_start', 'animation_offset_end')
        properties = {name: getattr(strip, name) for name in fields}
        members.append({'scene': scene.name, 'sound': strip.sound.name,
                        'sound_filepath': strip.sound.filepath,
                        'properties': properties})
    return sorted(members, key=lambda row: (row['scene'],
                                           row['properties']['channel'],
                                           row['properties']['name']))


def _resource_closure(params):
    """Scan actual saved-file dependencies; empty resources is not a shortcut."""
    import bpy
    if len(bpy.data.libraries):
        raise Failure('UNSUPPORTED',
                      'Linked-library transitive resources need a separate closure receipt')
    declared = {}
    for row in params['resources']:
        key = os.path.normcase(str(Path(row['file']).resolve()))
        declared[key] = row
    for row in declared.values():
        if not Path(row['file']).is_file():
            raise Failure('NOT_FOUND', 'Animation resource missing: ' + row['file'])
        if digest(row['file']) != row['expected_sha256']:
            raise Failure('CONFLICT', 'Animation resource SHA mismatch: ' + row['file'])
    external = []
    seen = set()
    counts = {}
    skipped = {'no_external_path': 0, 'packed': 0, 'builtin': 0}

    def check(path, kind, owner, raw):
        path = Path(path).resolve()
        key = os.path.normcase(str(path))
        if key in seen:
            return
        seen.add(key)
        if not path.is_file():
            raise Failure('CONFLICT',
                          'Discovered animation dependency is missing: ' + str(path))
        if key not in declared:
            raise Failure('CONFLICT',
                          'Undeclared animation resource: ' + str(path))
        if digest(path) != declared[key]['expected_sha256']:
            raise Failure('CONFLICT',
                          'Discovered animation resource SHA mismatch: ' + str(path))
        external.append({'kind': kind, 'owner': owner,
                         'file': str(path), 'sha256': declared[key]['expected_sha256'],
                         'stored_path_before': raw})

    for collection in SCANNED_COLLECTIONS:
        items = getattr(bpy.data, collection, ())
        counts[collection] = len(items)
        for item in items:
            raw = getattr(item, 'filepath', '') or ''
            if not raw:
                skipped['no_external_path'] += 1
                continue
            if raw == '<builtin>':
                skipped['builtin'] += 1
                continue
            packed = (bool(getattr(item, 'packed_file', None))
                      or bool(getattr(item, 'packed_files', []))
                      or bool(getattr(item, 'is_linked_packed', False)))
            source = getattr(item, 'source', '')
            if packed:
                skipped['packed'] += 1
                continue
            # pathlib treats //... as an absolute UNC path on Windows, while
            # Blender always interprets it relative to the opened .blend.
            if raw.startswith('//') or not Path(raw).is_absolute():
                raise Failure('UNSUPPORTED',
                              'This animation slice requires absolute external resource paths: '
                              + item.name)
            if source in ('TILED', 'SEQUENCE', 'MOVIE') or any(
                    token in raw for token in ('<UDIM>', '<UVTILE>', '####', '%04d')):
                raise Failure('UNSUPPORTED',
                              'Dynamic animation resource needs an explicit member workflow: '
                              + item.name)
            resolved = bpy.path.abspath(raw, library=getattr(item, 'library', None))
            if collection == 'cache_files' and Path(resolved).is_dir():
                raise Failure('UNSUPPORTED',
                              'Directory cache resources need an explicit receipt: '
                              + item.name)
            check(resolved, collection, item.name, raw)

    # A native static Sound strip references the already-checked Sound ID.
    # Its persistent scalar witness must also survive save/cold reopen.
    # General VSE, modifiers, retiming and animated audio remain unsupported.
    sound_members = []
    for scene in bpy.data.scenes:
        sound_members.extend(_static_sound_members(scene))

    # Geometry-node string filepaths are absolute static inputs or refused.
    for tree in bpy.data.node_groups:
        for node in tree.nodes:
            for socket in getattr(node, 'inputs', ()):
                if getattr(socket, 'bl_idname', '') != 'NodeSocketStringFilePath':
                    continue
                raw = getattr(socket, 'default_value', '') or ''
                if not raw:
                    continue
                owner = tree.name + '/' + node.name + '/' + socket.identifier
                if socket.is_linked or raw.startswith('//') or not Path(raw).is_absolute() or any(
                        token in raw for token in ('<UDIM>', '<UVTILE>', '####')):
                    raise Failure('UNSUPPORTED',
                                  'Node filepath needs an explicit static path: ' + owner)
                check(raw, 'node_filepath', owner, raw)

    unreferenced = [row for key, row in declared.items() if key not in seen]
    if unreferenced:
        for row in unreferenced:
            external.append({'kind': 'declared_unreferenced',
                             'owner': None, 'file': row['file'],
                             'sha256': row['expected_sha256'],
                             'stored_path_before': None})
    return {'status': 'closed',
            'policy': 'actual static datablock/node filepaths scanned and hashed; '
                      'empty resources is not treated as proof',
            'scanned_collections': list(SCANNED_COLLECTIONS),
            'counts': counts, 'skipped': skipped,
            'declared_count': len(declared),
            'external_count': len([row for row in external
                                   if row['kind'] != 'declared_unreferenced']),
            'declared_unreferenced': [row['file'] for row in unreferenced],
            'external': external,
            'static_sound_members': sound_members,
            'limitations': [
                'Linked-library transitive contents, UDIM/sequences/movies and '
                'directory caches require separate member receipts.',
                'Cache files inside an opened blend are rejected unless this '
                'first slice adds an explicit cache workflow.',
            ]}


def _checkpoint_factory(job, started):
    launch = read_json(Path(job) / 'launch.json')
    timeout = float(launch.get('timeout_seconds', 120))

    def checkpoint():
        if (Path(job) / 'cancel.request').exists():
            raise Failure('CANCELLED', 'Animation job cancelled')
        if time.monotonic() - started > timeout:
            raise Failure('TIMEOUT', 'Animation job exceeded timeout')
    return checkpoint


def _launch_verifier(config, job):
    import bpy
    job = Path(job).resolve()
    request_path = job / 'animation-verification-request.json'
    atomic_json(request_path, config)
    verifier = Path(__file__).with_name('animation_verifier.py')
    if not verifier.is_file():
        raise Failure('NOT_FOUND',
                      'animation_verifier.py is required for independent reopen')
    args = [bpy.app.binary_path, '--background', '--factory-startup',
            '--disable-autoexec', '--offline-mode', '--python-exit-code', '1',
            '--python', str(verifier), '--', str(request_path)]
    try:
        with (job / 'animation-reopen.log').open('wb') as log:
            process = subprocess.run(
                args, stdout=log, stderr=subprocess.STDOUT, timeout=120,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0))
    except subprocess.TimeoutExpired as exc:
        raise Failure('TIMEOUT', 'Independent animation reopen timed out') from exc
    output = Path(config['output'])
    result = read_json(output) if output.is_file() else {
        'status': 'failed', 'error': 'No independent reader result'}
    if process.returncode or result.get('status') != 'passed':
        raise Failure('VALIDATION_FAILED',
                      'Independent animation reopen: ' + str(result.get('error')))
    return result


def retime_mapping_evidence(before, after, review):
    warp = review.get('warp')
    rows = warp.get('rows') if isinstance(warp, dict) else None
    if not isinstance(rows, list) or not rows:
        raise Failure('VALIDATION_FAILED',
                      'Retime review lacks an explicit time-mapping witness')
    mapping = {}
    for row in rows:
        if not isinstance(row, dict) or 'source' not in row or 'native' not in row:
            raise Failure('VALIDATION_FAILED', 'Retime warp row is malformed')
        source = round(float(row['source']), 9)
        native = float(row['native'])
        if not math.isfinite(source) or not math.isfinite(native):
            raise Failure('VALIDATION_FAILED', 'Retime warp row is non-finite')
        if source in mapping and abs(mapping[source] - native) > EPSILON:
            raise Failure('VALIDATION_FAILED',
                          'Retime warp maps one source frame twice')
        mapping[source] = native
    before_event = before['event']
    after_event = after['event']
    if abs(float(before_event['frame']) - float(after_event['frame'])) > EPSILON:
        raise Failure('VALIDATION_FAILED',
                      'Retime changed the event window start')
    if abs(float(before_event['end']) - float(after_event['end'])) > EPSILON:
        raise Failure('VALIDATION_FAILED',
                      'Retime changed the event window end')
    if abs(float(warp.get('start', before_event['frame']))
           - float(before_event['frame'])) > EPSILON:
        raise Failure('VALIDATION_FAILED', 'Retime warp start differs from the event')
    if abs(float(warp.get('contact', before_event['contact']))
           - float(before_event['contact'])) > EPSILON:
        raise Failure('VALIDATION_FAILED', 'Retime warp contact differs from the record')
    if abs(float(warp.get('target_contact', after_event['contact']))
           - float(after_event['contact'])) > EPSILON:
        raise Failure('VALIDATION_FAILED',
                      'Retime warp target_contact differs from the committed record')
    if abs(float(warp.get('end', before_event['end']))
           - float(after_event['end'])) > EPSILON:
        raise Failure('VALIDATION_FAILED', 'Retime warp end differs from the event')
    before_rows = before.get('bindings_by_target')
    after_rows = after.get('bindings_by_target')
    if not isinstance(before_rows, dict) or not isinstance(after_rows, dict):
        raise Failure('VALIDATION_FAILED', 'Retime evidence lacks binding maps')
    if set(before_rows) != set(after_rows):
        raise Failure('VALIDATION_FAILED',
                      'Retime target binding inventory changed')
    stable_fields = ('handle_left_type', 'handle_right_type', 'interpolation',
                     'easing', 'back', 'amplitude', 'period', 'type')
    mapped = 0
    target_count = 0
    for ident, rows_before in before_rows.items():
        rows_after = after_rows[ident]
        if len(rows_before) != len(rows_after):
            raise Failure('VALIDATION_FAILED',
                          'Retime changed key count for target ' + ident)
        used = set()
        expected_destinations = set()
        for row_after in rows_after:
            native_time = float(row_after['key']['co'][0])
            if (not math.isfinite(native_time)
                    or abs(float(row_after['time']) - native_time) > EPSILON):
                raise Failure('VALIDATION_FAILED',
                              'After evidence time differs from its native key')
        for row in rows_before:
            # Native key witnesses retain exact float32 times even in older
            # evidence whose `time` was reconstructed from a rounded offset.
            source = float(row['key']['co'][0])
            if (not math.isfinite(source)
                    or abs(float(row['time']) - source) > EPSILON):
                raise Failure('VALIDATION_FAILED',
                              'Before evidence key time differs from its binding')
            destination = mapping.get(round(source, 9))
            if destination is None:
                raise Failure('VALIDATION_FAILED',
                              'Retime warp lacks source frame ' + str(source))
            matches = [candidate for candidate in rows_after
                       if abs(float(candidate['key']['co'][0]) - destination) <= EPSILON
                       and id(candidate) not in used]
            if len(matches) != 1:
                raise Failure('VALIDATION_FAILED',
                              'Retime mapped key is missing or ambiguous at '
                              + str(destination))
            candidate = matches[0]
            used.add(id(candidate))
            expected_destinations.add(round(destination, 6))
            before_key = row['key']
            after_key = candidate['key']
            if abs(float(before_key['co'][1])
                   - float(after_key['co'][1])) > EPSILON:
                raise Failure('VALIDATION_FAILED',
                              'Retime changed a moved key value')
            for field in stable_fields:
                if before_key.get(field) != after_key.get(field):
                    raise Failure('VALIDATION_FAILED',
                                  'Retime changed key field ' + field)
            mapped += 1
        actual_destinations = {round(float(row['key']['co'][0]), 6)
                               for row in rows_after}
        if actual_destinations != expected_destinations:
            raise Failure('VALIDATION_FAILED',
                          'Retime after key times differ from mapped key times')
        target_count += 1
    return {
        'status': 'passed',
        'targets': target_count,
        'bindings': mapped,
        'mapping_rows': len(mapping),
        'before_contact': before_event['contact'],
        'after_contact': after_event['contact'],
        'start': after_event['frame'],
        'end': after_event['end'],
        'scope': 'record bindings -> review warp -> committed actual semantic keys',
    }


def isolate_failed_candidate(job, error):
    """Never leave a failed verification at the ordinary success path."""
    job = Path(job).resolve()
    source = job / 'animation-candidate.blend'
    failed = job / 'animation-candidate.failed.blend'
    if failed.exists():
        failed = job / ('animation-candidate.failed-' + uuid.uuid4().hex + '.blend')
    marker = job / 'animation-candidate.failed.json'
    result = {'status': 'failed', 'candidate_status': 'failed',
              'ordinary_candidate_absent': not source.exists(),
              'error': error, 'failed_candidate': None}
    if source.is_file():
        try:
            os.replace(source, failed)
            result['failed_candidate'] = descriptor(failed)
        except OSError as exc:
            result['isolation_error'] = str(exc)
            try:
                source.unlink()
                result['ordinary_candidate_absent'] = True
            except OSError as unlink_error:
                result['unlink_error'] = str(unlink_error)
    result['ordinary_candidate_absent'] = not source.exists()
    atomic_json(marker, result)
    return result


def _verification_config(candidate, params, evidence, runtime, closure,
                         animation, job):
    return {
        'animation_verification_version': '1.0',
        'candidate': candidate,
        'source': {'file': params['file'], 'sha256': params['expected_sha256']},
        'implementation_sha256': params['implementation_sha256'],
        'runtime': runtime,
        'request': params['manifest'],
        'resources': copy.deepcopy(params['resources']),
        'resource_closure_sha256': _canonical_sha(closure),
        'expected_contact': evidence['event']['contact'],
        'record_evidence_sha256': _canonical_sha(evidence),
        'animation_fingerprint': copy.deepcopy(animation),
        'output': str(Path(job).resolve() / 'animation-reopen.json'),
    }


def run(params, job):
    params = normalize_params(params)
    job = Path(job).resolve()
    started = time.monotonic()
    manifest = params['manifest']
    report = {
        'animation_report_version': '1.0',
        'status': 'running',
        'mode': 'run',
        'action': manifest['action'],
        'source': {'file': params['file'], 'sha256': params['expected_sha256']},
        'runtime': None,
        'implementation_sha256': params['implementation_sha256'],
        'signature': signature(params),
        'candidate': None,
        'candidate_status': None,
        'verification': None,
        'checks': {'source_sha256': 'pending', 'resources': 'pending',
                   'review_read_only': 'pending', 'execute': 'pending',
                   'record_evidence': 'pending', 'capture_animation_unchanged': 'pending',
                   'retime_mapping': 'pending', 'independent_reopen': 'pending',
                   'render': 'not_run'},
        'acceptance': 'technical_only; visual and GUI Undo/Redo remain external',
        'limitations': [
            'No render or preview is produced or substituted for evidence.',
            'Record evidence reads only the request Scene ID property '
            'acw_clip_event_state and compares it with active native Action/slot channels.',
        ],
    }
    try:
        runtime = _runtime_descriptor()
        report['runtime'] = runtime
        _check_identity(params)
        import bpy
        with ExitStack() as stack:
            source_guard = _guard_inputs(params, stack)
            _open_source(params)
            _validate_context(manifest)
            closure = _resource_closure(params)
            report['resources'] = closure
            atomic_json(job / 'animation-resource-closure.json', closure)
            before_state = state_fingerprint()
            before_animation = animation_fingerprint()
            report['before_state'] = before_state
            report['before_animation'] = before_animation
            scene = bpy.data.scenes[manifest['context']['scene']]
            before_record = _read_scene_record(scene, required=False)
            before_evidence = None
            if manifest['action'] == 'retime':
                before_evidence = clip_record_evidence(
                    manifest, manifest['event']['contact'])
                atomic_json(job / 'animation-before-record.json', before_evidence)
                report['before_record_evidence'] = descriptor(
                    job / 'animation-before-record.json')
            clip_event = _clip_event()
            review = _dict(_call(clip_event.review, __import__('bpy').context,
                                 copy.deepcopy(manifest)),
                           'clip_event.review result')
            reviewed = state_fingerprint()
            if reviewed['sha256'] != before_state['sha256']:
                raise Failure('VALIDATION_FAILED',
                              'clip_event.review changed actions or records')
            atomic_json(job / 'animation-review.json', review)
            report['review'] = descriptor(job / 'animation-review.json')
            report['checks']['review_read_only'] = 'passed'
            checkpoint = _checkpoint_factory(job, started)
            checkpoint()
            execution = _dict(
                _call(clip_event.execute, __import__('bpy').context, review,
                      checkpoint=checkpoint),
                'clip_event.execute result')
            checkpoint()
            atomic_json(job / 'animation-execute.json', execution)
            report['execution'] = execution
            report['checks']['execute'] = 'passed'
            if _canonical_sha(_resource_closure(params)) != _canonical_sha(closure):
                raise Failure('VALIDATION_FAILED',
                              'Animation execute changed static resources or SOUND members')
            after_animation = animation_fingerprint()
            report['after_animation'] = after_animation
            retime_mapping = None
            if manifest['action'] == 'capture':
                if before_record is not None:
                    raise Failure('CONFLICT',
                                  'Capture source already contains a clip event record')
                if after_animation['sha256'] != before_animation['sha256']:
                    raise Failure('VALIDATION_FAILED',
                                  'Capture changed native animation; it must only write the record')
                report['checks']['capture_animation_unchanged'] = 'passed'
                after_evidence = clip_record_evidence(
                    manifest, manifest['event']['contact'])
            else:
                if after_animation['sha256'] == before_animation['sha256']:
                    raise Failure('VALIDATION_FAILED',
                                  'Retime did not change native animation')
                after_evidence = clip_record_evidence(
                    manifest, manifest['event']['target_contact'])
                retime_mapping = retime_mapping_evidence(
                    before_evidence, after_evidence, review)
                report['retime_mapping'] = retime_mapping
                report['checks']['retime_mapping'] = 'passed'
            atomic_json(job / 'animation-record-evidence.json', after_evidence)
            report['record_evidence'] = descriptor(
                job / 'animation-record-evidence.json')
            report['record_summary'] = {
                'event': after_evidence['event'],
                'target_count': after_evidence['target_count'],
                'binding_count': after_evidence['binding_count'],
                'record_sha256': after_evidence['record_sha256'],
                'record_id': after_evidence['record_id'],
                'revision': after_evidence['revision'],
            }
            report['checks']['record_evidence'] = 'passed'
            candidate_path = job / 'animation-candidate.blend'
            if os.path.lexists(candidate_path):
                raise Failure('CONFLICT', 'Owned animation candidate already exists')
            if os.path.normcase(str(candidate_path)) == os.path.normcase(
                    str(Path(params['file']).resolve())):
                raise Failure('CONFLICT', 'Animation candidate path equals source')
            checkpoint()
            staged_path = job / 'animation-candidate.staged.blend'
            if os.path.lexists(staged_path):
                raise Failure('CONFLICT', 'Staged animation candidate already exists')
            bpy.ops.wm.save_as_mainfile(filepath=str(staged_path),
                                        check_existing=False,
                                        relative_remap=False, copy=True)
            checkpoint()
            os.replace(staged_path, candidate_path)
            candidate = descriptor(candidate_path)
            report['candidate'] = candidate
            from filesystem import FileGuard
            candidate_guard = stack.enter_context(FileGuard(candidate['file']))
            if candidate_guard.sha256() != candidate['sha256']:
                raise Failure('CONFLICT',
                              'Animation candidate changed before verification')
            config = _verification_config(
                candidate, params, after_evidence, runtime, closure,
                after_animation, job)
            checkpoint()
            verification = _launch_verifier(config, job)
            report['verification'] = verification
            if candidate_guard.sha256() != candidate['sha256']:
                raise Failure('CONFLICT',
                              'Animation candidate changed during verification')
            if source_guard.sha256() != params['expected_sha256']:
                raise Failure('CONFLICT', 'Animation source changed during job')
            report.update(status='passed', completed_at=time.time(),
                          candidate_status='accepted',
                          seconds=round(time.monotonic() - started, 3))
            report['checks'].update({
                'source_sha256': 'passed',
                'resources': 'closed_by_actual_scan',
                'independent_reopen': 'passed',
            })
            atomic_json(job / 'animation-report.json', report)
            data = {
                'status': 'passed',
                'action': manifest['action'],
                'source': report['source'],
                'candidate': candidate,
                'candidate_status': 'accepted',
                'implementation_sha256': params['implementation_sha256'],
                'resources': closure,
                'before_state': before_state,
                'before_animation': before_animation,
                'after_animation': after_animation,
                'record_evidence': report['record_evidence'],
                'record_summary': report['record_summary'],
                'before_record_evidence': report.get('before_record_evidence'),
                'retime_mapping': retime_mapping,
                'execution': execution,
                'review': report['review'],
                'verification': verification,
                'checks': report['checks'],
                'report': descriptor(job / 'animation-report.json'),
            }
            return data
    except Failure as exc:
        error = {'code': exc.code, 'message': str(exc)}
        isolation = isolate_failed_candidate(job, error)
        if isolation.get('failed_candidate'):
            report['candidate'] = isolation['failed_candidate']
        report.update(status='failed', error=error,
                      candidate_status='failed',
                      failed_candidate=isolation.get('failed_candidate'),
                      candidate_isolation=isolation,
                      seconds=round(time.monotonic() - started, 3))
        atomic_json(job / 'animation-report.json', report)
        raise
    except Exception as exc:
        error = {'code': 'WORKER_FAILED', 'message': str(exc)}
        isolation = isolate_failed_candidate(job, error)
        if isolation.get('failed_candidate'):
            report['candidate'] = isolation['failed_candidate']
        report.update(status='failed', error=error,
                      candidate_status='failed',
                      failed_candidate=isolation.get('failed_candidate'),
                      candidate_isolation=isolation,
                      seconds=round(time.monotonic() - started, 3))
        atomic_json(job / 'animation-report.json', report)
        raise


def inspect(params, job):
    params = normalize_inspect_params(params)
    job = Path(job).resolve()
    started = time.monotonic()
    manifest = params['manifest']
    report = {
        'animation_report_version': '1.0',
        'status': 'running',
        'mode': 'inspect',
        'action': manifest['action'],
        'source': {'file': params['file'], 'sha256': params['expected_sha256']},
        'runtime': None,
        'implementation_sha256': params['implementation_sha256'],
        'signature': signature(params),
        'checks': {'source_sha256': 'pending', 'resources': 'pending',
                   'read_only': 'pending', 'saves': 'none', 'render': 'not_run'},
        'acceptance': 'technical_read_only; visual review remains external',
    }
    try:
        runtime = _runtime_descriptor()
        report['runtime'] = runtime
        _check_identity(params)
        import bpy
        with ExitStack() as stack:
            source_guard = _guard_inputs(params, stack)
            _open_source(params)
            _validate_context(manifest)
            closure = _resource_closure(params)
            report['resources'] = closure
            atomic_json(job / 'animation-resource-closure.json', closure)
            before = state_fingerprint()
            report['before'] = before
            inspection = _dict(
                _call(_clip_event().inspect, __import__('bpy').context,
                      copy.deepcopy(manifest)),
                'clip_event.inspect result')
            after = state_fingerprint()
            if after['sha256'] != before['sha256']:
                raise Failure('VALIDATION_FAILED',
                              'clip_event.inspect changed actions or records')
            atomic_json(job / 'animation-inspection.json', inspection)
            report['inspection'] = descriptor(job / 'animation-inspection.json')
            scene = bpy.data.scenes[manifest['context']['scene']]
            record = _read_scene_record(scene, required=False)
            if record is None:
                evidence = {'schema': 'animation-clip-record-evidence-v1',
                            'status': 'absent', 'record_property': RECORD_PROPERTY,
                            'scene': scene.name,
                            'view_layer': manifest['context']['view_layer']}
                record_status = 'absent'
                expected_contact = None
            else:
                expected_contact = _inspect_expected_contact(manifest, record)
                evidence = clip_record_evidence(
                    manifest, expected_contact=expected_contact)
                record_status = 'verified'
            atomic_json(job / 'animation-record-evidence.json', evidence)
            report['record_status'] = record_status
            report['expected_contact'] = expected_contact
            report['record_evidence'] = descriptor(
                job / 'animation-record-evidence.json')
            post_evidence_state = state_fingerprint()
            if post_evidence_state['sha256'] != before['sha256']:
                raise Failure('VALIDATION_FAILED',
                              'Record evidence extraction changed actions or records')
            if source_guard.sha256() != params['expected_sha256']:
                raise Failure('CONFLICT', 'Animation source changed during inspection')
            report.update(status='inspected', completed_at=time.time(),
                          seconds=round(time.monotonic() - started, 3))
            report['checks'].update({'source_sha256': 'passed',
                                     'resources': 'closed_by_actual_scan',
                                     'read_only': 'passed',
                                     'record_evidence': record_status})
            atomic_json(job / 'animation-report.json', report)
            return {
                'status': 'inspected',
                'action': manifest['action'],
                'source': report['source'],
                'implementation_sha256': params['implementation_sha256'],
                'resources': closure,
                'before': before,
                'record_status': record_status,
                'expected_contact': expected_contact,
                'record_evidence': evidence,
                'inspection': inspection,
                'checks': report['checks'],
                'report': descriptor(job / 'animation-report.json'),
            }
    except Failure as exc:
        report.update(status='failed',
                      error={'code': exc.code, 'message': str(exc)},
                      seconds=round(time.monotonic() - started, 3))
        atomic_json(job / 'animation-report.json', report)
        raise
    except Exception as exc:
        report.update(status='failed',
                      error={'code': 'WORKER_FAILED', 'message': str(exc)},
                      seconds=round(time.monotonic() - started, 3))
        atomic_json(job / 'animation-report.json', report)
        raise
