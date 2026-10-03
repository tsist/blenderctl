# SPDX-License-Identifier: GPL-3.0-or-later
"""Declarative local rigs, explicit weights and layered animation candidates."""
import json,math
from pathlib import Path
import bpy
from mathutils import Matrix,Quaternion
from protocol import Failure,atomic_json,digest
from inspection import all_ids,identity,value,record
from rig_contract import normalize,MODEL_OPS,SCENE_OPS,SAMPLE,validate
import scenes,modeling,nodes

POSE_KEY='blenderctl_pose_v1'
def local(item):
    if item.library or item.override_library:raise Failure('UNSUPPORTED','Rig edits require local non-override data')
    return item
def object_for(name):return local(scenes.find(bpy.data.objects,name,True))
def rig_for(name):
    o=object_for(name)
    if o.type!='ARMATURE':raise Failure('INVALID_REQUEST','Armature object required')
    local(o.data)
    if len(o.data.bones)>256:raise Failure('UNSUPPORTED','Rig exceeds 256 bones')
    return o
def pose_for(rig,bone):return scenes.find(rig_for(rig).pose.bones,bone)
def scope(item,policy):
    local(item)
    if policy=='reject_shared' and item.users-int(item.use_fake_user)>1:raise Failure('CONFLICT','Shared data requires explicit shared scope or a copy')
def update():
    for o in bpy.data.objects:o.update_tag()
    scenes.update()
def frame_set(frame):
    base=math.floor(frame)
    bpy.context.scene.frame_set(base,subframe=frame-base);update()
def channel(ref):
    owner=object_for(ref['object']);item=pose_for(owner.name,ref['bone']) if 'bone' in ref else owner
    prop=ref['property'];index=ref['component']
    if index>=len(getattr(item,prop)):raise Failure('INVALID_REQUEST','Invalid channel component')
    if prop=='rotation_euler' and item.rotation_mode!='XYZ':raise Failure('UNSUPPORTED','Euler channels require XYZ rotation mode')
    if prop=='rotation_quaternion' and item.rotation_mode!='QUATERNION':raise Failure('UNSUPPORTED','Quaternion channels require QUATERNION mode')
    return owner,item,item.path_from_id(prop),index
def action_for(name,policy=None):
    a=local(scenes.find(bpy.data.actions,name))
    if policy:
        scope(a,policy)
        if a.get(POSE_KEY):raise Failure('UNSUPPORTED','Captured poses are immutable; capture a new pose to change them')
    return a
def slot_for(action,name):
    s=next((s for s in action.slots if s.identifier==name),None)
    if not s:raise Failure('NOT_FOUND','Action slot identifier missing: '+name)
    if s.target_id_type!='OBJECT':raise Failure('UNSUPPORTED','Only OBJECT action slots supported')
    return s
def bag_for(action,slot):
    if len(action.layers)!=1 or len(action.layers[0].strips)!=1 or action.layers[0].strips[0].type!='KEYFRAME':raise Failure('UNSUPPORTED','Action edits require one layer and one KEYFRAME strip')
    return action.layers[0].strips[0].channelbag(slot,ensure=True)
def new_action(name,slot_name):
    scenes.fresh(bpy.data.actions,name);a=scenes.named(bpy.data.actions.new(name),name);s=a.slots.new(id_type='OBJECT',name=slot_name);layer=a.layers.new('Animation');layer.strips.new(type='KEYFRAME');a.use_fake_user=True
    if s.name_display!=slot_name:raise Failure('CONFLICT','Blender changed requested slot name')
    return a,s
def assign(owner,action,slot,replace):
    validate_action_target(owner,action,slot)
    ad=owner.animation_data_create()
    if ad.use_tweak_mode:raise Failure('UNSUPPORTED','NLA tweak mode must be exited before editing')
    if ad.action and not replace:raise Failure('CONFLICT','Active action replacement requires replace=true')
    ad.action=action;ad.action_slot=slot;ad.action_blend_type='REPLACE';ad.action_influence=1;ad.action_extrapolation='HOLD'
    if ad.action!=action or ad.action_slot!=slot:raise Failure('VALIDATION_FAILED','Action slot assignment failed')
def validate_action_target(owner,action,slot):
    if len(action.layers)!=1 or len(action.layers[0].strips)!=1:raise Failure('UNSUPPORTED','Action binding supports one layer and strip')
    bag=action.layers[0].strips[0].channelbag(slot)
    if not bag:return
    items=[owner,*(list(owner.pose.bones) if owner.pose else [])]
    allowed={item.path_from_id(prop):(item,prop) for item in items for prop in ('location','rotation_euler','rotation_quaternion','scale')}
    for c in bag.fcurves:
        if c.data_path not in allowed:raise Failure('UNSUPPORTED','Action path not supported by target object: '+c.data_path)
        item,prop=allowed[c.data_path]
        if c.array_index>=len(getattr(item,prop)):raise Failure('INVALID_REQUEST','Action curve component outside target range')
        if prop=='rotation_euler' and item.rotation_mode!='XYZ' or prop=='rotation_quaternion' and item.rotation_mode!='QUATERNION':raise Failure('CONFLICT','Action rotation mode differs from target')
def channel_animated(owner,path,index):
    ad=owner.animation_data
    if not ad:return False
    bindings=[(ad.action,ad.action_slot)] if ad.action else []
    bindings.extend((s.action,s.action_slot) for t in ad.nla_tracks for s in t.strips if s.action)
    for action,slot in bindings:
        if not slot:continue
        for layer in action.layers:
            for strip in layer.strips:
                if strip.type=='KEYFRAME':
                    bag=strip.channelbag(slot)
                    if bag and bag.fcurves.find(path,index=index):return True
    return False
def curve_write(action,slot,path,index,keys,replace):
    bag=bag_for(action,slot);c=bag.fcurves.find(path,index=index)
    if c:
        if not replace:raise Failure('CONFLICT','Curve exists; explicit replacement required')
        bag.fcurves.remove(c)
    c=bag.fcurves.new(data_path=path,index=index)
    for k in keys:
        p=c.keyframe_points.insert(k['frame'],k['value'],options={'FAST'});p.interpolation=k.get('interpolation','LINEAR');p.handle_left_type=p.handle_right_type='AUTO_CLAMPED'
    c.update();return c
def static_pose_owner(o,bone=None):
    ad=o.animation_data
    if ad and (ad.action or ad.nla_tracks or ad.drivers):raise Failure('CONFLICT','Direct pose edits require no active action, NLA or drivers')
    if bone and bone.constraints:raise Failure('CONFLICT','Direct pose edits require an unconstrained bone')
def transform_set(item,transform):
    q=Quaternion(transform['rotation_quaternion'])
    if abs(q.magnitude-1)>1e-5:raise Failure('INVALID_REQUEST','Quaternion must be normalized')
    item.rotation_mode='QUATERNION'
    for k,v in transform.items():nodes.checked(item,k,v)
def mesh_for(op):
    o=object_for(op['object'])
    if o.type!='MESH':raise Failure('INVALID_REQUEST','Mesh object required')
    local(o.data);modeling.bounded(o.data)
    if o.data.shape_keys:raise Failure('UNSUPPORTED','Weight authoring on shape-key meshes requires separate validation')
    if o.data.users>1:
        if op['data_scope']=='reject_shared':raise Failure('CONFLICT','Weight storage is shared with another mesh user')
        if o.data.asset_data or o.data.get('asset_id'):raise Failure('UNSUPPORTED','Cannot duplicate mesh asset identity')
        o.data=o.data.copy()
    return o
def weights_report(o,rig):
    deform={b.name for b in rig.data.bones if b.use_deform};groups={g.index:g.name for g in o.vertex_groups};sums=[];maximum=0;invalid=0
    for v in o.data.vertices:
        ws=[g.weight for g in v.groups if groups.get(g.group) in deform];sums.append(sum(ws));maximum=max(maximum,len([w for w in ws if w>0]));invalid+=sum(not math.isfinite(w) or w<0 or w>1 for w in ws)
    return {'vertices':len(sums),'unweighted':sum(s<1e-8 for s in sums),'not_normalized':sum(abs(s-1)>1e-4 for s in sums),'sum_min':min(sums,default=0),'sum_max':max(sums,default=0),'max_influences':maximum,'invalid_weights':invalid}
def dep_node(o,bone=None):return (o.as_pointer(),bone)
def dependency_graph(extra=None):
    graph={}
    for o in bpy.data.objects:
        key=dep_node(o);graph.setdefault(key,[]).extend([dep_node(o.parent,o.parent_bone if o.parent_type=='BONE' else None)] if o.parent else [])
        for owner,bone in [(o,None),*([(b,b.name) for b in o.pose.bones] if o.pose else [])]:
            k=dep_node(o,bone)
            if bone:graph.setdefault(k,[]).extend([dep_node(o),*([dep_node(o,owner.parent.name)] if owner.parent else [])])
            for c in owner.constraints:
                if getattr(c,'target',None):graph[k].append(dep_node(c.target,getattr(c,'subtarget',None) or None))
                if getattr(c,'pole_target',None):graph[k].append(dep_node(c.pole_target,getattr(c,'pole_subtarget',None) or None))
                if bone and c.type=='IK':
                    parent=owner.parent;count=1
                    while parent and (c.chain_count==0 or count<c.chain_count):
                        ancestor=graph.setdefault(dep_node(o,parent.name),[])
                        if c.target:ancestor.append(dep_node(c.target,c.subtarget or None))
                        if c.pole_target:ancestor.append(dep_node(c.pole_target,c.pole_subtarget or None))
                        parent=parent.parent;count+=1
        for m in o.modifiers:
            if m.type=='ARMATURE' and m.object:graph[key].extend([dep_node(m.object),*[dep_node(m.object,b.name) for b in m.object.data.bones]])
        if o.animation_data:
            for c in o.animation_data.drivers:
                bone=next((b.name for b in o.pose.bones if c.data_path.startswith(b.path_from_id()+'.')),None) if o.pose else None
                for v in c.driver.variables:
                    for t in v.targets:
                        if isinstance(t.id,bpy.types.Object):
                            b=next((b.name for b in t.id.pose.bones if t.data_path.startswith(b.path_from_id()+'.')),None) if t.id.pose else None
                            graph.setdefault(dep_node(o,bone),[]).append(dep_node(t.id,b))
    if extra:
        for a,b in extra:graph.setdefault(a,[]).append(b)
    nodes.acyclic(graph)
def animation_safe(profile=None,source=None,job=None):
    import drivers
    if profile is not None:
        drivers.check(profile,source,job)
    for item,_ in drivers.owners():
        ad=getattr(item,'animation_data',None)
        if ad and ad.use_tweak_mode:raise Failure('UNSUPPORTED','NLA tweak mode unsupported')
        if profile is None and ad and any(c.driver.type=='SCRIPTED' for c in ad.drivers):raise Failure('UNSUPPORTED','Scripted drivers require a separate trusted execution workflow')
def pose_basis(p):
    kw={'parent_matrix':p.parent.matrix,'parent_matrix_local':p.parent.bone.matrix_local} if p.parent else {}
    return p.bone.convert_local_to_pose(p.matrix,p.bone.matrix_local,invert=True,**kw)
def matrix_transform(mat):
    loc,rot,scale=mat.decompose()
    if not scenes.matrix_close(mat,Matrix.LocRotScale(loc,rot,scale)):raise Failure('UNSUPPORTED','Sampled local pose contains shear')
    return {'location':list(loc),'rotation_quaternion':list(rot),'scale':list(scale)}
def rest_signature(rig,bones):return modeling.sha([{'name':n,'parent':rig.data.bones[n].parent.name if rig.data.bones[n].parent else None,'matrix':value(rig.data.bones[n].matrix_local),'inherit_scale':rig.data.bones[n].inherit_scale,'inherit_rotation':rig.data.bones[n].use_inherit_rotation,'local_location':rig.data.bones[n].use_local_location,'connected':rig.data.bones[n].use_connect} for n in bones])
def capture_motion(o,frames):
    animation_safe();samples=[]
    for frame in frames:
        frame_set(frame);e=o.evaluated_get(bpy.context.evaluated_depsgraph_get())
        samples.append({'frame':frame,'object':matrix_transform(e.matrix_basis),'bones':{p.name:matrix_transform(pose_basis(p)) for p in e.pose.bones} if e.pose else {},'world':value(e.matrix_world),'pose_world':{p.name:value(e.matrix_world@p.matrix) for p in e.pose.bones} if e.pose else {}})
    return samples
def motion_action(o,name,samples,replace):
    a,s=new_action(name,o.name);assign(o,a,s,replace)
    for bone,item in [(None,o),*([(p.name,p) for p in o.pose.bones] if o.pose else [])]:
        item.rotation_mode='QUATERNION'
        for prop,size in [('location',3),('rotation_quaternion',4),('scale',3)]:
            vectors=[row['object'][prop] if bone is None else row['bones'][bone][prop] for row in samples]
            if prop=='rotation_quaternion':
                for i in range(1,len(vectors)):
                    if sum(x*y for x,y in zip(vectors[i-1],vectors[i]))<0:vectors[i]=[-v for v in vectors[i]]
            for index in range(size):curve_write(a,s,item.path_from_id(prop),index,[{'frame':row['frame'],'value':vec[index]} for row,vec in zip(samples,vectors)],False)
    update();return a
def bake(op):
    o=object_for(op['object'])
    if o.parent or o.constraints:raise Failure('UNSUPPORTED','Pose bake requires no object parent or object constraints')
    if o.type=='ARMATURE':rig_for(o.name)
    ad=o.animation_data
    if ad:
        bindings=[(ad.action,ad.action_slot)] if ad.action else []
        bindings.extend((s.action,s.action_slot) for t in ad.nla_tracks for s in t.strips if s.action)
        for a,s in bindings:
            if not s:raise Failure('UNSUPPORTED','Baking requires explicit action slots')
            validate_action_target(o,a,s)
        allowed={item.path_from_id(prop) for item in [o,*(list(o.pose.bones) if o.pose else [])] for prop in ('location','rotation_euler','rotation_quaternion','scale')}
        if any(c.data_path not in allowed for c in ad.drivers):raise Failure('UNSUPPORTED','Bake cannot remove non-transform drivers')
    if ad and (ad.action or ad.nla_tracks) and not op['replace']:raise Failure('CONFLICT','Bake replaces active/NLA animation; replace=true required')
    if o.pose and any(p.constraints for p in o.pose.bones) and not op['clear_constraints']:raise Failure('CONFLICT','Bake requires explicit constraint clearing')
    if ad and ad.drivers and not op['clear_drivers']:raise Failure('CONFLICT','Bake requires explicit driver clearing')
    before=capture_motion(o,op['frames'])
    if o.pose:
        for p in o.pose.bones:
            for c in list(p.constraints):p.constraints.remove(c)
    if ad:
        for t in list(ad.nla_tracks):ad.nla_tracks.remove(t)
        for c in list(ad.drivers):o.driver_remove(c.data_path,c.array_index)
    motion_action(o,op['name'],before,op['replace']);after=capture_motion(o,op['frames'])
    for a,b in zip(before,after):
        mismatch=scenes.compare({'world':a['world'],'pose_world':a['pose_world']},{'world':b['world'],'pose_world':b['pose_world']})
        if mismatch:raise Failure('VALIDATION_FAILED','Baked evaluated transform differs at frame '+str(a['frame'])+': '+mismatch)
    return {'frames':op['frames'],'evaluated_transform_match':True,'scope':'sampled_frames_only; between-frame motion not guaranteed'}
def retarget(op):
    source=rig_for(op['source']);target=rig_for(op['target'])
    if source==target:raise Failure('CONFLICT','Retarget source and target must differ')
    if target.parent or target.constraints or any(p.constraints for p in target.pose.bones) or target.animation_data and (target.animation_data.nla_tracks or target.animation_data.drivers):raise Failure('UNSUPPORTED','Retarget requires an unconstrained target without NLA/drivers')
    mapping={m['target']:m['source'] for m in op['mapping']}
    if len(mapping)!=len(op['mapping']) or len(set(mapping.values()))!=len(mapping):raise Failure('INVALID_REQUEST','Retarget map must be one-to-one')
    for t,s in mapping.items():
        tb=scenes.find(target.data.bones,t);sb=scenes.find(source.data.bones,s)
        if (tb.parent is None)!=(sb.parent is None) or tb.parent and mapping.get(tb.parent.name)!=sb.parent.name:raise Failure('UNSUPPORTED','Retarget requires matching mapped hierarchy')
        tr=tb.parent.matrix_local.inverted()@tb.matrix_local if tb.parent else tb.matrix_local;sr=sb.parent.matrix_local.inverted()@sb.matrix_local if sb.parent else sb.matrix_local
        if tr.to_quaternion().rotation_difference(sr.to_quaternion()).angle>1e-4:raise Failure('UNSUPPORTED','Retarget local rest axes differ')
    target_basis={p.name:matrix_transform(p.matrix_basis) for p in target.pose.bones};target_transform=matrix_transform(target.matrix_basis)
    motion=capture_motion(source,op['frames']);samples=[]
    for row in motion:
        bones=dict(target_basis)
        for t,s in mapping.items():bones[t]={**row['bones'][s],'location':[v*op['translation_scale'] for v in row['bones'][s]['location']]}
        samples.append({'frame':row['frame'],'object':target_transform,'bones':bones})
    motion_action(target,op['name'],samples,op['replace'])
    observed=capture_motion(target,op['frames'])
    for expected,actual in zip(samples,observed):
        for n,t in expected['bones'].items():
            a=Matrix.LocRotScale(t['location'],Quaternion(t['rotation_quaternion']),t['scale']);r=actual['bones'][n];b=Matrix.LocRotScale(r['location'],Quaternion(r['rotation_quaternion']),r['scale'])
            if not scenes.matrix_close(a,b):raise Failure('VALIDATION_FAILED','Retarget local basis did not evaluate as requested')
    return {'mapping':op['mapping'],'frames':op['frames'],'space':'matching_rest_axes_local_basis','translation_scale':op['translation_scale'],'root_object_motion':'not_transferred'}

def execute(op,context,driver_profile=None,job=None):
    k=op['op']
    if k=='skin.rebind':
        from skin_rebind import execute as rebind
        return rebind(op,context,driver_profile)
    if k=='animation.retarget_pose':
        from retargeting import execute as retarget_pose
        return retarget_pose(op,context,driver_profile,job)
    if k in ('skin.transfer_weights','skin.auto_weights'):
        from weight_transfer import execute as transfer
        return transfer(op,context)
    if k in MODEL_OPS or k in SCENE_OPS:modeling.execute(op,context);return
    if k=='rig.create':
        scenes.fresh(bpy.data.objects,op['name']);scenes.fresh(bpy.data.armatures,op['name']+'.Armature');collection=scenes.find(bpy.data.collections,op['collection'],True)
        arm=scenes.named(bpy.data.armatures.new(op['name']+'.Armature'),op['name']+'.Armature');o=scenes.named(bpy.data.objects.new(op['name'],arm),op['name']);collection.objects.link(o)
        with modeling.operator_context(o,context):
            modeling.finished(bpy.ops.object.mode_set(mode='EDIT'))
            for b in op['bones']:
                edit=scenes.named(arm.edit_bones.new(b['name']),b['name']);edit.head=b['head'];edit.tail=b['tail'];edit.roll=math.radians(b['roll_deg']);edit.use_deform=b['deform']
            for b in op['bones']:
                edit=arm.edit_bones[b['name']];edit.parent=arm.edit_bones.get(b['parent']) if b['parent'] else None;edit.use_connect=b['connected']
            modeling.finished(bpy.ops.object.mode_set(mode='OBJECT'))
        for p in o.pose.bones:p.rotation_mode='QUATERNION'
    elif k=='bone.configure':
        o=rig_for(op['rig']);scope(o.data,op['scope']);b=scenes.find(o.data.bones,op['bone']);b.use_deform=op['deform'];b.inherit_scale=op['inherit_scale'];b.use_inherit_rotation=op['inherit_rotation']
    elif k in ('skin.weights','skin.normalize'):
        o=mesh_for(op);rig=rig_for(op['rig']);vertices=modeling.select(o.data,o.data.vertices,op['selection'])
        if k=='skin.weights':
            bone=scenes.find(rig.data.bones,op['bone'])
            if not bone.use_deform:raise Failure('INVALID_REQUEST','Weight target bone is not deform-enabled')
            if len(op['weights']) not in (1,len(vertices)):raise Failure('INVALID_REQUEST','Weights must be one constant or one per selected vertex')
            group=o.vertex_groups.get(bone.name) or o.vertex_groups.new(name=bone.name)
            if group.lock_weight:raise Failure('CONFLICT','Vertex group is locked')
            for i,v in enumerate(vertices):
                amount=op['weights'][0 if len(op['weights'])==1 else i];previous=next((e.weight for e in v.groups if e.group==group.index),0)
                desired=amount if op['mode']=='REPLACE' else previous+amount if op['mode']=='ADD' else previous-amount
                if not -1e-7<=desired<=1+1e-7:raise Failure('INVALID_REQUEST','Weight arithmetic would clamp outside 0..1')
                group.add([v.index],max(0,min(1,desired)),'REPLACE')
        else:
            groups=[g for g in o.vertex_groups if g.name in rig.data.bones and rig.data.bones[g.name].use_deform]
            if any(g.lock_weight for g in groups):raise Failure('CONFLICT','A deform group is locked')
            ids={g.index:g for g in groups}
            for v in vertices:
                ws=sorted([(ids[e.group],e.weight) for e in v.groups if e.group in ids and e.weight>0],key=lambda t:(-t[1],t[0].name))[:op['max_influences']];total=sum(w for _,w in ws)
                if total<1e-8:raise Failure('VALIDATION_FAILED','Cannot normalize an unweighted vertex')
                for g in groups:g.remove([v.index])
                for g,w in ws:g.add([v.index],w/total,'REPLACE')
        o.data.update()
    elif k=='skin.bind':
        o=object_for(op['object']);rig=rig_for(op['rig'])
        if o.type!='MESH':raise Failure('INVALID_REQUEST','Mesh required for binding')
        if any(m.type=='ARMATURE' for m in o.modifiers):raise Failure('CONFLICT','Mesh already has an armature modifier')
        diagnostics=weights_report(o,rig)
        if diagnostics['invalid_weights'] or diagnostics['not_normalized']>(diagnostics['unweighted'] if op['allow_unweighted'] else 0):raise Failure('VALIDATION_FAILED','Deform weights must be normalized; unweighted vertices require explicit allowance')
        dependency_graph([(dep_node(o),dep_node(rig,b.name)) for b in rig.data.bones]);scenes.fresh(o.modifiers,op['name']);m=scenes.named(o.modifiers.new(op['name'],'ARMATURE'),op['name']);m.object=rig;m.use_vertex_groups=True;m.use_bone_envelopes=False;m.use_deform_preserve_volume=op['preserve_volume']
    elif k=='skin.unbind':
        o=object_for(op['object']);m=scenes.find(o.modifiers,op['modifier'])
        if m.type!='ARMATURE':raise Failure('INVALID_REQUEST','Armature modifier required')
        o.modifiers.remove(m)
    elif k=='pose.set':
        o=rig_for(op['rig']);p=scenes.find(o.pose.bones,op['bone']);static_pose_owner(o,p);transform_set(p,op['transform'])
    elif k=='pose.constraint':
        o=rig_for(op['rig']);p=scenes.find(o.pose.bones,op['bone']);target=object_for(op['target']);sub=op.get('subtarget');edges=[]
        if sub:pose_for(target.name,sub)
        edges.append((dep_node(o,p.name),dep_node(target,sub)))
        if op['type']=='IK':
            chain=p
            for _ in range(op['chain_count']-1):
                chain=chain.parent
                if chain is None:raise Failure('INVALID_REQUEST','IK chain_count exceeds bone ancestry')
                edges.append((dep_node(o,chain.name),dep_node(target,sub)))
        if op.get('pole_target'):
            pole=object_for(op['pole_target']);ps=op.get('pole_subtarget')
            if ps:pose_for(pole.name,ps)
            edges.append((dep_node(o,p.name),dep_node(pole,ps)))
            if op['type']=='IK':
                chain=p.parent
                for _ in range(op['chain_count']-1):edges.append((dep_node(o,chain.name),dep_node(pole,ps)));chain=chain.parent
        dependency_graph(edges);scenes.fresh(p.constraints,op['name']);c=scenes.named(p.constraints.new(op['type']),op['name']);c.target=target;c.subtarget=sub or '';c.influence=op['influence']
        if op['type']=='IK':
            c.chain_count=op['chain_count'];c.use_stretch=False
            if op.get('pole_target'):c.pole_target=pole;c.pole_subtarget=op.get('pole_subtarget','');c.pole_angle=math.radians(op.get('pole_angle_deg',0))
        else:c.owner_space=op.get('owner_space','WORLD');c.target_space=op.get('target_space','WORLD')
    elif k=='pose.constraint_remove':
        p=pose_for(op['rig'],op['bone']);p.constraints.remove(scenes.find(p.constraints,op['name']))
    elif k=='action.create':new_action(op['name'],op['slot_name'])
    elif k=='action.slot':
        a=action_for(op['action'],op['scope'])
        if any(s.name_display==op['name'] for s in a.slots):raise Failure('CONFLICT','Slot name collision')
        s=a.slots.new(id_type='OBJECT',name=op['name'])
        if s.name_display!=op['name']:raise Failure('CONFLICT','Blender changed requested slot name')
    elif k=='action.copy':
        a=action_for(op['action']);scenes.fresh(bpy.data.actions,op['name'])
        if a.asset_data or a.get('asset_id'):raise Failure('UNSUPPORTED','Cannot duplicate action asset identity')
        scenes.named(a.copy(),op['name'])
    elif k=='action.assign':
        a=action_for(op['action']);assign(object_for(op['object']),a,slot_for(a,op['slot']),op['replace'])
    elif k=='action.detach':
        o=object_for(op['object'])
        if o.animation_data:o.animation_data.action=None
    elif k in ('action.curve','action.curve_remove'):
        a=action_for(op['action'],op['scope']);s=slot_for(a,op['slot']);o,item,path,index=channel(op['channel'])
        if o.animation_data and o.animation_data.drivers.find(path,index=index):raise Failure('CONFLICT','Remove the driver before authoring an action curve')
        if k=='action.curve':curve_write(a,s,path,index,op['keys'],op['replace'])
        else:
            bag=bag_for(a,s);c=bag.fcurves.find(path,index=index)
            if not c:raise Failure('NOT_FOUND','Curve missing')
            bag.fcurves.remove(c)
    elif k=='nla.add':
        o=object_for(op['object']);ad=o.animation_data_create();a=action_for(op['action']);s=slot_for(a,op['slot']);scenes.fresh(ad.nla_tracks,op['track'])
        validate_action_target(o,a,s)
        track=scenes.named(ad.nla_tracks.new(),op['track']);strip=scenes.named(track.strips.new(op['strip'],op['frame_start'],a),op['strip']);strip.action_slot=s;strip.action_frame_start=op['action_start'];strip.action_frame_end=op['action_end'];strip.scale=op['scale'];strip.repeat=op['repeat'];strip.frame_start=op['frame_start'];strip.blend_type=op['blend'];strip.extrapolation=op['extrapolation'];strip.use_auto_blend=False;strip.blend_in=strip.blend_out=0;strip.use_animated_influence=False;strip.influence=op['influence'];ad.use_nla=True
        expected=op['frame_start']+(op['action_end']-op['action_start'])*op['scale']*op['repeat']
        if abs(strip.frame_end-expected)>1e-3:raise Failure('VALIDATION_FAILED','NLA time mapping differs from request')
    elif k in ('nla.configure','nla.remove'):
        o=object_for(op['object']);ad=o.animation_data
        if not ad:raise Failure('NOT_FOUND','No animation data')
        t=scenes.find(ad.nla_tracks,op['track'])
        if k=='nla.remove':ad.nla_tracks.remove(t)
        else:t.mute=op['mute'];t.is_solo=op['solo']
    elif k in ('driver.add','driver.remove'):
        o,item,path,index=channel(op['channel']);old=o.animation_data.drivers.find(path,index=index) if o.animation_data else None
        if k=='driver.remove':
            if not old:raise Failure('NOT_FOUND','Driver missing')
            o.driver_remove(path,index)
        else:
            if old and not op['replace']:raise Failure('CONFLICT','Driver replacement requires replace=true')
            if channel_animated(o,path,index):raise Failure('CONFLICT','Remove active/NLA channel curves before adding a driver')
            sources=[channel(s) for s in op['sources']];dependency_graph([(dep_node(o,op['channel'].get('bone')),dep_node(s[0],ref.get('bone'))) for s,ref in zip(sources,op['sources'])])
            if old:o.driver_remove(path,index)
            curve=o.driver_add(path,index);d=curve.driver;d.type=op['type']
            for v in list(d.variables):d.variables.remove(v)
            for i,(source,_,source_path,source_index) in enumerate(sources):
                v=d.variables.new();v.name='input'+str(i);v.type='SINGLE_PROP';v.targets[0].id=source;v.targets[0].data_path=source_path+'['+str(source_index)+']'
    elif k=='pose.capture':
        o=rig_for(op['rig']);update();e=o.evaluated_get(bpy.context.evaluated_depsgraph_get());poses={n:matrix_transform(pose_basis(scenes.find(e.pose.bones,n))) for n in op['bones']};a,s=new_action(op['name'],o.name)
        for n,t in poses.items():
            for prop,values in t.items():
                for i,v in enumerate(values):curve_write(a,s,o.pose.bones[n].path_from_id(prop),i,[{'frame':1,'value':v}],False)
        a[POSE_KEY]=json.dumps({'bones':op['bones'],'rest_sha256':rest_signature(o,op['bones']),'transforms':poses},sort_keys=True)
    elif k=='pose.apply':
        o=rig_for(op['rig']);static_pose_owner(o);a=action_for(op['pose'])
        if not a.get(POSE_KEY):raise Failure('INVALID_REQUEST','Action is not a captured pose')
        pose=json.loads(a[POSE_KEY])
        if rest_signature(o,pose['bones'])!=pose['rest_sha256']:raise Failure('CONFLICT','Pose rest hierarchy does not match target')
        for n in op['bones']:
            if n not in pose['transforms']:raise Failure('NOT_FOUND','Bone absent from captured pose')
            p=scenes.find(o.pose.bones,n);static_pose_owner(o,p);transform_set(p,pose['transforms'][n])
    elif k=='animation.bake':return bake(op)
    elif k=='animation.retarget':return retarget(op)
    else:raise Failure('INVALID_REQUEST','Unknown rig operation')
    update()

def state():
    return {'rig_report_version':'1.0','scene':scenes.state(),
        'rigs':[{'object':o.name,'data':identity(o.data),'rest_sha256':rest_signature(o,[b.name for b in o.data.bones]),'bones':[{'name':b.name,'parent':b.parent.name if b.parent else None,'head':value(b.head_local),'tail':value(b.tail_local),'matrix_local':value(b.matrix_local),'connected':b.use_connect,'deform':b.use_deform,'inherit_scale':b.inherit_scale,'inherit_rotation':b.use_inherit_rotation,'pose_matrix':value(o.pose.bones[b.name].matrix),'basis':value(o.pose.bones[b.name].matrix_basis)} for b in o.data.bones]} for o in sorted(bpy.data.objects,key=lambda o:o.name) if o.type=='ARMATURE'],
        'skins':[{'object':o.name,'topology_sha256':modeling.topology(o.data),'groups':[{'name':g.name,'index':g.index,'locked':g.lock_weight} for g in o.vertex_groups],'weights_sha256':modeling.sha([[(g.group,g.weight) for g in v.groups] for v in o.data.vertices]),'bindings':[{'modifier':m.name,'rig':m.object.name,'weights':weights_report(o,m.object)} for m in o.modifiers if m.type=='ARMATURE' and m.object]} for o in sorted(bpy.data.objects,key=lambda o:o.name) if o.type=='MESH'],
        'coverage':'local_rest_pose_weights_layered_actions_slots_NLA_and_non_scripted_drivers; sampled_deformation_separate'}
def inspect(params,job):
    bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    if 'context' in params:scenes.activate(params['context'])
    update();report=state()
    import drivers
    report['driver_audit']=drivers.audit(params['file']);atomic_json(job/'driver-audit.json',report['driver_audit'])
    from rig_profile import profile,suggest
    report['profile']=profile();atomic_json(job/'rig-profile.json',report['profile'])
    if params.get('suggest_mapping'):
        m=params['suggest_mapping']
        try:report['mapping_suggestions']=suggest(object_for(m['source']),object_for(m['target']))
        except ValueError as exc:raise Failure('INVALID_REQUEST',str(exc)) from exc
        atomic_json(job/'rig-mapping-suggestions.json',report['mapping_suggestions'])
    atomic_json(job/'rig-report.json',report);return report
def sample(params,job):
    spec=params['manifest'];validate(spec,SAMPLE);bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False);animation_safe(params.get('driver_profile'),params['file'],job)
    context={'scene':spec['scene'],'view_layer':spec['view_layer'],'frame':math.floor(spec['frames'][0]),'mode':'OBJECT','active_object':None,'selected_objects':[]};scenes.activate(context);rows=[]
    for frame in spec['frames']:
        frame_set(frame);graph=bpy.context.evaluated_depsgraph_get();objects=[]
        import drivers
        drivers.evaluated(params.get('driver_profile'))
        for wanted in spec['objects']:
            o=scenes.find(bpy.data.objects,wanted['name'])
            if o.name not in bpy.context.view_layer.objects:raise Failure('NOT_FOUND','Sample target outside active view layer')
            e=o.evaluated_get(graph);row={'name':o.name,'matrix_world':value(e.matrix_world),'bones':{},'vertices':[]}
            for n in wanted.get('bones',[]):
                if not e.pose:raise Failure('INVALID_REQUEST','Bone sample requires rig')
                p=scenes.find(e.pose.bones,n);parent_local=p.matrix
                if p.parent:
                    try:parent_local=p.parent.matrix.inverted()@p.matrix
                    except ValueError:parent_local=None
                row['bones'][n]={'matrix_world':value(e.matrix_world@p.matrix),'matrix_parent_local':value(parent_local) if parent_local is not None else None,'head_world':value(e.matrix_world@p.head),'tail_world':value(e.matrix_world@p.tail)}
            if wanted.get('vertices'):
                if o.type!='MESH':raise Failure('INVALID_REQUEST','Vertex sample requires mesh')
                mesh=e.to_mesh(preserve_all_data_layers=False,depsgraph=graph)
                try:
                    modeling.bounded(mesh)
                    if max(wanted['vertices'])>=len(mesh.vertices):raise Failure('INVALID_REQUEST','Sample vertex index outside evaluated mesh')
                    row['evaluated_vertex_count']=len(mesh.vertices);row['vertices']=[{'index':i,'world':value(e.matrix_world@mesh.vertices[i].co)} for i in wanted['vertices']]
                finally:e.to_mesh_clear()
            objects.append(row)
        rows.append({'frame':frame,'time_seconds':(frame-bpy.context.scene.frame_start)/(bpy.context.scene.render.fps/bpy.context.scene.render.fps_base),'objects':objects})
    def finite(item):
        if isinstance(item,dict):return 'non_finite' not in item and all(finite(v) for v in item.values())
        if isinstance(item,list):return all(finite(v) for v in item)
        return math.isfinite(item) if type(item) in (int,float) else True
    if not finite(rows):raise Failure('VALIDATION_FAILED','Sampled transform or vertex is non-finite')
    report={'animation_sample_version':'1.0','scene':spec['scene'],'view_layer':spec['view_layer'],'fps':bpy.context.scene.render.fps/bpy.context.scene.render.fps_base,'time_origin_frame':bpy.context.scene.frame_start,'frames':rows,'scope':'requested_frames_and_evaluated_indices_only; no continuous-time or topology-correspondence guarantee'};atomic_json(job/'animation-samples.json',report);return report
def prepare(params,job):
    manifest=normalize(params['manifest'])
    if params.get('file'):
        bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
        if tuple(bpy.data.version[:2])!=tuple(bpy.app.version[:2]):raise Failure('UNSUPPORTED','Rig edits require matching Blender major/minor; convert a separate asset candidate first')
    else:bpy.ops.wm.read_factory_settings(use_empty=True);scenes.named(bpy.context.scene,manifest.get('initial_scene','Scene'))
    if bpy.context.mode!='OBJECT':raise Failure('UNSUPPORTED','Source must be in object mode')
    animation_safe(params.get('driver_profile'),params.get('file'),job);atomic_json(job/'rig-before.json',state());actions=[];adapters=[]
    for index,op in enumerate(manifest['operations']):
        scenes.prepare_context(manifest['context'])
        try:detail=execute(op,manifest['context'],params.get('driver_profile'),job)
        except Failure as exc:raise Failure(exc.code,f'Operation {index} ({op["op"]}): {exc}') from exc
        actions.append({'index':index,'op':op['op'],'status':'applied_in_candidate_memory','detail':detail});atomic_json(job/'rig-actions.json',actions)
        if op['op'] in ('animation.retarget_pose','skin.transfer_weights','skin.auto_weights','skin.rebind'):
            adapters.append({'index':index,'op':op['op'],'detail':detail});atomic_json(job/'rig-adapter-report.json',{'version':'1.0','operations':adapters})
    scenes.activate(manifest['context']);update();retained=[]
    for item in all_ids():
        if not item.library and not item.is_embedded_data and item.users==0 and not item.use_fake_user:item.use_fake_user=True;retained.append(identity(item))
    expected=state();atomic_json(job/'rig-expected.json',expected);candidate=job/'rig-candidate.blend'
    if candidate.exists():raise Failure('CONFLICT','Candidate already exists')
    bpy.context.preferences.filepaths.save_version=0;bpy.ops.wm.save_as_mainfile(filepath=str(candidate),copy=True,relative_remap=True,check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False);scenes.activate(manifest['context']);update();observed=state();atomic_json(job/'rig-report.json',observed);mismatch=scenes.compare(expected,observed)
    if mismatch:raise Failure('VALIDATION_FAILED','Rig candidate reopen mismatch: '+mismatch)
    atomic_json(job/'rig-change.json',{'operations':manifest['operations'],'context':manifest['context'],'retained_orphans':retained,'reopen':'pass'})
    return {'candidate':str(candidate),'candidate_sha256':digest(candidate),'operations':len(actions),'context':manifest['context'],'report':str(job/'rig-report.json'),'reopen':'pass','publication':'working_candidate_only','next_step':'project plan-copy/plan-files; transaction apply; animation sample'}
