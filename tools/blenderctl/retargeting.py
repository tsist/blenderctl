# SPDX-License-Identifier: GPL-3.0-or-later
"""Parent rest-frame deltas with explicit mapping, sampled motion and error proof."""
import math
import bpy
from mathutils import Matrix,Vector
from protocol import Failure
from inspection import value

def finite_matrix(m):
    if any(not math.isfinite(v) for row in m for v in row) or abs(m.determinant())<1e-12:
        raise Failure('UNSUPPORTED','Retarget requires finite nonsingular transforms')

def source_anchors(source, target, mapping, entries, adapter):
    """Validate the declared induced source tree before changing any data."""
    mapped_sources = set(mapping.values())
    anchors = {}
    for name, source_name in mapping.items():
        if source_name not in source.data.bones:
            raise Failure('NOT_FOUND', 'Missing source bone ' + source_name)
        target_bone = target.data.bones[name]
        source_bone = source.data.bones[source_name]
        anchor = mapping[target_bone.parent.name] if target_bone.parent else None
        if adapter == 'REST_FRAME_V1':
            if (source_bone.parent.name if source_bone.parent else None) != anchor:
                raise Failure('UNSUPPORTED', 'Mapped parent hierarchy differs; an explicit REST_CHAIN_V1 mapping is required to collapse source ancestors')
        else:
            chain = []
            current = source_bone
            while current and current.name != anchor:
                if current.name != source_name and current.name in mapped_sources:
                    raise Failure('UNSUPPORTED', 'Source chain crosses another mapped ancestor: ' + current.name)
                chain.append(current.name)
                current = current.parent
            if anchor is not None and current is None:
                raise Failure('UNSUPPORTED', 'Mapped target parent is not a source ancestor: ' + name)
            if entries[name]['source_chain'] != list(reversed(chain)):
                raise Failure('CONFLICT', 'Declared source chain differs from source hierarchy: ' + name)
        anchors[name] = anchor
    return anchors

def execute(op,context,driver_profile=None,job=None):
    import rigging as r
    from retarget_contract import OP
    from scene_contract import validate
    validate(op,OP)
    if op['adapter'] == 'CONTROL_ACTION_SET_V1':
        from retarget_actions import execute as actions
        return actions(op, context, driver_profile,job)
    if op['adapter'] == 'CONTROL_CHANNELS_V1':
        from retarget_controls import execute as controls
        return controls(op, context, driver_profile)
    source=r.object_for(op['source']);target=r.object_for(op['target'])
    for rig in (source,target):
        if rig.type!='ARMATURE' or not 1<=len(rig.data.bones)<=2048:raise Failure('UNSUPPORTED','Retarget requires 1..2048 bones')
        r.local(rig.data)
        if rig.name not in bpy.context.view_layer.objects:raise Failure('NOT_FOUND','Retarget rig outside active view layer: '+rig.name)
    if driver_profile is None:
        import drivers
        if any(getattr(owner,'animation_data',None) and owner.animation_data.drivers for owner,_ in drivers.owners()):
            raise Failure('UNSUPPORTED','Retarget requires a source-bound profile when drivers are present')
    if source==target:raise Failure('CONFLICT','Source and target must differ')
    if target.data.users>1:raise Failure('CONFLICT','Retarget requires isolated target armature data')
    if target.parent or target.constraints or any(p.constraints for p in target.pose.bones):raise Failure('UNSUPPORTED','Target constraints or parent require a separate control-rig adapter')
    if target.animation_data and (target.animation_data.drivers or target.animation_data.nla_tracks):raise Failure('UNSUPPORTED','Target drivers or NLA are not replaced')
    affine=op.get('affine_policy','REJECT')=='SVD_HELPERS'
    if affine and any(b.inherit_scale!='FULL' or not b.use_inherit_rotation or not b.use_local_location for b in target.data.bones):
        raise Failure('UNSUPPORTED','SVD_HELPERS requires FULL inheritance, local location and rotation inheritance')
    for item,key in ((source,'source_rest_sha256'),(target,'target_rest_sha256')):
        if r.rest_signature(item,[b.name for b in item.data.bones])!=op[key]:raise Failure('CONFLICT','Rest profile hash changed: '+item.name)
    mapping={x['target']:x['source'] for x in op['mapping']}
    if len(mapping)!=len(op['mapping']) or len(set(mapping.values()))!=len(mapping):raise Failure('INVALID_REQUEST','Mapping must be one-to-one')
    if set(mapping)!=set(target.pose.bones.keys()):raise Failure('INVALID_REQUEST','Every target bone must be explicitly mapped')
    anchors=source_anchors(source,target,mapping,{x['target']:x for x in op['mapping']},op['adapter'])
    names=sorted(mapping,key=lambda n:len(target.data.bones[n].parent_recursive))
    initial=target.matrix_basis.copy();finite_matrix(initial);samples=[];desired=[];first=None
    for frame in sorted(op['frames']):
        r.frame_set(frame)
        import drivers
        drivers.evaluated(driver_profile)
        evaluated=source.evaluated_get(bpy.context.evaluated_depsgraph_get())
        if source.parent or source.constraints:raise Failure('UNSUPPORTED','Source object parent/constraints require another root-motion adapter')
        source_object=evaluated.matrix_basis.copy();finite_matrix(source_object)
        if first is None:first=source_object.copy()
        if op['root_motion']=='SOURCE_DELTA':
            delta=first.inverted()@source_object;delta.translation*=op['translation_scale'];obj_matrix=initial@delta
        else:obj_matrix=initial.copy()
        bones={};poses={};helper_samples={}
        for t in names:
            sp=evaluated.pose.bones[mapping[t]];tb=target.data.bones[t]
            anchor=evaluated.pose.bones[anchors[t]] if anchors[t] else None
            sr=anchor.bone.matrix_local.inverted()@sp.bone.matrix_local if anchor else sp.bone.matrix_local.copy()
            sm=anchor.matrix.inverted()@sp.matrix if anchor else sp.matrix.copy()
            tr=tb.parent.matrix_local.inverted()@tb.matrix_local if tb.parent else tb.matrix_local.copy()
            finite_matrix(sm);finite_matrix(sr);finite_matrix(tr)
            # Preserve the full parent-relative linear map. Intermediate shear can
            # be required by inheritance compensation and need not imply shear in
            # the final editable basis returned by convert_local_to_pose.
            linear=sm.to_3x3()@sr.to_3x3().inverted()@tr.to_3x3()
            translation=tr.translation+(sm.translation-sr.translation)*op['translation_scale']
            local=linear.to_4x4();local.translation=translation
            pose=poses[tb.parent.name]@local if tb.parent else local
            finite_matrix(pose)
            kw={'parent_matrix':poses[tb.parent.name],'parent_matrix_local':tb.parent.matrix_local} if tb.parent else {}
            basis=tb.convert_local_to_pose(pose,tb.matrix_local,invert=True,**kw)
            finite_matrix(basis)
            if affine:
                from retarget_affine import factor_basis
                helper_samples[t],bones[t]=factor_basis(basis)
            else:bones[t]=r.matrix_transform(basis)
            poses[t]=pose
        samples.append({'frame':frame,'object':r.matrix_transform(obj_matrix),'bones':bones,'helper_samples':helper_samples})
        desired.append({'frame':frame,'object':value(obj_matrix),'poses':{n:value(p) for n,p in poses.items()}})
    helpers={};rest_change={}
    if affine:
        from retarget_affine import create_helpers
        original_parents={n:target.data.bones[n].parent.name if target.data.bones[n].parent else None for n in names}
        helpers=create_helpers(target,names,context,max_rest_error=op.get('max_rest_error',1e-6),report=rest_change)
        if op['adapter']=='REST_CHAIN_V1':
            from retarget_affine import _matrix
            # Helper insertion changes float32 rest recomposition. Solve against
            # the actual helper/original rest frames while preserving the desired
            # poses computed before insertion. The explicit rest budget remains
            # separate from the final evaluated pose error budget.
            for row,want in zip(samples,desired):
                for n in names:
                    parent=original_parents[n];helper=target.data.bones[helpers[n]];bone=target.data.bones[n]
                    pose=Matrix(want['poses'][n])
                    kw={'parent_matrix':Matrix(want['poses'][parent]),'parent_matrix_local':target.data.bones[parent].matrix_local} if parent else {}
                    full_basis=helper.convert_local_to_pose(pose,helper.matrix_local,invert=True,**kw)
                    helper_transform,_=factor_basis(full_basis)
                    helper_pose=helper.convert_local_to_pose(_matrix(helper_transform),helper.matrix_local,**kw)
                    basis=bone.convert_local_to_pose(pose,bone.matrix_local,parent_matrix=helper_pose,parent_matrix_local=helper.matrix_local,invert=True)
                    row['bones'][n]=r.matrix_transform(basis)
                    row['bones'][helpers[n]]=helper_transform
        else:
            for row in samples:
                row['bones'].update({helpers[n]:v for n,v in row['helper_samples'].items()})
    r.motion_action(target,op['name'],samples,op['replace'])
    rows=[];maximum=0.0;head_error=tail_error=0.0
    for want in desired:
        r.frame_set(want['frame']);e=target.evaluated_get(bpy.context.evaluated_depsgraph_get())
        actual_object=e.matrix_basis;finite_matrix(actual_object);error=max(abs(a-b) for x,y in zip(want['object'],actual_object) for a,b in zip(x,y));maximum=max(maximum,error)
        proof=[]
        for n,raw in want['poses'].items():
            wanted=Matrix(raw);actual=e.pose.bones[n].matrix
            finite_matrix(actual)
            error=max(abs(a-b) for x,y in zip(wanted,actual) for a,b in zip(x,y));maximum=max(maximum,error)
            h=(wanted.translation-actual.translation).length
            tail=Vector((0,target.data.bones[n].length,0));t=(wanted@tail-actual@tail).length
            head_error=max(head_error,h);tail_error=max(tail_error,t)
            proof.append({'bone':n,'maximum_matrix_error':error,'head_error':h,'tail_error':t})
        rows.append({'frame':want['frame'],'bones':proof})
    if maximum>op['max_matrix_error']:raise Failure('VALIDATION_FAILED','Retarget pose exceeded declared matrix error')
    return {'adapter':op['adapter'],'source':source.name,'target':target.name,'mapping':op['mapping'],
      'source_rest_sha256':op['source_rest_sha256'],'target_rest_sha256':op['target_rest_sha256'],
      'frames':sorted(op['frames']),'translation_scale':op['translation_scale'],'root_motion':op['root_motion'],
      'affine_policy':op.get('affine_policy','REJECT'),'helpers':helpers,
      'rest_change':rest_change,
      'target_output_rest_sha256':r.rest_signature(target,[b.name for b in target.data.bones]),
      'bone_lengths':[{'source':mapping[n],'target':n,'source_length':source.data.bones[mapping[n]].length,'target_length':target.data.bones[n].length} for n in names],
      'maximum_matrix_error':maximum,'maximum_head_error':head_error,'maximum_tail_error':tail_error,'samples':rows,
      'semantics':('declared mapped-ancestor full linear delta; source intermediate bone motion included; ' if op['adapter']=='REST_CHAIN_V1' else 'parent-frame full linear delta; ')+'target rest offsets plus scaled source translation delta; final basis must be TRS representable',
      'limits':'sampled integer frames only; no automatic IK foot lock, collision correction or between-frame quality guarantee'}
