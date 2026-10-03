# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixed GUI native worker and separate-process reopen verifier; no user Python execution."""
import os,sys,time,traceback,subprocess
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import bpy
from mathutils import Quaternion,Vector
from protocol import Failure,atomic_json,read_json,digest
from sculpt_contract import normalize
from sculpt_state import capture,compare
from review_contract import key

job=Path(sys.argv[sys.argv.index('--')+1]);request=read_json(job/'request.json')
spec=normalize(request['params'])['manifest']
pending=job/'sculpt-pending.blend'
state={};draw_handle=None
start=time.monotonic()

def progress(phase,**extra):
    atomic_json(job/'sculpt-progress.json',{'phase':phase,'pid':os.getpid(),'elapsed':time.monotonic()-start,**extra})

def check():
    if (job/'cancel.request').exists():raise Failure('CANCELLED','Replay cancelled')
    if time.monotonic()-start>read_json(job/'launch.json')['timeout_seconds']:raise Failure('TIMEOUT','Replay deadline reached')

def finish_error(exc):
    global draw_handle
    if draw_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(draw_handle,'WINDOW');draw_handle=None
    atomic_json(job/'worker-result.json',{'ok':False,'data':None,'error':{'code':getattr(exc,'code','WORKER_FAILED'),'message':str(exc)}})
    (job/'sculpt-error.log').write_text(traceback.format_exc(),encoding='utf-8')
    bpy.ops.wm.quit_blender()

def verify():
    # New process rereads both files, never trusts the producer's preserved-fields claim.
    if digest(spec['source']['file'])!=spec['source']['expected_sha256']:raise Failure('CONFLICT','Source SHA differs in verifier')
    bpy.ops.wm.open_mainfile(filepath=spec['source']['file'],load_ui=False,use_scripts=False)
    before=capture(spec['object'])
    bpy.ops.wm.open_mainfile(filepath=str(pending),load_ui=False,use_scripts=False)
    after=capture(spec['object'])
    result=compare(before,after,spec['object'],spec['max_displacement'])
    result.update(candidate_sha256=digest(pending),source_sha256=digest(spec['source']['file']),
                  request_sha256=key(request),before_sha256=key(before),after_sha256=key(after),
                  verifier_pid=os.getpid())
    atomic_json(job/'sculpt-verification.json',result)

def painted():
    if bpy.context.area==state.get('area') and bpy.context.region and bpy.context.region.type=='WINDOW':
        state['draws']=state.get('draws',0)+1

def prepare():
    global draw_handle
    try:
        check()
        if bpy.app.background or bpy.app.version!=(5,2,1) or bpy.app.build_hash.decode()!='9e2066aef7ef':
            raise Failure('UNSUPPORTED','GUI Draw V1 requires verified Blender 5.2.1 build 9e2066aef7ef')
        if digest(spec['source']['file'])!=spec['source']['expected_sha256']:raise Failure('CONFLICT','Source SHA mismatch')
        bpy.ops.wm.open_mainfile(filepath=spec['source']['file'],load_ui=False,use_scripts=False)
        before=capture(spec['object']);atomic_json(job/'sculpt-before.json',before)
        state['before']=before
        state['selected']=[o.name for o in bpy.context.view_layer.objects if o.select_get()]
        state['active']=bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None
        o=bpy.data.objects[spec['object']]
        for ob in bpy.context.view_layer.objects:ob.select_set(False)
        o.select_set(True);bpy.context.view_layer.objects.active=o
        windows=bpy.context.window_manager.windows
        if not windows:raise Failure('UNSUPPORTED','No desktop window available')
        w=windows[0];a=next((a for a in w.screen.areas if a.type=='VIEW_3D'),None)
        if a is None:raise Failure('UNSUPPORTED','No VIEW_3D area')
        reg=next((r for r in a.regions if r.type=='WINDOW'),None)
        if reg is None:raise Failure('UNSUPPORTED','No viewport region')
        state.update(window=w,area=a,region=reg,object=o)
        rv=a.spaces.active.region_3d
        rv.view_rotation=Quaternion(spec['view']['rotation']);rv.view_location=Vector(spec['view']['location'])
        rv.view_distance=spec['view']['distance'];rv.view_perspective='ORTHO'
        # Reject embedded brushes: do not let a renamed/custom Draw masquerade as factory behavior.
        if bpy.data.brushes:raise Failure('UNSUPPORTED','Source must not embed brush datablocks; use an Object-mode source copy')
        with bpy.context.temp_override(window=w,area=a,region=reg):
            bpy.ops.object.mode_set(mode='SCULPT')
        brush=bpy.context.tool_settings.sculpt.brush
        if brush is None or brush.sculpt_brush_type!='DRAW':raise Failure('UNSUPPORTED','Factory Draw brush unavailable; no fallback')
        # Bind to factory brush settings, set all exposed request controls; avoid source workspace brush settings.
        if brush.name!='Draw':raise Failure('UNSUPPORTED','Only factory Draw asset supported')
        brush.strength=spec['brush']['strength'];brush.size=spec['brush']['radius_pixels']
        brush.use_pressure_strength=True;brush.use_pressure_size=False
        sculpt=bpy.context.tool_settings.sculpt
        for owner in (sculpt,sculpt.mesh_automasking_settings):
            for p in owner.bl_rna.properties:
                if not p.is_readonly and p.type in ('BOOLEAN','INT','FLOAT','ENUM'):
                    value=(set(p.default_flag) if p.is_enum_flag else p.default) if p.type=='ENUM' else (list(p.default_array) if p.is_array else p.default)
                    setattr(owner,p.identifier,value)
        unified=sculpt.unified_paint_settings
        unified.use_unified_size=False;unified.use_unified_strength=False
        sculpt.use_symmetry_x=False;sculpt.use_symmetry_y=False;sculpt.use_symmetry_z=False
        draw_handle=bpy.types.SpaceView3D.draw_handler_add(painted,(),'WINDOW','POST_PIXEL')
        state['ready_start']=time.monotonic();a.tag_redraw()
        progress('waiting_for_viewport_draw')
        bpy.app.timers.register(replay,first_interval=.05)
    except Exception as exc:finish_error(exc)
    return None

def replay():
    global draw_handle
    try:
        check()
        if not state.get('draws'):
            if time.monotonic()-state['ready_start']>10:raise Failure('UNSUPPORTED','Viewport did not draw within readiness deadline')
            state['area'].tag_redraw();return .05
        o,w,a,reg=(state[k] for k in ('object','window','area','region'))
        bpy.types.SpaceView3D.draw_handler_remove(draw_handle,'WINDOW');draw_handle=None
        if [reg.width,reg.height]!=spec['view']['region_size']:
            raise Failure('UNSUPPORTED',f'Viewport size differs: observed {[reg.width,reg.height]}; expected {spec["view"]["region_size"]}')
        from bpy_extras.view3d_utils import location_3d_to_region_2d
        with bpy.context.temp_override(window=w,area=a,region=reg):
            rv=a.spaces.active.region_3d;rv.update()
            if (rv.view_perspective!='ORTHO' or (rv.view_location-Vector(spec['view']['location'])).length>1e-5 or
                abs(rv.view_distance-spec['view']['distance'])>max(1e-5,spec['view']['distance']*1e-6) or
                abs(abs(rv.view_rotation.dot(Quaternion(spec['view']['rotation'])))-1)>1e-6):
                raise Failure('CONFLICT','Viewport state differs from replay manifest')
            stroke=[]
            for i,s in enumerate(spec['stroke']):
                xy=location_3d_to_region_2d(reg,rv,o.matrix_world@Vector(s['location']))
                if xy is None:raise Failure('INVALID_REQUEST','Stroke location cannot be projected')
                stroke.append(dict(name='Stroke',location=s['location'],mouse=list(xy),mouse_event=list(xy),
                    pressure=s['pressure'],size=spec['brush']['radius_pixels'],x_tilt=0,y_tilt=0,time=s['time'],is_start=i==0))
            atomic_json(job/'sculpt-resolved-stroke.json',{'request_sha256':key(request),'stroke':stroke,
                'region_size':[reg.width,reg.height],'view_matrix':[list(r) for r in rv.view_matrix],
                'projection_matrix':[list(r) for r in rv.window_matrix]})
            if not bpy.ops.sculpt.brush_stroke.poll():raise Failure('UNSUPPORTED','Native sculpt operator context unavailable')
            progress('native_stroke',samples=len(stroke))
            result=bpy.ops.sculpt.brush_stroke(stroke=stroke,mode=spec['brush']['mode'],override_location=False,ignore_background_click=False)
            if 'FINISHED' not in result:raise Failure('WORKER_FAILED','Native stroke did not finish')
            bpy.ops.object.mode_set(mode='OBJECT')
        check()
        # The source admitted no brush/library/sculpt-mask data. Drop only newly
        # created brush-session data so the candidate remains a plain replayable mesh.
        for brush in list(bpy.data.brushes):bpy.data.brushes.remove(brush)
        for library in list(bpy.data.libraries):bpy.data.libraries.remove(library)
        for attr in list(o.data.attributes):
            if attr.name.startswith('.sculpt'):o.data.attributes.remove(attr)
        after=capture(spec['object']);metrics=compare(state['before'],after,spec['object'],spec['max_displacement'])
        for ob in bpy.context.view_layer.objects:ob.select_set(ob.name in state['selected'])
        bpy.context.view_layer.objects.active=bpy.data.objects.get(state['active']) if state['active'] else None
        bpy.context.preferences.filepaths.save_version=0
        bpy.ops.wm.save_as_mainfile(filepath=str(pending))
        progress('independent_reopen')
        command=[bpy.app.binary_path,'-b','--factory-startup','--disable-autoexec','--offline-mode','--threads','2',
                 '--python-exit-code','23','--python',str(Path(__file__).resolve()),'--',str(job),'--verify']
        with (job/'sculpt-verifier.log').open('wb') as log:
            verifier=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
            progress('independent_reopen',verifier_pid=verifier.pid)
            code=verifier.wait()
        check()
        if code:raise Failure('VALIDATION_FAILED','Independent reopen failed; see sculpt-verifier.log')
        verification=read_json(job/'sculpt-verification.json')
        if verification['request_sha256']!=key(request) or verification['candidate_sha256']!=digest(pending):
            raise Failure('CONFLICT','Candidate/request changed after verification')
        candidate=job/'sculpt-candidate.blend'
        if candidate.exists():raise Failure('CONFLICT','Candidate destination already exists')
        pending.rename(candidate)
        report={'version':'1.0','kind':'native_sculpt_replay','backend':spec['backend'],'request_sha256':key(request),
            'source':spec['source'],'candidate':{'file':str(candidate),'expected_sha256':digest(candidate)},
            'object':spec['object'],'brush_replay_verified':True,'artistic_quality':'pending_human_review',
            'verification':{'file':str(job/'sculpt-verification.json'),'expected_sha256':digest(job/'sculpt-verification.json')},
            'blender_build':bpy.app.build_hash.decode(),'os_sandbox':False,**metrics}
        from sculpt_host import runtime_files
        report['runtime_hashes']={str(p):digest(p) for p in runtime_files(bpy.app.binary_path)}
        atomic_json(job/'sculpt-report.json',report)
        atomic_json(job/'worker-result.json',{'ok':True,'data':report,'error':None})
        progress('verified')
        bpy.ops.wm.quit_blender()
    except Exception as exc:finish_error(exc)
    return None

if '--verify' in sys.argv:
    verify()
else:
    progress('starting_gui')
    bpy.app.timers.register(prepare,first_interval=0)
