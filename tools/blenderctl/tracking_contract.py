# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded, hashed PNG sequence contract for native camera tracking."""
from pathlib import Path
import math
from copy import deepcopy
from protocol import Failure
from scene_contract import obj,array,number,integer,enum,NAME,validate
from model_contract import SHA

PATH={'type':'string','minLength':1}
RESOURCE=obj({'frame':integer(1,120),'file':PATH,'expected_sha256':SHA})
RESOURCES=array(RESOURCE,2,120)
MARKER=obj({'frame':integer(1,120),'co':array(number(0,1),2,2)})
PREPARE=obj({'clip_name':NAME,'scene':NAME,'sequence':RESOURCES,
 'width':integer(32,4096),'height':integer(32,4096),'fps':number(1,120),
 'focal_length_pixels':number(1,100000),'distortion':{'const':'NONE'},
 'mode':enum('TRACK','EXPLICIT'),'tracks':array(obj({'name':NAME,'markers':array(MARKER,1,120)}),8,256),
 'pattern_size':integer(5,101),'search_size':integer(7,301),'correlation_min':number(.1,1)})
LEGACY_PREPARE=PREPARE
DOCUMENT=obj({'file':PATH,'expected_sha256':SHA})
SOURCE_RESOURCE=obj({'frame':integer(1,1048574),'file':PATH,'expected_sha256':SHA})
SOURCE_TIME={'first_frame':integer(1,1048574),'frame_step':integer(1,120),
             'frame_count':integer(2,120),'fps':number(1,120)}
SOURCE={'oneOf':[
 obj({'kind':{'const':'PNG_SEQUENCE'},**SOURCE_TIME,'files':array(SOURCE_RESOURCE,2,120)}),
 obj({'kind':{'const':'MOVIE'},**SOURCE_TIME,**DOCUMENT['properties']})]}
INPUT_PREPARE=obj({'adapter':{'const':'CLIP_INPUT_V1'},'source':SOURCE,
 'scene_start':integer(1,1048455),**{k:v for k,v in LEGACY_PREPARE['properties'].items() if k!='sequence'}})
CALIBRATION=obj({'model':{'const':'POLYNOMIAL_V1'},'principal_point_pixels':array(number(0,4096),2,2),
                 'k1':number(-1,1),'k2':number(-1,1),'k3':number(-1,1),
                 'reference':{'type':'string','minLength':1,'maxLength':1024}})
INPUT_PREPARE['properties']['calibration']=CALIBRATION
INPUT_PREPARE['properties']['distortion']=enum('NONE','POLYNOMIAL')
PREPARE={'oneOf':[LEGACY_PREPARE,INPUT_PREPARE]}
SOLVE=obj({'clip':NAME,'camera_name':NAME,'points_name':NAME,'keyframe_a':integer(1,120),'keyframe_b':integer(1,120),
 'min_tracks':integer(8,256),'max_missing_fraction':number(0,.5),'min_parallax_degrees':number(.01,45),
 'min_nonplanarity_ratio':number(.00001,1),'max_reprojection_error':number(.0001,10),'max_average_error':number(.0001,10)})
SOLVE['properties']['preview_points']=obj({'radius_pixels':number(1,6)})
PREPARE_PARAMS=obj({'manifest':PREPARE})
RESOURCE_INPUT={'oneOf':[RESOURCES,DOCUMENT]}
INSPECT_PARAMS=obj({'file':PATH,'expected_sha256':SHA,'clip':NAME,'resources':RESOURCE_INPUT})
SOLVE_PARAMS=obj({'file':PATH,'expected_sha256':SHA,'manifest':SOLVE,'resources':RESOURCE_INPUT})

def resource_rows(value):
    if isinstance(value,list):return deepcopy(value)
    from filesystem import FileGuard
    from protocol import read_json
    with FileGuard(value['file']) as guard:
        if guard.sha256()!=value['expected_sha256']:raise Failure('CONFLICT','Tracking resources descriptor changed')
        rows=read_json(value['file']);validate(rows,RESOURCES)
    return rows

def calibration_check(m):
    c=m.get('calibration')
    if not c:
        if m['distortion']!='NONE':raise Failure('INVALID_REQUEST','Polynomial distortion requires explicit calibration')
        return
    cx,cy=c['principal_point_pixels'];w,h=m['width'],m['height'];f=m['focal_length_pixels']
    if not 0<cx<w or not 0<cy<h:raise Failure('INVALID_REQUEST','Principal point must lie inside the image')
    if m['distortion']=='NONE' and any(c[k] for k in ('k1','k2','k3')):raise Failure('INVALID_REQUEST','NONE distortion requires zero coefficients')
    # Verify radial scale and radial derivative over twice the corner radius.
    end=4*(max(cx,w-cx)**2+max(cy,h-cy)**2)/f**2
    for a,b,d in ((c['k1'],c['k2'],c['k3']),(3*c['k1'],5*c['k2'],7*c['k3'])):
        samples=[0,end]
        if d:
            discriminant=4*b*b-12*d*a
            if discriminant>=0:samples.extend((-2*b+s*math.sqrt(discriminant))/(6*d) for s in (-1,1))
        elif b:samples.append(-a/(2*b))
        if any(1+a*u+b*u*u+d*u*u*u<.1 for u in samples if 0<=u<=end):raise Failure('UNSUPPORTED','Calibration folds or approaches a singular radial map in the declared field')
    if 1+c['k1']*end+c['k2']*end**2+c['k3']*end**3<.5:raise Failure('UNSUPPORTED','Calibration radial map does not cover image corners')

def normalize(params,command):
    validate(params,{'tracking.prepare':PREPARE_PARAMS,'tracking.inspect':INSPECT_PARAMS,'tracking.solve':SOLVE_PARAMS}[command])
    result=deepcopy(params)
    extended=command=='tracking.prepare' and result['manifest'].get('adapter')=='CLIP_INPUT_V1'
    if extended:
        m=result['manifest'];source=m['source'];n=source['frame_count']
        indices=[source['first_frame']+i*source['frame_step'] for i in range(n)]
        if indices[-1]>1048574:raise Failure('INVALID_REQUEST','Source frame range exceeds native frame limit')
        if abs(m['fps']-source['fps']/source['frame_step'])>1e-7:raise Failure('INVALID_REQUEST','Output fps must equal source fps divided by frame step')
        rows=source['files'] if source['kind']=='PNG_SEQUENCE' else []
        if rows and [r['frame'] for r in rows]!=indices:raise Failure('INVALID_REQUEST','PNG source frames must exactly match the declared window')
        if source['kind']=='MOVIE':
            p=Path(source['file'])
            if not p.is_absolute() or p.suffix.lower() not in ('.mp4','.mov','.mkv','.avi'):raise Failure('INVALID_REQUEST','Movie requires an absolute MP4/MOV/MKV/AVI path')
            source['file']=str(p.resolve())
    else:
        value=result['manifest']['sequence'] if command=='tracking.prepare' else result['resources']
        if isinstance(value,dict) and not Path(value['file']).is_absolute():raise Failure('INVALID_REQUEST','Resources descriptor requires an absolute path')
        rows=resource_rows(value);n=len(rows)
    if not extended and [r['frame'] for r in rows]!=list(range(1,len(rows)+1)):
        raise Failure('INVALID_REQUEST','Sequence frames must be consecutive from one')
    paths=set()
    for row in rows:
        p=Path(row['file'])
        if not p.is_absolute() or p.suffix.lower()!='.png':raise Failure('INVALID_REQUEST','Sequence requires absolute PNG paths')
        row['file']=str(p.resolve());key=row['file'].casefold()
        if key in paths:raise Failure('INVALID_REQUEST','Duplicate sequence path')
        paths.add(key)
    if command!='tracking.prepare':
        p=Path(result['file'])
        if not p.is_absolute() or p.suffix.lower()!='.blend':raise Failure('INVALID_REQUEST','Tracking input requires an absolute blend path')
        result['file']=str(p.resolve())
    if command=='tracking.prepare':
        m=result['manifest']
        if extended:calibration_check(m)
        if m['width']*m['height']*n>150000000:raise Failure('RESOURCE_LIMIT','Sequence pixel budget exceeded')
        if m['pattern_size']%2!=1 or m['search_size']%2!=1 or m['search_size']<=m['pattern_size']:
            raise Failure('INVALID_REQUEST','Pattern/search sizes must be odd and search larger')
        names=set();observations=0
        for t in m['tracks']:
            if t['name'] in names:raise Failure('INVALID_REQUEST','Duplicate track name')
            names.add(t['name']);frames=[x['frame'] for x in t['markers']]
            if frames!=sorted(set(frames)) or frames[-1]>n:raise Failure('INVALID_REQUEST','Track frames must be unique, ordered and inside sequence')
            if m['mode']=='TRACK' and frames!=[1]:raise Failure('INVALID_REQUEST','TRACK accepts only first-frame seeds')
            observations+=len(frames)
        if observations>30000:raise Failure('RESOURCE_LIMIT','Observation budget exceeded')
    if command=='tracking.solve':
        m=result['manifest']
        if not m['keyframe_a']<m['keyframe_b']<=n:raise Failure('INVALID_REQUEST','Keyframes must be ordered inside sequence')
        if m['camera_name']==m['points_name']:raise Failure('INVALID_REQUEST','Camera and pointcloud names must differ')
    return result

def input_documents(params):
    m=params.get('manifest',{})
    if m.get('adapter')=='CLIP_INPUT_V1':
        s=m['source'];return s['files'] if s['kind']=='PNG_SEQUENCE' else [s]
    if 'sequence' in m:return m['sequence']
    value=params['resources'];return resource_rows(value)+([value] if isinstance(value,dict) else [])

TEXT={'type':'string'}
NULL_NUMBER={'oneOf':[number(),{'type':'null'}]}
VEC=array(number(),3,3)
OBSERVATION=obj({'tracks':array(obj({'name':NAME,'markers':array(obj({'frame':integer(0,121),'co':array(number(),2,2),'mute':{'type':'boolean'}}),0,122)}),0,256),
 'cameras':array(obj({'frame':integer(1,120),'matrix':array(array(number(),4,4),4,4)}),0,120),
 'bundles':array(obj({'name':NAME,'co':VEC}),0,256)})
INTRINSICS=obj({'focal_length_pixels':number(),'principal_point':array(number(),2,2),'pixel_aspect':number(),
 'distortion_model':TEXT,'k1':number(),'k2':number(),'k3':number()})
REPORT=obj({'tracking_report_version':{'const':'1.0'},'stage':enum('PREPARE','INSPECT','SOLVE'),'clip':NAME,'scene':NAME,
 'mode':enum('TRACK','EXPLICIT'),'frames':integer(2,120),'tracks':integer(0,256),'markers_sha256':SHA,'intrinsics':INTRINSICS,
 'reconstruction_valid':{'type':'boolean'},'camera_count':integer(0,120),'bundle_count':integer(0,256),
 'native_average_error':NULL_NUMBER,'fps':number(1,120),'fps_source':TEXT,'scale':TEXT,'quality_scope':TEXT,'scope':TEXT})
REPORT['properties']['tracking_report_version']=enum('1.0','1.1')
REPORT['properties'].update(adapter={'const':'CLIP_INPUT_V1'},scene_start=integer(1,1048574),scene_end=integer(1,1048574),
                            projection_space=TEXT,calibration_basis=TEXT)
INPUT_REPORT=obj({'version':{'const':'1.0'},'adapter':{'const':'CLIP_INPUT_V1'},'kind':enum('PNG_SEQUENCE','MOVIE'),
 'source_fps':number(1,120),'scene_fps':number(1,120),'scene_start':integer(1,1048455),
 'frames':array(obj({'clip_frame':integer(1,120),'source_frame':integer(1,1048574),'scene_frame':integer(1,1048574),
                    'source_seconds':number(0,1048574),'source':obj({'file':PATH,'sha256':SHA}),'decoded_sha256':SHA}),2,120),
 'time_basis':TEXT,'decode':TEXT})
REVIEW_REPORT=obj({'version':{'const':'1.0'},'primary_sha256':SHA,'review_sha256':SHA,'point_count':integer(8,256),
 'radius_pixels_at_first_frame':number(1,6),'camera_reopen_max_error':number(0,1e-5),'geometry_reopen_max_error':number(0,1e-5),
 'projection_space':{'const':'UNDISTORTED_PINHOLE'},'calibration':{'oneOf':[CALIBRATION,{'type':'null'}]},
 'external_files':array(PATH,0,0),'scope':TEXT})
QUALITY=obj({'tracking_quality_version':{'const':'1.0'},'passed':{'type':'boolean'},'failures':array(obj({'code':TEXT,'detail':TEXT}),0,40000),
 'frames':array(obj({'frame':integer(1,120),'tracks':integer(0,256)}),0,120),'bundle_count':integer(0,256),'camera_count':integer(0,120),
 'missing_fraction':number(0,1),'native_average_error':NULL_NUMBER,'reprojection_max_pixels':NULL_NUMBER,'reprojection_rms_pixels':NULL_NUMBER,
 'minimum_depth':NULL_NUMBER,'nonplanarity_ratio':number(0,1),'median_parallax_degrees':number(0,180),'limits':SOLVE,'scope':TEXT})
