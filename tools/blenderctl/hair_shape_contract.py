# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit evaluated-surface region, arc resampling and native radius profile."""
from scene_contract import obj,array,number,integer,NAME,BOOL
V=array(number(-100,100),3,3)
OP=obj({'op':{'const':'hair.shape'},'adapter':{'const':'TRIANGLE_GROOM_V1'},
    'source':NAME,'surface':NAME,'name':NAME,'collection':NAME,
    'region_triangles':array(integer(0,1000000),1,8192),
    'max_projection_distance':number(.000001,1),'root_offset':number(0,.05),
    'points_per_curve':integer(2,64),'length':number(.00001,10),
    'controls':array(obj({'id':integer(0,2147483647),'points':array(V,2,64)}),0,128),
    'radius_profile':array(obj({'u':number(0,1),'radius':number(0,.05)}),2,32),
    'frame_start':integer(1,10000),'frame_end':integer(1,10000),'hide_source':BOOL})
OPS={'hair.shape':OP}

def normalize(op):
    from scene_contract import validate
    from protocol import Failure
    validate(op,OP)
    def bad(s):raise Failure('INVALID_REQUEST',s)
    if len(set(op['region_triangles']))!=len(op['region_triangles']):bad('Surface region triangles must be unique')
    if not 1<=op['frame_end']-op['frame_start']<=31:bad('Shape witness range must contain 2..32 contiguous integer frames')
    p=op['radius_profile']
    if p[0]['u']!=0 or p[-1]['u']!=1 or any(a['u']>=b['u'] for a,b in zip(p,p[1:])) or not any(r['radius']>0 for r in p):bad('Radius profile requires increasing stations from 0 to 1 and a positive radius')
    if len({c['id'] for c in op['controls']})!=len(op['controls']):bad('Duplicate guide control IDs')
    for c in op['controls']:
        if c['points'][0]!=[0,0,0] or any(a==b for a,b in zip(c['points'],c['points'][1:])):bad('Controls must begin at zero without consecutive duplicate points')
    return op
