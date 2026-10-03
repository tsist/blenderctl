# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded rig/animation authoring contracts, shared by the CLI and exported schemas."""
from protocol import Failure
from scene_contract import obj,enum,array,number,integer,NAME,BOOL,VEC,CONTEXT,validate
from model_contract import MANIFEST as MODEL_MANIFEST,normalize as model_normalize,OPS as MODEL_OPS,SCENE_OPS,SHA,SELECTION
SCOPE=enum('reject_shared','shared');DATA_SCOPE=enum('reject_shared','single_user')
SLOT={**NAME,'maxLength':255}
FRAMES={**array(integer(-10000,100000),1,120),'uniqueItems':True}
CHANNEL=obj({'object':NAME,'bone':NAME,'property':enum('location','rotation_euler','rotation_quaternion','scale'),'component':integer(0,3)},['object','property','component'])
TRANSFORM=obj({'location':VEC,'rotation_quaternion':array(number(-1,1),4,4),'scale':array(number(.0001,10000),3,3)})
BONE=obj({'name':NAME,'head':VEC,'tail':VEC,'roll_deg':number(-360,360),'parent':{'oneOf':[NAME,{'type':'null'}]},'connected':BOOL,'deform':BOOL})
OPS={}
def operation(name,props,required=None):OPS[name]=obj({'op':{'const':name},**props},['op',*(props if required is None else required)])
operation('rig.create',{'name':NAME,'collection':NAME,'bones':array(BONE,1,256)})
operation('bone.configure',{'rig':NAME,'bone':NAME,'scope':SCOPE,'deform':BOOL,'inherit_scale':enum('FULL','FIX_SHEAR','ALIGNED','AVERAGE','NONE','NONE_LEGACY'),'inherit_rotation':BOOL})
operation('skin.weights',{'object':NAME,'rig':NAME,'bone':NAME,'data_scope':DATA_SCOPE,'selection':SELECTION,'weights':array(number(0,1),1,200000),'mode':enum('REPLACE','ADD','SUBTRACT')})
operation('skin.normalize',{'object':NAME,'rig':NAME,'data_scope':DATA_SCOPE,'selection':SELECTION,'max_influences':integer(1,32)})
operation('skin.bind',{'object':NAME,'rig':NAME,'name':NAME,'preserve_volume':BOOL,'allow_unweighted':BOOL})
operation('skin.unbind',{'object':NAME,'modifier':NAME})
operation('skin.rebind',{'adapter':{'const':'SAME_REST_SKIN_V1'},'source':NAME,'target':NAME,'source_rest_sha256':SHA,'target_rest_sha256':SHA,'meshes':array(obj({'object':NAME,'modifier':NAME,'topology_sha256':SHA}),1,16),'frames':FRAMES,'max_rest_error':number(1e-7,1e-4),'shape_drivers':{'const':'REMAP_SOURCE_RIG'},'parent_policy':{'const':'REPLACE_SOURCE_KEEP_LOCAL'},'weights_policy':{'const':'PRESERVE_NATIVE'}})
operation('pose.set',{'rig':NAME,'bone':NAME,'transform':TRANSFORM})
operation('pose.constraint',{'rig':NAME,'bone':NAME,'name':NAME,'type':enum('COPY_TRANSFORMS','COPY_LOCATION','COPY_ROTATION','IK'),'target':NAME,'subtarget':NAME,'influence':number(0,1),'owner_space':enum('WORLD','POSE','LOCAL'),'target_space':enum('WORLD','POSE','LOCAL'),'chain_count':integer(1,32),'pole_target':NAME,'pole_subtarget':NAME,'pole_angle_deg':number(-180,180)},['rig','bone','name','type','target','influence'])
operation('pose.constraint_remove',{'rig':NAME,'bone':NAME,'name':NAME})
operation('action.create',{'name':NAME,'slot_name':NAME})
operation('action.slot',{'action':NAME,'name':NAME,'scope':SCOPE})
operation('action.copy',{'action':NAME,'name':NAME})
operation('action.assign',{'object':NAME,'action':NAME,'slot':SLOT,'replace':BOOL})
operation('action.detach',{'object':NAME})
operation('action.curve',{'action':NAME,'slot':SLOT,'scope':SCOPE,'channel':CHANNEL,'keys':array(obj({'frame':number(-10000,100000),'value':number(-1e6,1e6),'interpolation':enum('CONSTANT','LINEAR','BEZIER')}),1,500),'replace':BOOL})
operation('action.curve_remove',{'action':NAME,'slot':SLOT,'scope':SCOPE,'channel':CHANNEL})
operation('nla.add',{'object':NAME,'track':NAME,'strip':NAME,'action':NAME,'slot':SLOT,'frame_start':integer(-10000,100000),'action_start':number(-10000,100000),'action_end':number(-10000,100000),'scale':number(.01,100),'repeat':number(.01,100),'influence':number(0,1),'blend':enum('REPLACE','ADD','SUBTRACT','MULTIPLY','COMBINE'),'extrapolation':enum('NOTHING','HOLD','HOLD_FORWARD')})
operation('nla.configure',{'object':NAME,'track':NAME,'mute':BOOL,'solo':BOOL})
operation('nla.remove',{'object':NAME,'track':NAME})
operation('driver.add',{'channel':CHANNEL,'type':enum('SUM','AVERAGE','MIN','MAX'),'sources':array(CHANNEL,1,8),'replace':BOOL})
operation('driver.remove',{'channel':CHANNEL})
operation('pose.capture',{'rig':NAME,'name':NAME,'bones':{**array(NAME,1,256),'uniqueItems':True}})
operation('pose.apply',{'rig':NAME,'pose':NAME,'bones':{**array(NAME,1,256),'uniqueItems':True}})
operation('animation.bake',{'object':NAME,'name':NAME,'frames':FRAMES,'replace':BOOL,'clear_constraints':BOOL,'clear_drivers':BOOL})
operation('animation.retarget',{'source':NAME,'target':NAME,'name':NAME,'frames':FRAMES,'mapping':array(obj({'source':NAME,'target':NAME}),1,256),'translation_scale':number(.0001,10000),'replace':BOOL})
from retarget_contract import OP as RETARGET_POSE
from weight_transfer_contract import OPS as TRANSFER_OPS
OPS['animation.retarget_pose']=RETARGET_POSE
OPS.update(TRANSFER_OPS)
MANIFEST=obj({'initial_scene':NAME,'context':CONTEXT,'operations':array({'oneOf':[*MODEL_MANIFEST['properties']['operations']['items']['oneOf'],*OPS.values()]},1,500)},['context','operations'])
SAMPLE=obj({'scene':NAME,'view_layer':NAME,'frames':{**array(number(-10000,100000),1,120),'uniqueItems':True},'objects':array(obj({'name':NAME,'bones':{**array(NAME,0,256),'uniqueItems':True},'vertices':{**array(integer(0,199999),0,256),'uniqueItems':True}},['name']),1,32)})
def normalize(manifest):
    validate(manifest,MANIFEST)
    prior=[o for o in manifest['operations'] if o['op'] in MODEL_OPS or o['op'] in SCENE_OPS]
    if prior:model_normalize({**manifest,'operations':prior})
    for op in manifest['operations']:
        kind=op['op']
        if op.get('adapter')=='CONTROL_ACTION_SET_V1':
            names=[c['name'] for c in op['clips']]
            if len(set(names))!=len(names) or op['active_action'] not in names:raise Failure('INVALID_REQUEST','Output action names must be unique and active_action must select an output')
            for clip in op['clips']:
                if clip['frames']!=sorted(clip['frames']):raise Failure('INVALID_REQUEST','Clip frames must be increasing')
            witnesses=op['quality']['witnesses']
            if len({w['source'] for w in witnesses})!=len(witnesses) or len({w['target'] for w in witnesses})!=len(witnesses):raise Failure('INVALID_REQUEST','Quality witnesses must be one-to-one')
        if kind=='rig.create':
            bones={b['name']:b for b in op['bones']}
            if len(bones)!=len(op['bones']):raise Failure('INVALID_REQUEST','Duplicate bone names')
            for b in op['bones']:
                if sum((x-y)**2 for x,y in zip(b['head'],b['tail']))<1e-12:raise Failure('INVALID_REQUEST','Zero-length bone')
                parent=b['parent'];seen={b['name']}
                while parent is not None:
                    if parent not in bones:raise Failure('NOT_FOUND','Unknown bone parent')
                    if parent in seen:raise Failure('CONFLICT','Bone hierarchy cycle')
                    seen.add(parent);parent=bones[parent]['parent']
                if b['connected'] and (not b['parent'] or any(abs(x-y)>1e-6 for x,y in zip(b['head'],bones[b['parent']]['tail']))):raise Failure('INVALID_REQUEST','Connected bone head must equal parent tail')
        if kind=='pose.constraint':
            ik={'chain_count','pole_target','pole_subtarget','pole_angle_deg'}
            if op['type']=='IK':
                if 'chain_count' not in op or 'owner_space' in op or 'target_space' in op:raise Failure('INVALID_REQUEST','IK requires chain_count and does not accept copy-space settings')
                if ('pole_subtarget' in op or 'pole_angle_deg' in op) and 'pole_target' not in op:raise Failure('INVALID_REQUEST','Pole settings require pole_target')
            elif set(op)&ik:raise Failure('INVALID_REQUEST','IK settings on copy constraint')
        if kind=='action.curve':
            frames=[k['frame'] for k in op['keys']]
            if frames!=sorted(set(frames)):raise Failure('INVALID_REQUEST','Keyframe times must be unique and increasing')
        if kind=='nla.add' and op['action_end']<=op['action_start']:raise Failure('INVALID_REQUEST','NLA action range must be positive')
        for key in ('channel',):
            for channel in ([op[key]] if key in op else [])+op.get('sources',[]):
                if channel['component']>=(4 if channel['property']=='rotation_quaternion' else 3):raise Failure('INVALID_REQUEST','Channel component out of range')
        if 'frames' in op and op['frames']!=sorted(op['frames']):raise Failure('INVALID_REQUEST','Sample frames must be increasing')
    return manifest
