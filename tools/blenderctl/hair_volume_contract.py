# SPDX-License-Identifier: GPL-3.0-or-later
"""Separate strand population and sampled density-field contracts."""
from scene_contract import obj,array,number,integer,NAME,BOOL,validate
from protocol import Failure
BASE={'source':NAME,'name':NAME,'collection':NAME,'frame_start':integer(1,10000),'frame_end':integer(1,10000),'hide_source':BOOL}
DENSITY=obj({'op':{'const':'hair.density'},'adapter':{'const':'TRIANGLE_CHILDREN_V1'},**BASE,
    'region_triangles':{**array(integer(0,1000000),1,8192),'uniqueItems':True},'count':integer(1,512),'seed':integer(0,2147483647),'max_guide_distance':number(.00001,10),'root_offset':number(0,.05),'radius_scale':number(.01,10)})
VOLUME=obj({'op':{'const':'hair.volume'},'adapter':{'const':'CURVE_DENSITY_GRID_V1'},**BASE,
    'voxel_size':number(.0001,1),'kernel_radius':number(.0002,2),'density':number(.001,100),'max_voxels':integer(8,1000000),'color':array(number(0,1),3,3)})
OPS={'hair.density':DENSITY,'hair.volume':VOLUME}
def normalize(op):
    validate(op,OPS[op['op']])
    if not 1<=op['frame_end']-op['frame_start']<=31:raise Failure('INVALID_REQUEST','Density/volume requires 2..32 consecutive integer frames')
    if op['op']=='hair.volume' and not 2*op['voxel_size']<=op['kernel_radius']<=16*op['voxel_size']:raise Failure('INVALID_REQUEST','Density kernel radius must be between 2 and 16 voxels')
    return op
