# SPDX-License-Identifier: GPL-3.0-or-later
"""Versioned public evidence envelope for explicit S08 adapters."""
from scene_contract import obj,array,integer,number
DETAILS=[]
for adapter,extra in [
 ('CONTROL_ACTION_SET_V1',{'clips':{'type':'array','minItems':1},'budget':{'type':'object'},'source_binding_restored':{'const':True},'original_actions_unchanged':{'const':True},'driver_inventory_unchanged':{'const':True}}),
 ('SAME_REST_SKIN_V1',{'maximum_rest_error':number(0,.0001),'mesh_data_unchanged':{'const':True},'driver_changes_explicit':{'const':True},'samples':{'type':'array'}}),
 ('REST_FRAME_V1',{'maximum_matrix_error':number(0,.001),'maximum_head_error':number(0,100),'maximum_tail_error':number(0,100),'mapping':{'type':'array'},'frames':{'type':'array'},'helpers':{'type':'object'}}),
 ('REST_CHAIN_V1',{'maximum_matrix_error':number(0,.001),'maximum_head_error':number(0,100),'maximum_tail_error':number(0,100),'mapping':{'type':'array'},'frames':{'type':'array'},'helpers':{'type':'object'}}),
 ('CONTROL_CHANNELS_V1',{'maximum_channel_error':number(0,.001),'maximum_root_world_error':number(0,.001),'source_unchanged':{'const':True},'driver_inventory_unchanged':{'const':True},'unmapped_action_curves_unchanged':{'const':True},'samples':{'type':'array'}}),
 ('SURFACE_BARYCENTRIC_V1',{'source_unchanged':{'const':True},'max_normalization_error':number(0,.001),'vertices':{'type':'array'}}),
 ('NEAREST_SEGMENT_V1',{'max_normalization_error':number(0,1e-6),'vertices':{'type':'array'}})]:
    DETAILS.append({'type':'object','required':['adapter',*extra], 'properties':{'adapter':{'const':adapter},**extra}})
REPORT=obj({'version':{'const':'1.0'},'operations':array(obj({'index':integer(0,499),'op':{'enum':['animation.retarget_pose','skin.transfer_weights','skin.auto_weights','skin.rebind']},'detail':{'oneOf':DETAILS}}),1,500)})
