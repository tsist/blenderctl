# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit, bounded rest-surface weight transfer contract."""
from scene_contract import obj,enum,array,number,integer,NAME,BOOL
from model_contract import SHA

OP=obj({'op':{'const':'skin.transfer_weights'},'adapter':{'const':'SURFACE_BARYCENTRIC_V1'},
    'source':NAME,'target':NAME,'rig':NAME,'source_topology_sha256':SHA,'target_topology_sha256':SHA,
    'data_scope':enum('reject_shared','single_user'),'mapping':array(obj({'source':NAME,'target':NAME}),1,256),
    'max_distance':number(1e-8,1000),'max_influences':integer(1,32),'max_normalization_error':number(1e-8,.001),
    'purpose':enum('CLOTHING','BODY'),'replace':BOOL})
OPS={'skin.transfer_weights':OP}
AUTO_OP=obj({'op':{'const':'skin.auto_weights'},'adapter':{'const':'NEAREST_SEGMENT_V1'},
    'target':NAME,'rig':NAME,'bones':{**array(NAME,1,256),'uniqueItems':True},
    'data_scope':enum('reject_shared','single_user'),'max_distance':number(1e-8,1000),
    'max_influences':integer(1,32),'power':number(1,4),'replace':BOOL,'purpose':{'const':'BODY'}})
OPS['skin.auto_weights']=AUTO_OP
