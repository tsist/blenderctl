# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit native control channels; preserve the rig graph, never invert arbitrary IK."""
import math
import bpy
from mathutils import Matrix, Quaternion
from inspection import curve_content, record, value
from protocol import Failure
from retargeting import finite_matrix
import drivers
import rigging as r

PROPERTIES = ('location', 'rotation_quaternion', 'scale')


def channel_values(item, prop):
    if prop == 'rotation_quaternion' and item.rotation_mode != 'QUATERNION':
        raise Failure('UNSUPPORTED', 'Control rotation channels require existing QUATERNION mode: ' + item.name)
    values = list(getattr(item, prop))
    if any(not math.isfinite(v) or abs(v) > 1e6 for v in values):
        raise Failure('UNSUPPORTED', 'Non-finite or excessive control channel value')
    if prop == 'rotation_quaternion' and Quaternion(values).magnitude < 1e-8:
        raise Failure('UNSUPPORTED', 'Zero control quaternion')
    return values


def matrix_error(a, b):
    return max(abs(x - y) for row_a, row_b in zip(a, b) for x, y in zip(row_a, row_b))


def source_sample(source):
    evaluated = source.evaluated_get(bpy.context.evaluated_depsgraph_get())
    finite_matrix(evaluated.matrix_world)
    poses = {}
    for bone in evaluated.pose.bones:
        finite_matrix(bone.matrix)
        poses[bone.name] = value(bone.matrix)
    return evaluated, {'world': value(evaluated.matrix_world), 'poses': poses}


def action_curves(action, slot, exclude):
    if not action:
        return []
    bag = action.layers[0].strips[0].channelbag(slot)
    return sorted([curve_content(c) for c in bag.fcurves if c.data_path not in exclude],
                  key=lambda c: (c['path'], c['index'])) if bag else []


def execute(op, context, driver_profile=None):
    source, target = r.object_for(op['source']), r.object_for(op['target'])
    for rig in (source, target):
        if rig.type != 'ARMATURE' or not 1 <= len(rig.data.bones) <= 2048:
            raise Failure('UNSUPPORTED', 'Control transfer requires 1..2048 bones')
        r.local(rig.data)
        if rig.name not in bpy.context.view_layer.objects:
            raise Failure('NOT_FOUND', 'Rig outside active view layer: ' + rig.name)
    if source == target:
        raise Failure('CONFLICT', 'Source and target must differ')
    for rig, key in ((source, 'source_rest_sha256'), (target, 'target_rest_sha256')):
        if r.rest_signature(rig, list(rig.data.bones.keys())) != op[key]:
            raise Failure('CONFLICT', 'Control rig rest signature changed: ' + rig.name)
    inventory_before = drivers.sha(drivers.inventory()[0])
    if driver_profile is None and any(getattr(o, 'animation_data', None) and o.animation_data.drivers for o, _ in drivers.owners()):
        raise Failure('UNSUPPORTED', 'Control transfer with drivers requires a source-bound driver profile')

    mapping = op['mapping']
    if len({m['target'] for m in mapping}) != len(mapping) or len({m['source'] for m in mapping}) != len(mapping):
        raise Failure('INVALID_REQUEST', 'Control mapping must be one-to-one')
    writable = {}
    for item in mapping:
        if item['source'] not in source.pose.bones or item['target'] not in target.pose.bones:
            raise Failure('NOT_FOUND', 'Mapped control bone is missing')
        for prop in item['properties']:
            channel_values(source.pose.bones[item['source']], prop)
            channel_values(target.pose.bones[item['target']], prop)
            writable[target.pose.bones[item['target']].path_from_id(prop)] = len(getattr(target.pose.bones[item['target']], prop))
    for name in op['witness_bones']:
        if name not in target.pose.bones:
            raise Failure('NOT_FOUND', 'Missing target witness bone: ' + name)

    root = op['root_motion'] == 'SOURCE_WORLD_DELTA'
    if root:
        if target.constraints or (target.parent and target.parent_type != 'OBJECT'):
            raise Failure('UNSUPPORTED', 'World root transfer supports an unconstrained object with ordinary OBJECT parenting')
        if any(abs(v) > 1e-8 for v in (*target.delta_location, *target.delta_rotation_euler)) or any(abs(v-1) > 1e-8 for v in target.delta_scale) or tuple(target.delta_rotation_quaternion) != (1., 0., 0., 0.):
            raise Failure('UNSUPPORTED', 'World root transfer requires identity target delta transforms')
        for prop in PROPERTIES:
            channel_values(target, prop)
            writable[target.path_from_id(prop)] = len(getattr(target, prop))
    ad = target.animation_data
    if ad and (ad.use_tweak_mode or ad.nla_tracks):
        raise Failure('UNSUPPORTED', 'Target NLA/tweak mode needs a separate action composition adapter')
    if ad and any(c.data_path in writable for c in ad.drivers):
        raise Failure('CONFLICT', 'A driver owns a requested target control/root channel')
    old_action, old_slot = (ad.action, ad.action_slot) if ad else (None, None)
    if old_action:
        if not op['replace']:
            raise Failure('CONFLICT', 'Active action replacement requires replace=true; a preserving copy will be made')
        r.local(old_action)
        if old_action.get(r.POSE_KEY) or old_action.asset_data or old_action.get('asset_id'):
            raise Failure('UNSUPPORTED', 'Captured poses/action assets require an explicit asset copy workflow')
        if not old_slot or len(old_action.slots) != 1 or len(old_action.layers) != 1 or len(old_action.layers[0].strips) != 1 or old_action.layers[0].strips[0].type != 'KEYFRAME':
            raise Failure('UNSUPPORTED', 'Target action requires one OBJECT slot, one layer and one KEYFRAME strip')
        if old_slot.target_id_type != 'OBJECT' or ad.action_blend_type != 'REPLACE' or abs(ad.action_influence-1.) > 1e-8:
            raise Failure('UNSUPPORTED', 'Target action must have unit REPLACE influence')
    r.scenes.fresh(bpy.data.actions, op['name'])
    old_contents = {a.name: record(a)['action'] for a in bpy.data.actions}
    preserved = action_curves(old_action, old_slot, writable)

    samples = []
    source_origin = target_origin = None
    for frame in op['frames']:
        r.frame_set(frame)
        drivers.evaluated(driver_profile)
        evaluated, original_source = source_sample(source)
        target_e = target.evaluated_get(bpy.context.evaluated_depsgraph_get())
        curves = {}
        for item in mapping:
            for prop in item['properties']:
                values = channel_values(evaluated.pose.bones[item['source']], prop)
                if prop == 'location':
                    values = [v * op['translation_scale'] for v in values]
                curves[target.pose.bones[item['target']].path_from_id(prop)] = values
        desired_world = None
        if root:
            source_world = evaluated.matrix_world.copy()
            if source_origin is None:
                source_origin, target_origin = source_world.copy(), target_e.matrix_world.copy()
                finite_matrix(target_origin)
            delta = source_origin.inverted() @ source_world
            delta.translation *= op['translation_scale']
            desired_world = target_origin @ delta
            finite_matrix(target_e.matrix_basis)
            # W = parent_world * parent_inverse * basis. Recover the evaluated
            # parent factor, including the animated ordinary parent and inverse.
            parent_factor = target_e.matrix_world @ target_e.matrix_basis.inverted()
            finite_matrix(parent_factor)
            basis = parent_factor.inverted() @ desired_world
            finite_matrix(basis)
            transform = r.matrix_transform(basis)  # rejects unrepresentable shear
            curves.update(transform)
        samples.append({'frame': frame, 'curves': curves, 'world': value(desired_world) if root else None,
                        'source': original_source})

    # Clone the existing action instead of sampling/replacing unrelated channels.
    if old_action:
        action = old_action.copy()
        action.name = op['name']
        slot = next(s for s in action.slots if s.identifier == old_slot.identifier)
    else:
        action, slot = r.new_action(op['name'], target.name)
    if action.name != op['name']:
        raise Failure('CONFLICT', 'Blender changed requested control action name')
    action.use_fake_user = True
    ad = target.animation_data_create()
    ad.action, ad.action_slot = action, slot
    if not old_action:
        ad.action_blend_type, ad.action_influence, ad.action_extrapolation = 'REPLACE', 1., 'HOLD'
    for path, size in writable.items():
        for index in range(size):
            r.curve_write(action, slot, path, index,
                          [{'frame': row['frame'], 'value': row['curves'][path][index]} for row in samples], True)
    r.update()
    if action_curves(action, slot, writable) != preserved:
        raise Failure('VALIDATION_FAILED', 'Unmapped target action channels changed')
    for name, contents in old_contents.items():
        if record(bpy.data.actions[name])['action'] != contents:
            raise Failure('VALIDATION_FAILED', 'An original action changed: ' + name)

    maximum_channel = maximum_root = maximum_source = 0.
    proof = []
    for row in samples:
        r.frame_set(row['frame'])
        drivers.evaluated(driver_profile)
        _, original_source = source_sample(source)
        maximum_source = max(maximum_source, matrix_error(row['source']['world'], original_source['world']),
                             *(matrix_error(row['source']['poses'][n], m) for n, m in original_source['poses'].items()))
        evaluated = target.evaluated_get(bpy.context.evaluated_depsgraph_get())
        for path, expected in row['curves'].items():
            actual = evaluated.path_resolve(path)
            if any(not math.isfinite(v) for v in actual):
                raise Failure('VALIDATION_FAILED', 'Evaluated target channel is non-finite')
            maximum_channel = max(maximum_channel, *(abs(a-b) for a, b in zip(actual, expected)))
        finite_matrix(evaluated.matrix_world)
        if root:
            maximum_root = max(maximum_root, matrix_error(evaluated.matrix_world, row['world']))
        witnesses = {}
        for name in op['witness_bones']:
            matrix = evaluated.pose.bones[name].matrix
            finite_matrix(matrix)
            witnesses[name] = value(matrix)
        proof.append({'frame': row['frame'], 'world': value(evaluated.matrix_world), 'witness_pose_matrices': witnesses})
    if max(maximum_channel, maximum_root, maximum_source) > op['max_matrix_error']:
        raise Failure('VALIDATION_FAILED', 'Control/root transfer or source preservation exceeded declared error')
    if drivers.sha(drivers.inventory()[0]) != inventory_before:
        raise Failure('VALIDATION_FAILED', 'Driver inventory changed during control transfer')
    if r.rest_signature(target, list(target.data.bones.keys())) != op['target_rest_sha256']:
        raise Failure('VALIDATION_FAILED', 'Target rest data changed')
    return {'adapter': op['adapter'], 'source': source.name, 'target': target.name, 'action': action.name,
            'mapping': mapping, 'frames': op['frames'], 'root_motion': op['root_motion'],
            'maximum_channel_error': maximum_channel, 'maximum_root_world_error': maximum_root,
            'maximum_source_pose_error': maximum_source, 'source_unchanged': True,
            'driver_inventory_unchanged': True, 'unmapped_action_curves_unchanged': True, 'samples': proof,
            'semantics': 'explicit native pre-constraint channels; location scaled; rig constraints/drivers remain active; root world delta relative to first sampled frame',
            'limits': 'caller declares semantically corresponding controls; no IK inversion or rest-axis conversion; finite witness poses do not prove desired deformation; sampled frames only'}
