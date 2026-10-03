# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit same-rest skin rebinding, without touching mesh/weight/key geometry."""
import copy
import math
import bpy
from protocol import Failure
from inspection import mesh_content, identity
import drivers, modeling, rigging, scenes


def execute(op, context, profile):
    source = rigging.object_for(op['source'])
    target = rigging.object_for(op['target'])
    if source == target:
        raise Failure('CONFLICT', 'Skin source and target must differ')
    for rig, label in ((source, 'source'), (target, 'target')):
        if rig.type != 'ARMATURE' or not 1 <= len(rig.data.bones) <= 2048:
            raise Failure('UNSUPPORTED', 'Skin adapter requires 1..2048 local bones')
        rigging.local(rig.data)
        if rig.name not in bpy.context.view_layer.objects:
            raise Failure('UNSUPPORTED', 'Skin rigs must be in the active view layer')
        if rigging.rest_signature(rig, [b.name for b in rig.data.bones]) != op[label+'_rest_sha256']:
            raise Failure('CONFLICT', 'Skin rest signature changed: '+label)
    if set(source.data.bones.keys()) != set(target.data.bones.keys()):
        raise Failure('UNSUPPORTED', 'Same-rest rebinding requires identical named bone sets')
    worst = 0.
    props = ['use_deform', 'inherit_scale', 'use_inherit_rotation', 'use_local_location']
    props += [p.identifier for p in source.data.bones[0].bl_rna.properties if p.identifier.startswith('bbone_')]
    for bone in source.data.bones:
        other = target.data.bones[bone.name]
        if (bone.parent.name if bone.parent else None) != (other.parent.name if other.parent else None):
            raise Failure('UNSUPPORTED', 'Same-rest rebinding requires matching parents')
        worst = max(worst, max(abs(a-b) for ra, rb in zip(bone.matrix_local, other.matrix_local) for a,b in zip(ra,rb)))
        worst = max(worst, abs(bone.length-other.length))
        for prop in props:
            a,b = getattr(bone,prop),getattr(other,prop)
            if hasattr(a,'name'):a=a.name
            if hasattr(b,'name'):b=b.name
            if hasattr(a,'__len__') and not isinstance(a,str):a=list(a)
            if hasattr(b,'__len__') and not isinstance(b,str):b=list(b)
            if a != b:
                raise Failure('UNSUPPORTED', 'Bone deformation settings differ: '+bone.name+'/'+prop)
    if worst > op['max_rest_error']:
        raise Failure('UNSUPPORTED', 'Same-rest rebinding rest error exceeds explicit budget')
    plans=[]; names=set(); key_owners=set(); weight_diagnostics=[]
    for row in op['meshes']:
        obj=rigging.object_for(row['object'])
        if obj.name in names:raise Failure('INVALID_REQUEST','Duplicate mesh in skin operation')
        names.add(obj.name)
        if obj.type!='MESH':raise Failure('INVALID_REQUEST','Skin adapter requires meshes')
        rigging.local(obj.data);modeling.bounded(obj.data)
        ad=obj.animation_data
        allowed_visibility={m.path_from_id(p) for m in obj.modifiers if m.type!='ARMATURE' for p in ('show_viewport','show_render')}
        if ad and (ad.action or ad.nla_tracks or any(f.data_path not in allowed_visibility for f in ad.drivers)):
            raise Failure('UNSUPPORTED','Skin object animation permits only preserved non-armature modifier visibility drivers')
        if ad and ad.drivers and profile is None:
            raise Failure('UNSUPPORTED','Mesh modifier drivers require a source-bound native profile')
        if obj.name not in bpy.context.view_layer.objects or obj.constraints or obj.data.animation_data:
            raise Failure('UNSUPPORTED','Skin mesh requires active-layer local unanimated object/data without constraints')
        if obj.data.users != 1:
            raise Failure('CONFLICT','Rebinding requires a single-user working mesh')
        if modeling.topology(obj.data)!=row['topology_sha256']:
            raise Failure('CONFLICT','Skin topology signature changed')
        arms=[m for m in obj.modifiers if m.type=='ARMATURE']
        if len(arms)!=1 or arms[0].name!=row['modifier'] or arms[0].object!=source:
            raise Failure('CONFLICT','Declared existing source Armature modifier does not match')
        m=arms[0]
        if not m.use_vertex_groups or m.use_bone_envelopes or m.use_multi_modifier:
            raise Failure('UNSUPPORTED','Skin rebinding requires vertex-group deformation without envelopes or multi modifier')
        if obj.parent and (obj.parent!=source or obj.parent_type!='OBJECT'):
            raise Failure('UNSUPPORTED','Skin parent must be absent or the declared source rig as OBJECT')
        weights=rigging.weights_report(obj,source)
        if any(not math.isfinite(g.weight) for v in obj.data.vertices for g in v.groups) or weights['unweighted']:
            raise Failure('VALIDATION_FAILED','Every vertex requires finite positive deform weights')
        weight_diagnostics.append({'object':obj.name,**weights,'policy':'PRESERVE_NATIVE'})
        keys=obj.data.shape_keys
        if keys:
            rigging.local(keys)
            key_owners.add(keys.as_pointer())
        # The adapter only changes these explicit relationships, not other modifier dependencies.
        plans.append((obj,m,mesh_content(obj.data)))
    before,handles=drivers.inventory(); expected=copy.deepcopy(before); remapped=[]
    for row in expected:
        owner,f=handles[row['driver_id']]
        if owner.as_pointer() not in key_owners:continue
        if profile is None:raise Failure('UNSUPPORTED','Shape-key drivers require a source-bound native profile')
        for v in row['variables']:
            for dep in v['targets']:
                if dep['id']==identity(source):dep['id']=identity(target)
    # Do not introduce a driver cycle through selected shape-key datablocks/meshes.
    for owner,f in handles.values():
        if owner not in (target,target.data):continue
        for var in f.driver.variables:
            for dep in var.targets:
                if dep.id and (dep.id.as_pointer() in key_owners or isinstance(dep.id,bpy.types.Object) and dep.id.name in names):
                    raise Failure('UNSUPPORTED','Target rig depends on a selected skin')
    parent=target
    while parent:
        if parent.name in names:raise Failure('UNSUPPORTED','Target rig has a selected skin ancestor')
        parent=parent.parent
    for obj,m,content in plans:
        m.object=target
        if obj.parent==source:obj.parent=target
        keys=obj.data.shape_keys
        if keys and keys.animation_data:
            for f in keys.animation_data.drivers:
                for var in f.driver.variables:
                    for dep in var.targets:
                        if dep.id==source:
                            dep.id=target;remapped.append({'mesh':obj.name,'path':f.data_path,'variable':var.name})
        if mesh_content(obj.data)!=content:
            raise Failure('VALIDATION_FAILED','Rebinding changed mesh geometry/UV/weights/shape-key coordinates')
    rigging.update()
    if drivers.inventory()[0]!=expected:
        raise Failure('VALIDATION_FAILED','Unexpected driver inventory mutation')
    samples=[]
    for frame in op['frames']:
        rigging.frame_set(frame);drivers.evaluated(profile)
        deps=bpy.context.evaluated_depsgraph_get();meshes=[]
        for obj,_,_ in plans:
            ev=obj.evaluated_get(deps);mesh=ev.to_mesh()
            try:
                if not mesh.vertices or any(not math.isfinite(x) for v in mesh.vertices for x in v.co):
                    raise Failure('VALIDATION_FAILED','Invalid evaluated rebound mesh')
                meshes.append({'object':obj.name,'vertices':len(mesh.vertices)})
            finally:ev.to_mesh_clear()
        samples.append({'frame':frame,'meshes':meshes})
    return {'adapter':'SAME_REST_SKIN_V1','maximum_rest_error':worst,'meshes':[o.name for o,_,_ in plans],
            'mesh_data_unchanged':True,'driver_changes_explicit':True,'remapped_shape_driver_targets':remapped,
            'samples':samples,'weight_diagnostics':weight_diagnostics,'scope':'Same named rest rig; preserve raw native weights (including existing non-unit entries), key geometry, modifier stack and parent local transform. Sampled finite geometry is not an artistic quality assessment.'}
