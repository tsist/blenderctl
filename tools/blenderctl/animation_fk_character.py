"""Saved-file adapter for the same FK sample characters and native pose diagnosis as the panel."""
import json
import math
import os
from pathlib import Path

from material_draft_handoff import fingerprint, guard_file, name, open_saved_context, read_document, require
from protocol import Failure, digest, read_json


def prepare_character(args, guards):
    params = read_document(guard_file(args.params.absolute(), guards))
    common = {'file', 'expected_sha256', 'context', 'operation'}
    target = {'rig', 'instance_uuid'}
    actions = {'create':{'kind', 'height_m'}, 'inspect':target, 'knees':target,
               'practice':target | {'frames', 'knee_start', 'knee_peak'}}
    require(isinstance(params, dict) and isinstance(params.get('operation'), str) and params['operation'] in actions,
            'INVALID_REQUEST', 'Select a character operation')
    op = params['operation']
    require(set(params) == common | actions[op], 'INVALID_REQUEST', 'Unexpected or missing character parameters')
    ctx = params['context']
    require(isinstance(ctx, dict) and set(ctx) == {'scene','view_layer','frame'} and name(ctx['scene'])
            and name(ctx['view_layer']) and type(ctx['frame']) is int, 'INVALID_REQUEST', 'Specify saved scene, layer and frame')
    require(isinstance(params['file'], str) and isinstance(params['expected_sha256'], str), 'INVALID_REQUEST',
            'Supply a SHA-bound saved project')
    source = guard_file(params['file'], guards, params['expected_sha256'])
    require(source.suffix.lower() == '.blend', 'INVALID_REQUEST', 'Expected a .blend working copy')
    if op == 'create':
        require(params['kind'] in ('cylinder_v1','humanoid_fk_two_segment_v2')
                and type(params['height_m']) in (int,float) and 0.3 <= params['height_m'] <= 3.0,
                'INVALID_REQUEST', 'Specify a supported type and height between 0.3 and 3.0 meters')
    else:
        require(name(params['rig']) and isinstance(params['instance_uuid'], str) and len(params['instance_uuid']) == 32,
                'INVALID_REQUEST', 'Expected a named rig and the UUID returned from create')
    if op == 'practice':
        frames = params['frames']
        require(isinstance(frames, list) and len(frames) == 3
                and all(type(f) in (float,int) and math.isfinite(f) for f in frames) and frames[0] < frames[1] < frames[2]
                and all(type(params[k]) in (float,int) and 0 <= params[k] <= 150 for k in ('knee_start','knee_peak')),
                'INVALID_REQUEST', 'Expected three increasing frames and knee angles in 0..150 degrees')
    return {**params, 'file':str(source), 'command':'fk-character', 'edition':args.edition}


def observe(bpy, context, rig, uid):
    from animation_curves_workflow_addon.pose_channel import action_witness
    groups = [c for c in rig.users_collection if c.get('ac_instance_uuid') == uid]
    require(len(groups) == 1, 'CONFLICT', 'Owned collection is absent or ambiguous')
    collection = groups[0]
    meta = json.loads(collection['ac_template_metadata'])
    require(meta['instance_uuid'] == uid, 'CONFLICT', 'Collection metadata belongs to another character')
    parts = {}
    for obj in collection.objects:
        if obj.type == 'MESH':
            parts[obj.name] = {'vertices':len(obj.data.vertices), 'polygons':len(obj.data.polygons),
                               'armature':any(m.type == 'ARMATURE' and m.object == rig for m in obj.modifiers)}
    ad = rig.animation_data
    action = ad.action if ad else None
    return {'rig':rig.name, 'instance_uuid':uid, 'collection':collection.name, 'kind':meta['template_id'],
            'height_m':meta['height_m'], 'bones':[bone.name for bone in rig.data.bones], 'parts':parts,
            'action':action.name if action else None, 'curves_sha256':fingerprint(action_witness(action)) if action else None,
            'active':context.view_layer.objects.active.name if context.view_layer.objects.active else None,
            'selected':sorted(obj.name for obj in context.selected_objects)}


def work_character(phase, data, job):
    import bpy
    from animation_author import load_author
    from animation_event_store import dependencies

    edition = data.get('edition', '0.5.0')
    load_author(bpy, edition, allow_53=True)
    from animation_curves_workflow_addon import knee_check, practice_writer, templates_native
    from inspection import all_ids, identity
    runtime = {'blender':bpy.app.version_string, 'build':bpy.app.build_hash.decode(), 'edition':edition}
    writing = phase == 'author' and data['operation'] in ('create','practice')
    file = data['stage'] if phase == 'verify' else data['file']
    context = open_saved_context(bpy, {**data,'file':file})
    scene = context.scene
    require(context.mode == 'OBJECT' and not scene.library, 'UNSUPPORTED', 'Expected a local scene in Object mode')
    timing = {'fps':scene.render.fps, 'fps_base':scene.render.fps_base, 'start':scene.frame_start, 'end':scene.frame_end}
    deps = dependencies(bpy)
    active = context.view_layer.objects.active
    selection = sorted(o.name for o in context.selected_objects)
    original = list(all_ids()) if writing else []
    if writing and data['operation'] == 'create':
        new = templates_native.create_template(context, data['kind'], {'height_m':data['height_m']})
        rig, uid = new['armature'], new['instance_uuid']
    else:
        target = data
        if phase == 'verify' and data['operation'] == 'create':
            target = read_json(job / 'author-result.json')['data']['record']
        uid = target['instance_uuid']
        rig = context.view_layer.objects.get(target['rig'])
        require(rig and rig.type == 'ARMATURE' and rig.get('ac_instance_uuid') == uid
                and rig.get('ac_template_owner') == templates_native.OWNER and not rig.library,
                'CONFLICT', 'Named rig or native instance identity differs in the saved scene')
    if writing and data['operation'] == 'practice':
        require(not rig.animation_data or not rig.animation_data.action, 'CONFLICT',
                'This rig already has an Action. Practice requires an unbound rig in its rest pose.')
        practice_writer.create(bpy, rig.name, uid, data['frames'], data['knee_start'], data['knee_peak'])
    record = observe(bpy, context, rig, uid)
    require(context.view_layer.objects.active == active and record['selected'] == selection, 'VALIDATION_FAILED',
            'Character operation changed the active object or selection')
    if data['operation'] == 'knees':
        for obj in context.selected_objects:
            obj.select_set(False)
        context.view_layer.objects.active = rig
        rig.select_set(True)
        record['knees'] = knee_check.inspect(context)
        require(record['knees']['status'] == 'MEASURED', 'UNSUPPORTED', record['knees'].get('reason','Knees unavailable'))
    retained = []
    if writing:
        for item in original:
            if not item.library and not item.is_embedded_data and item.users == 0 and not item.use_fake_user:
                item.use_fake_user = True
                retained.append(identity(item))
        bpy.context.preferences.filepaths.save_version = 0
        require(bpy.ops.wm.save_as_mainfile(filepath=data['stage'], copy=True, relative_remap=True, check_existing=False)
                == {'FINISHED'}, 'IO_ERROR', 'Could not save FK candidate')
    return {'record':record, 'timing':timing, 'dependencies':deps, 'worker_pid':os.getpid(),
             'retained_orphans':retained, 'runtime':runtime}
