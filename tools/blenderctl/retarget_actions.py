# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded, explicit action sets on native control rigs with midpoint quality gates."""
import math
import bpy
from mathutils import Matrix
from inspection import record, value
from protocol import Failure, atomic_json
from retargeting import finite_matrix
from retarget_controls import execute as transfer, matrix_error, channel_values
import drivers
import rigging as r


def check_times(frames):
    return sorted(set(frames + [(a+b)/2 for a,b in zip(frames,frames[1:])]))


def properties(owner):
    """Only transforms and existing numeric custom properties, never arbitrary RNA."""
    result = {}
    for item in (owner, *owner.pose.bones):
        for prop in ('location','rotation_quaternion','rotation_euler','rotation_axis_angle','scale'):
            result[item.path_from_id(prop)] = (item, prop, False)
        for prop in item.keys():
            if type(item[prop]) in (int,float) and math.isfinite(item[prop]):
                path = ('' if item==owner else item.path_from_id()) + '["' + bpy.utils.escape_identifier(prop) + '"]'
                result[path] = (item, prop, True)
    return result


def snapshot(props):
    rows = []
    for item, prop, custom in props.values():
        current = item[prop] if custom else getattr(item,prop)
        rows.append((item,prop,custom,current if custom else list(current)))
    return rows


def restore(rows):
    for item,prop,custom,current in rows:
        if custom:item[prop] = current
        else:setattr(item,prop,current)


def selection(owner, name, identifier, props):
    action = r.action_for(name)
    slot = r.slot_for(action, identifier)
    if action.get(r.POSE_KEY):
        raise Failure('UNSUPPORTED','Captured pose is not a source animation clip')
    if len(action.layers)!=1 or len(action.layers[0].strips)!=1 or action.layers[0].strips[0].type!='KEYFRAME':
        raise Failure('UNSUPPORTED','Action selection requires one layer and one KEYFRAME strip')
    bag = action.layers[0].strips[0].channelbag(slot)
    if not bag or not bag.fcurves:
        raise Failure('UNSUPPORTED','Selected slot has no curves')
    if len(bag.fcurves)>32768 or sum(len(c.keyframe_points)+len(c.sampled_points) for c in bag.fcurves)>500000:
        raise Failure('UNSUPPORTED','Selected action exceeds curve/key budget')
    for curve in bag.fcurves:
        if curve.data_path not in props:
            raise Failure('UNSUPPORTED','Selected action path outside transforms/numeric custom properties: '+curve.data_path)
        item,prop,custom = props[curve.data_path]
        if curve.array_index<0 or curve.array_index >= (1 if custom else len(getattr(item,prop))):
            raise Failure('INVALID_REQUEST','Selected action component outside target channel')
        mode = item.rotation_mode
        if (prop=='rotation_euler' and mode!='XYZ' or prop=='rotation_quaternion' and mode!='QUATERNION' or prop=='rotation_axis_angle' and mode!='AXIS_ANGLE'):
            raise Failure('CONFLICT','Selected action rotation mode differs from rig')
    return action, slot


def binding(owner):
    ad = owner.animation_data
    if not ad:
        raise Failure('UNSUPPORTED','Action sets require existing animation data on both rigs')
    if ad.use_tweak_mode or ad.nla_tracks:
        raise Failure('UNSUPPORTED','Action sets require no NLA tracks or tweak mode on either rig')
    if ad.action and (not ad.action_slot or ad.action_blend_type!='REPLACE' or abs(ad.action_influence-1)>1e-8):
        raise Failure('UNSUPPORTED','Input active actions require an explicit slot and unit REPLACE influence')
    return ad.action, ad.action_slot, ad.action_blend_type, ad.action_influence, ad.action_extrapolation


def bind(owner, action, slot, settings=None):
    ad=owner.animation_data
    ad.action=action
    if slot is not None:ad.action_slot=slot
    ad.action_blend_type,ad.action_influence,ad.action_extrapolation = settings or ('REPLACE',1.,'HOLD')
    if ad.action!=action or action is not None and ad.action_slot!=slot:
        raise Failure('VALIDATION_FAILED','Explicit action slot binding failed')


def local_delta(p):
    finite_matrix(p.matrix)
    if p.parent:
        finite_matrix(p.parent.matrix)
        pose = p.parent.matrix.inverted() @ p.matrix
        rest = p.parent.bone.matrix_local.inverted() @ p.bone.matrix_local
    else:pose,rest=p.matrix,p.bone.matrix_local
    finite_matrix(rest)
    return rest.inverted() @ pose


def evaluate(source,target,op,frames,profile):
    rows=[]
    for frame in frames:
        r.frame_set(frame);drivers.evaluated(profile)
        graph=bpy.context.evaluated_depsgraph_get()
        src,dst=source.evaluated_get(graph),target.evaluated_get(graph)
        finite_matrix(src.matrix_world);finite_matrix(dst.matrix_world)
        for p in src.pose.bones:finite_matrix(p.matrix)
        rows.append({'frame':frame,'source_world':value(src.matrix_world),
            'source_pose':{p.name:value(p.matrix) for p in src.pose.bones},
            'target_world':value(dst.matrix_world),
            'channels':{m['target']:{prop:[v*(op['translation_scale'] if prop=='location' else 1.) for v in channel_values(src.pose.bones[m['source']],prop)] for prop in m['properties']} for m in op['mapping']},
            'witnesses':{w['target']:value(local_delta(src.pose.bones[w['source']])) for w in op['quality']['witnesses']}})
    return rows


def quality(source,target,op,expected,profile):
    maximum_channel=maximum_source=maximum_world=maximum_local=0.
    proof=[];first={};motion={w['target']:0. for w in op['quality']['witnesses']}
    for row in expected:
        r.frame_set(row['frame']);drivers.evaluated(profile)
        graph=bpy.context.evaluated_depsgraph_get();src,dst=source.evaluated_get(graph),target.evaluated_get(graph)
        finite_matrix(src.matrix_world);finite_matrix(dst.matrix_world)
        for p in src.pose.bones:finite_matrix(p.matrix)
        maximum_source=max(maximum_source,matrix_error(src.matrix_world,row['source_world']),
            *(matrix_error(p.matrix,row['source_pose'][p.name]) for p in src.pose.bones))
        maximum_world=max(maximum_world,matrix_error(dst.matrix_world,row['target_world']))
        for name,channels in row['channels'].items():
            for prop,values in channels.items():
                actual=channel_values(dst.pose.bones[name],prop)
                maximum_channel=max(maximum_channel,*(abs(a-b) for a,b in zip(actual,values)))
        witnesses={}
        for w in op['quality']['witnesses']:
            name=w['target'];actual=local_delta(dst.pose.bones[name]);wanted=row['witnesses'][name]
            # Translation uses the same explicit scale as transferred channels.
            wanted=Matrix(wanted);wanted.translation*=op['translation_scale']
            maximum_local=max(maximum_local,matrix_error(actual,wanted))
            first.setdefault(name,actual.copy())
            motion[name]=max(motion[name],matrix_error(actual,first[name]))
            witnesses[name]={'source':w['source'],'domain':w['domain'],'local_delta':value(actual),'matrix_error':matrix_error(actual,wanted)}
        proof.append({'frame':row['frame'],'witnesses':witnesses})
    result={'maximum_channel_error':maximum_channel,'maximum_source_pose_error':maximum_source,
        'maximum_kept_world_error':maximum_world,'maximum_local_matrix_error':maximum_local,
        'witness_local_motion':motion,'samples':proof}
    passed=max(maximum_channel,maximum_source,maximum_world)<=op['max_matrix_error'] and maximum_local<=op['quality']['max_local_matrix_error']
    passed=passed and all(motion[w['target']]>=w['min_motion'] for w in op['quality']['witnesses'])
    return result,passed


def execute(op,context,driver_profile=None,job=None):
    source,target=r.object_for(op['source']),r.object_for(op['target'])
    for rig in (source,target):
        if rig.type!='ARMATURE' or not 1<=len(rig.data.bones)<=2048:
            raise Failure('UNSUPPORTED','Action sets require 1..2048 bones per rig')
        r.local(rig.data)
        if rig.name not in bpy.context.view_layer.objects:raise Failure('NOT_FOUND','Rig outside active view layer')
    if source==target:raise Failure('CONFLICT','Source and target must differ')
    total=len(source.data.bones)+len(target.data.bones)
    frame_passes=sum(2*len(c['frames'])+2*len(check_times(c['frames'])) for c in op['clips'])
    budget={'total_bones':total,'frame_passes':frame_passes,'bone_frame_samples':total*frame_passes,
        'declared':op['budget'],'semantics':'conservative rig bone/frame visits; not dependency graph CPU, memory or simulation cost'}
    if job:atomic_json(job/'action-set-progress.json',{'phase':'preflight','budget':budget})
    if total>op['budget']['max_total_bones'] or total*frame_passes>op['budget']['max_bone_frame_samples']:
        raise Failure('UNSUPPORTED','Declared action-set bone/frame budget exceeded before creating actions')
    source_binding=binding(source);binding(target)
    props_s,props_t=properties(source),properties(target)
    base=op['target_baseline'];baseline=selection(target,base['action'],base['slot'],props_t)
    if len(baseline[0].slots)!=1:raise Failure('UNSUPPORTED','Target baseline requires one slot; source clips may select among multiple slots')
    selected=[selection(source,c['source_action'],c['source_slot'],props_s) for c in op['clips']]
    for clip in op['clips']:r.scenes.fresh(bpy.data.actions,clip['name'])
    if len({m['source'] for m in op['mapping']})!=len(op['mapping']) or len({m['target'] for m in op['mapping']})!=len(op['mapping']):
        raise Failure('INVALID_REQUEST','Control mapping must be one-to-one')
    for m in op['mapping']:
        if m['source'] not in source.pose.bones or m['target'] not in target.pose.bones:raise Failure('NOT_FOUND','Mapped control missing')
        for prop in m['properties']:
            channel_values(source.pose.bones[m['source']],prop);channel_values(target.pose.bones[m['target']],prop)
    for w in op['quality']['witnesses']:
        if w['source'] not in source.pose.bones or w['target'] not in target.pose.bones:
            raise Failure('NOT_FOUND','Quality witness bone missing')
    for rig,key in ((source,'source_rest_sha256'),(target,'target_rest_sha256')):
        if r.rest_signature(rig,list(rig.data.bones.keys()))!=op[key]:raise Failure('CONFLICT','Action-set rest signature changed')
    original={a.name:record(a)['action'] for a in bpy.data.actions}
    inventory=drivers.sha(drivers.inventory()[0])
    source_values,target_values=snapshot(props_s),snapshot(props_t)
    reports=[];outputs={}
    try:
        for index,(clip,choice) in enumerate(zip(op['clips'],selected)):
            # Unkeyed properties must not inherit values left by a previous clip.
            bind(source,None,None);bind(target,None,None)
            restore(source_values);restore(target_values)
            bind(source,*choice);bind(target,*baseline)
            times=check_times(clip['frames'])
            if job:atomic_json(job/'action-set-progress.json',{'phase':'sampling','clip_index':index,'action':clip['name'],'budget':budget})
            expected=evaluate(source,target,op,times,driver_profile)
            single={k:op[k] for k in ('op','source','target','source_rest_sha256','target_rest_sha256','mapping','translation_scale','root_motion','replace','max_matrix_error')}
            single.update(adapter='CONTROL_CHANNELS_V1',name=clip['name'],frames=clip['frames'],witness_bones=[w['target'] for w in op['quality']['witnesses']])
            transfer(single,context,driver_profile)
            outputs[clip['name']]=(target.animation_data.action,target.animation_data.action_slot)
            if job:atomic_json(job/'action-set-progress.json',{'phase':'quality','clip_index':index,'action':clip['name'],'budget':budget})
            observed,passed=quality(source,target,op,expected,driver_profile)
            reports.append({'source_action':choice[0].name,'source_slot':choice[1].identifier,'action':clip['name'],
                'slot':outputs[clip['name']][1].identifier,'frames':clip['frames'],'check_frames':times,'quality':observed,'passed':passed})
            if job:atomic_json(job/'action-set-quality.json',{'adapter':op['adapter'],'clips':reports,'budget':budget})
            if not passed:raise Failure('VALIDATION_FAILED','Action-set midpoint/local-motion quality gate failed: '+clip['name'])
        bind(source,None,None);restore(source_values)
        bind(source,source_binding[0],source_binding[1],source_binding[2:])
        bind(target,None,None);restore(target_values)
        bind(target,*outputs[op['active_action']]);r.frame_set(context['frame'])
        if any(record(bpy.data.actions[n])['action']!=v for n,v in original.items()):
            raise Failure('VALIDATION_FAILED','An original action changed during action-set transfer')
        if drivers.sha(drivers.inventory()[0])!=inventory:raise Failure('VALIDATION_FAILED','Driver inventory changed')
        if job:atomic_json(job/'action-set-progress.json',{'phase':'validated_in_memory','clips':len(reports),'budget':budget})
        return {'adapter':op['adapter'],'source':source.name,'target':target.name,'active_action':op['active_action'],
            'clips':reports,'budget':budget,'source_binding_restored':True,'original_actions_unchanged':True,'driver_inventory_unchanged':True,
            'limits':'explicit native controls, KEEP world and single-slot target baseline; sparse keys must pass every interval midpoint; unkeyed values start at input context; selected local witnesses only, no continuous-time, mesh, cloth, foot-lock or export interpolation guarantee'}
    finally:
        # Also restore the input binding if validation fails; no failed candidate is saved.
        bind(source,None,None);restore(source_values)
        bind(source,source_binding[0],source_binding[1],source_binding[2:])
