"""Finite owned CUDA0/FP32 light computation. Fixed runtime; no model/scene/network."""
import ast, hashlib, json, os, struct, sys, time, types, zlib
from pathlib import Path

DIAGNOSTIC_KEYS={
    'PARA':('para',),'FORM':('form',),'DARK_SIDE':('dark_side',),'RIM':('rim',),'LINE':('line',),
    'DELTA':('delta',),'DEPTH':('depth','depth_valid'),'NORMAL':('normal','normal_valid'),
    'VALIDITY':('depth_valid','normal_valid','subject'),
}

def diagnostic_rgba(view,values,source,delta_gain,np):
    """Build a display-only diagnostic; never replaces the real linear output."""
    if view=='RESULT':return None
    if view in ('PARA','FORM','DARK_SIDE','RIM','LINE'):
        key={'PARA':'para','FORM':'form','DARK_SIDE':'dark_side','RIM':'rim','LINE':'line'}[view]
        rgb=np.repeat(np.clip(values[key],0,1)[...,None],3,axis=-1)
    elif view=='DELTA':rgb=np.clip(np.abs(values['delta'])*delta_gain,0,1)
    elif view=='DEPTH':rgb=np.repeat(np.clip(values['depth']*values['depth_valid'],0,1)[...,None],3,axis=-1)
    elif view=='NORMAL':rgb=(values['normal']*.5+.5)*values['normal_valid'][...,None]
    elif view=='VALIDITY':rgb=np.stack((values['depth_valid'],values['normal_valid'],values['subject']),axis=-1)
    else:raise ValueError('Unknown diagnostic view')
    return np.concatenate((rgb.astype(np.float32),source[...,3:4]),axis=-1)

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1048576),b''):h.update(block)
    return h.hexdigest()

def read_json(path):
    # Same bounded Windows atomic-publication handoff used by Host/client.
    # Persistent permission errors still fail; never fall back to an older file.
    for attempt in range(4):
        try:return json.loads(path.read_text(encoding='utf-8'))
        except PermissionError:
            if attempt==3:raise
            time.sleep(.005)

def run(job):
    target=job/'depth';binding=json.loads((target/'binding.json').read_text(encoding='utf-8'));profile=binding['profile'];started=time.monotonic()
    def need(value,message,code='VALIDATION_FAILED'):
        if not value:
            exc=RuntimeError(message);exc.code=code;raise exc
    def checkpoint():
        need(not (job/'depth.cancel.request').exists(),'Owned CUDA stage cancelled','CANCELLED')
        need(time.monotonic()-started<=binding['timeout_seconds'],'Owned CUDA stage timed out','TIMEOUT')
    need(sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode,'Isolated no-site/no-pyc required')
    need(sys.version_info[:3]==tuple(profile['python_version']) and sha(sys.executable)==profile['python']['expected_sha256'],'Actual GPU Python identity differs')
    need(Path(sys.pycache_prefix).is_dir() and not any(Path(sys.pycache_prefix).iterdir()),'Empty pycache prefix required')
    codes={r['role']:r for r in binding['codes']}
    for r in binding['codes']:checkpoint();need(sha(r['file'])==r['sha256'],'Frozen code changed')
    need(Path(codes['depth_worker']['file']).resolve()==Path(__file__).resolve(),'Actual GPU worker differs')
    r=codes['session:compositing_relight_session_owned_worker.py']
    ownership=types.ModuleType('hot_worker_ownership');exec(compile(Path(r['file']).read_bytes(),r['file'],'exec'),ownership.__dict__)
    actual=ownership.worker_gate(target,binding['request_signature'])
    need(os.path.normcase(actual['actual_exe'])==os.path.normcase(binding['native_python']['file']) and
         sha(actual['actual_exe'])==binding['native_python']['expected_sha256'],'Actual native interpreter body differs')
    site=Path(profile['venv_root'])/'Lib/site-packages';sys.path.insert(0,str(site))
    def offline(event,args):
        if event in ('socket.connect','socket.bind','socket.getaddrinfo','urllib.Request','http.client.connect'):raise RuntimeError('Network forbidden')
    sys.addaudithook(offline)
    code=Path(codes['import_boundary']['file']).read_bytes()
    selected=[n for n in ast.parse(code).body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in ('ModelError','need','install_import_boundary')]
    scope={'Path':Path,'os':os,'sys':sys};exec(compile(ast.Module(body=selected,type_ignores=[]),'qualified_import_boundary','exec'),scope)
    inv=binding['inventory'];need(sha(inv['file'])==inv['sha256'],'Live guarded runtime inventory changed')
    inventory=json.loads(Path(inv['file']).read_text(encoding='utf-8'))
    scope['install_import_boundary']([r['file'] for r in inventory if Path(r['file']).suffix.lower() in ('.py','.pyd','.dll')],sys.base_prefix,
                                   [r['file'] for r in inventory if Path(r['file']).suffix.lower()=='.pyi'])
    import importlib.metadata as md
    actual={d.metadata['Name'].lower().replace('_','-'):d.version for d in md.distributions(path=[str(site)])}
    required={k.lower().replace('_','-'):v for k,v in profile['package_versions'].items()}
    need(all(actual.get(k)==v for k,v in required.items()) and set(actual)==set(required)|{'pip'} and actual['pip']=='26.1.2','GPU dependency versions differ')
    t=time.perf_counter();import numpy as np;import torch;import_seconds=time.perf_counter()-t
    need(Path(np.__file__).resolve().is_relative_to(site) and Path(torch.__file__).resolve().is_relative_to(site),'Modules outside fixed site')
    need(torch.__version__==profile['runtime']['torch'] and torch.version.cuda==profile['runtime']['CUDA'] and torch.cuda.is_available(),'GPU runtime differs')
    torch.cuda.set_device(0)
    need(torch.cuda.get_device_name(0)==profile['gpu']['name'] and list(torch.cuda.get_device_capability(0))==profile['gpu']['capability'],'GPU identity differs')
    torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
    r=codes['session:compositing_relight_local_edits.py'];edits=types.ModuleType('owned_local_edits');exec(compile(Path(r['file']).read_bytes(),r['file'],'exec'),edits.__dict__)
    r=codes['relight_kernel'];m=types.ModuleType('owned_relight_kernel');m.__file__=r['file'];exec(compile(Path(r['file']).read_bytes(),r['file'],'exec'),m.__dict__)
    h,w=binding['domain']['height'],binding['domain']['width'];shapes=[(h,w,4),(h,w),(h,w,3),(h,w),(h,w),(h,w),(h,w),(h,w)]
    dtypes=[np.float32,np.float32,np.float32,np.bool_,np.bool_,np.float32,np.float32,np.float32]
    arrays=[]
    for r,shape,dtype in zip(binding['packed'],shapes,dtypes):
        checkpoint();need(sha(r['file'])==r['sha256'],'Packed input changed')
        with Path(r['file']).open('rb') as f:x=np.load(f,allow_pickle=False);need(f.read(1)==b'','Packed NPY tail')
        need(x.shape==shape and x.dtype==dtype and x.flags.c_contiguous,'Packed numerical dtype/domain differs');arrays.append(x)
    divisor=binding['preview_divisor'];preview_arrays=[np.ascontiguousarray(x[divisor//2::divisor,divisor//2::divisor]) if divisor>1 else x for x in arrays]
    tensor_full=[torch.from_numpy(x).to('cuda:0') for x in arrays]
    tensor_preview=tensor_full if divisor==1 else [torch.from_numpy(x).to('cuda:0') for x in preview_arrays]
    def default_local(tensors):
        shape=tensors[1].shape
        return {'rough_mul':torch.ones(shape,dtype=torch.float32,device='cuda:0'),
                'rough_add':torch.zeros(shape,dtype=torch.float32,device='cuda:0'),
                'receiver':torch.zeros(shape,dtype=torch.float32,device='cuda:0'),
                'caster':tensors[3].to(dtype=torch.float32)}
    base_local_full=default_local(tensor_full)
    base_local_preview=base_local_full if divisor==1 else default_local(tensor_preview)
    torch.cuda.synchronize();loaded=time.monotonic()
    def publish(path,value):
        temp=path.with_name('.'+os.urandom(16).hex()+'.partial')
        with temp.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,allow_nan=False);f.flush();os.fsync(f.fileno())
        os.rename(temp,path)
    def member(path,role,x):return {'file':str(path),'sha256':sha(path),'bytes':path.stat().st_size,'role':role,'shape':list(x.shape),'dtype':str(x.dtype)}
    def save_array(path,role,x):
        with path.open('xb') as f:np.save(f,np.ascontiguousarray(x),allow_pickle=False)
        with path.open('rb') as f:y=np.load(f,allow_pickle=False);need(f.read(1)==b'' and np.array_equal(y,x),'Output NPY readback differs')
        return member(path,role,x)
    def display(path,rgba,role='display_srgb_png'):
        linear=np.clip(rgba[...,:3].astype(np.float64),0,1);srgb=np.where(linear<=.0031308,linear*12.92,1.055*linear**(1/2.4)-.055)
        pixels=np.concatenate((np.rint(srgb*255).astype(np.uint8),np.rint(rgba[...,3:4]*255).astype(np.uint8)),axis=-1)
        hh,ww=pixels.shape[:2]
        def chunk(tag,data):return struct.pack('>I',len(data))+tag+data+struct.pack('>I',zlib.crc32(tag+data)&0xffffffff)
        data=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',ww,hh,8,6,0,0,0))+chunk(b'sRGB',b'\x00')+chunk(b'IDAT',zlib.compress(b''.join(b'\x00'+r.tobytes() for r in pixels),3))+chunk(b'IEND',b'')
        with path.open('xb') as f:f.write(data)
        return member(path,role,pixels)
    baseline=display(target/'baseline.png',preview_arrays[0])
    commands=target/'commands';results=job/'revisions';commands.mkdir(exist_ok=False);results.mkdir(exist_ok=False)
    publish(target/'gpu-ready.json',{'schema':'relight-hot-gpu-ready-v1','status':'ready','request_signature':binding['request_signature'],'baseline_display':baseline,
        'reference_depth':float(np.median(arrays[1][arrays[3]])),'runtime':{'Python':sys.version,'Torch':str(torch.__version__),'CUDA':torch.version.cuda,'device':torch.cuda.get_device_name(0),'dtype':'float32','TF32':False,'autocast':False,'fallback':False},'import_seconds':import_seconds,'loaded_seconds':time.monotonic()-started})
    processed=set();records=[];generation_load_count=0;current_data=None;edited_full=None;edited_preview=None
    generation_root=job.parent/'generations'
    while True:
        checkpoint()
        if (target/'stop.request').exists():break
        paths=sorted(p for p in commands.glob('*.json') if p.name not in processed)
        if not paths:time.sleep(.01);continue
        need(len(paths)==1,'Host dispatched concurrent work to single-active GPU')
        path=paths[0];processed.add(path.name);command=read_json(path);request=command['request'];data=command['data']
        need(path.stem==request['request_id'] and request['session_id']==binding['session_id'] and request['incarnation_id']==binding['incarnation_id'] and request['cache_id']==binding['cache_id'],'Dispatch identity differs')
        need(type(request['revision']) is int and request['revision']>0,'Typed revision required')
        params=request['parameters'];need(hashlib.sha256(json.dumps(params,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()==request['parameters_sha256'],'Dispatch params SHA differs')
        need(params['kind'] in ('preview','final') and params['config']['frame']==binding['params']['config']['frame'],'Dispatch frame/kind differs')
        folder=results/(str(request['revision']).zfill(9)+'-'+request['request_id']);folder.mkdir(exist_ok=False)
        began=time.perf_counter();checkpoint();config=json.loads(json.dumps(params['config']));d=divisor if params['kind']=='preview' else 1
        if d>1 and config['frame']['kind']=='metric_perspective':
            for key in ('fx','fy'):config['frame'][key]/=d
            for key in ('cx','cy'):config['frame'][key]=(config['frame'][key]-.5)/d
        elif d>1:
            # Keep source-space relative vectors exact at sampled centers.
            shift=.5/binding['domain']['height']
            for light in config['lights']:
                for key in ('position','aim'):
                    if key in light:light[key]=[light[key][0]-shift,light[key][1]+shift,light[key][2]]
            if 'projection' in config:
                point=config['projection']['point'];config['projection']['point']=[point[0]-shift,point[1]+shift,point[2]]
            if config.get('anime'):
                config['anime']['rim_width']/=d
        expected_data=edits.derived_id(binding['cache_id'],params.get('local_edits',edits.empty()))
        need(type(data) is dict and set(data)=={'derived_cache_id','generation'} and data['derived_cache_id']==expected_data,'Derived data identity differs')
        if data['generation'] is None:
            need(edits.is_empty(params.get('local_edits',edits.empty())),'Nonempty edits require prepared generation')
            tensors=tensor_preview if params['kind']=='preview' else tensor_full
            local=base_local_preview if params['kind']=='preview' else base_local_full
        else:
            if current_data!=data:
                r=data['generation'];gpath=Path(r['file'])
                need(gpath==generation_root/expected_data/'generation.json' and sha(gpath)==r['sha256'],'Generation path/SHA differs')
                generation=json.loads(gpath.read_text(encoding='utf-8'))
                expected_schema='relight-local-generation-v2' if params['local_edits']['version']==edits.VERSION else 'relight-local-generation-v1'
                need(generation['schema']==expected_schema and generation['base_cache_id']==binding['cache_id'] and generation['derived_cache_id']==expected_data and generation['recipe']==params['local_edits'] and generation['normal_options']==binding['normal_options'],'Generation binding differs')
                members=generation['members'];names=['scalar','normal','validity','normal_validity','light','highlight','line']
                if expected_schema=='relight-local-generation-v2':names+=['rough_mul','rough_add','receiver','caster']
                need(type(members) is list and len(members)==len(names) and [r['role'] for r in members]==names,'Generation members differ')
                gen_shapes=shapes+[(h,w)]*4;gen_types=dtypes+[np.float32]*4
                loaded_arrays=[arrays[0]]
                for i,r in enumerate(members,1):
                    checkpoint();file=Path(r['file']);need(file==gpath.parent/(names[i-1]+'.npy') and file.stat().st_size==r['bytes'] and sha(file)==r['sha256'],'Derived member identity changed')
                    with file.open('rb') as f:x=np.load(f,allow_pickle=False);need(f.read(1)==b'','Derived NPY tail')
                    need(x.shape==gen_shapes[i] and x.dtype==gen_types[i] and x.flags.c_contiguous and np.isfinite(x).all(),'Derived numerical domain differs')
                    loaded_arrays.append(x)
                edited_full=[tensor_full[0]]+[torch.from_numpy(x).to('cuda:0') for x in loaded_arrays[1:]]
                edited_preview=edited_full if divisor==1 else [tensor_preview[0]]+[torch.from_numpy(np.ascontiguousarray(x[divisor//2::divisor,divisor//2::divisor])).to('cuda:0') for x in loaded_arrays[1:]]
                torch.cuda.synchronize();current_data=data;generation_load_count+=1
                del loaded_arrays
            tensors=edited_preview if params['kind']=='preview' else edited_full
            local=dict(zip(('rough_mul','rough_add','receiver','caster'),tensors[8:])) if len(tensors)==12 else (base_local_preview if params['kind']=='preview' else base_local_full)
        ref=preview_arrays if params['kind']=='preview' else arrays
        with torch.no_grad(),torch.autocast(device_type='cuda',enabled=False):
            torch.cuda.synchronize();torch.cuda.reset_peak_memory_stats();t=time.perf_counter()
            try:
                diagnostics={} if config['version']=='relight-kernel-draft-0.4' else None
                out,delta,parts=m.render(*tensors[:8],config,m.Backend(torch,'cuda:0'),local=local,diagnostics=diagnostics)
            except m.ProjectionError as exc:
                publish(folder/'complete.json',{'schema':'relight-session-gpu-complete-v1','status':'rejected','request':request,'data':data,
                        'error':{'code':'INVALID_REQUEST','message':str(exc)}})
                records.append({'revision':request['revision'],'request_id':request['request_id'],'kind':params['kind'],'status':'rejected'})
                continue
            torch.cuda.synchronize();kernel_seconds=time.perf_counter()-t
            rgba=out.cpu().numpy();need(np.array_equal(rgba[...,3],ref[0][...,3]),'Alpha changed')
            output=save_array(folder/'output.npy','output',rgba);display_row=display(folder/'display.png',rgba)
            members=[output,display_row];view_display=None;actual_view='RESULT'
            if diagnostics and config['anime']['view']!='RESULT':
                actual_view=config['anime']['view']
                values={key:diagnostics[key].cpu().numpy() for key in DIAGNOSTIC_KEYS[actual_view]}
                view_rgba=diagnostic_rgba(actual_view,values,ref[0],config['anime']['delta_gain'],np)
                view_display=display(folder/'view.png',view_rgba,'diagnostic_srgb_png')
                members.append(view_display)
            if params['kind']=='final':
                members.append(save_array(folder/'delta.npy','delta',delta.cpu().numpy()))
                if diagnostics and 'environment_delta' in diagnostics:
                    members.append(save_array(folder/'environment-delta.npy','environment_delta',diagnostics['environment_delta'].cpu().numpy()))
                for i,p in enumerate(parts):
                    fields=('diffuse','specular','visibility','delta')
                    if config.get('projection',{}).get('enabled'):fields+=('projection_visibility','projection_delta')
                    for name in fields:members.append(save_array(folder/(str(i)+'-'+name+'.npy'),'light:'+str(i)+':'+name,p[name].cpu().numpy()))
        checkpoint()
        terminal={'schema':'relight-session-gpu-complete-v1','status':'passed','request':request,'members':members,
                  'output':output,'display':display_row,'domain':{'width':rgba.shape[1],'height':rgba.shape[0]},
                  'data':data,'sampling_divisor':d,'peak_allocated_bytes':int(torch.cuda.max_memory_allocated()),
                  'timings':{'kernel_seconds':kernel_seconds,'worker_seconds':time.perf_counter()-began,'worker_completed_perf':time.perf_counter()},'packed_inputs_reused':True,'view':actual_view}
        if view_display is not None:terminal['view_display']=view_display
        publish(folder/'complete.json',terminal);records.append({'revision':request['revision'],'request_id':request['request_id'],'kind':params['kind'],'kernel_seconds':kernel_seconds})
    for r in binding['packed']:need(sha(r['file'])==r['sha256'],'Packed changed during session')
    publish(target/'worker-report.json',{'status':'passed','mode':'session','request_signature':binding['request_signature'],'revisions':records,'runtime_load_count':1,'tensor_upload_count':1+generation_load_count,'data_generation_upload_count':generation_load_count,'model_executed':False,'weights_read':False})
    return records

if __name__=='__main__':
    job=Path(sys.argv[1])
    try:run(job)
    except BaseException as exc:
        code=getattr(exc,'code','VALIDATION_FAILED');path=job/'depth/worker-report.json';frames=[];tb=exc.__traceback__
        while tb:frames.append({'file':tb.tb_frame.f_code.co_filename,'line':tb.tb_lineno,'function':tb.tb_frame.f_code.co_name});tb=tb.tb_next
        if not path.exists():
            with path.open('x',encoding='utf-8') as f:json.dump({'status':'failed','error':{'code':code,'message':str(exc)},'frames':frames},f,indent=2)
        raise SystemExit({'CANCELLED':7,'TIMEOUT':6,'CONFLICT':4}.get(code,8))
