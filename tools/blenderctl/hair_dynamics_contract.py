# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded native ribbon Cloth driven by managed surface guides."""
from scene_contract import obj,array,number,integer,NAME
LEGACY=obj({'op':{'const':'hair.dynamics'},'adapter':{'const':'RIBBON_CLOTH_V1'},
    'source':NAME,'name':NAME,'proxy_name':NAME,'collection':NAME,
    'width':number(.0001,.2),'quality':integer(4,20),'mass':number(.001,1),
    'tension':number(1,100),'bending':number(.001,10),'air_damping':number(0,10),
    'collision':NAME,'collision_distance':number(.001,.1),'hide_source':{'type':'boolean'}})
MULTI=obj({**{k:v for k,v in LEGACY['properties'].items() if k!='collision'},
    'adapter':{'const':'RIBBON_COLLIDERS_V1'},'collision_collection':NAME,
    'colliders':array(obj({'object':NAME,'distance_mode':{'enum':['ORIENTED_SURFACE','UNSIGNED_SURFACE']}}),1,8),
    'frame_start':integer(1,10000),'frame_end':integer(1,10000),
    'collision_quality':integer(2,8)})
MULTI['properties']['minimum_clearance']=number(0,.1)
OP={'oneOf':[LEGACY,MULTI]}
OPS={'hair.dynamics':OP}
