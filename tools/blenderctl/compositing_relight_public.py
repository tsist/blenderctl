"""Owned single-batch depth consumer. Mature guards/ProcessTree; no model execution."""
from contextlib import ExitStack
import os, sys, time, uuid
from pathlib import Path
from compositing_depth_public import (BoundedInputs, Guard, row, descriptor, key, read, publish, need,
    launch_identity as depth_identity, inspect_admission, runtime_identity, freeze_codes, resources_used)
from compositing_relight_contract import normalize_params, signature, input_descriptors, admit_bundle, DepthError
from compositing_depth_estimate_public import PROFILE_SHA, MANIFEST_SHA
from processes import ProcessTree
from compositing_encode_contract import EncodeError
from compositing_relight_session_owned_worker import claim_worker,OwnedWorkerError

NAMES=('compositing_relight_contract.py','compositing_relight_kernel.py','compositing_relight_public.py',
       'compositing_relight_io_worker.py','compositing_relight_gpu_worker.py','compositing_relight_color_io.py','compositing_depth_estimate_worker.py','compositing_relight_session_owned_worker.py')

# Already-installed executable body of the original qualified Python 3.13.14;
# the original profile, venv and 14 packages remain unchanged.
NATIVE_PYTHON={'file':'C:/Program Files/WindowsApps/PythonSoftwareFoundation.Python.3.13_3.13.3824.0_x64__qbz5n2kfra8p0/python3.13.exe',
               'expected_sha256':'ad169f4cb4bfb78c7a5c030a4529c19d6643276778e33994c93e145b6191c3ec'}

def protect_native_body(inputs):
    """Exact OS-managed interpreter body, outside the 14-package payload scope.

    WindowsApps ancestors are not opened by the current user. Keep a native
    deny-write/delete FILE guard and fixed byte identity for this one already
    installed executable; all user inputs/packages retain full ancestor guards.
    The business gate additionally binds the live root PID, FILETIME and image.
    No generic Guard fallback or arbitrary runtime descriptor is accepted.
    """
    path=Path(NATIVE_PYTHON['file'])
    inputs.stack.enter_context(Guard(path))
    need(inputs.checked_digest(path)==NATIVE_PYTHON['expected_sha256'],'Fixed native interpreter body changed')
    inputs.files[key(path)]={'file':str(path),'sha256':NATIVE_PYTHON['expected_sha256']}
    return {'scope':'fixed OS interpreter body only; ancestors/OS/base-Python security not certified',
            'file_guard':True,'pre_post_sha256':True,'full_ancestor_guards':False,'descriptor':NATIVE_PYTHON}
def launch_identity():
    base=Path(__file__).parent;rows=depth_identity()['files']+[row(base/n,'relight:'+n) for n in NAMES]
    return {'profile':'depth-relight-public-v1','files':rows,
            'sha256':signature(sorted([{'role':r['role'],'sha256':r['sha256']} for r in rows],key=lambda r:r['role']))}

def gpu_admission(desc,inputs,checkpoint):
    need(desc['expected_sha256']==PROFILE_SHA,'Unqualified fixed GPU runtime profile')
    profile=read(inputs.protect(desc));need(profile['schema']=='dav2-local-qualified-profile-v1','Wrong GPU runtime profile')
    need(profile['trusted_installed_manifest']['expected_sha256']==MANIFEST_SHA,'Wrong trusted manifest')
    for name in ('root_payload_review','payload_report','payload_terminal'):
        read(inputs.protect(profile[name]))
    for d in profile['receipts'].values():inputs.protect(d)
    manifest=read(inputs.protect(profile['trusted_installed_manifest']))
    need(manifest['schema']=='dav2-wheel-installed-payload-v2' and len(manifest['files'])==16221 and len(manifest['packages'])==14,'Trusted payload range differs')
    expected={};site=Path(profile['venv_root'])/'Lib/site-packages';namespaces=set()
    for r in manifest['files']:
        checkpoint();path=Path(r['file'])
        need(path.is_relative_to(profile['venv_root']) and key(path) not in expected and type(r['bytes']) is int and path.stat().st_size==r['bytes'],'Payload member path/size differs')
        inputs.protect(descriptor(r));expected[key(path)]=r
        if path.is_relative_to(site):namespaces.add(path.relative_to(site).parts[0])
    def inventory():
        actual=set()
        for ns in sorted(namespaces):
            checkpoint();root=site/ns;paths=[root] if root.is_file() else root.rglob('*')
            for p in paths:
                if p.is_file() and p.suffix.lower() in ('.py','.pyd','.dll'):actual.add(key(p))
        return actual
    trusted={k for k,r in expected.items() if Path(r['file']).suffix.lower() in ('.py','.pyd','.dll')}
    need(inventory()==trusted,'Extra/missing executable in approved runtime namespace')
    inputs.protect(profile['python'])
    return profile, manifest['files'], inventory, trusted

def owned_stage(stage,binding,checkpoint,no_site=False):
    target=stage/'depth';gate=target/'gate';gate.mkdir(exist_ok=False);cache=target/'empty-pycache';cache.mkdir(exist_ok=False)
    flags=['-I',*(['-S'] if no_site else []),'-B','-X','pycache_prefix='+str(cache)]
    python=binding['runtime']['python']['file'];codes={r['role']:r for r in binding['codes']}
    env=os.environ.copy()
    if no_site:
        need(binding['mode']=='compute' and binding.get('native_python')==NATIVE_PYTHON,
             'Only admitted GPU compute may use direct native worker','CONFLICT')
        command=[NATIVE_PYTHON['file'],*flags,codes['depth_worker']['file'],str(stage)]
        env['__PYVENV_LAUNCHER__']=python
    else:
        command=[python,*flags,codes['host:bootstrap.py']['file'],str(gate),python,*flags,codes['depth_worker']['file'],str(stage)]
    lifecycle={'ownership':'direct native actual worker with verified Job membership and live handle' if no_site else 'existing gated ProcessTree JobObject',
               'timeout_seconds':binding['timeout_seconds'],'partial_eligible':False,'stage':binding['mode']}
    if no_site:lifecycle.update(native_python=NATIVE_PYTHON,native_body_protection=binding['native_body_protection'])
    first=None;tree=None;claimed=None;cancel_at=None;started=time.monotonic();secondary=[]
    def check_stage():
        checkpoint();need(time.monotonic()-started<=binding['timeout_seconds'],'Owned relight stage timed out','TIMEOUT')
    try:
        with (target/'worker.stdout.log').open('x',encoding='utf-8') as stdout,(target/'worker.stderr.log').open('x',encoding='utf-8') as stderr:
            tree=ProcessTree(command,job_name='blenderctl-relight-'+uuid.uuid4().hex,stdin=None,stdout=stdout,stderr=stderr,env=env,shell=False)
            lifecycle['process_tree_root_pid']=tree.process.pid
            if no_site:
                claimed=claim_worker(tree,target,binding['request_signature'],check_stage,expected_image=NATIVE_PYTHON['file'])
                lifecycle['actual_worker']=claimed.facts
            else:
                lifecycle['bootstrap_pid']=tree.process.pid;(gate/'worker.gate').touch(exist_ok=False)
            publish(target/'owned-process.json',lifecycle)
            while tree.process.poll() is None:
                if first is None:
                    try:check_stage()
                    except Exception as exc:
                        first=exc;cancel_at=time.monotonic();(stage/'depth.cancel.request').touch(exist_ok=True)
                elif time.monotonic()-cancel_at>=2:tree.terminate();break
                time.sleep(.05)
            lifecycle['returncode']=tree.process.poll()
            if first is None and lifecycle['returncode'] in (6,7):
                first=DepthError('TIMEOUT' if lifecycle['returncode']==6 else 'CANCELLED','Owned relight child cancelled/timed out')
            try:lifecycle['usage']=tree.usage()
            except Exception as exc:secondary.append({'stage':'usage','message':str(exc)})
    except Exception as exc:
        if first is None:first=exc
        else:secondary.append({'stage':'execution','message':str(exc)})
    finally:
        if tree is not None:
            lifecycle['reap_started_perf']=time.perf_counter()
            try:tree.terminate()
            except Exception as exc:
                secondary.append({'stage':'terminate','message':str(exc)})
                if first is None:first=exc
            if claimed is not None:
                try:claimed.wait_exit(timeout_seconds=2)
                except Exception as exc:
                    secondary.append({'stage':'actual_worker_wait','message':str(exc)})
                    if first is None:first=exc
                try:claimed.close()
                except Exception as exc:
                    secondary.append({'stage':'actual_worker_handle_close','message':str(exc)})
                    if first is None:first=exc
            try:tree.close();lifecycle['tree_handle_closed']=tree.handle is None
            except Exception as exc:
                secondary.append({'stage':'close','message':str(exc)})
                if first is None:first=exc
            lifecycle['reap_finished_perf']=time.perf_counter()
            lifecycle['root_returncode_after_reap']=tree.process.poll()
            if no_site and first is None:
                try:need(claimed is not None and claimed.facts['exit_confirmed'] and claimed.facts['handle_closed'] and lifecycle['tree_handle_closed'],
                         'Actual worker closure incomplete','CONFLICT')
                except Exception as exc:first=exc
        lifecycle.update(elapsed_seconds=time.monotonic()-started,secondary_errors=secondary)
        if first:lifecycle['first_error']={'code':getattr(first,'code','WORKER_FAILED'),'message':str(first)}
        try:publish(stage/'relight-lifecycle.json',lifecycle)
        except Exception as exc:
            if first is None:first=exc
        if first:raise first
    report=read(target/'worker-report.json')
    error=report.get('error',{});code=error.get('code','WORKER_FAILED')
    if code not in ('CONFLICT','TIMEOUT','CANCELLED','VALIDATION_FAILED','IO_ERROR','UNSUPPORTED','RESOURCE_LIMIT'):code='VALIDATION_FAILED'
    need(lifecycle['returncode']==0 and report.get('status')=='passed',error.get('message','Owned relight stage failed'),code)
    need(report.get('request_signature')==binding['request_signature'] and report.get('mode')==binding['mode'],'Stage request/mode binding differs')
    checkpoint();return report,lifecycle

def run(params,job,cancel_parent,expected_identity):
    params=normalize_params(params);job=Path(job).resolve();started=time.monotonic();target=job/'relight'
    try:
        need(not params['config'].get('projection',{}).get('enabled'),
             'Controlled projection requires depth-session local receiver/caster maps', 'UNSUPPORTED')
        need(not target.exists(),'Fresh owned relight job required');target.mkdir(exist_ok=False)
        identity=launch_identity();launch=read(job/'launch.json')
        need(identity==expected_identity==launch.get('relight_identity') and launch.get('relight_request_signature')==signature(params),'Queued relight source/request changed')
        def checkpoint():
            cancel_parent();need(not (job/'cancel.request').exists(),'Public relight cancelled','CANCELLED')
        with ExitStack() as stack:
            inputs=BoundedInputs(stack,checkpoint)
            for name in ('request.json','launch.json'):inputs.protect(descriptor(row(job/name)))
            for r in identity['files']:inputs.protect(descriptor(r))
            for d in input_descriptors(params):inputs.protect(d)
            bundle=inspect_admission({'bundle':params['bundle']},inputs,depth_identity())
            domain=admit_bundle(params,bundle)
            resources_used(params,list(inputs.files.values()))
            runtime=runtime_identity(checkpoint);need(runtime==bundle['runtime'],'Bundled decoder runtime has not qualified this parent','UNSUPPORTED')
            for r in runtime['files']:inputs.protect(descriptor(r))
            codes=freeze_codes(identity['files'],target,inputs);by_role={r['role']:r for r in codes}
            gpu=None;guard_time=None;namespace=None;trusted=None
            if params['compute']['device']=='CUDA0_FP32':
                t=time.monotonic();gpu,files,namespace,trusted=gpu_admission(params['compute']['runtime_profile'],inputs,checkpoint);guard_time=time.monotonic()-t
                native_protection=protect_native_body(inputs)
                inv=target/'gpu-runtime-inventory.json';publish(inv,files);inv=row(inv);inputs.protect(descriptor(inv))
            reports=[];packed=None;computed=None
            def stage(mode):
                nonlocal packed,computed
                folder=target/mode;folder.mkdir(exist_ok=False);out=folder/'depth';out.mkdir(exist_ok=False)
                selected=[r for r in codes if r['role']!='depth_worker']
                selected.append(dict(by_role['relight:compositing_relight_kernel.py'],role='relight_kernel'))
                selected.append(dict(by_role['relight:compositing_relight_color_io.py'],role='color_io'))
                gpu_compute=gpu is not None and mode=='compute'
                worker=by_role['relight:compositing_relight_gpu_worker.py' if gpu_compute else 'relight:compositing_relight_io_worker.py']
                selected.append(dict(worker,role='depth_worker'))
                selected.append(dict(by_role['relight:compositing_depth_estimate_worker.py'],role='import_boundary'))
                if mode=='pack':
                    needed=[row(bundle['request']['params']['rgb']['file'])]+[r for r in bundle['members'] if r['role'] in ('scalar','normal','validity','normal_validity')]
                    needed+= [row(d['file']) for d in input_descriptors(params) if key(d['file'])!=key(params['bundle']['file']) and d!=params['compute'].get('runtime_profile') and d!=params['source'].get('parent_RGB')]
                else:needed=(packed or [])+(computed or [])
                if not gpu_compute:needed+=runtime['files']+selected
                binding={'mode':mode,'params':params,'request_signature':signature(params),'domain':domain,'bundle':bundle,
                         'codes':selected,'runtime':{'python':row(gpu['python']['file'])} if gpu_compute else runtime,
                         'timeout_seconds':params['budget']['timeout_seconds'],'input_files':list({key(r['file']):r for r in needed}.values()),
                         'packed':packed,'computed':computed}
                if gpu_compute:binding.update(profile=gpu,inventory=inv,native_python=NATIVE_PYTHON,native_body_protection=native_protection)
                publish(out/'binding.json',binding);inputs.protect(descriptor(row(out/'binding.json')))
                report,lifecycle=owned_stage(folder,binding,checkpoint,gpu_compute)
                for r in report['members']:
                    need(Path(r['file']).parent==out and type(r['bytes']) is int and Path(r['file']).stat().st_size==r['bytes'],'Stage output escaped/size changed')
                    inputs.protect(descriptor(r))
                inputs.protect(descriptor(row(out/'worker-report.json')))
                reports.append({'mode':mode,'report':row(out/'worker-report.json'),'lifecycle':row(folder/'relight-lifecycle.json'),'stats':report['stats']})
                if mode=='pack':packed=report['members']
                if mode=='compute':computed=report['members']
                return report
            stage('pack');stage('compute');encoded=stage('encode')
            if namespace:need(namespace()==trusted,'GPU import namespace changed while guarded')
            post=time.monotonic();inputs.recheck();post_seconds=time.monotonic()-post;checkpoint()
            report={'schema':'compositing-relight-report-v1','status':'passed','operation':'depth-relight','request_signature':signature(params),
                    'source_bundle':params['bundle'],'source_RGB':bundle['request']['params']['rgb'],'source':params['source'],'domain':domain,
                    'frame':params['config']['frame'],'config':params['config'],'masks':params['masks'],'compute':params['compute'],
                    'members':encoded['members'],'stages':reports,'frozen_sources':codes,'producer_identity':identity,
                    'input_protection':'Windows live deny-write/delete plus ancestor guards and pre/post SHA',
                    'GPU_runtime_guard_seconds':guard_time,'post_guard_seconds':post_seconds,'host_elapsed_seconds':time.monotonic()-started,
                    'model_executed':False,'weights_read':False,'display_quality_qualified':False,'hot_session':False,
                    'qualification':'experimental finite supplemental-light consumer; explicit geometric/artistic boundary, not inverse lighting',
                    'public_parent_success_required':True}
            publish(job/'relight-report.json',report)
            published={'schema':'compositing-relight-bundle-v1','status':'complete','parent_job_id':job.name,
                       'request_signature':signature(params),'parent_documents':{n:row(job/n) for n in ('request.json','launch.json')},
                       'source_bundle':params['bundle'],'report':row(job/'relight-report.json'),'members':report['members'],
                       'stage_key':signature({'params':params,'implementation':identity['sha256'],'source_keys':bundle['stage_keys']}),
                       'public_parent_success_required':True,'display_quality_qualified':False}
            publish(job/'relight-bundle.json',published);checkpoint()
            return report|{'bundle':row(job/'relight-bundle.json')}
    except Exception as original:
        # Guard failures use the mature encoder exception class; preserve its
        # primary code at this domain boundary rather than losing it in runner.
        exc=DepthError(original.code,str(original)) if isinstance(original,(EncodeError,OwnedWorkerError)) else original
        failure={'status':'failed','error':{'code':getattr(exc,'code','WORKER_FAILED'),'message':str(exc)},'partial_eligible':False}
        try:
            if not (job/'relight-failure.json').exists():publish(job/'relight-failure.json',failure)
        except Exception:pass
        if exc is not original:raise exc from original
        raise
