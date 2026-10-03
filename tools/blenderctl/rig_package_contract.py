# SPDX-License-Identifier: GPL-3.0-or-later
"""Whole-project native rig delivery; never an implicit selection export."""
from protocol import Failure
from scene_contract import obj,array,enum,number,NAME,validate
MANIFEST=obj({'adapter':{'const':'NATIVE_RIG_PACKAGE_V1'},'scope':{'const':'WHOLE_PROJECT'},'scene':NAME,'view_layer':NAME,'objects':{**array(NAME,1,16),'uniqueItems':True},'frames':{**array(number(1,100000),2,120),'uniqueItems':True}})
def normalize(v):
    validate(v,MANIFEST)
    if v['frames']!=sorted(v['frames']) or v['frames'][-1]-v['frames'][0]>59:raise Failure('INVALID_REQUEST','Native rig witness frames must increase over at most 59 frames')
    return v
