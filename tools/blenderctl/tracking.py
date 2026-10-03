# SPDX-License-Identifier: GPL-3.0-or-later
"""Managed PNG sequence tracking and native reconstruction with explicit resources."""
import bpy
import json
import hashlib
import math
import shutil
import struct
from pathlib import Path
from contextlib import contextmanager
from mathutils import Matrix
from protocol import Failure,atomic_json,digest
from tracking_contract import validate,REPORT,OBSERVATION,RESOURCES,INPUT_REPORT,QUALITY
import tracking_quality

KEY='blenderctl_tracking_v1'

def hashed(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def reject(message,code='VALIDATION_FAILED'):
    raise Failure(code,message)

def resource_check(rows,complete=False):
    """Caller holds supervisor FileGuards, or owns unpublished job-local copies."""
    if not rows:reject('Tracking sequence is empty')
    if [r['frame'] for r in rows]!=list(range(1,len(rows)+1)):reject('Tracking resources must be ordered consecutive frames from one')
    paths=[]
    for row in rows:
        p=Path(row['file'])
        if not p.is_absolute() or p.suffix.lower()!='.png' or not p.is_file():reject('Tracking requires existing absolute PNG resources')
        if digest(p)!=row['expected_sha256']:reject('Tracking resource SHA256 mismatch: '+p.name)
        paths.append(p.resolve())
    if len(set(paths))!=len(paths):reject('Tracking resource paths must be unique')
    if complete:
        directories={p.parent for p in paths}
        if len(directories)!=1:reject('Managed sequence must reside in one directory')
        actual={p.resolve() for p in next(iter(directories)).iterdir() if p.is_file()}
        if actual!=set(paths):reject('Managed sequence directory differs from complete declared file set')
    return paths

def copied(rows,job):
    paths=resource_check(rows)
    directory=job/'frames';directory.mkdir()
    result=[]
    for row,p in zip(rows,paths):
        target=directory/f'frame{row["frame"]:06d}.png'
        shutil.copyfile(p,target)
        if digest(target)!=row['expected_sha256']:reject('Tracking resource copy SHA mismatch')
        result.append({'frame':row['frame'],'file':str(target.resolve()),'expected_sha256':row['expected_sha256']})
    return result

@contextmanager
def clip_context(clip):
    windows=bpy.context.window_manager.windows
    if not windows or not windows[0].screen.areas:reject('No background editor context available','UNSUPPORTED')
    window=windows[0];area=window.screen.areas[0];old_type=area.type
    area.type='CLIP_EDITOR';area.spaces.active.clip=clip
    region=next((r for r in area.regions if r.type=='WINDOW'),None)
    if region is None:reject('No clip editor window region','UNSUPPORTED')
    try:
        with bpy.context.temp_override(window=window,area=area,region=region):yield
    finally:area.type=old_type

def observations(clip):
    return {'tracks':[{'name':t.name,'markers':[{'frame':m.frame,'co':list(m.co),'mute':m.mute} for m in t.markers]} for t in clip.tracking.tracks],
            'cameras':[{'frame':c.frame,'matrix':[list(r) for r in c.matrix]} for c in clip.tracking.reconstruction.cameras],
            'bundles':[{'name':t.name,'co':list(t.bundle)} for t in clip.tracking.tracks if t.has_bundle]}

def marker_hash(clip):return hashed(observations(clip)['tracks'])

def intrinsics(clip):
    c=clip.tracking.camera
    return {'focal_length_pixels':c.focal_length_pixels,'principal_point':list(c.principal_point),
            'pixel_aspect':c.pixel_aspect,'distortion_model':c.distortion_model,
            'k1':c.k1,'k2':c.k2,'k3':c.k3}

def setup_intrinsics(clip,spec):
    c=clip.tracking.camera;c.sensor_width=36;c.focal_length_pixels=spec['focal_length_pixels']
    c.principal_point=(0,0);c.pixel_aspect=1;c.distortion_model='POLYNOMIAL';c.k1=c.k2=c.k3=0
    calibration=spec.get('calibration')
    if calibration:
        c.principal_point_pixels=calibration['principal_point_pixels']
        c.k1=calibration['k1'];c.k2=calibration['k2'];c.k3=calibration['k3']
    expected_principal=spec.get('calibration',{}).get('principal_point_pixels',[spec['width']/2,spec['height']/2])
    if abs(c.focal_length_pixels-spec['focal_length_pixels'])>1e-3 or max(abs(x-y) for x,y in zip(c.principal_point_pixels,expected_principal))>1e-3:
        reject('Native camera clamped the declared focal length or principal point','UNSUPPORTED')
    settings=clip.tracking.settings
    settings.use_tripod_solver=False;settings.use_keyframe_selection=False
    for key in ('refine_intrinsics_focal_length','refine_intrinsics_principal_point','refine_intrinsics_radial_distortion','refine_intrinsics_tangential_distortion'):
        setattr(settings,key,False)
    clip.use_proxy=False

def metadata(clip):
    raw=clip.get(KEY)
    if not isinstance(raw,str):reject('Clip is not managed by the tracking adapter','UNSUPPORTED')
    try:value=json.loads(raw)
    except (ValueError,TypeError):reject('Invalid tracking metadata')
    if value.get('version') not in ('1.0','1.1'):reject('Unsupported tracking metadata version','UNSUPPORTED')
    if (value.get('version')=='1.1')!=(value.get('recipe',{}).get('adapter')=='CLIP_INPUT_V1'):
        reject('Tracking metadata version and input adapter differ')
    if (value['version']=='1.1')!=('input' in value):reject('Tracking metadata version and input mapping differ')
    return value

def store(clip,value):clip[KEY]=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)

def safe_scene():
    if len(bpy.data.scenes)!=1 or len(bpy.data.movieclips)!=1 or bpy.data.libraries or bpy.data.texts:
        reject('Tracking requires one managed scene and clip without libraries or text scripts','UNSUPPORTED')
    scene=bpy.context.scene
    if scene.rigidbody_world or scene.compositing_node_group or (scene.sequence_editor and scene.sequence_editor.strips) or bpy.data.node_groups:
        reject('Tracking scene contains unmanaged evaluation systems','UNSUPPORTED')
    for collection in (bpy.data.objects,bpy.data.scenes,bpy.data.movieclips,bpy.data.cameras,bpy.data.meshes,bpy.data.materials,bpy.data.worlds):
        for block in collection:
            ad=getattr(block,'animation_data',None)
            if block.library or block.override_library or ad and (ad.drivers or ad.nla_tracks):reject('Tracking excludes linked IDs, drivers and NLA','UNSUPPORTED')
    for o in bpy.data.objects:
        if o.type not in ('CAMERA','MESH') or not o.get(KEY) or o.parent or o.constraints or o.modifiers or o.particle_systems or o.rigid_body:
            reject('Tracking scene contains unmanaged objects or evaluation','UNSUPPORTED')
    if bpy.data.images or bpy.data.sounds:reject('Tracking scene contains unmanaged media','UNSUPPORTED')
    if bpy.data.armatures or bpy.data.curves or bpy.data.hair_curves or bpy.data.metaballs or bpy.data.volumes or bpy.data.particles:
        reject('Tracking scene contains unmanaged geometry systems','UNSUPPORTED')

def opened(params):
    """Worker-only: source inputs are locked by the supervisor for the whole job.

    saved() also calls this on new, unpublished files owned by this job.
    A standalone caller must hold FileGuards on the blend and every PNG.
    """
    from tracking_contract import resource_rows
    params={**params,'resources':resource_rows(params['resources'])}
    if digest(params['file'])!=params['expected_sha256']:reject('Tracking blend SHA mismatch')
    resource_check(params['resources'],complete=True)
    bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    safe_scene()
    clip=bpy.data.movieclips[0];meta=metadata(clip)
    from tracking_contract import normalize
    normalize({'manifest':meta['recipe']},'tracking.prepare')
    if meta['sequence']!=params['resources']:reject('Resources differ from managed clip declaration')
    if meta['version']=='1.1':
        from tracking_input import verify_report
        verify_report(meta['recipe'],meta['sequence'],meta.get('input'))
    if Path(bpy.path.abspath(clip.filepath)).resolve()!=Path(meta['sequence'][0]['file']).resolve():reject('Clip filepath differs from declared sequence')
    start=meta['recipe'].get('scene_start',1) if meta.get('solve') else 1
    if clip.use_proxy or clip.use_proxy_custom_directory or clip.frame_start!=start or clip.frame_offset!=0:
        reject('Clip proxy or timing differs from managed sequence')
    if marker_hash(clip)!=meta['markers_sha256'] or intrinsics(clip)!=meta['intrinsics']:reject('Tracking markers or intrinsics differ from managed metadata')
    scene=bpy.context.scene;spec=meta['recipe']
    if scene.name!=spec['scene'] or scene.frame_start!=start or scene.frame_end!=start+len(meta['sequence'])-1:reject('Tracking scene or frame range changed')
    if abs(scene.render.fps/scene.render.fps_base-spec['fps'])>1e-4 or scene.render.resolution_x!=spec['width'] or scene.render.resolution_y!=spec['height'] or scene.render.resolution_percentage!=100 or scene.render.pixel_aspect_x!=1 or scene.render.pixel_aspect_y!=1:
        reject('Tracking scene dimensions or frame rate changed')
    solved=meta.get('solve')
    expected_objects={solved['camera_name'],solved['points_name']} if solved else set()
    if {o.name for o in bpy.data.objects}!=expected_objects:reject('Managed reconstruction objects differ from metadata')
    if solved:verify_objects(clip,solved,spec)
    return clip,meta

def verify_objects(clip,solved,recipe):
    camera=bpy.data.objects[solved['camera_name']];points=bpy.data.objects[solved['points_name']]
    if camera.type!='CAMERA' or points.type!='MESH' or points.animation_data or points.data.shape_keys:
        reject('Invalid managed reconstruction object types or animation')
    obs=observations(clip)
    if len(points.data.vertices)!=len(obs['bundles']) or json.loads(points['tracking_bundle_names'])!=[b['name'] for b in obs['bundles']]:reject('Bundle pointcloud differs from reconstruction')
    if max((max(abs(v.co[i]-b['co'][i]) for i in range(3)) for v,b in zip(points.data.vertices,obs['bundles'])),default=0)>1e-5:reject('Bundle positions differ from reconstruction')
    if max(abs(points.matrix_world[i][j]-(1 if i==j else 0)) for i in range(4) for j in range(4))>1e-7:reject('Bundle pointcloud transform changed')
    cx,cy=recipe.get('calibration',{}).get('principal_point_pixels',[recipe['width']/2,recipe['height']/2])
    sx=(recipe['width']/2-cx)/recipe['width'];sy=(recipe['height']/2-cy)/recipe['width']
    if bpy.context.scene.camera!=camera or camera.data.type!='PERSP' or camera.data.sensor_width!=36 or abs(camera.data.lens*recipe['width']/camera.data.sensor_width-recipe['focal_length_pixels'])>1e-3 or camera.data.sensor_fit!='HORIZONTAL' or abs(camera.data.shift_x-sx)>1e-6 or abs(camera.data.shift_y-sy)>1e-6:reject('Output camera intrinsics changed')
    frame=bpy.context.scene.frame_current
    for row in obs['cameras']:
        bpy.context.scene.frame_set(row['frame']+recipe.get('scene_start',1)-1)
        if max(abs(camera.matrix_world[i][j]-row['matrix'][i][j]) for i in range(4) for j in range(4))>1e-5:reject('Animated output camera differs from reconstruction')
    bpy.context.scene.frame_set(frame)

def outputs(clip,meta,job,stage):
    observation=observations(clip);rec=clip.tracking.reconstruction
    report={'tracking_report_version':'1.0','stage':stage,'clip':clip.name,'scene':bpy.context.scene.name,
            'mode':meta['recipe']['mode'],'frames':len(meta['sequence']),'tracks':len(clip.tracking.tracks),
            'markers_sha256':marker_hash(clip),'intrinsics':intrinsics(clip),'reconstruction_valid':rec.is_valid,
            'camera_count':len(rec.cameras),'bundle_count':sum(t.has_bundle for t in clip.tracking.tracks),
            'native_average_error':rec.average_error if rec.is_valid and math.isfinite(rec.average_error) else None,
            'fps':meta['recipe']['fps'],'fps_source':'DECLARED','scale':'ARBITRARY_MONOCULAR_SIMILARITY',
            'quality_scope':'Internal geometric consistency; independent external truth is a separate acceptance artifact',
            'scope':'Managed static-scene PNG tracking, fixed intrinsics, zero distortion; arbitrary footage and metric scale not guaranteed'}
    if meta['version']=='1.1':
        report.update(tracking_report_version='1.1',adapter='CLIP_INPUT_V1',scene_start=meta['recipe']['scene_start'],
                      scene_end=meta['recipe']['scene_start']+len(meta['sequence'])-1,
                      projection_space='UNDISTORTED_PINHOLE_CAMERA; markers and copied frames retain source distortion',
                      calibration_basis=meta['recipe'].get('calibration',{}).get('reference','declared centered square-pixel intrinsics'),
                      scope='Managed indexed PNG/movie windows, fixed declared square-pixel intrinsics and optional polynomial radial calibration; no intrinsic estimation, rolling shutter or metric-scale claim')
    validate(report,REPORT);validate(observation,OBSERVATION);validate(meta['sequence'],RESOURCES)
    atomic_json(job/'tracking-report.json',report);atomic_json(job/'tracking-observation.json',observation)
    atomic_json(job/'tracking-resources.json',meta['sequence'])
    if meta['version']=='1.1':
        validate(meta['input'],INPUT_REPORT);atomic_json(job/'tracking-input.json',meta['input'])
    return report

def saved(clip,meta,job,stage):
    expected=observations(clip);candidate=job/'tracking-candidate.blend';clip.use_fake_user=True
    bpy.context.preferences.filepaths.save_version=0
    bpy.ops.wm.save_as_mainfile(filepath=str(candidate),check_existing=False,relative_remap=False)
    clip,meta=opened({'file':str(candidate),'expected_sha256':digest(candidate),'resources':meta['sequence']})
    if observations(clip)!=expected:reject('Tracking candidate changed after save and reopen')
    report=outputs(clip,meta,job,stage)
    return {'candidate':str(candidate),'candidate_sha256':digest(candidate),'reopen':'pass','report':str(job/'tracking-report.json'),
            'resources':str(job/'tracking-resources.json'),'resources_sha256':digest(job/'tracking-resources.json'),
            'frames':report['frames'],'tracks':report['tracks']}

def prepare(params,job):
    spec=params['manifest'];input_report=None
    if spec.get('adapter')=='CLIP_INPUT_V1':
        from tracking_input import prepare as prepare_input
        rows,input_report=prepare_input(spec,job)
    else:rows=copied(spec['sequence'],job)
    for row in rows:
        with open(row['file'],'rb') as stream:header=stream.read(24)
        if len(header)!=24 or header[:8]!=b'\x89PNG\r\n\x1a\n' or header[12:16]!=b'IHDR' or struct.unpack('>II',header[16:24])!=(spec['width'],spec['height']):reject('PNG dimensions or signature differ from manifest')
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene=bpy.context.scene;scene.name=spec['scene'];scene.frame_start=1;scene.frame_end=len(rows)
    scene.render.resolution_x=spec['width'];scene.render.resolution_y=spec['height'];scene.render.resolution_percentage=100
    scene.render.fps=round(spec['fps']);scene.render.fps_base=round(spec['fps'])/spec['fps'];scene.frame_set(1)
    clip=bpy.data.movieclips.load(rows[0]['file']);clip.name=spec['clip_name']
    if clip.source!='SEQUENCE' or list(clip.size)!=[spec['width'],spec['height']] or clip.frame_duration!=len(rows):reject('Loaded PNG sequence dimensions or frame count differ from manifest')
    setup_intrinsics(clip,spec)
    settings=clip.tracking.settings;settings.default_pattern_size=spec['pattern_size'];settings.default_search_size=spec['search_size']
    settings.default_motion_model='Loc';settings.default_correlation_min=spec['correlation_min'];settings.use_default_normalization=True
    for item in spec['tracks']:
        first=item['markers'][0];track=clip.tracking.tracks.new(name=item['name'],frame=first['frame']);track.select=True
        track.markers[0].co=first['co']
        for marker in item['markers'][1:]:track.markers.insert_frame(marker['frame'],co=marker['co'])
    if spec['mode']=='TRACK':
        with clip_context(clip):
            if not bpy.ops.clip.track_markers.poll():reject('Native tracking context unavailable','UNSUPPORTED')
            status=bpy.ops.clip.track_markers('EXEC_DEFAULT',backwards=False,sequence=True)
        if status!={'FINISHED'}:reject('Native tracking did not finish')
    meta={'version':'1.0','recipe':spec,'sequence':rows,'markers_sha256':marker_hash(clip),'intrinsics':intrinsics(clip)}
    if input_report is not None:meta.update(version='1.1',input=input_report)
    store(clip,meta)
    return saved(clip,meta,job,'PREPARE')

def inspect(params,job):
    clip,meta=opened(params)
    if clip.name!=params['clip']:reject('Requested clip does not match managed clip')
    return outputs(clip,meta,job,'INSPECT')

def solve(params,job):
    clip,meta=opened(params);spec=params['manifest']
    if clip.name!=spec['clip']:reject('Requested clip does not match managed clip')
    if clip.tracking.reconstruction.is_valid or bpy.data.objects:reject('Solve expects an unsolved managed tracking candidate','UNSUPPORTED')
    rows=copied(meta['sequence'],job);clip.filepath=rows[0]['file'];meta['sequence']=rows
    setup_intrinsics(clip,meta['recipe']);obj=clip.tracking.objects[0]
    declared_intrinsics=intrinsics(clip)
    obj.keyframe_a=spec['keyframe_a'];obj.keyframe_b=spec['keyframe_b']
    native_error=None;status={'CANCELLED'}
    try:
        with clip_context(clip):
            if not bpy.ops.clip.solve_camera.poll():reject('Native camera solve context unavailable','UNSUPPORTED')
            status=bpy.ops.clip.solve_camera('EXEC_DEFAULT')
    except RuntimeError as ex:native_error=str(ex)
    if intrinsics(clip)!=declared_intrinsics:reject('Solve changed declared fixed intrinsics')
    observation=observations(clip);rec=clip.tracking.reconstruction
    quality=tracking_quality.evaluate(observation,meta['recipe']['width'],meta['recipe']['height'],meta['recipe']['focal_length_pixels'],spec,len(rows),rec.average_error,meta['recipe'].get('calibration'))
    if status!={'FINISHED'} or not rec.is_valid:
        quality['passed']=False;quality['failures'].append({'code':'NATIVE_SOLVE','detail':native_error or 'Native camera reconstruction invalid'})
    validate(quality,QUALITY);atomic_json(job/'tracking-quality.json',quality)
    if not quality['passed']:reject('Tracking solve quality failed: '+','.join(sorted({f['code'] for f in quality['failures']})))
    outputs(clip,meta,job,'SOLVE')
    camera_data=bpy.data.cameras.new(spec['camera_name']);camera_data.sensor_width=36
    camera_data.lens=meta['recipe']['focal_length_pixels']*36/meta['recipe']['width'];camera_data.sensor_fit='HORIZONTAL'
    cx,cy=meta['recipe'].get('calibration',{}).get('principal_point_pixels',[meta['recipe']['width']/2,meta['recipe']['height']/2])
    camera_data.shift_x=(meta['recipe']['width']/2-cx)/meta['recipe']['width']
    camera_data.shift_y=(meta['recipe']['height']/2-cy)/meta['recipe']['width']
    camera=bpy.data.objects.new(spec['camera_name'],camera_data);bpy.context.scene.collection.objects.link(camera);camera[KEY]='CAMERA'
    camera.rotation_mode='QUATERNION';bpy.context.scene.camera=camera
    for row in observation['cameras']:
        matrix=Matrix(row['matrix']);camera.matrix_world=matrix
        frame=row['frame']+meta['recipe'].get('scene_start',1)-1
        camera.keyframe_insert(data_path='location',frame=frame);camera.keyframe_insert(data_path='rotation_quaternion',frame=frame)
    mesh=bpy.data.meshes.new(spec['points_name']);mesh.from_pydata([b['co'] for b in observation['bundles']],[],[])
    points=bpy.data.objects.new(spec['points_name'],mesh);bpy.context.scene.collection.objects.link(points);points[KEY]='POINTS'
    points['tracking_bundle_names']=json.dumps([b['name'] for b in observation['bundles']])
    meta['solve']=spec;meta['markers_sha256']=marker_hash(clip);meta['intrinsics']=intrinsics(clip);store(clip,meta)
    start=meta['recipe'].get('scene_start',1);clip.frame_start=start
    bpy.context.scene.frame_start=start;bpy.context.scene.frame_end=start+len(rows)-1
    bpy.context.scene.frame_set(start)
    result=saved(clip,meta,job,'SOLVE');result['quality']=str(job/'tracking-quality.json')
    if 'preview_points' in spec:
        from tracking_preview import create
        result.update(create(meta,observation,result,job))
    return result
