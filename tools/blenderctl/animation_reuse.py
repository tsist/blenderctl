"""Thin protected ``animation.clip-apply`` worker adapter.

The adapter reuses the already verified animation helpers instead of growing a
second scene-inspection framework:

* ``animation_workflow._action_rows`` for native Action witnesses;
* ``animation_workflow.animation_fingerprint`` for the saved animation state;
* ``animation_workflow._resource_closure`` for static resource closure;
* ``animation_workflow._checkpoint_factory`` for cancellation/timeout.

``source_witness`` is deliberately a small, targeted witness of the
pre-existing Scene settings, named objects/data, object Action bindings and
existing Action hashes at apply time.  It is not a whole-file qualification
and does not attempt a second generic RNA snapshot.  ``inspection.record`` /
``snapshot`` are not part of this frozen copy; even when present they are
inspection evidence, not an independent saved-candidate native reader.

Exact saved Action curves and owner bindings are covered once by the reused
``animation_fingerprint``.  ``role_witness`` only checks the native role map,
native role metadata, newly-created Action inventory and key totals instead of
duplicating a second per-owner Action fingerprint.

The fixed add-on remains the only native apply core.  This module opens the
destination, calls ``clip_asset.apply_clip``, verifies Scene/non-target and
resource preservation, saves only a new job candidate, and launches a fresh
read-only Blender verifier.
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
from animation_reuse_contract import normalize_params, signature
from animation_workflow import (_action_rows, _checkpoint_factory,
                                _resource_closure, animation_fingerprint)


EPSILON = 1e-9
SOURCE_SCHEMA = 'animation-reuse-source-witness-v1'
NATIVE_SCHEMA = 'animation-reuse-native-witness-v1'
SOURCE_SCOPE = (
    'pre-existing Scene settings/properties, named objects and data summaries, '
    'object Action bindings and pre-existing Action witnesses captured at apply '
    'time; targeted, not a whole-file or all-datablock qualification')


def descriptor(path):
    path = Path(path).resolve()
    return {'file': str(path), 'sha256': digest(path),
            'bytes': path.stat().st_size}


def _plain(value, label):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise Failure('VALIDATION_FAILED',
                      label + ' is not JSON-safe: ' + str(exc)) from exc


def _call(fn, *args, default_code='VALIDATION_FAILED', **kwargs):
    try:
        return fn(*args, **kwargs)
    except Failure:
        raise
    except Exception as exc:
        code = getattr(exc, 'code', None)
        raise Failure(code if code in CODES else default_code, str(exc)) from exc


def _runtime_descriptor():
    import bpy
    return {'version': bpy.app.version_string,
            'build': bpy.app.build_hash.decode(),
            'binary_sha256': digest(bpy.app.binary_path)}


def _check_identity(params):
    from animation_identity import implementation_sha256
    if implementation_sha256() != params['implementation_sha256']:
        raise Failure('CONFLICT',
                      'Animation reuse implementation changed after request normalization')


def _fixed_clip_asset():
    addon = Path(__file__).resolve().parent.parent / 'animation_curves_workflow_addon'
    module_file = addon / 'clip_asset.py'
    if not addon.is_dir() or not module_file.is_file():
        raise Failure('NOT_FOUND',
                      'Fixed animation add-on clip_asset.py is missing: ' + str(addon))
    try:
        loaded = importlib.import_module(
            'animation_curves_workflow_addon.clip_asset')
    except Exception as exc:
        raise Failure('NOT_FOUND',
                      'Fixed animation add-on clip_asset import failed: '
                      + str(exc)) from exc
    if not callable(getattr(loaded, 'apply_clip', None)):
        raise Failure('UNSUPPORTED',
                      'Fixed animation add-on clip_asset.apply_clip is not callable')
    return loaded


def _guard_inputs(params, stack):
    """Hold the destination, frozen library and every resource against writes."""
    from filesystem import FileGuard
    library = params['manifest']['clip']['library']
    rows = [{'file': params['file'],
             'expected_sha256': params['expected_sha256']},
            {'file': library['file'],
             'expected_sha256': library['expected_sha256']}]
    rows.extend(params['resources'])
    guards = {}
    for row in rows:
        path = Path(row['file'])
        key = os.path.normcase(str(path.resolve()))
        if key in guards:
            if guards[key][0]['expected_sha256'] != row['expected_sha256']:
                raise Failure('CONFLICT',
                              'Conflicting animation reuse input hashes: '
                              + str(path))
            continue
        if not path.is_file():
            raise Failure('NOT_FOUND', 'Animation reuse input not found: ' + str(path))
        guard = stack.enter_context(FileGuard(path))
        actual = guard.sha256()
        if actual != row['expected_sha256']:
            raise Failure('CONFLICT', 'Animation reuse input SHA mismatch: ' + str(path))
        guards[key] = (row, guard)
    return guards


def _open_destination(params):
    import bpy
    bpy.ops.wm.open_mainfile(filepath=params['file'], load_ui=False,
                             use_scripts=False)
    if tuple(bpy.data.version[:2]) != tuple(bpy.app.version[:2]):
        raise Failure('UNSUPPORTED',
                      'Animation reuse requires matching Blender major/minor')


def _round(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return repr(value)
    return round(number, 9) if math.isfinite(number) else repr(value)


def _scene_settings(scene):
    render = scene.render
    unit = scene.unit_settings
    view = scene.view_settings
    display = scene.display_settings
    return {
        'name': scene.name,
        'fps': int(render.fps),
        'fps_base': _round(render.fps_base),
        'frame_start': int(scene.frame_start),
        'frame_end': int(scene.frame_end),
        'frame_current': int(scene.frame_current),
        'frame_subframe': _round(scene.frame_subframe),
        'unit_system': unit.system,
        'scale_length': _round(unit.scale_length),
        'length_unit': unit.length_unit,
        'render_engine': render.engine,
        'resolution_x': int(render.resolution_x),
        'resolution_y': int(render.resolution_y),
        'resolution_percentage': int(render.resolution_percentage),
        'film_transparent': bool(render.film_transparent),
        'use_nodes': bool(getattr(scene, 'use_nodes', False)),
        'view_transform': view.view_transform,
        'look': view.look,
        'display_device': display.display_device,
        'camera': scene.camera.name if scene.camera is not None else None,
        'world': scene.world.name if scene.world is not None else None,
    }


def _check_destination(scene, manifest):
    clip = manifest['clip']
    timing = clip['timing']
    if int(scene.render.fps) != int(timing['fps']):
        raise Failure('CONFLICT', 'Scene fps does not match clip timing')
    if abs(float(scene.render.fps_base)
           - float(timing['fps_base'])) > EPSILON:
        raise Failure('CONFLICT', 'Scene fps_base does not match clip timing')
    if abs(float(scene.unit_settings.scale_length)
           - float(clip['meters_per_blender_unit'])) > EPSILON:
        raise Failure('CONFLICT',
                      'Scene unit scale does not match clip meters_per_blender_unit')
    return _scene_settings(scene)


def _refresh_evaluation(scene):
    import bpy
    frame = scene.frame_current
    subframe = scene.frame_subframe
    view_layer = getattr(bpy.context, 'view_layer', None)
    if view_layer is not None:
        view_layer.update()
    scene.frame_set(frame, subframe=subframe)
    if (scene.frame_current, scene.frame_subframe) != (frame, subframe):
        raise Failure('VALIDATION_FAILED', 'Scene frame changed during evaluation')


def _sha_value(value, label):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             sort_keys=True, separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise Failure('VALIDATION_FAILED',
                      label + ' is not JSON-safe: ' + str(exc)) from exc
    return hashlib.sha256(encoded).hexdigest()


def _properties(item):
    if item is None or not hasattr(item, 'keys'):
        return {}
    return {str(key): _property_value(item[key]) for key in sorted(item.keys())}


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
    if hasattr(value, 'bl_rna') and hasattr(value, 'name'):
        return {'id_type': value.bl_rna.identifier, 'name': value.name}
    return repr(value)


def _id_identifier(item):
    return item.bl_rna.identifier if item is not None else None


def _persistent(item):
    return (getattr(item, 'users', 0) > 0
            or bool(getattr(item, 'use_fake_user', False)))


def _transform_row(obj):
    import bpy
    try:
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    except (AttributeError, RuntimeError):
        evaluated = obj
    return {
        'location': [_round(value) for value in evaluated.location],
        'rotation_mode': evaluated.rotation_mode,
        'rotation_euler': [_round(value) for value in evaluated.rotation_euler],
        'rotation_quaternion': [_round(value) for value in evaluated.rotation_quaternion],
        'scale': [_round(value) for value in evaluated.scale],
        'matrix_world': [_round(value) for row in evaluated.matrix_world
                         for value in row],
    }


def _data_summary(data):
    if data is None:
        return None
    kind = _id_identifier(data)
    row = {'type': kind, 'name': data.name,
           'users': getattr(data, 'users', 0)}
    if kind.endswith('Mesh'):
        row.update(vertices=len(data.vertices), edges=len(data.edges),
                   polygons=len(data.polygons),
                   materials=[material.name if material is not None else None
                              for material in data.materials])
    elif kind.endswith('Camera'):
        row.update(camera_type=data.type, lens=_round(data.lens),
                   sensor_width=_round(data.sensor_width))
    elif kind.endswith('Light'):
        row.update(light_type=data.type, energy=_round(data.energy),
                   color=[_round(value) for value in data.color])
    elif kind.endswith('Armature'):
        row.update(bones=len(data.bones))
    return row


def _object_witness(obj):
    animation = obj.animation_data
    action = animation.action if animation is not None else None
    slot = getattr(animation, 'action_slot', None) if animation is not None else None
    return {
        'type': obj.type,
        'data': _data_summary(obj.data),
        'parent': obj.parent.name if obj.parent is not None else None,
        'transform': _transform_row(obj),
        'collections': sorted(collection.name for collection in obj.users_collection),
        'hide_render': bool(obj.hide_render),
        'hide_viewport': bool(obj.hide_viewport),
        'properties': _properties(obj),
        'action': action.name if action is not None else None,
        'action_slot': slot.identifier if slot is not None else None,
    }


def source_witness():
    """Small targeted witness of pre-existing state, not a full-scene snapshot."""
    import bpy
    scene = bpy.context.scene
    objects = {}
    object_names = []
    data_names = []
    for obj in sorted(bpy.data.objects, key=lambda item: item.name):
        if not _persistent(obj):
            continue
        objects[obj.name] = _object_witness(obj)
        object_names.append(obj.name)
        if obj.data is not None:
            data_names.append(_id_identifier(obj.data) + ':' + obj.data.name)
    actions = {}
    for row in _action_rows():
        if row['users'] > 0 or row['fake_user']:
            actions[row['name']] = _sha_value(row, 'action ' + row['name'])
    return {
        'schema': SOURCE_SCHEMA,
        'scope': SOURCE_SCOPE,
        'scene': _scene_settings(scene),
        'scene_properties': _properties(scene),
        'objects': objects,
        'object_names': sorted(object_names),
        'data_names': sorted(set(data_names)),
        'actions': actions,
    }


def compare_source_witness(actual, expected):
    errors = []
    if actual.get('scene') != expected.get('scene'):
        errors.append('scene settings')
    if actual.get('scene_properties') != expected.get('scene_properties'):
        errors.append('scene properties')
    actual_objects = actual.get('objects', {})
    for name, row in expected.get('objects', {}).items():
        if actual_objects.get(name) != row:
            errors.append('object:' + name)
            if len(errors) >= 64:
                return errors
    actual_actions = actual.get('actions', {})
    for name, witness in expected.get('actions', {}).items():
        if actual_actions.get(name) != witness:
            errors.append('action:' + name)
            if len(errors) >= 64:
                return errors
    return errors


def _name_from(value, label, keys=('name',)):
    if isinstance(value, str) and value.strip() and '\x00' not in value:
        return value
    if isinstance(value, dict):
        for key in keys + ('name',):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip() \
                    and '\x00' not in candidate:
                return candidate
    raise Failure('VALIDATION_FAILED',
                  label + ' must provide a non-empty native name')


def _canonical_uuid(value):
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _prop(item, key, label, required=True):
    if item is None or not hasattr(item, 'keys'):
        if required:
            raise Failure('VALIDATION_FAILED', label + ' has no properties')
        return None
    if key not in item.keys():
        if required:
            raise Failure('VALIDATION_FAILED',
                          label + ' lacks native property ' + key)
        return None
    return item[key]


def _canonical_prop(item, key, label):
    value = _canonical_uuid(_prop(item, key, label))
    if value is None:
        raise Failure('VALIDATION_FAILED',
                      label + '.' + key + ' is not a valid UUID')
    return value


def _common_provenance(item, manifest, label):
    clip = manifest['clip']
    expected = {
        'ac_clip_id': clip['clip_id'],
        'ac_clip_instance_name': manifest['instance_name'],
        'ac_clip_source_file': clip['source']['file'],
        'ac_clip_source_sha256': clip['source']['sha256'],
    }
    actual = {}
    for key, value in expected.items():
        found = _prop(item, key, label)
        if found != value:
            raise Failure('VALIDATION_FAILED',
                          label + '.' + key + ' provenance differs')
        actual[key] = found
    return actual


def _wrapper_provenance(wrapper, manifest, summary):
    clip = manifest['clip']
    actual = {'common': _common_provenance(wrapper, manifest, 'wrapper')}
    if _prop(wrapper, 'ac_clip_schema', 'wrapper') != manifest['schema']:
        raise Failure('VALIDATION_FAILED', 'wrapper schema provenance differs')
    if _prop(wrapper, 'ac_clip_library_file', 'wrapper') != clip['library']['file']:
        raise Failure('VALIDATION_FAILED', 'wrapper library file provenance differs')
    if _prop(wrapper, 'ac_clip_library_sha256', 'wrapper') != \
            clip['library']['expected_sha256']:
        raise Failure('VALIDATION_FAILED', 'wrapper library SHA provenance differs')
    try:
        mapping = json.loads(_prop(wrapper, 'ac_clip_mapping', 'wrapper'))
        assets = json.loads(_prop(wrapper, 'ac_clip_assets', 'wrapper'))
    except (TypeError, ValueError) as exc:
        raise Failure('VALIDATION_FAILED',
                      'wrapper provenance JSON is invalid: ' + str(exc)) from exc
    if _sha_value(mapping, 'wrapper mapping') != _sha_value(
            summary['mapping'], 'result mapping'):
        raise Failure('VALIDATION_FAILED', 'wrapper mapping provenance differs')
    if not isinstance(assets, list):
        raise Failure('VALIDATION_FAILED', 'wrapper asset provenance is not a list')
    actual.update(mapping_sha256=_sha_value(mapping, 'wrapper mapping'),
                  assets=len(assets))
    return actual


def _actor_metadata(collection, root, rig, actor_result, manifest):
    spec = next(actor for actor in manifest['clip']['actors']
                if actor['role'] == actor_result['role'])
    instance = _canonical_uuid(actor_result['instance_uuid'])
    if instance is None:
        raise Failure('VALIDATION_FAILED',
                      'native actor instance_uuid is invalid')
    common = _common_provenance(collection, manifest,
                                'actor ' + actor_result['role'])
    collection_uuid = _canonical_prop(
        collection, 'ac_instance_uuid', 'actor collection')
    root_uuid = _canonical_prop(root, 'ac_instance_uuid', 'actor root')
    rig_uuid = _canonical_prop(rig, 'ac_instance_uuid', 'actor rig')
    for label, value in (('collection', collection_uuid),
                         ('root', root_uuid), ('rig', rig_uuid)):
        if value != instance:
            raise Failure('VALIDATION_FAILED',
                          'actor ' + actor_result['role'] + ' ' + label
                          + ' instance UUID differs')
    source_asset = _canonical_prop(
        collection, 'acw_source_asset_id', 'actor collection')
    if source_asset != _canonical_uuid(spec['asset_id']):
        raise Failure('VALIDATION_FAILED',
                      'actor source asset_id provenance differs')
    source_template = _prop(
        collection, 'ac_clip_source_template_uuid', 'actor collection')
    if (not isinstance(source_template, str)
            or source_template.lower() != spec['template_uuid'].lower()):
        raise Failure('VALIDATION_FAILED',
                      'actor source template UUID provenance differs')
    collection_text = _prop(collection, 'ac_template_metadata', 'actor collection')
    root_text = _prop(root, 'ac_template_metadata', 'actor root')
    if not isinstance(collection_text, str) or collection_text != root_text:
        raise Failure('VALIDATION_FAILED',
                      'actor template metadata is missing or differs')
    try:
        metadata = json.loads(collection_text)
    except (TypeError, ValueError) as exc:
        raise Failure('VALIDATION_FAILED',
                      'actor template metadata JSON is invalid: ' + str(exc)) from exc
    if (not isinstance(metadata, dict)
            or _canonical_uuid(metadata.get('instance_uuid')) != instance):
        raise Failure('VALIDATION_FAILED',
                      'actor template metadata instance_uuid differs')
    tagged_items = [collection, root, rig, rig.data]
    materials = []
    for obj in collection.objects:
        if obj.type == 'MESH':
            tagged_items.append(obj)
            if obj.data is not None:
                tagged_items.append(obj.data)
                materials.extend(item for item in obj.data.materials
                                 if item is not None)
    tagged_items.extend(materials)
    seen = set()
    tagged_count = 0
    for item in tagged_items:
        if item is None:
            continue
        pointer = item.as_pointer()
        if pointer in seen:
            continue
        seen.add(pointer)
        if 'ac_instance_uuid' not in item.keys():
            raise Failure('VALIDATION_FAILED',
                          'required actor ownership tag is missing: ' + item.name)
        if _canonical_uuid(item.get('ac_instance_uuid')) != instance:
            raise Failure('VALIDATION_FAILED',
                          'tagged actor ID has a different instance UUID: '
                          + item.name)
        tagged_count += 1
    if tagged_count < 4:
        raise Failure('VALIDATION_FAILED',
                      'actor ownership metadata is incomplete')
    return {
        'instance_uuid': actor_result['instance_uuid'],
        'instance_uuid_canonical': instance,
        'collection_uuid': collection_uuid,
        'root_uuid': root_uuid,
        'rig_uuid': rig_uuid,
        'source_asset_id': source_asset,
        'source_template_uuid': source_template,
        'template_metadata_instance_uuid': instance,
        'tagged_count': tagged_count,
        'common': common,
    }


def _light_metadata(obj, light_result, manifest):
    spec = next(light for light in manifest['clip']['lights']
                if light['role'] == light_result['role'])
    common = _common_provenance(obj, manifest, 'light ' + light_result['role'])
    source_asset = _canonical_prop(obj, 'acw_source_asset_id', 'light object')
    if source_asset != _canonical_uuid(spec['asset_id']):
        raise Failure('VALIDATION_FAILED',
                      'light source asset_id provenance differs')
    if _prop(obj, 'ac_clip_role', 'light object') != light_result['role']:
        raise Failure('VALIDATION_FAILED', 'light role provenance differs')
    return {'source_asset_id': source_asset, 'common': common}


def _descendant_collections(collection):
    names = set()
    stack = [collection]
    while stack:
        current = stack.pop()
        if current.name in names:
            continue
        names.add(current.name)
        stack.extend(current.children)
    return names


def _collection_in_scene(scene, collection):
    stack = [scene.collection]
    seen = set()
    while stack:
        current = stack.pop()
        if current == collection:
            return True
        if current.name in seen:
            continue
        seen.add(current.name)
        stack.extend(current.children)
    return False


def _lookup_object(name, allowed=None):
    import bpy
    obj = bpy.data.objects.get(name)
    if obj is not None:
        return obj
    armature = bpy.data.armatures.get(name)
    if armature is not None:
        owners = [item for item in bpy.data.objects if item.data == armature]
        if allowed is not None:
            owners = [item for item in owners if item.name in allowed]
        if len(owners) == 1:
            return owners[0]
    raise Failure('VALIDATION_FAILED', 'Native role object not found: ' + str(name))


def _resolve_wrapper(name, source, scene):
    import bpy
    baseline = set(source.get('objects', {}))
    # Wrapper names may gain Blender's .001 suffix on collision.
    candidates = [
        collection for collection in bpy.data.collections
        if (collection.name == name or collection.name.startswith(name + '.'))
        and _collection_in_scene(scene, collection)
        and not any(obj.name in baseline for obj in collection.all_objects)
    ]
    exact = [collection for collection in candidates if collection.name == name]
    if len(exact) == 1:
        return exact[0]
    if len(candidates) == 1:
        return candidates[0]
    raise Failure('VALIDATION_FAILED',
                  'Applied wrapper collection is missing or ambiguous: ' + name)


def _resolve_role_collection(name, wrapper):
    import bpy
    descendants = _descendant_collections(wrapper)
    candidates = [
        collection for collection in bpy.data.collections
        if collection.name in descendants
        and (collection.name == name or collection.name.startswith(name + '.'))
    ]
    exact = [collection for collection in candidates if collection.name == name]
    if len(exact) == 1:
        return exact[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _check_new_object(obj, source, label):
    if obj.name in set(source.get('object_names', ())):
        raise Failure('VALIDATION_FAILED',
                      label + ' reused a pre-existing object: ' + obj.name)
    if obj.data is not None:
        key = _id_identifier(obj.data) + ':' + obj.data.name
        if key in set(source.get('data_names', ())):
            raise Failure('VALIDATION_FAILED',
                          label + ' reused pre-existing data: ' + key)


def _material_rows(value):
    if value is None:
        return []
    if isinstance(value, dict):
        if 'role' in value or 'material' in value:
            rows = [value]
        else:
            rows = [({**child, 'role': role} if isinstance(child, dict)
                     else {'role': role, 'material': child})
                    for role, child in value.items()]
    elif isinstance(value, list):
        rows = value
    else:
        raise Failure('VALIDATION_FAILED',
                      'clip_asset.apply_clip materials must be a map or array')
    result = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise Failure('VALIDATION_FAILED',
                          'clip_asset.apply_clip material row must be an object')
        role = row.get('role')
        if not isinstance(role, str) or not role.strip():
            raise Failure('VALIDATION_FAILED',
                          'clip_asset.apply_clip material role is missing')
        kind = row.get('kind')
        if kind not in ('body', 'chest', 'face'):
            raise Failure('VALIDATION_FAILED', 'Native material kind is missing or unsupported')
        result.append({'role': role, 'kind': kind,
                       'material': _name_from(row.get('material'),
                                              'material[' + str(index) + ']')})
    return result


def _role_rows(value, label):
    if isinstance(value, dict):
        if 'role' in value:
            return [value]
        return [({**child, 'role': role} if isinstance(child, dict)
                 else {'role': role}) for role, child in value.items()]
    if isinstance(value, list):
        return value
    raise Failure('VALIDATION_FAILED',
                  'clip_asset.apply_clip ' + label + ' must be a map or array')


def _apply_summary(value, manifest):
    if not isinstance(value, dict):
        raise Failure('VALIDATION_FAILED',
                      'clip_asset.apply_clip must return a JSON object')
    value = _plain(value, 'clip_asset.apply_clip result')
    schema = value.get('schema')
    if schema is not None and not isinstance(schema, str):
        raise Failure('VALIDATION_FAILED',
                      'clip_asset.apply_clip schema must be a string')
    status = value.get('status')
    if status is not None:
        if not isinstance(status, str):
            raise Failure('VALIDATION_FAILED',
                          'clip_asset.apply_clip status must be a string')
        if status.strip().lower() in ('failed', 'failure', 'error', 'cancelled',
                                      'canceled'):
            raise Failure('VALIDATION_FAILED',
                          'clip_asset.apply_clip reported failure: ' + status)
    if 'collection' not in value:
        raise Failure('VALIDATION_FAILED',
                      'clip_asset.apply_clip result lacks collection')
    collection = _name_from(value['collection'], 'collection',
                            keys=('name', 'collection'))
    if 'actors' not in value or 'lights' not in value:
        raise Failure('VALIDATION_FAILED',
                      'clip_asset.apply_clip result lacks actors or lights')
    actor_specs = {row['role']: row for row in manifest['clip']['actors']}
    light_specs = {row['role']: row for row in manifest['clip']['lights']}
    source_ids = set()
    for spec in list(actor_specs.values()) + list(light_specs.values()):
        source_ids.add(_canonical_uuid(spec['asset_id']))
        if 'template_uuid' in spec:
            source_ids.add(_canonical_uuid(spec['template_uuid']))
    source_ids.discard(None)
    actors = {}
    for row in _role_rows(value['actors'], 'actors'):
        if not isinstance(row, dict):
            raise Failure('VALIDATION_FAILED',
                          'clip_asset.apply_clip actor row must be an object')
        role = row.get('role')
        if role not in ('A', 'B') or role not in actor_specs:
            raise Failure('VALIDATION_FAILED',
                          'clip_asset.apply_clip actor role is invalid: '
                          + str(role))
        if role in actors:
            raise Failure('VALIDATION_FAILED',
                          'clip_asset.apply_clip duplicate actor role: ' + str(role))
        instance_raw = row.get('instance_uuid')
        instance = _canonical_uuid(instance_raw)
        if instance is None:
            raise Failure('VALIDATION_FAILED',
                          'clip_asset.apply_clip actor ' + str(role)
                          + ' has no canonical instance_uuid')
        if instance in source_ids:
            raise Failure('VALIDATION_FAILED',
                          'actor instance_uuid was not reissued')
        spec = actor_specs[role]
        if row.get('template_uuid') is not None:
            if str(row['template_uuid']).lower() != spec['template_uuid'].lower():
                raise Failure('VALIDATION_FAILED',
                              'actor template_uuid differs from manifest')
        if row.get('source_asset_id') is not None:
            if _canonical_uuid(row['source_asset_id']) != \
                    _canonical_uuid(spec['asset_id']):
                raise Failure('VALIDATION_FAILED',
                              'actor source_asset_id differs from manifest')
        actors[role] = {
            'role': role,
            'collection': _name_from(row.get('collection'),
                                     'actor ' + str(role) + '.collection'),
            'root': _name_from(row.get('root'), 'actor ' + str(role) + '.root'),
            'rig': _name_from(row.get('rig'), 'actor ' + str(role) + '.rig'),
            'instance_uuid': instance_raw,
            'instance_uuid_canonical': instance,
            'objects': row.get('objects'),
            'meshes': row.get('meshes'),
        }
    if set(actors) != {'A', 'B'}:
        raise Failure('VALIDATION_FAILED',
                      'clip_asset.apply_clip must return actor roles A and B')
    if actors['A']['instance_uuid_canonical'] == \
            actors['B']['instance_uuid_canonical']:
        raise Failure('VALIDATION_FAILED',
                      'clip_asset.apply_clip did not issue distinct actor instances')
    lights = {}
    for row in _role_rows(value['lights'], 'lights'):
        if not isinstance(row, dict):
            raise Failure('VALIDATION_FAILED',
                          'clip_asset.apply_clip light row must be an object')
        role = row.get('role')
        if role not in ('block', 'counter') or role not in light_specs:
            raise Failure('VALIDATION_FAILED',
                          'clip_asset.apply_clip light role is invalid: '
                          + str(role))
        if role in lights:
            raise Failure('VALIDATION_FAILED',
                          'clip_asset.apply_clip duplicate light role: ' + str(role))
        if row.get('source_asset_id') is not None:
            if _canonical_uuid(row['source_asset_id']) != \
                    _canonical_uuid(light_specs[role]['asset_id']):
                raise Failure('VALIDATION_FAILED',
                              'light source_asset_id differs from manifest')
        lights[role] = {
            'role': role,
            'object': _name_from(row.get('object'),
                                 'light ' + str(role) + '.object'),
            'data': _name_from(row.get('data'),
                               'light ' + str(role) + '.data'),
            'action': row.get('action'),
        }
    if set(lights) != {'block', 'counter'}:
        raise Failure('VALIDATION_FAILED',
                      'clip_asset.apply_clip must return light roles block and counter')
    action_keys = ('actions', 'action_counts', 'action_count',
                   'action_witnesses', 'witnesses')
    action_value = next((value[key] for key in action_keys if key in value), None)
    if not isinstance(action_value, dict) or not isinstance(
            action_value.get('by_owner'), list) or not action_value['by_owner']:
        raise Failure('VALIDATION_FAILED',
                      'clip_asset.apply_clip result lacks native action evidence')
    for field in ('count', 'curves', 'keys'):
        if isinstance(action_value.get(field), bool) or not isinstance(
                action_value.get(field), int):
            raise Failure('VALIDATION_FAILED',
                          'native action total is invalid: ' + field)
    names = []
    for row in action_value['by_owner']:
        if not isinstance(row, dict) or not isinstance(row.get('action'), str) \
                or not row['action']:
            raise Failure('VALIDATION_FAILED',
                          'native action evidence row is invalid')
        names.append(row['action'])
    if len(set(names)) != len(names):
        raise Failure('VALIDATION_FAILED',
                      'native action evidence contains duplicate Actions')
    action_counts = {
        'count': action_value['count'],
        'curves': action_value['curves'],
        'keys': action_value['keys'],
        'names': names,
    }
    assets = value.get('assets')
    if isinstance(assets, dict) and assets.get('library_sha256') is not None:
        if assets['library_sha256'] != \
                manifest['clip']['library']['expected_sha256']:
            raise Failure('VALIDATION_FAILED',
                          'native result library SHA differs from request')
    mapping = value.get('mapping')
    if not isinstance(mapping, dict) or not mapping:
        raise Failure('VALIDATION_FAILED',
                      'clip_asset.apply_clip result lacks a non-empty mapping')
    return {
        'schema': schema,
        'status': status,
        'collection': collection,
        'actors': [actors[role] for role in ('A', 'B')],
        'lights': [lights[role] for role in ('block', 'counter')],
        'materials': _material_rows(value.get('materials')),
        'mapping': mapping,
        'trim': value.get('trim'),
        'action_counts': action_counts,
        'assets': assets,
        'limits': value.get('limits'),
        'reported_fields': sorted(value),
        'manifest_actors': copy.deepcopy(manifest['clip']['actors']),
        'manifest_lights': copy.deepcopy(manifest['clip']['lights']),
    }


def role_witness(summary, source, scene):
    """Read saved native roles/actions/metadata, not just the returned report."""
    import bpy
    manifest = summary['manifest']
    collection = _resolve_wrapper(summary['collection'], source, scene)
    wrapper_objects = {obj.name for obj in collection.all_objects}
    wrapper_meta = _wrapper_provenance(collection, manifest, summary)
    action_rows = {row['name']: row for row in _action_rows()}
    baseline_actions = set(source.get('actions', {}))
    actors = {}
    for actor_result in summary['actors']:
        role = actor_result['role']
        if role not in ('A', 'B') or role in actors:
            raise Failure('VALIDATION_FAILED', 'Invalid actor role: ' + str(role))
        actor_collection = _resolve_role_collection(
            actor_result['collection'], collection)
        if actor_collection is None:
            raise Failure('VALIDATION_FAILED',
                          'Actor collection is missing: '
                          + actor_result['collection'])
        root = _lookup_object(actor_result['root'], wrapper_objects)
        rig = _lookup_object(actor_result['rig'], wrapper_objects)
        for label, obj in (('actor root', root), ('actor rig', rig)):
            if obj.name not in wrapper_objects:
                raise Failure('VALIDATION_FAILED',
                              label + ' is outside the wrapper collection: '
                              + obj.name)
            _check_new_object(obj, source, label)
        if root.type != 'EMPTY' or root.parent is not None:
            raise Failure('VALIDATION_FAILED',
                          'actor root is not a parentless Empty: ' + root.name)
        if rig.type != 'ARMATURE' or rig.parent != root or rig.data is None:
            raise Failure('VALIDATION_FAILED',
                          'actor rig is not an Armature child of root: ' + rig.name)
        metadata = _actor_metadata(actor_collection, root, rig, actor_result,
                                   manifest)
        objects = list(actor_collection.objects)
        if root.name not in {obj.name for obj in objects} \
                or rig.name not in {obj.name for obj in objects}:
            raise Failure('VALIDATION_FAILED',
                          'actor root/rig are not in the actor collection')
        for obj in objects:
            _check_new_object(obj, source, 'actor object')
        meshes = sum(1 for obj in objects if obj.type == 'MESH')
        if actor_result.get('objects') is not None \
                and int(actor_result['objects']) != len(objects):
            raise Failure('VALIDATION_FAILED',
                          'actor object count differs for ' + role)
        if actor_result.get('meshes') is not None \
                and int(actor_result['meshes']) != meshes:
            raise Failure('VALIDATION_FAILED',
                          'actor mesh count differs for ' + role)
        actors[role] = {
            'collection': actor_collection.name,
            'root': root.name,
            'rig': rig.name,
            'instance_uuid': actor_result['instance_uuid'],
            'metadata': metadata,
            'root_transform': _transform_row(root),
            'rig_transform': _transform_row(rig),
            'objects': len(objects),
            'meshes': meshes,
        }
    if set(actors) != {'A', 'B'}:
        raise Failure('VALIDATION_FAILED',
                      'Native actor witness must contain roles A and B')
    lights = {}
    for light_result in summary['lights']:
        role = light_result['role']
        obj = _lookup_object(light_result['object'], wrapper_objects)
        if obj.name not in wrapper_objects:
            raise Failure('VALIDATION_FAILED',
                          'light is outside the wrapper collection: ' + obj.name)
        _check_new_object(obj, source, 'light ' + role)
        if obj.type != 'LIGHT' or obj.data is None \
                or obj.data.name != light_result['data']:
            raise Failure('VALIDATION_FAILED',
                          'light object/data differs for ' + role)
        metadata = _light_metadata(obj, light_result, manifest)
        lights[role] = {
            'object': obj.name,
            'data': obj.data.name,
            'metadata': metadata,
            'transform': _transform_row(obj),
        }
    if set(lights) != {'block', 'counter'}:
        raise Failure('VALIDATION_FAILED',
                      'Native light witness must contain block and counter')
    materials = []
    for material_row in summary['materials']:
        material = bpy.data.materials.get(material_row['material'])
        if material is None:
            raise Failure('VALIDATION_FAILED',
                          'Native material is missing: ' + material_row['material'])
        tree = material.node_tree if material.use_nodes else None
        materials.append({
            'role': material_row['role'],
            'material': material.name,
            'users': material.users,
            'node_tree': tree.name if tree is not None else None,
            'action': (tree.animation_data.action.name
                       if tree is not None and tree.animation_data
                       and tree.animation_data.action else None),
        })
    action_names = sorted(summary['action_counts']['names'])
    if len(action_names) != int(summary['action_counts']['count']):
        raise Failure('VALIDATION_FAILED',
                      'Native Action name/count inventory is inconsistent')
    if set(action_names) & baseline_actions:
        raise Failure('VALIDATION_FAILED',
                      'Native Action inventory shares pre-existing Actions')
    missing_actions = [name for name in action_names if name not in action_rows]
    if missing_actions:
        raise Failure('VALIDATION_FAILED',
                      'Native Actions are missing from the saved file: '
                      + ', '.join(missing_actions[:8]))
    new_actions = set(action_rows) - baseline_actions
    if set(action_names) != new_actions:
        raise Failure('VALIDATION_FAILED',
                      'Saved new Action inventory differs from the native result')
    action_keyframes = sum(action_rows[name]['keyframes']
                           for name in action_names)
    if action_keyframes != int(summary['action_counts']['keys']):
        raise Failure('VALIDATION_FAILED',
                      'Saved Action key total differs from the native result')
    return {
        'schema': NATIVE_SCHEMA,
        'collection': {
            'name': collection.name,
            'in_scene': True,
            'objects': sorted(wrapper_objects),
        },
        'wrapper': wrapper_meta,
        'actors': actors,
        'lights': lights,
        'materials': materials,
        'mapping_sha256': _sha_value(summary['mapping'], 'apply mapping'),
        'action_names': action_names,
        'action_keyframes': action_keyframes,
        'action_totals': {
            'native_count': summary['action_counts']['count'],
            'native_curves': summary['action_counts']['curves'],
            'native_keys': summary['action_counts']['keys'],
            'observed_count': len(action_names),
            'observed_keys': action_keyframes,
        },
        'limits': summary.get('limits'),
    }


def _resource_witness(closure):
    files = {}
    for row in closure['external']:
        files[row['file']] = row['sha256']
    return {
        'schema': 'animation-reuse-resource-witness-v1',
        'files': files,
        'static_sound_members': closure['static_sound_members'],
        'declared_count': closure['declared_count'],
    }


def _drop_loading_libraries(params):
    """Remove Blender's temporary append-library handle after a local append.

    ``bpy.data.libraries.load(...).append`` can leave one Library datablock
    even though all appended IDs are local.  The native core rejects linked or
    override IDs; this helper verifies that no linked ID exists, that the only
    handle names the declared frozen library, and then removes the handle so
    the existing static resource closure can run on the local candidate.
    """
    import bpy
    declared = os.path.normcase(str(
        Path(params['manifest']['clip']['library']['file']).resolve()))
    libraries = list(bpy.data.libraries)
    if not libraries:
        return []
    for library in libraries:
        path = bpy.path.abspath(library.filepath, library=library.library)
        if os.path.normcase(str(Path(path).resolve())) != declared:
            raise Failure('UNSUPPORTED',
                          'Unexpected library handle after apply: ' + library.filepath)
    linked = []
    for prop in bpy.data.bl_rna.properties:
        if prop.type != 'COLLECTION':
            continue
        for item in getattr(bpy.data, prop.identifier, ()):
            if isinstance(item, bpy.types.ID) and item.library is not None:
                linked.append(item.bl_rna.identifier + ':' + item.name)
                if len(linked) >= 16:
                    break
        if len(linked) >= 16:
            break
    if linked:
        raise Failure('UNSUPPORTED',
                      'Appended clip contains linked/override IDs: '
                      + ', '.join(linked))
    removed = [library.filepath for library in libraries]
    for library in libraries:
        bpy.data.libraries.remove(library, do_unlink=True)
    return removed


def _save_candidate(params, job):
    import bpy
    job = Path(job).resolve()
    candidate = job / 'animation-reuse-candidate.blend'
    staged = job / 'animation-reuse-candidate.staged.blend'
    library = params['manifest']['clip']['library']['file']
    if os.path.lexists(candidate):
        raise Failure('CONFLICT', 'Owned animation reuse candidate already exists')
    if os.path.lexists(staged):
        raise Failure('CONFLICT', 'Staged animation reuse candidate already exists')
    for protected in (params['file'], library):
        if os.path.normcase(str(candidate)) == os.path.normcase(
                str(Path(protected).resolve())):
            raise Failure('CONFLICT', 'Candidate path equals a protected input')
    bpy.ops.wm.save_as_mainfile(filepath=str(staged), check_existing=False,
                                relative_remap=False, copy=True)
    if not staged.is_file():
        raise Failure('VALIDATION_FAILED',
                      'Blender did not create the staged animation reuse candidate')
    os.replace(staged, candidate)
    return descriptor(candidate)


def isolate_failed_candidate(job, error):
    """Never leave a failed verification on the ordinary success path."""
    job = Path(job).resolve()
    result = {'status': 'failed', 'candidate_status': 'failed',
              'ordinary_candidate_absent': True,
              'failed_candidate': None, 'failed_staged': None,
              'error': error}
    for source_name, key in (
            ('animation-reuse-candidate.blend', 'failed_candidate'),
            ('animation-reuse-candidate.staged.blend', 'failed_staged')):
        source = job / source_name
        if not source.is_file():
            continue
        failed = job / (source.stem + '.failed.blend')
        if failed.exists():
            failed = job / (source.stem + '.failed-' + uuid.uuid4().hex + '.blend')
        try:
            os.replace(source, failed)
            result[key] = descriptor(failed)
        except OSError as exc:
            result['isolation_error_' + key] = str(exc)
            try:
                source.unlink()
                result[key] = None
            except OSError as unlink_error:
                result['unlink_error_' + key] = str(unlink_error)
    result['ordinary_candidate_absent'] = not (
        job / 'animation-reuse-candidate.blend').exists()
    atomic_json(job / 'animation-reuse-candidate.failed.json', result)
    return result


def _launch_verifier(config, job):
    import bpy
    job = Path(job).resolve()
    request_path = job / 'animation-reuse-verification-request.json'
    atomic_json(request_path, config)
    verifier = Path(__file__).with_name('animation_reuse_verifier.py')
    if not verifier.is_file():
        raise Failure('NOT_FOUND',
                      'animation_reuse_verifier.py is required for independent reopen')
    args = [bpy.app.binary_path, '--background', '--factory-startup',
            '--disable-autoexec', '--offline-mode', '--python-exit-code', '1',
            '--python', str(verifier), '--', str(request_path)]
    try:
        with (job / 'animation-reuse-reopen.log').open('wb') as log:
            process = subprocess.run(
                args, stdout=log, stderr=subprocess.STDOUT, timeout=120,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0))
    except subprocess.TimeoutExpired as exc:
        raise Failure('TIMEOUT',
                      'Independent animation reuse reopen timed out') from exc
    output = Path(config['output'])
    result = read_json(output) if output.is_file() else {
        'status': 'failed', 'error': 'No independent reader result'}
    if process.returncode or result.get('status') != 'passed':
        raise Failure('VALIDATION_FAILED',
                      'Independent animation reuse reopen: '
                      + str(result.get('error')))
    return result


def _expected_config(params, runtime, scene_settings, source, resources,
                     apply_summary, native_witness, animation, candidate):
    native_sha = _sha_value(native_witness, 'native witness')
    return {
        'animation_reuse_verification_version': '1.0',
        'command': 'animation.clip-apply',
        'request_schema': 'animation-native-clip-apply-v1',
        'clip_schema': 'animation-native-clip-v1',
        'source': {'file': params['file'],
                   'sha256': params['expected_sha256']},
        'library': dict(params['manifest']['clip']['library']),
        'resources': copy.deepcopy(params['resources']),
        'implementation_sha256': params['implementation_sha256'],
        'runtime': copy.deepcopy(runtime),
        'request': copy.deepcopy(params['manifest']),
        'candidate': copy.deepcopy(candidate),
        'scene_name': scene_settings['name'],
        'scene_settings': copy.deepcopy(scene_settings),
        'source_witness': copy.deepcopy(source),
        'source_witness_sha256': _sha_value(source, 'source witness'),
        'source_witness_scope': SOURCE_SCOPE,
        'resource_witness': copy.deepcopy(resources),
        'apply_summary': copy.deepcopy(apply_summary),
        'native_witness': copy.deepcopy(native_witness),
        'native_witness_sha256': native_sha,
        'animation_fingerprint': copy.deepcopy(animation),
        'output': '',
    }


def run(params, job):
    params = normalize_params(params)
    job = Path(job).resolve()
    started = time.monotonic()
    manifest = params['manifest']
    clip = manifest['clip']
    report = {
        'animation_reuse_report_version': '1.0',
        'status': 'running',
        'mode': 'clip-apply',
        'command': 'animation.clip-apply',
        'request_schema': 'animation-native-clip-apply-v1',
        'clip_schema': 'animation-native-clip-v1',
        'clip_id': clip['clip_id'],
        'instance_name': manifest['instance_name'],
        'source': {'file': params['file'],
                   'sha256': params['expected_sha256']},
        'library': dict(clip['library']),
        'source_provenance': dict(clip['source']),
        'request_start': manifest['start'],
        'request_end': manifest['end'],
        'runtime': None,
        'implementation_sha256': params['implementation_sha256'],
        'signature': signature(params),
        'candidate': None,
        'candidate_status': None,
        'verification': None,
        'source_witness_scope': SOURCE_SCOPE,
        'checks': {
            'source_sha256': 'pending', 'library_sha256': 'pending',
            'runtime': 'pending', 'implementation': 'pending',
            'destination_open': 'pending', 'destination_scene': 'pending',
            'resources': 'pending', 'apply': 'pending',
            'scene_preserved': 'pending', 'source_preserved': 'pending',
            'resource_closure': 'pending', 'role_metadata': 'pending',
            'animation': 'pending', 'candidate_save': 'pending',
            'independent_reopen': 'pending', 'source_unchanged': 'pending',
            'render': 'not_run', 'visual': 'not_reviewed',
        },
        'acceptance': (
            'technical_only; source/target deformed-mesh comparison, manual '
            'post-edit continuation and native SOLID recording remain external'),
        'limitations': [
            'clip_asset.apply_clip is a fixed add-on dependency; this adapter '
            'does not implement or save native clip content itself.',
            'source_witness protects only the pre-existing objects/actions and '
            'Scene settings listed at apply time; it is not a whole-file claim.',
            'A Blender append Library handle is removed only after confirming '
            'it is the declared frozen library and no linked ID remains.',
            'No render, preview, asset display or character assignment is produced.',
            'The independent verifier witnesses saved native structure, Actions, '
            'resources and native role metadata; it is not a visual review.',
            'Exact Action curves/bindings are covered by the reused '
            'animation_workflow.animation_fingerprint; role_witness only checks '
            'role metadata, Action inventory and key totals.',
        ],
    }
    try:
        runtime = _runtime_descriptor()
        report['runtime'] = runtime
        _check_identity(params)
        report['checks']['implementation'] = 'passed'
        report['checks']['runtime'] = 'passed'
        import bpy
        with ExitStack() as stack:
            guards = _guard_inputs(params, stack)
            report['checks']['source_sha256'] = 'passed'
            report['checks']['library_sha256'] = 'passed'
            report['checks']['resources'] = 'passed'
            _open_destination(params)
            report['checks']['destination_open'] = 'passed'
            scene = bpy.context.scene
            if scene is None:
                raise Failure('VALIDATION_FAILED',
                              'Animation reuse destination has no current Scene')
            _check_destination(scene, manifest)
            _refresh_evaluation(scene)
            before_settings = _scene_settings(scene)
            report['scene'] = {'name': scene.name,
                               'before': before_settings, 'after': None}
            report['checks']['destination_scene'] = 'passed'
            closure_before = _resource_closure({'resources': params['resources']})
            resource_before = _resource_witness(closure_before)
            report['resources'] = closure_before
            report['resource_witness'] = resource_before
            atomic_json(job / 'animation-reuse-resource-closure.json',
                        closure_before)
            report['checks']['resource_closure'] = 'passed'
            before_animation = animation_fingerprint()
            source_before = source_witness()
            report['before_animation'] = before_animation
            report['source_witness'] = source_before
            report['source_witness_sha256'] = _sha_value(
                source_before, 'source witness')
            checkpoint = _checkpoint_factory(job, started)
            checkpoint()
            native = _fixed_clip_asset()
            apply_result = _call(
                native.apply_clip, bpy.context, copy.deepcopy(manifest),
                default_code='VALIDATION_FAILED')
            checkpoint()
            apply_result = _plain(apply_result, 'clip_asset.apply_clip result')
            atomic_json(job / 'animation-reuse-apply.json', apply_result)
            apply_summary = _apply_summary(apply_result, manifest)
            report['apply'] = {
                'result': descriptor(job / 'animation-reuse-apply.json'),
                'summary': apply_summary,
            }
            report['checks']['apply'] = 'passed'
            if bpy.context.scene != scene:
                raise Failure('VALIDATION_FAILED',
                              'clip_asset.apply_clip changed the current Scene')
            report['removed_library_handles'] = _drop_loading_libraries(params)
            _refresh_evaluation(scene)
            after_settings = _scene_settings(scene)
            report['scene']['after'] = after_settings
            if _sha_value(after_settings, 'after Scene settings') != _sha_value(
                    before_settings, 'before Scene settings'):
                raise Failure('VALIDATION_FAILED',
                              'clip_asset.apply_clip changed Scene settings')
            report['checks']['scene_preserved'] = 'passed'
            closure_after = _resource_closure({'resources': params['resources']})
            resource_after = _resource_witness(closure_after)
            report['resource_closure_after'] = closure_after
            report['resource_witness_after'] = resource_after
            if _sha_value(resource_after, 'after resources') != _sha_value(
                    resource_before, 'before resources'):
                raise Failure('VALIDATION_FAILED',
                              'clip_asset.apply_clip replaced or removed a resource')
            source_after = source_witness()
            source_errors = compare_source_witness(source_after, source_before)
            if source_errors:
                raise Failure('VALIDATION_FAILED',
                              'clip_asset.apply_clip changed pre-existing source state: '
                              + ', '.join(source_errors[:16]))
            report['source_witness_after_sha256'] = _sha_value(
                source_after, 'source witness after apply')
            report['checks']['source_preserved'] = 'passed'
            after_animation = animation_fingerprint()
            report['after_animation'] = after_animation
            if after_animation['sha256'] == before_animation['sha256']:
                raise Failure('VALIDATION_FAILED',
                              'clip_asset.apply_clip did not add native animation')
            apply_summary['manifest'] = copy.deepcopy(manifest)
            native_witness = role_witness(apply_summary, source_before, scene)
            report['native_witness'] = native_witness
            report['native_witness_sha256'] = _sha_value(
                native_witness, 'native witness')
            atomic_json(job / 'animation-reuse-native-witness.json',
                        native_witness)
            report['native_witness_report'] = descriptor(
                job / 'animation-reuse-native-witness.json')
            report['checks']['role_metadata'] = 'passed'
            report['checks']['animation'] = 'passed'
            checkpoint()
            candidate = _save_candidate(params, job)
            report['candidate'] = candidate
            report['candidate_status'] = 'saved_pending_reopen'
            from filesystem import FileGuard
            candidate_guard = stack.enter_context(FileGuard(candidate['file']))
            if digest(candidate['file']) != candidate['sha256']:
                raise Failure('CONFLICT',
                              'Animation reuse candidate changed before verification')
            report['checks']['candidate_save'] = 'passed'
            expected = _expected_config(params, runtime, before_settings,
                                        source_before, resource_before,
                                        apply_summary, native_witness,
                                        after_animation, candidate)
            expected.pop('output', None)
            atomic_json(job / 'animation-reuse-expected.json', expected)
            report['expected'] = descriptor(job / 'animation-reuse-expected.json')
            config = copy.deepcopy(expected)
            config['output'] = str(job / 'animation-reuse-reopen.json')
            checkpoint()
            verification = _launch_verifier(config, job)
            report['verification'] = verification
            report['reopen'] = descriptor(job / 'animation-reuse-reopen.json')
            report['verification_request'] = descriptor(
                job / 'animation-reuse-verification-request.json')
            if digest(candidate['file']) != candidate['sha256']:
                raise Failure('CONFLICT',
                              'Animation reuse candidate changed during verification')
            for row, _guard in guards.values():
                if digest(row['file']) != row['expected_sha256']:
                    raise Failure('CONFLICT',
                                  'Animation reuse input changed during job: '
                                  + row['file'])
            report['checks']['source_unchanged'] = 'passed'
            report['checks']['independent_reopen'] = 'passed'
            report.update(status='passed', completed_at=time.time(),
                          candidate_status='accepted',
                          seconds=round(time.monotonic() - started, 3))
            atomic_json(job / 'animation-reuse-report.json', report)
            return {
                'status': 'passed',
                'command': 'animation.clip-apply',
                'request_schema': 'animation-native-clip-apply-v1',
                'clip_schema': 'animation-native-clip-v1',
                'clip_id': clip['clip_id'],
                'instance_name': manifest['instance_name'],
                'source': report['source'],
                'library': report['library'],
                'candidate': candidate,
                'candidate_status': 'accepted',
                'implementation_sha256': params['implementation_sha256'],
                'resources': report['resource_witness'],
                'scene': report['scene'],
                'source_witness_scope': SOURCE_SCOPE,
                'source_witness_sha256': report['source_witness_sha256'],
                'apply': {
                    'result': report['apply']['result'],
                    'collection': apply_summary['collection'],
                    'actors': apply_summary['actors'],
                    'lights': apply_summary['lights'],
                    'materials': apply_summary['materials'],
                    'mapping': apply_summary['mapping'],
                    'trim': apply_summary['trim'],
                    'action_counts': apply_summary['action_counts'],
                    'limits': apply_summary['limits'],
                },
                'native_witness': report['native_witness'],
                'native_witness_sha256': report['native_witness_sha256'],
                'verification': verification,
                'checks': report['checks'],
                'report': descriptor(job / 'animation-reuse-report.json'),
                'expected': descriptor(job / 'animation-reuse-expected.json'),
                'acceptance': report['acceptance'],
            }
    except Failure as exc:
        error = {'code': exc.code, 'message': str(exc)}
        isolation = isolate_failed_candidate(job, error)
        report.update(status='failed', error=error, candidate_status='failed',
                      failed_candidate=isolation.get('failed_candidate'),
                      candidate_isolation=isolation,
                      seconds=round(time.monotonic() - started, 3))
        atomic_json(job / 'animation-reuse-report.json', report)
        raise
    except Exception as exc:
        error = {'code': 'WORKER_FAILED', 'message': str(exc)}
        isolation = isolate_failed_candidate(job, error)
        report.update(status='failed', error=error, candidate_status='failed',
                      failed_candidate=isolation.get('failed_candidate'),
                      candidate_isolation=isolation,
                      seconds=round(time.monotonic() - started, 3))
        atomic_json(job / 'animation-reuse-report.json', report)
        raise
