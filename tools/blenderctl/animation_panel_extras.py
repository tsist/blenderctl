"""Native value readback, playback-range import and explicit two-head timing."""

import math
from pathlib import Path

from material_draft_handoff import fingerprint, guard_file, name, read_document, require
from protocol import atomic_json


def signed_json(reference, guards):
    require(type(reference) is dict and reference.keys() == {'file','expected_sha256'}
            and all(type(value) is str for value in reference.values()), 'INVALID_REQUEST', 'Supply a signed JSON reference')
    return read_document(guard_file(reference['file'], guards, reference['expected_sha256']))


def prepare_extra(data, guards):
    operation, params = data['operation'], data['parameters']
    if operation == 'head-apply':
        plan = signed_json(params['plan'], guards)
        require(type(plan) is dict and plan.get('schema') == 'animation.head-panel-plan.v1'
                and plan.get('source') == {'file': data['file'],'sha256': data['expected_sha256']}
                and plan.get('context') == data['context'], 'CONFLICT', 'Head plan belongs to a different saved source or context')
        data['head_plan'] = plan
        params = plan.get('parameters')
    if operation in {'head-plan','head-apply'}:
        fields = {'event','rig_a','rig_b','frame_a','frame_b','delta'}
        require(type(params) is dict and params.keys() == fields and all(name(params[key]) for key in ('event','rig_a','rig_b'))
                and params['rig_a'] != params['rig_b'] and all(type(params[key]) in (int,float) and -1e6 <= params[key] <= 1e6
                and math.isfinite(params[key]) for key in ('frame_a','frame_b','delta')), 'INVALID_REQUEST',
                'Specify two distinct owned rigs, their frames and a shared delta')
        data['head_parameters'] = params
    if operation == 'asset-playback-range':
        manifest = signed_json(params['manifest'], guards)
        fields = {'schema','clip','instance_name','start','end','scale','translation','yaw_quarter_turns'}
        require(type(manifest) is dict and fields <= manifest.keys() <= fields | {'source_window'}
                and manifest['schema'] == 'animation-native-clip-apply-v1' and type(manifest['clip']) is dict
                and name(manifest['instance_name']) and len(manifest['instance_name']) <= 48
                and type(manifest['start']) is int and type(manifest['end']) is int and 1 <= manifest['start'] < manifest['end'] <= 1000000
                and type(manifest['scale']) in (int,float) and .1 <= manifest['scale'] <= 10
                and type(manifest['yaw_quarter_turns']) is int and 0 <= manifest['yaw_quarter_turns'] <= 3, 'INVALID_REQUEST',
                'Supply the native clip-apply manifest with the panel-compatible playback bounds')
        translation = manifest['translation']
        require(type(translation) is list and len(translation) == 3
                and all(type(value) in (int,float) and math.isfinite(value) for value in translation),
                'INVALID_REQUEST', 'Expected three finite translation axes')
        source = manifest['clip'].get('library')
        require(type(source) is dict and source.keys() == {'file','expected_sha256'}
                and all(type(value) is str for value in source.values()), 'INVALID_REQUEST', 'The descriptor needs its signed blend library')
        guard_file(source['file'], guards, source['expected_sha256'])
        data['asset_manifest'] = manifest


def read_channel(bpy, context, ui, parameters, invoke):
    obj = context.view_layer.objects.get(parameters['object'])
    require(obj is not None and context.mode == 'OBJECT', 'CONFLICT', 'Expected a named Object-mode channel owner')
    with bpy.context.temp_override(active_object=obj, object=obj):
        settings = ui._settings(context)
        channel = parameters['channel']
        require(channel in {row[0] for row in ui._channel_items(settings, bpy.context)}, 'INVALID_REQUEST',
                'The panel channel is unavailable for this object')
        settings.channel = channel
        invoke(bpy.ops.animation_curves.read_current_value, settings)
        target, _ = ui._target(bpy.context)
        field = 'rotation_value' if channel.startswith('rotation_') else 'value'
        return {'frame': context.scene.frame_current_final, 'channel': channel, 'target': target, 'panel_field': field,
                'value': getattr(settings, field), 'layout': 'saved_scene_playhead'}


def explicit_heads(bpy, context, clip, ui, data, before):
    from animation_curves_workflow_addon import pose_event, pose_event_store, cross_event_store

    require(not clip.has_record(context.scene) and not ui._event_state(context)['events']
            and not pose_event_store.raw(context) and not cross_event_store.raw(context), 'CONFLICT',
            'A saved event ledger owns this scene; use its event workflow')
    parameters = data['head_parameters']
    rows, _ = pose_event.review(context, parameters)
    participants = [{'object': row['arm'].name, 'instance_uuid': row['arm'].get('ac_instance_uuid'),
                     'action': row['w']['action'].name, 'slot': row['w']['slot'].identifier, 'source_frame': row['frame'],
                     'destination_frame': row['dest'], 'locks': [row['previous'], row['next']],
                     'key': row['key']} for row in rows]
    plan = {'schema': 'animation.head-panel-plan.v1', 'source': {'file': data['file'],'sha256': data['expected_sha256']},
            'context': data['context'], 'parameters': parameters, 'snapshot': fingerprint(before), 'participants': participants}
    if data['operation'] == 'head-plan':
        return plan
    require(plan == data['head_plan'], 'CONFLICT', 'The actual two-head plan differs from the approved review')
    origin = pose_event.origin(context, parameters)
    result = pose_event.execute(context, origin)
    for item, row in zip(participants, rows, strict=True):
        arm = row['arm']
        action, slot = arm.animation_data.action, arm.animation_data.action_slot
        curve = action.layers[0].strips[0].channelbag(slot).fcurves.find(pose_event.PATH, index=1)
        frames = [key.co.x for key in curve.keyframe_points]
        require(action.name != item['action'] and item['source_frame'] not in frames and item['destination_frame'] in frames
                and all(time in frames for time in item['locks']), 'VALIDATION_FAILED', 'Bound head key or locked neighbor differs')
        item['candidate_action'] = action.name
    return {'participants': participants, 'native': result, 'saved_event_ledger': False}


def apply_asset_range(bpy, context, manifest, job):
    from animation_curves_workflow_addon import clip_asset_ui

    clip_asset_ui.register()
    path = job / 'native-clip.json'
    atomic_json(path, manifest['clip'])
    before = set(bpy.data.collections)
    opts = {'filepath': str(path), 'instance_name': manifest['instance_name'], 'start': manifest['start'], 'end': manifest['end'],
            'scale': manifest['scale'], 'translation': manifest['translation'], 'yaw': str(manifest['yaw_quarter_turns']),
            'set_playback_range': True, 'project_source_placement': False}
    if 'source_window' in manifest:
        window = manifest['source_window']
        opts.update(use_source_window=True, source_start=window['start'], source_end=window['end'])
    require(bpy.ops.animation_curves.clip_asset_apply(**opts) == {'FINISHED'}, 'VALIDATION_FAILED', 'Native playback-range import failed')
    scene = context.scene
    ranges = [scene.frame_start, scene.frame_end]
    new = sorted(collection.name for collection in set(bpy.data.collections) - before)
    require(ranges == [manifest['start'], manifest['end']] and len(new) > 0, 'VALIDATION_FAILED',
            'Native clip or its playback interval was not written')
    return {'playback_range': ranges, 'new_collections': new, 'instance_name': manifest['instance_name']}
