# SPDX-License-Identifier: GPL-3.0-or-later
"""S07 font and conversion contracts, with no new runtime dependency."""
from scene_contract import obj,array,integer,number,enum,BOOL,NAME
SHA={'type':'string','pattern':'^[0-9a-f]{64}$'}
FILE=obj({'file':{'type':'string','minLength':1},'expected_sha256':SHA})
TEXT_LAYOUT=obj({'align_x':enum('LEFT','CENTER','RIGHT'),'align_y':enum('TOP','TOP_BASELINE','CENTER','BOTTOM_BASELINE','BOTTOM'),
    'space_character':number(.1,10),'space_word':number(.1,10),'space_line':number(.1,10),
    'offset':number(-10,10),'shear':number(-1,1),'resolution':integer(1,32),'bevel_depth':number(0,10),'bevel_resolution':integer(0,8)})
VERIFY=obj({'adapter':{'const':'CURVE_FONT_V1'},'fonts':array(FILE,0,4),'max_coordinate_error':number(1e-9,1e-3)})
CURVE_SETTINGS={'oneOf':[obj({'dimensions':{'const':dimension},'fill_mode':enum(*modes),
    'bevel_resolution':integer(0,8),'use_fill_caps':BOOL,'extrude':number(0,1000)})
    for dimension,modes in [('2D',('NONE','FRONT','BACK','BOTH')),('3D',('FULL','BACK','FRONT','HALF'))]]}
CONVERSION_REPORT=obj({'operation_index':integer(0,499),'curve_conversion_report_version':{'const':'1.0'},
    'adapter':{'const':'CURVE_FONT_V1'},'source':{'type':'object'},'target':{'type':'object'},
    'vertices':integer(1,1000000),'faces':integer(1,1000000),'maximum_coordinate_error':number(0,1e-3),
    'coordinate_tolerance':number(1e-9,1e-3),'topology':{'const':'exact_evaluated_match'},
    'materials':{'const':'exact_match'},'world_transform':{'const':'preserved'},
    'glyph_validation':{'const':'cmap_not_visual_quality'},'source_data_retained':{'type':'string'}})

def input_documents(params):
    docs=[]
    for op in params['manifest']['operations']:
        if op.get('font'):docs.append(op['font'])
        if op.get('verify'):docs+=op['verify']['fonts']
    return docs
