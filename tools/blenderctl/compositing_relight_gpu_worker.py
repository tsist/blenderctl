"""Finite owned CUDA0/FP32 light computation. Fixed runtime; no model/scene/network."""
import ast, hashlib, json, os, sys, time, types
from pathlib import Path

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1048576),b''):h.update(block)
    return h.hexdigest()

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
    r=codes['relight:compositing_relight_session_owned_worker.py']
    ownership=types.ModuleType('single_worker_ownership');exec(compile(Path(r['file']).read_bytes(),r['file'],'exec'),ownership.__dict__)
    native_actual=ownership.worker_gate(target,binding['request_signature'])
    need(os.path.normcase(native_actual['actual_exe'])==os.path.normcase(binding['native_python']['file']) and
         sha(native_actual['actual_exe'])==binding['native_python']['expected_sha256'],'Actual native interpreter body differs')
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
    r=codes['relight_kernel'];m=types.ModuleType('owned_relight_kernel');m.__file__=r['file'];exec(compile(Path(r['file']).read_bytes(),r['file'],'exec'),m.__dict__)
    h,w=binding['domain']['height'],binding['domain']['width'];shapes=[(h,w,4),(h,w),(h,w,3),(h,w),(h,w),(h,w),(h,w),(h,w)]
    dtypes=[np.float32,np.float32,np.float32,np.bool_,np.bool_,np.float32,np.float32,np.float32]
    arrays=[]
    for r,shape,dtype in zip(binding['packed'],shapes,dtypes):
        checkpoint();need(sha(r['file'])==r['sha256'],'Packed input changed')
        with Path(r['file']).open('rb') as f:x=np.load(f,allow_pickle=False);need(f.read(1)==b'','Packed NPY tail')
        need(x.shape==shape and x.dtype==dtype and x.flags.c_contiguous,'Packed numerical dtype/domain differs');arrays.append(x)
    rows=[]
    def save(name,role,x):
        checkpoint();path=target/name;x=np.ascontiguousarray(x)
        with path.open('xb') as f:np.save(f,x,allow_pickle=False)
        with path.open('rb') as f:restored=np.load(f,allow_pickle=False);need(f.read(1)==b'' and np.array_equal(restored,x),'GPU output NPY readback differs')
        rows.append({'file':str(path),'sha256':sha(path),'bytes':path.stat().st_size,'role':role,'dtype':str(x.dtype),'shape':list(x.shape)})
    with torch.no_grad(),torch.autocast(device_type='cuda',enabled=False):
        tensors=[torch.from_numpy(x).to('cuda:0') for x in arrays];torch.cuda.synchronize();torch.cuda.reset_peak_memory_stats();t=time.perf_counter()
        ready={'schema':'relight-single-gpu-ready-v1','request_signature':binding['request_signature'],
               'actual_worker':native_actual,'stage':'tensors_uploaded_before_kernel','device':'CUDA0_FP32',
               'GPU_name':torch.cuda.get_device_name(0),'allocated_bytes':int(torch.cuda.memory_allocated()),
               'elapsed_seconds':time.monotonic()-started,'completion_authority':False}
        need(ready['allocated_bytes']>0,'CUDA tensors were not allocated')
        partial=target/'gpu-ready.partial'
        with partial.open('x',encoding='utf-8') as f:json.dump(ready,f,allow_nan=False);f.flush();os.fsync(f.fileno())
        os.rename(partial,target/'gpu-compute-ready.json');checkpoint()
        out,delta,parts=m.render(*tensors,binding['params']['config'],m.Backend(torch,'cuda:0'));torch.cuda.synchronize();seconds=time.perf_counter()-t
        result=out.cpu().numpy();need(np.array_equal(result[...,3],arrays[0][...,3]),'Alpha differs')
        save('output.npy','output',result);save('delta.npy','delta',delta.cpu().numpy())
        for i,p in enumerate(parts):
            for name in ('diffuse','specular','visibility','delta'):save(str(i)+'-'+name+'.npy','light:'+str(i)+':'+name,p[name].cpu().numpy())
    for r in binding['packed']:checkpoint();need(sha(r['file'])==r['sha256'],'Packed input changed after compute')
    report={'status':'passed','mode':'compute','request_signature':binding['request_signature'],'members':rows,
            'stats':{'device':'CUDA0_FP32','fallback':False,'TF32':False,'autocast':False,'kernel_seconds':seconds,
                     'import_seconds':import_seconds,'peak_allocated_bytes':int(torch.cuda.max_memory_allocated()),'light_ids':[p['id'] for p in parts]},
            'seconds':time.monotonic()-started,'weights_read':False,'model_executed':False,'parent_success_required':True}
    with (target/'worker-report.json').open('x',encoding='utf-8') as f:json.dump(report,f,indent=2,allow_nan=False)
    return report

if __name__=='__main__':
    job=Path(sys.argv[1])
    try:run(job)
    except BaseException as exc:
        code=getattr(exc,'code','VALIDATION_FAILED');path=job/'depth/worker-report.json';frames=[];tb=exc.__traceback__
        while tb:frames.append({'file':tb.tb_frame.f_code.co_filename,'line':tb.tb_lineno,'function':tb.tb_frame.f_code.co_name});tb=tb.tb_next
        if not path.exists():
            with path.open('x',encoding='utf-8') as f:json.dump({'status':'failed','error':{'code':code,'message':str(exc)},'type':type(exc).__name__,'frames':frames,'partial_eligible':False},f,indent=2)
        raise SystemExit({'CANCELLED':7,'TIMEOUT':6,'CONFLICT':4}.get(code,8))
