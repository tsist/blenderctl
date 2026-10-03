# SPDX-License-Identifier: GPL-3.0-or-later
"""S06 explicit, density-preserving UDIM layout contract."""
from scene_contract import obj, array, integer, number, enum, NAME, BOOL

SHA={'type':'string','pattern':'^[0-9a-f]{64}$'}
TILE=integer(1001,1100)
ISLAND=obj({'seed_face':integer(0,999999),'tile':TILE,'locked':BOOL},['seed_face','locked'])
TARGET=obj({'object':NAME,'data_scope':enum('reject_shared','single_user','shared'),
    'layer':NAME,'topology_sha256':SHA,'uv_sha256':SHA,'source_resolution':integer(16,16384),
    'islands':array(ISLAND,1,512)})
DENSITY={'oneOf':[obj({'mode':{'const':'PRESERVE'}}),obj({'mode':{'const':'TARGET'},'pixels_per_meter':number(.000001,1e7)})]}
LAYOUT=obj({'op':{'const':'uv.layout'},'adapter':{'const':'DETERMINISTIC_UDIM_V1'},
    'targets':array(TARGET,1,32),'tiles':array(obj({'number':TILE,'resolution':integer(16,16384)}),1,100),
    'density':DENSITY,'meters_per_unit':number(.000001,1e6),'margin_pixels':number(0,1024),
    'allow_rotate':BOOL,'allow_scale':BOOL})

# Fixed before testing; tolerances never grow to make a failed packing pass.
UV_EPS=2e-5
AREA_EPS=1e-10
DENSITY_REL_EPS=2e-4

N=number(0,1e300)
BOX=array(number(-1e6,1e6),4,4)
ISLAND_REPORT=obj({'key':{'type':'string','minLength':1,'maxLength':300},'object':NAME,'layer':NAME,'seed_face':integer(0,999999),
    'faces':array(integer(0,999999),1,1000000),'tile':TILE,'selected':BOOL,'locked':BOOL,
    'world_area_m2':N,'initial_density':N,'target_density':N,'scale':N,'bounds_before':BOX,
    'rotation_degrees':enum(0,90),'bounds_after':BOX,'uv_area':N,'observed_density':N,
    'triangle_density_range':array(N,2,2),'density_relative_error':N})
CHECK=obj({'triangle_pairs':integer(0,2000000),'maximum_overlap_area':N,
    'minimum_checked_gap_uv':{'oneOf':[N,{'type':'null'}]}})
REPORT=obj({'version':{'const':'1.0'},'layouts':array(obj({'operation_index':integer(0,499),
    'uv_layout_report_version':{'const':'1.0'},'adapter':{'const':'DETERMINISTIC_UDIM_V1'},
    'density_scope':{'const':'BASE_MESH_WORLD_METRIC_AT_CONTEXT_FRAME'},
    'tolerances':obj({'uv':{'const':UV_EPS},'overlap_area':{'const':AREA_EPS},'density_relative':{'const':DENSITY_REL_EPS}}),
    'islands':array(ISLAND_REPORT,1,2048),'tiles':{'type':'object','patternProperties':{'^10[0-9]{2}$|^1100$':CHECK},'additionalProperties':False},
    'preservation':{'const':'pass'},'unselected_outside_tiles':{'const':'unchanged_not_validated'},'packing':{'const':'deterministic_conservative_rectangles; failure_is_not_proof_of_geometric_infeasibility'}}),1,500)})
