# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded native Draw replay. No script text, implicit backend, or algorithm fallback."""
import math, os
from pathlib import Path
from scene_contract import obj,array,enum,integer,number,NAME,validate
from node_contract import FILE
from exchange_contract import descriptor
from protocol import Failure

VEC=array(number(-10000,10000),3,3)
VIEW=obj({'rotation':array(number(-1,1),4,4),'location':VEC,'distance':number(.01,10000),
          'region_size':array(integer(64,4096),2,2)})
SAMPLE=obj({'location':VEC,'pressure':number(0,1),'time':number(0,3600)})
MANIFEST=obj({'version':{'const':'1.0'},'backend':{'const':'WINDOWS_GUI_DRAW_V1'},
    'source':FILE,'object':NAME,'brush':obj({'type':{'const':'DRAW'},'mode':enum('NORMAL','INVERT'),
    'strength':number(.001,1),'radius_pixels':integer(1,500)}),'view':VIEW,
    'stroke':array(SAMPLE,2,256),'max_displacement':number(1e-6,1000)})
PARAMS=obj({'manifest':MANIFEST})

def normalize(params):
    validate(params,PARAMS)
    m=params['manifest'];descriptor(m['source'])
    if Path(m['source']['file']).suffix.lower()!='.blend':raise Failure('INVALID_REQUEST','Sculpt source must be .blend')
    if abs(sum(x*x for x in m['view']['rotation'])-1)>1e-6:raise Failure('INVALID_REQUEST','View quaternion must have unit length')
    ts=[p['time'] for p in m['stroke']]
    if ts[0]!=0 or any(b<=a for a,b in zip(ts,ts[1:])):raise Failure('INVALID_REQUEST','Stroke times must start at zero and strictly increase')
    if not any(p['pressure']>0 for p in m['stroke']):raise Failure('INVALID_REQUEST','Stroke requires positive pressure')
    if os.name!='nt':raise Failure('UNSUPPORTED','Native GUI Draw backend currently requires Windows desktop')
    return params
