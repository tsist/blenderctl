"""Protected saved-file lighting work unit with fresh reopen and render evidence."""
import copy,json,os,subprocess,sys,time
from pathlib import Path
from contextlib import ExitStack
import bpy
from protocol import Failure,atomic_json,digest,read_json
from lighting_workflow_contract import normalize_params,normalize_inspect_params,normalize_template_params,normalize_edit_params,_template,_call
from lighting_identity import implementation_sha256
from lighting_workflow_addon import core
from lighting_workflow_addon.contract import WorkflowError
from geometry_verifier import near

def descriptor(p):return {'file':str(p),'sha256':digest(p)}
def _signature(p):
    import hashlib
    keys=('file','expected_sha256','resources','preview','implementation_sha256','template','target')
    body={k:p.get(k) for k in keys}
    if 'template' not in p:body['manifest']=p.get('manifest')
    return hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def _resources(params):
    declared={os.path.normcase(str(Path(r['file']).resolve())):r['expected_sha256'] for r in params['resources']}
    rows=[]
    for kind in ('images','fonts','libraries','sounds','movieclips','volumes'):
        for item in getattr(bpy.data,kind):
            raw=getattr(item,'filepath','')
            if not raw or raw=='<builtin>' or getattr(item,'packed_file',None) or getattr(item,'packed_files',[]):continue
            if getattr(item,'source','') in ('TILED','SEQUENCE','MOVIE') or '<UDIM>' in raw or '####' in raw:
                raise Failure('UNSUPPORTED','Dynamic resource member expansion required: '+item.name)
            path=str(Path(bpy.path.abspath(raw,library=getattr(item,'library',None))).resolve());key=os.path.normcase(path)
            if key not in declared:raise Failure('CONFLICT','Undeclared lighting dependency: '+path)
            if digest(path)!=declared[key]:raise Failure('CONFLICT','Resource SHA mismatch: '+path)
            rows.append({'kind':kind,'name':item.name,'file':path,'sha256':declared[key],'stored_path':raw})
    # Use the existing render gate for bound graphs, scripted drivers and caches.
    return {'status':'checked_static_id_paths','rows':rows,'limitations':['Dynamic UDIM/sequence/cache inputs need their existing dedicated contracts']}
def _absolute_paths(closure):
    for r in closure['rows']:
        item=getattr(bpy.data,r['kind']).get(r['name'])
        if item and not item.library:item.filepath=r['file']
def _reopen(candidate,params,inspection,protection,excluded,job):
    manifest=params['manifest'];cfg={'candidate':candidate,'implementation_sha256':params['implementation_sha256'],
        'scene':manifest['scene'],'view_layer':manifest['view_layer'],'frame':params['_effective_frame'],
        'rig_id':manifest['rig_id'],'inspection':inspection,'protection':protection,'excluded_names':excluded,
        'output':str(job/'lighting-reopen.json'),'runtime':params['_runtime']}
    path=job/'lighting-verification-request.json';atomic_json(path,cfg)
    args=[bpy.app.binary_path,'--background','--factory-startup','--disable-autoexec','--offline-mode','--python',str(Path(__file__).with_name('lighting_verifier.py')),'--',str(path)]
    with (job/'lighting-reopen.log').open('wb') as log:
        proc=subprocess.run(args,stdout=log,stderr=subprocess.STDOUT,timeout=120,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    result=read_json(cfg['output']) if Path(cfg['output']).is_file() else {'status':'failed','error':'No independent reader result'}
    if proc.returncode or result['status']!='passed':raise Failure('VALIDATION_FAILED','Independent lighting reopen: '+str(result.get('error')))
    return result
def _previews(params,candidate,job,progress,report):
    import rendering
    spec=params.get('preview')
    if not spec:return []
    m=params['manifest'];scene=bpy.data.scenes[m['scene']]
    cameras=spec.get('cameras') or ([scene.camera.name] if scene.camera else [])
    if not cameras:raise Failure('NOT_FOUND','No preview camera')
    frames=spec.get('frames') or [params['_effective_frame']]
    total=len(cameras)*len(frames)*len(spec['engines'])
    if total>24:raise Failure('RESOURCE_LIMIT','Lighting preview exceeds 24 images')
    for engine in spec['engines']:
        for cam in cameras:
            for frame in frames:
                progress('preview',completed=len(report['previews']),total=total,engine=engine,camera=cam,frame=frame)
                directory=job/'previews'/('%02d'%len(report['previews']));directory.mkdir(parents=True,exist_ok=False)
                render_spec={'scene':m['scene'],'view_layer':m['view_layer'],'camera':cam,'engine':engine,
                    'device':spec['device'] if engine=='CYCLES' else {'backend':'GRAPHICS'},'frames':[frame],
                    'width':spec['width'],'height':spec['height'],'samples':spec['samples'],'format':'PNG','depth':'8',
                    'transparent':bool(scene.render.film_transparent),'color':{'view':scene.view_settings.view_transform,'exposure':scene.view_settings.exposure,'gamma':scene.view_settings.gamma},
                    'compositor':False,'passes':[],'denoise':bool(spec.get('denoise',False)) if engine=='CYCLES' else False,'motion_blur':0}
                out=rendering.run({'file':candidate['file'],'expected_sha256':candidate['sha256'],'resources':params['resources'],'manifest':render_spec},directory,preserve_color_settings=True)
                for row in out['outputs']:
                    report['previews'].append({**row,'engine':engine,'camera':cam,'status':'passed'})
                atomic_json(job/'lighting-report.json',report)
                scene=bpy.data.scenes[m['scene']]
    return report['previews']
def run(params,job,*,cancel_parent=None):
    params=normalize_params(params);job=Path(job);started=time.monotonic();candidate_guard=None
    params['_runtime']={'version':bpy.app.version_string,'build':bpy.app.build_hash.decode(),'binary_sha256':digest(bpy.app.binary_path)}
    report={'lighting_report_version':'1.0','status':'running','source':{'file':params['file'],'sha256':params['expected_sha256']},'runtime':params['_runtime'],
        'implementation_sha256':params['implementation_sha256'],'signature':_signature(params),'previews':[],
        'checks':{'visual_quality':'not_received','GUI_visible':'not_run'},'recovery':{'available':False},
        'support':{'milestone':'M2','later':['four scene cases','motion acceptance'],
            'limitations':['Sky sun_disc is Cycles-only; equivalent dual-engine Sky control requires sun_disc=False',
                'Eevee additional World extracted Sun mechanism is not verified',
                'Synthetic HDRI probe is a test fixture, not a production lighting resource']}}
    def progress(stage,**extra):
        if (job/'cancel.request').exists() or (cancel_parent and (Path(cancel_parent)/'cancel.request').exists()):raise Failure('CANCELLED','Lighting cancelled at checkpoint')
        atomic_json(job/'lighting-report.json',report)
        atomic_json(job/'lighting-progress.json',{'stage':stage,'status':'running','seconds':time.monotonic()-started,**extra})
    try:
        progress('source_open')
        _check_inputs(params)
        bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
        if tuple(bpy.data.version[:2])!=tuple(bpy.app.version[:2]):raise Failure('UNSUPPORTED','Lighting requires matching Blender major/minor')
        closure=_resources(params);report['resources']=closure
        _absolute_paths(closure)
        from geometry_workflow import _retain_unlinked_ids
        report['persistence_exceptions']=_retain_unlinked_ids()
        if 'template' in params:
            from lighting_workflow_addon.templates import instantiate
            params['manifest']=instantiate(_template(params['template']),params['target'])
            report['template']=params['template'];report['target']=params['target']
        m=params['manifest'];report['resolved_manifest']=m;scene=bpy.data.scenes.get(m['scene'])
        if not scene:raise Failure('NOT_FOUND','Missing target scene')
        bpy.context.window.scene=scene;bpy.context.window.view_layer=scene.view_layers[m['view_layer']]
        params['_effective_frame']=m.get('frame',scene.frame_current)
        scene.frame_set(params['_effective_frame']);bpy.context.view_layer.update()
        if params.get('resume'):
            old=read_json(params['resume']['file'])
            if old.get('signature')!=report['signature'] or old.get('implementation_sha256')!=params['implementation_sha256'] or old.get('runtime')!=params['_runtime']:
                raise Failure('CONFLICT','Resume inputs/manifest/implementation differ')
            if not old.get('saved_expectation') or old.get('effective_frame')!=params['_effective_frame']:
                raise Failure('CONFLICT','Resume requires the original saved expectation and matching frame; legacy reports without it require a new run from source')
            candidate=old['candidate']
            if digest(candidate['file'])!=candidate['sha256']:raise Failure('CONFLICT','Checkpoint changed')
            bpy.ops.wm.open_mainfile(filepath=candidate['file'],load_ui=False,use_scripts=False)
            report['operation']=old.get('operation');report['protection']=old.get('protection');report['candidate']=candidate
            excluded=old.get('excluded_names',[]);report['excluded_names']=excluded
            report['resumed_from']=params['resume']
            report['saved_expectation']=copy.deepcopy(old['saved_expectation'])
        else:
            progress('apply')
            operation=core.apply(m);report['operation']=operation
            report['protection']=operation.get('protection',{'status':'not_reported'})
            if report['protection'].get('status')!='passed':raise Failure('VALIDATION_FAILED','Core must verify non-target protection')
            excluded=operation.get('excluded_names',[]);report['excluded_names']=excluded
            progress('save_checkpoint')
            candidate_path=job/'lighting-candidate.blend'
            if candidate_path.exists():raise Failure('CONFLICT','Owned candidate already exists')
            # Relative inputs were explicitly resolved above; preserve their absolute bindings.
            bpy.ops.wm.save_as_mainfile(filepath=str(candidate_path),check_existing=False,relative_remap=False)
            candidate=descriptor(candidate_path);report['candidate']=candidate
        from filesystem import FileGuard
        candidate_guard=FileGuard(candidate['file']);candidate_guard.__enter__()
        if candidate_guard.sha256()!=candidate['sha256']:raise Failure('CONFLICT','Candidate changed before guarded verification')
        report['effective_frame']=params['_effective_frame']
        report['recovery']={'available':True,'checkpoint':candidate,'resume_report':str(job/'lighting-report.json'),'instruction':'Hash this report and submit the original request with resume descriptor; runtime and inputs must match'}
        if params.get('resume'):
            expected=report['saved_expectation']['inspection'];protection=report['saved_expectation']['protection']
        else:
            expected=core.inspect(m['scene'],m['rig_id'])
            protection=core.snapshot_protection(m['scene'],excluded_names=excluded)
            report['saved_expectation']={'inspection':expected,'protection':protection}
        progress('independent_reopen')
        report['verification']=_reopen(candidate,params,expected,protection,excluded,job)
        atomic_json(job/'lighting-manifest.json',m)
        progress('preview')
        _previews(params,candidate,job,progress,report)
        if candidate_guard.sha256()!=candidate['sha256']:raise Failure('CONFLICT','Candidate changed during preview')
        report['status']='passed';report['metrics']={'wall_seconds':time.monotonic()-started,'preview_count':len(report['previews'])}
        atomic_json(job/'lighting-report.json',report)
        atomic_json(job/'lighting-progress.json',{'stage':'complete','status':'passed','completed':len(report['previews'])})
        return report
    except (Failure,WorkflowError) as e:
        report.update(status='failed',error={'code':e.code,'message':str(e)})
        atomic_json(job/'lighting-report.json',report)
        raise Failure(e.code,str(e)) from e
    except Exception as e:
        report.update(status='failed',error={'code':'WORKER_FAILED','message':str(e)})
        atomic_json(job/'lighting-report.json',report)
        raise
    finally:
        if candidate_guard is not None:candidate_guard.__exit__(None,None,None)
def inspect(params,job):
    params=normalize_inspect_params(params)
    _check_inputs(params)
    bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    closure=_resources(params)
    report={'status':'passed','source':{'file':params['file'],'sha256':params['expected_sha256']},'implementation_sha256':params['implementation_sha256'],
        'inspection':_call(core.inspect,params['scene'],params.get('rig_id')),'resources':closure,'checks':{'render':'not_run','visual_quality':'not_received'}}
    atomic_json(Path(job)/'lighting-report.json',report);return report

def _check_inputs(params):
    if digest(params['file'])!=params['expected_sha256']:raise Failure('CONFLICT','Source SHA mismatch')
    for r in params.get('resources',[]):
        if digest(r['file'])!=r['expected_sha256']:raise Failure('CONFLICT','Resource SHA mismatch: '+r['file'])

def save_template(params,job):
    params=normalize_template_params(params);job=Path(job);_check_inputs(params)
    bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    closure=_resources(params)
    from lighting_workflow_addon.templates import export,normalize_template
    resources=job/'template-resources';resources.mkdir(exist_ok=False)
    value=_call(export,params['scene'],params['rig_id'],resource_directory=str(resources))
    value['template_id']=params['template_id'];value['revision']=params['revision']
    value['source']={'file':params['file'],'expected_sha256':params['expected_sha256']};value=_call(normalize_template,value)
    for r in value['resources']:
        if digest(r['file'])!=r['expected_sha256']:raise Failure('CONFLICT','Exported resource SHA mismatch')
    destination=job/'lighting-template.json'
    if destination.exists():raise Failure('CONFLICT','Owned template already exists')
    atomic_json(destination,value)
    report={'status':'passed','template':descriptor(destination),'source':{'file':params['file'],'sha256':params['expected_sha256']},
        'implementation_sha256':params['implementation_sha256'],'resources':closure,'template_resources':value['resources'],
        'checks':{'actual_state_export':'passed','template_normalization':'passed','render':'not_run'}}
    atomic_json(job/'lighting-report.json',report);return report

def edit_template(params,job):
    params=normalize_edit_params(params);job=Path(job);value=params['edits']['content']
    for r in value['resources']:
        if digest(r['file'])!=r['expected_sha256']:raise Failure('CONFLICT','Template resource SHA mismatch')
    destination=job/'lighting-template.json'
    if destination.exists():raise Failure('CONFLICT','Owned template already exists')
    atomic_json(destination,value)
    report={'status':'passed','template':descriptor(destination),'parent':params['template'],
        'implementation_sha256':params['implementation_sha256'],'revision':value['revision'],'checks':{'template_normalization':'passed','render':'not_run'}}
    atomic_json(job/'lighting-report.json',report);return report

def verify_saved(params,old,job):
    """Recheck immutable successful batch items in a fresh process before reuse."""
    params=normalize_params(params);job=Path(job)
    params['_runtime']={'version':bpy.app.version_string,'build':bpy.app.build_hash.decode(),'binary_sha256':digest(bpy.app.binary_path)}
    if old.get('signature')!=_signature(params) or old.get('runtime')!=params['_runtime'] or old.get('implementation_sha256')!=params['implementation_sha256']:
        raise Failure('CONFLICT','Batch reuse identity differs')
    if old.get('status')!='passed' or not old.get('saved_expectation'):raise Failure('CONFLICT','Batch success lacks verification evidence')
    params['manifest']=old['resolved_manifest'];params['_effective_frame']=old['effective_frame']
    from filesystem import FileGuard
    with ExitStack() as stack:
        for row in [old['candidate'],*old.get('previews',[])]:
            guard=stack.enter_context(FileGuard(row['file']))
            if guard.sha256()!=row['sha256']:raise Failure('CONFLICT','Batch saved output changed')
        saved=old['saved_expectation']
        fresh=_reopen(old['candidate'],params,saved['inspection'],saved['protection'],old['excluded_names'],job)
    return {'status':'passed','fresh_reopen':fresh,'preview_hashes':'passed','candidate':old['candidate']}
