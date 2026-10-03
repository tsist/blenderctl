# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit rest-frame/chain retargeting, with no automatic mapping application."""
from copy import deepcopy
from scene_contract import obj,enum,array,number,integer,NAME,BOOL
SHA={'type':'string','pattern':'^[0-9a-f]{64}$'}
OP=obj({'op':{'const':'animation.retarget_pose'},'adapter':{'const':'REST_FRAME_V1'},
 'source':NAME,'target':NAME,'name':NAME,'source_rest_sha256':SHA,'target_rest_sha256':SHA,
 'mapping':array(obj({'source':NAME,'target':NAME,'domain':enum('BODY','FINGER','FACE','CLOTHING')}),1,2048),
 'frames':{**array(integer(-10000,100000),1,120),'uniqueItems':True},'translation_scale':number(.0001,10000),
 'root_motion':enum('STATIC','SOURCE_DELTA'),'replace':BOOL,'max_matrix_error':number(1e-7,1e-3)})
OP['properties']['affine_policy']=enum('REJECT','SVD_HELPERS')

# A separate shape keeps REST_FRAME_V1 strict and backward compatible. Chains
# include the source bone itself and exclude its mapped ancestor (if any).
FRAME_OP = OP
CHAIN_OP = deepcopy(FRAME_OP)
CHAIN_OP['properties']['adapter'] = {'const': 'REST_CHAIN_V1'}
CHAIN_OP['properties']['mapping']['items']['properties']['source_chain'] = {
    **array(NAME, 1, 2048), 'uniqueItems': True}
CHAIN_OP['properties']['mapping']['items']['required'].append('source_chain')
BUDGET_CHAIN_OP = deepcopy(CHAIN_OP)
BUDGET_CHAIN_OP['properties']['max_rest_error'] = number(1e-7, 1e-4)
BUDGET_CHAIN_OP['properties']['affine_policy'] = {'const': 'SVD_HELPERS'}
BUDGET_CHAIN_OP['required'].extend(['max_rest_error', 'affine_policy'])
CONTROL_OP = obj({'op': {'const': 'animation.retarget_pose'}, 'adapter': {'const': 'CONTROL_CHANNELS_V1'},
 'source': NAME, 'target': NAME, 'name': NAME, 'source_rest_sha256': SHA, 'target_rest_sha256': SHA,
 'mapping': array(obj({'source': NAME, 'target': NAME,
     'properties': {**array(enum('location', 'rotation_quaternion', 'scale'), 1, 3), 'uniqueItems': True}}), 1, 256),
 'frames': deepcopy(FRAME_OP['properties']['frames']), 'translation_scale': number(.0001, 10000),
 'root_motion': enum('KEEP', 'SOURCE_WORLD_DELTA'), 'replace': BOOL,
 'witness_bones': {**array(NAME, 1, 256), 'uniqueItems': True}, 'max_matrix_error': number(1e-7, 1e-3)})
TIMES = {**array(number(-10000, 100000), 2, 120), 'uniqueItems': True}
ACTION_SET_OP = obj({'op': {'const': 'animation.retarget_pose'}, 'adapter': {'const': 'CONTROL_ACTION_SET_V1'},
 'source': NAME, 'target': NAME, 'source_rest_sha256': SHA, 'target_rest_sha256': SHA,
 'mapping': deepcopy(CONTROL_OP['properties']['mapping']), 'translation_scale': number(.0001, 10000),
 'root_motion': {'const': 'KEEP'}, 'replace': {'type':'boolean','const': True}, 'max_matrix_error': number(1e-7, 1e-3),
 'target_baseline': obj({'action': NAME, 'slot': {**NAME, 'maxLength': 255}}),
 'clips': array(obj({'source_action': NAME, 'source_slot': {**NAME, 'maxLength': 255}, 'name': NAME, 'frames': TIMES}), 1, 16),
 'active_action': NAME,
 'budget': obj({'max_total_bones': integer(2, 4096), 'max_bone_frame_samples': integer(1, 2000000)}),
 'quality': obj({'max_local_matrix_error': number(1e-7, .01),
     'witnesses': array(obj({'source': NAME, 'target': NAME, 'domain': enum('BODY', 'FINGER', 'FACE', 'CLOTHING'),
         'min_motion': number(0, 10)}), 1, 256)})})
OP = {'oneOf': [FRAME_OP, CHAIN_OP, BUDGET_CHAIN_OP, CONTROL_OP, ACTION_SET_OP]}
