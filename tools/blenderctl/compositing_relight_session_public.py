"""One existing public job owns bounded resident CUDA worker and revision IPC."""
from contextlib import ExitStack
from pathlib import Path
import copy,json,os,re,time,uuid
from protocol import atomic_json
from processes import ProcessTree
from compositing_encode_contract import EncodeError
from compositing_depth_public import (BoundedInputs,Guard,row,descriptor,key,read as _read,publish,need,
    launch_identity as depth_identity,inspect_admission,runtime_identity,freeze_codes,resources_used)
from compositing_relight_public import launch_identity as relight_identity,gpu_admission,owned_stage,NATIVE_PYTHON,protect_native_body
from compositing_relight_contract import input_descriptors,admit_bundle,DepthError
from compositing_relight_session_contract import normalize_params,signature,check_message,preview_domain,preview_config,IDENTITY_FIELDS
from compositing_relight_revision import RevisionGate,completion
import compositing_relight_local_edits as local_edits
from compositing_relight_session_owned_worker import claim_worker,OwnedWorkerError

NAMES=('compositing_relight_session_contract.py','compositing_relight_session_client.py',
       'compositing_relight_session_public.py','compositing_relight_session_gpu_worker.py','compositing_relight_revision.py',
       'compositing_relight_session_owned_worker.py','compositing_relight_local_edits.py')

def read(path):
    # A newly renamed Windows file can still have a transient sharing denial.
    # Retry only that condition for at most 15ms; retain all schema/SHA checks
    # and propagate persistent ACL/IO failure rather than accept stale data.
    for attempt in range(4):
        try:return _read(path)
        except PermissionError:
            if attempt == 3:raise
            time.sleep(.005)

def publish_generation(path, value):
    """Same-directory no-clobber publish with temp no longer than generation.json.

    A 45-character UUID staging name crossed Windows MAX_PATH in real jobs
    even though all final members fit. Exclusive bounded retries preserve
    collisions and partial files; no system long-path setting is changed.
    """
    path=Path(path)
    for _ in range(8):
        temp=path.with_name('.g'+uuid.uuid4().hex[:8]+'.tmp')
        try:
            stream=temp.open('x',encoding='utf-8',newline='\n')
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError('Generation staging name collisions exhausted')
    with stream:
        json.dump(value,stream,indent=2,ensure_ascii=False,allow_nan=False)
        stream.flush();os.fsync(stream.fileno())
    os.rename(temp,path)

def launch_identity():
    base=Path(__file__).parent;codes=relight_identity()['files']+[row(base/n,'session:'+n) for n in NAMES]
    return {'profile':'depth-hot-session-v1','files':codes,'sha256':signature(sorted([{'role':r['role'],'sha256':r['sha256']} for r in codes],key=lambda r:r['role']))}

def run(params,job,cancel_parent,expected_identity):
    p=normalize_params(params);rp=p['relight'];job=Path(job).resolve();started=time.monotonic();target=job/'session';tree=None;claimed=None;life={};primary=None
    try:
        need(not target.exists(),'Fresh owned session required');target.mkdir(exist_ok=False);inbox=target/'inbox';inbox.mkdir();gpu_stage=target/'gpu';gpu_stage.mkdir();out=gpu_stage/'depth';out.mkdir()
        identity=launch_identity();launch=read(job/'launch.json');need(identity==expected_identity==launch.get('session_identity') and launch.get('session_request_signature')==signature(p),'Queued session identity changed')
        def checkpoint():cancel_parent();need(not (job/'cancel.request').exists(),'Owned session cancelled','CANCELLED')
        with ExitStack() as stack:
            # All later output guards remain nested in this earlier entry,
            # so the outer worker reaper runs before ANY input guards release.
            input_stack=stack.enter_context(ExitStack())
            inputs=BoundedInputs(input_stack,checkpoint)
            for n in ('request.json','launch.json'):inputs.protect(descriptor(row(job/n)))
            for r in identity['files']:inputs.protect(descriptor(r))
            for d in input_descriptors(rp):inputs.protect(d)
            bundle=inspect_admission({'bundle':rp['bundle']},inputs,depth_identity());domain=admit_bundle(rp,bundle);pd=preview_domain(domain,p['preview_divisor'])
            resources_used(rp,list(inputs.files.values()));runtime=runtime_identity(checkpoint);need(runtime==bundle['runtime'],'Different decode runtime','UNSUPPORTED')
            for r in runtime['files']:inputs.protect(descriptor(r))
            codes=freeze_codes(identity['files'],target,inputs);roles={r['role']:r for r in codes}
            common=[r for r in codes if r['role']!='depth_worker']+[dict(roles['relight:compositing_relight_kernel.py'],role='relight_kernel'),dict(roles['relight:compositing_relight_color_io.py'],role='color_io'),dict(roles['relight:compositing_depth_estimate_worker.py'],role='import_boundary')]
            pack_stage=target/'pack';pack_stage.mkdir();pack_out=pack_stage/'depth';pack_out.mkdir()
            selected=common+[dict(roles['relight:compositing_relight_io_worker.py'],role='depth_worker')]
            needed=[row(bundle['request']['params']['rgb']['file'])]+[r for r in bundle['members'] if r['role'] in ('scalar','normal','validity','normal_validity')]
            needed += [row(d['file']) for d in input_descriptors(rp) if d!=rp['bundle'] and d!=rp['compute']['runtime_profile'] and d!=rp['source'].get('parent_RGB')]+runtime['files']+selected
            b={'mode':'pack','params':rp,'request_signature':signature(rp),'domain':domain,'bundle':bundle,'codes':selected,'runtime':runtime,
               'timeout_seconds':rp['budget']['timeout_seconds'],'input_files':list({key(r['file']):r for r in needed}.values()),'packed':None,'computed':None}
            publish(pack_out/'binding.json',b);inputs.protect(descriptor(row(pack_out/'binding.json')));pack_report,_=owned_stage(pack_stage,b,checkpoint)
            packed=pack_report['members']
            for r in packed:need(Path(r['file']).parent==pack_out,'Pack escaped');inputs.protect(descriptor(r))
            t=time.monotonic();profile,files,namespace,trusted=gpu_admission(rp['compute']['runtime_profile'],inputs,checkpoint);pre_seconds=time.monotonic()-t
            native_protection=protect_native_body(inputs)
            inv=target/'gpu-runtime-inventory.json';publish(inv,files);inv=row(inv);inputs.protect(descriptor(inv))
            cache=signature({'source':rp['bundle'],'source_RGB':bundle['stage_keys']['RGB'],'masks':rp['masks'],'override':rp['source'],
                             'domain':domain,'preview_divisor':p['preview_divisor'],'local_edit_code':roles['session:compositing_relight_local_edits.py']['sha256'],'kernel':roles['relight:compositing_relight_kernel.py']['sha256']})
            gate=RevisionGate(job.name,cache);incarnation=gate._incarnation_id
            gc=common+[dict(roles['session:compositing_relight_session_gpu_worker.py'],role='depth_worker')]
            binding={'mode':'session','params':rp,'request_signature':signature(p),'domain':domain,'codes':gc,'runtime':{'python':row(profile['python']['file'])},
                     'profile':profile,'inventory':inv,'packed':packed,'preview_divisor':p['preview_divisor'],'timeout_seconds':p['lifetime_seconds']+120,
                     'native_python':NATIVE_PYTHON,'normal_options':bundle['request']['params']['normal'],
                     'session_id':job.name,'incarnation_id':incarnation,'cache_id':cache}
            publish(out/'binding.json',binding);inputs.protect(descriptor(row(out/'binding.json')))
            g=out/'gate';g.mkdir();pycache=out/'empty-pycache';pycache.mkdir();flags=['-I','-S','-B','-X','pycache_prefix='+str(pycache)];python=profile['python']['file']
            command=[NATIVE_PYTHON['file'],*flags,roles['session:compositing_relight_session_gpu_worker.py']['file'],str(gpu_stage)]
            env=os.environ.copy();env['__PYVENV_LAUNCHER__']=python
            stdout=stack.enter_context((out/'worker.stdout.log').open('x',encoding='utf-8'));stderr=stack.enter_context((out/'worker.stderr.log').open('x',encoding='utf-8'))
            tree=ProcessTree(command,job_name='blenderctl-hot-'+uuid.uuid4().hex,stdin=None,stdout=stdout,stderr=stderr,env=env,shell=False)
            def reap_owned():
                nonlocal tree
                if tree is None:return
                owned=tree
                life['reap_started_perf']=time.perf_counter()
                life['reap_root_returncode_before']=owned.process.poll()
                try:
                    # Reap the whole owned JobObject even if the bootstrap
                    # root has exited while a descendant remains.
                    owned.terminate()
                    life['reap_terminate_returned']=True
                except Exception as exc:life.setdefault('secondary_cleanup_errors',[]).append({'stage':'terminate','message':str(exc)})
                finally:
                    if claimed is not None:
                        try:claimed.wait_exit(timeout_seconds=2)
                        except Exception as exc:life.setdefault('secondary_cleanup_errors',[]).append({'stage':'actual_worker_wait','message':str(exc)})
                        try:claimed.close()
                        except Exception as exc:life.setdefault('secondary_cleanup_errors',[]).append({'stage':'actual_worker_handle_close','message':str(exc)})
                    try:
                        owned.close()
                        life['reap_close_returned']=True
                    except Exception as exc:life.setdefault('secondary_cleanup_errors',[]).append({'stage':'close','message':str(exc)})
                    life['reap_root_returncode_after']=owned.process.poll()
                    life['reap_finished_perf']=time.perf_counter()
                    tree=None
            # LIFO cleanup occurs before stdout and input/runtime guards release,
            # preserving the triggering exception and keeping the worker owned.
            stack.callback(reap_owned)
            life={'process_tree_root_pid':tree.process.pid,'ownership':'direct native actual worker; existing JobObject + verified business gate + live handle',
                  'resident_GPU':True,'model_executed':False,'weights_read':False,'native_python':NATIVE_PYTHON,
                  'native_body_protection':native_protection}
            claimed=claim_worker(tree,out,signature(p),checkpoint,expected_image=NATIVE_PYTHON['file'])
            life['actual_worker']=claimed.facts
            publish(out/'owned-process.json',life)
            ready_limit=time.monotonic()+min(120,rp['budget']['timeout_seconds'])
            def check_owned_running():
                code=tree.process.poll()
                if code is None:return
                if code in (6,7):raise DepthError('TIMEOUT' if code==6 else 'CANCELLED','Resident child returned original timeout/cancellation')
                report_path=out/'worker-report.json';error=read(report_path).get('error',{}) if report_path.is_file() else {}
                domain_code=error.get('code','WORKER_FAILED')
                if domain_code not in ('INVALID_REQUEST','CONFLICT','VALIDATION_FAILED','IO_ERROR','UNSUPPORTED','RESOURCE_LIMIT','TIMEOUT','CANCELLED'):domain_code='WORKER_FAILED'
                raise DepthError(domain_code,error.get('message','Resident GPU exited unexpectedly'))
            while not (out/'gpu-ready.json').exists():
                checkpoint();check_owned_running();need(time.monotonic()<ready_limit,'GPU startup timeout','TIMEOUT');time.sleep(.02)
            gr=read(out/'gpu-ready.json');need(gr['status']=='ready' and gr['request_signature']==signature(p),'GPU ready binding differs')
            for d in (row(out/'gpu-ready.json'),gr['baseline_display']):inputs.protect(descriptor(d))
            ready={'schema':'relight-session-ready-v1','session_id':job.name,'incarnation_id':incarnation,'cache_id':cache,'request_signature':signature(p),
                   'producer_identity':identity,'domain':pd,'source_domain':domain,'frame':rp['config']['frame'],'initial_config':rp['config'],
                   'local_edits_version':local_edits.VERSION,'max_data_generations':p.get('max_data_generations',16),'normal_options':bundle['request']['params']['normal'],
                   'reference_depth':gr['reference_depth'],'preview_divisor':p['preview_divisor'],'baseline_display':gr['baseline_display'],
                   'ready_seconds':time.monotonic()-started,'GPU_runtime_guard_seconds':pre_seconds,'runtime':gr['runtime'],
                   'final_output_contract':'linear-straight-exr-v1'}
            publish(job/'session-ready.json',ready);inputs.protect(descriptor(row(job/'session-ready.json')))
            import numpy as np
            import compositing_depth_io as io
            import compositing_relight_color_io as colorio
            original=io.read_npy(packed[0]['file'],(domain['height'],domain['width'],4),(np.float32,))
            shapes=[(domain['height'],domain['width'],4),(domain['height'],domain['width']),(domain['height'],domain['width'],3)]+[(domain['height'],domain['width'])]*5
            types=[np.float32,np.float32,np.float32,np.bool_,np.bool_,np.float32,np.float32,np.float32]
            base_arrays=[io.read_npy(r['file'],shape,(typ,)) for r,shape,typ in zip(packed,shapes,types)]
            generations={};generation_root=target/'generations';generation_root.mkdir()
            processed=set();events=[];accepted=[];last=None;active_started=None;active_generation=None;born=last_activity=time.monotonic();closing=False
            def get_generation(recipe):
                ident=local_edits.derived_id(cache,recipe)
                if local_edits.is_empty(recipe):return {'derived_cache_id':ident,'generation':None}
                if ident in generations:return generations[ident]
                need(len(generations)<p.get('max_data_generations',16),'Data generation budget exhausted; start a new session','RESOURCE_LIMIT')
                t=time.perf_counter()
                # Generate from the immutable prepared base, never from an earlier edit.
                try:values,summary=local_edits.apply(base_arrays,recipe,rp['config']['frame'],bundle['request']['params']['normal'],np)
                except (ValueError,TypeError,OverflowError) as exc:raise DepthError('INVALID_REQUEST',str(exc)) from exc
                folder=generation_root/ident;folder.mkdir(exist_ok=False);members=[]
                names=['scalar','normal','validity','normal_validity','light','highlight','line']
                if recipe['version']==local_edits.VERSION:
                    names+=['rough_mul','rough_add','receiver','caster']
                need(len(values)==len(names)+1,'Prepared generation member count differs')
                generation_shapes=shapes+[(domain['height'],domain['width'])]*4
                generation_types=types+[np.float32]*4
                for i,name in enumerate(names,1):
                    checkpoint();file=folder/(name+'.npy');value=np.ascontiguousarray(values[i])
                    with file.open('xb') as f:np.save(f,value,allow_pickle=False)
                    r=row(file,name)|{'shape':list(value.shape),'dtype':str(value.dtype)};inputs.protect(descriptor(r))
                    check=io.read_npy(file,generation_shapes[i],(generation_types[i],));need(np.array_equal(value,check),'Derived data readback changed')
                    members.append(r)
                record={'schema':'relight-local-generation-v2' if len(values)==12 else 'relight-local-generation-v1','base_cache_id':cache,'derived_cache_id':ident,'recipe':recipe,
                        'normal_options':bundle['request']['params']['normal'],'members':members,'summary':summary,
                        'prepare_seconds':time.perf_counter()-t,'source_arrays_modified':False}
                publish_generation(folder/'generation.json',record);r=row(folder/'generation.json');inputs.protect(descriptor(r))
                generations[ident]={'derived_cache_id':ident,'generation':r}
                return generations[ident]
            def dispatch(request):
                nonlocal active_started,active_generation
                while request is not None:
                    try:
                        recipe=request.parameters.decode().get('local_edits',local_edits.empty())
                        active_generation=get_generation(recipe)
                    except DepthError as exc:
                        if exc.code not in ('INVALID_REQUEST','RESOURCE_LIMIT'):raise
                        failure={'event':'rejected_local_edit','revision':request.revision,'parameters_sha256':request.parameters.sha256,'message':str(exc),'code':exc.code}
                        publish(target/('rejected-revision-'+str(request.revision)+'.json'),failure);events.append(failure)
                        request=gate.complete(completion(request,'failed',failure)).next_dispatch
                        continue
                    publish(out/'commands'/(request.request_id+'.json'),{'request':request.envelope(),'data':active_generation});active_started=time.monotonic()
                    return
            def reject(path,exc):
                receipt={'event':'rejected','file':str(path),'code':getattr(exc,'code','INVALID_REQUEST'),'message':str(exc)};events.append(receipt);publish(target/('rejected-'+uuid.uuid4().hex+'.json'),receipt)
            while True:
                checkpoint();check_owned_running()
                new=sorted(f for f in inbox.iterdir() if f.name not in processed and not f.name.startswith('.'))
                need(len(processed)+len(new)<=p['max_revisions'],'Session revision count exhausted','RESOURCE_LIMIT')
                for path in new:
                    processed.add(path.name)
                    try:
                        need(path.is_file() and re.fullmatch('[0-9]{9,10}\\.json',path.name),'Invalid inbox filename')
                        message=read(path);parameters=check_message(message,ready,rp['config']);need(path.stem==str(message['revision']).zfill(9),'Filename/revision differs')
                        cp=copy.deepcopy(rp);cp['config']=parameters['config'];admit_bundle(cp,bundle)
                        inputs.protect(descriptor(row(path)))
                        request=gate.submit(message['revision'],parameters,reason=message['reason']);last_activity=time.monotonic();events.append({'event':'accepted','revision':message['revision'],'parameters_sha256':message['parameters_sha256'],'reason':message['reason']})
                        if request:dispatch(request)
                    except (DepthError,ValueError,KeyError,TypeError) as exc:reject(path,exc)
                if (target/'stop.json').is_file():
                    stop=read(target/'stop.json');need(stop=={'schema':'relight-session-stop-v1',**{k:ready[k] for k in IDENTITY_FIELDS}},'Alien stop identity','CONFLICT');closing=True
                if time.monotonic()-born>=p['lifetime_seconds'] or time.monotonic()-last_activity>=p['idle_seconds']:closing=True
                active=gate.active
                if active:
                    need(time.monotonic()-active_started<=p['revision_timeout_seconds'],'Revision computation exceeded explicit budget','TIMEOUT')
                    folder=gpu_stage/'revisions'/(str(active.revision).zfill(9)+'-'+active.request_id);terminal=folder/'complete.json'
                    if terminal.is_file():
                        decoded=time.perf_counter();complete=read(terminal);need(complete['schema']=='relight-session-gpu-complete-v1' and complete['status'] in ('passed','rejected') and signature(complete['request'])==signature(active.envelope()),'GPU completion binding differs')
                        need(signature(complete.get('data'))==signature(active_generation),'Completion data generation differs')
                        if complete['status']=='rejected':
                            need(set(complete)=={'schema','status','request','data','error'} and
                                 set(complete['error'])=={'code','message'} and complete['error']['code']=='INVALID_REQUEST' and
                                 type(complete['error']['message']) is str,'Invalid projection refusal receipt')
                            inputs.protect(descriptor(row(terminal)))
                            failure={'event':'rejected_projection','revision':active.revision,'parameters_sha256':active.parameters.sha256,
                                     'message':complete['error']['message'],'code':complete['error']['code']}
                            publish(target/('rejected-revision-'+str(active.revision)+'.json'),failure);events.append(failure)
                            decision=gate.complete(completion(active,'failed',failure))
                            if decision.next_dispatch:dispatch(decision.next_dispatch)
                            continue
                        d=p['preview_divisor'] if active.parameters.decode()['kind']=='preview' else 1;expected=preview_domain(domain,d);hw=(expected['height'],expected['width'])
                        need(complete['domain']=={k:expected[k] for k in ('width','height')} and complete['sampling_divisor']==d,'Result domain/sampling changed')
                        members=complete['members'];names=[m['role'] for m in members];kind=active.parameters.decode()['kind']
                        config=active.parameters.decode()['config'];anime=config.get('anime')
                        need(type(members) is list and len(names)==len(set(names)) and {'output','display_srgb_png'}<=set(names),'Duplicate/missing revision members')
                        expected_roles={'output','display_srgb_png'}
                        requested_view=anime['view'] if anime and not config['bypass'] and config['mix']!=0 else 'RESULT'
                        actual_view=complete.get('view','RESULT');diagnostic=requested_view!='RESULT'
                        need(actual_view==requested_view,'Diagnostic view/fallback binding differs')
                        need(('view_display' in complete)==diagnostic and ('diagnostic_srgb_png' in names)==diagnostic,'Diagnostic member/fallback mismatch')
                        if diagnostic:expected_roles.add('diagnostic_srgb_png')
                        if kind=='final':
                            expected_roles.add('delta')
                            if 'environment' in config:expected_roles.add('environment_delta')
                            count=0 if config['bypass'] or config['mix']==0 else sum(
                                l['intensity']>0 and (not anime or not anime['solo_light_id'] or anime['solo_light_id']==l['id'])
                                for l in config['lights'])
                            fields=('diffuse','specular','visibility','delta')
                            if config.get('projection',{}).get('enabled'):fields+=('projection_visibility','projection_delta')
                            expected_roles|={f'light:{i}:{field}' for i in range(count) for field in fields}
                        need(set(names)==expected_roles,'Incomplete or unexpected revision members')
                        by_role={m['role']:m for m in members}
                        need(complete['output']==by_role['output'] and complete['display']==by_role['display_srgb_png'] and
                             Path(complete['output']['file'])==folder/'output.npy' and Path(complete['display']['file'])==folder/'display.png','Decode entry missing exact guarded member binding')
                        if diagnostic:
                            need(complete['view_display']==by_role['diagnostic_srgb_png'] and
                                 Path(complete['view_display']['file'])==folder/'view.png','Diagnostic decode entry/member binding differs')
                        for m in complete['members']:
                            need(Path(m['file']).parent==folder and type(m['bytes']) is int and Path(m['file']).stat().st_size==m['bytes'],'Revision member escaped or size changed')
                            inputs.protect(descriptor(m))
                            if Path(m['file']).suffix=='.npy':
                                shape=hw if m['role'].endswith((':visibility',':projection_visibility')) else hw+(4,) if m['role']=='output' else hw+(3,)
                                need(m['shape']==list(shape) and m['dtype']=='float32','Contribution member shape/dtype differs')
                                values=io.read_npy(m['file'],shape,(np.float32,));need(np.isfinite(values).all(),'Nonfinite contribution')
                        inputs.protect(descriptor(row(terminal)))
                        output=io.read_npy(complete['output']['file'],hw+(4,),(np.float32,));need(np.isfinite(output).all(),'Nonfinite revision')
                        source=original[d//2::d,d//2::d] if d>1 else original;need(np.array_equal(output[...,3],source[...,3]),'Alpha changed')
                        pixels,_=colorio.read(io,Path(complete['display']['file']),expected,'png',4)
                        lin=np.clip(output[...,:3].astype(np.float64),0,1);srgb=np.where(lin<=.0031308,lin*12.92,1.055*lin**(1/2.4)-.055)
                        expect=np.concatenate((np.rint(srgb*255).astype(np.uint8),np.rint(output[...,3:4]*255).astype(np.uint8)),axis=-1);need(np.array_equal(pixels,expect),'Complete display readback differs')
                        if diagnostic:
                            colorio.read(io,Path(complete['view_display']['file']),expected,'png',4)
                        published_members=list(complete['members']);linear_fields={};exr_timings={}
                        if kind=='final':
                            def final_checkpoint():
                                checkpoint();need(time.monotonic()-active_started<=p['revision_timeout_seconds'],
                                                  'Revision final encoding exceeded explicit budget','TIMEOUT')
                            exr=folder/'output-linear-straight.exr';exr_started=time.perf_counter()
                            colorio.write_linear_straight_exr(io,exr,output,expected,final_checkpoint)
                            exr_timings['host_exr_write_seconds']=time.perf_counter()-exr_started
                            linear=row(exr)|{'role':'linear_straight_exr','shape':list(output.shape),'dtype':'float32'}
                            inputs.protect(descriptor(linear));final_checkpoint();exr_started=time.perf_counter()
                            restored,exr_metadata=colorio.read_linear_straight_exr(io,exr,expected)
                            need(np.array_equal(restored.view(np.uint32),output.view(np.uint32)),'EXR float32 bits differ from output')
                            need(np.array_equal(restored[...,3].copy().view(np.uint32),source[...,3].copy().view(np.uint32)),
                                 'EXR Alpha bits differ from immutable source')
                            final_checkpoint();exr_timings['host_exr_readback_seconds']=time.perf_counter()-exr_started
                            published_members.append(linear)
                            linear_fields={'linear_output':linear,'linear_output_semantics':dict(colorio.LINEAR_EXR_SEMANTICS),
                                           'linear_output_decode':exr_metadata,'linear_output_producer':'HOST_FINAL_ENCODER'}
                        diagnostic_fields={'view':actual_view}
                        if diagnostic:diagnostic_fields['view_display']=complete['view_display']
                        manifest={'schema':'relight-session-published-v1','status':'passed','request':active.envelope(),'revision':active.revision,'parameters_sha256':active.parameters.sha256,
                                  'output':complete['output'],'display':complete['display'],'members':published_members,'domain':expected,**linear_fields,**diagnostic_fields,
                                  **active_generation,
                                  'timings':complete['timings']|exr_timings|{'host_decode_seconds':time.perf_counter()-decoded,'host_published_perf':time.perf_counter()},
                                  'authority':'live guarded owned session preview; not successful parent final bundle','parent_success_required':False,'session_must_be_running':True}
                        decision=gate.complete(completion(active,'passed',manifest));events.append({'event':decision.reason,'revision':active.revision})
                        publish(folder/'host-verified.json',manifest|{'publication_decision':decision.reason});inputs.protect(descriptor(row(folder/'host-verified.json')))
                        accepted.append(manifest)
                        if decision.published:atomic_json(target/'latest.json',manifest);last=manifest
                        if decision.next_dispatch:dispatch(decision.next_dispatch)
                if closing and gate.active is None:break
                time.sleep(.01)
            (out/'stop.request').touch(exist_ok=False)
            deadline=time.monotonic()+10
            while tree.process.poll() is None:checkpoint();need(time.monotonic()<deadline,'Resident stop timeout','TIMEOUT');time.sleep(.02)
            if tree.process.poll()!=0:check_owned_running()
            worker=read(out/'worker-report.json');need(worker['status']=='passed' and worker['request_signature']==signature(p),'Shutdown report differs')
            claimed.wait_exit(timeout_seconds=2);claimed.close()
            life.update(returncode=tree.process.poll(),usage=tree.usage());tree.close();tree=None
            need(namespace()==trusted,'Runtime import namespace changed');t=time.monotonic();inputs.recheck();post=time.monotonic()-t;checkpoint()
            report={'schema':'relight-session-report-v1','status':'closed','request_signature':signature(p),'ready':row(job/'session-ready.json'),
                    'last_passed':last,'verified_revisions':accepted,'events':events,'worker_report':row(out/'worker-report.json'),'lifecycle':life,
                    'source_bundle':rp['bundle'],'input_files':list(inputs.files.values()),'frozen_sources':codes,'post_guard_seconds':post,
                    'host_seconds':time.monotonic()-started,'model_executed':False,'weights_read':False,'runtime_load_count':1,'tensor_upload_count':worker['tensor_upload_count'],
                    'data_generations':list(generations.values()),'data_generation_count':len(generations)}
            publish(job/'session-report.json',report);publish(job/'session-complete.json',{'schema':'relight-session-closed-v1','status':'complete','parent_job_id':job.name,
                'report':row(job/'session-report.json'),'last_passed':last,'public_parent_success_required':True})
            checkpoint();return report|{'complete':row(job/'session-complete.json')}
    except Exception as original:
        primary=DepthError(original.code,str(original)) if isinstance(original,(EncodeError,OwnedWorkerError)) else original
        if tree is not None:
            try:tree.terminate();tree.close()
            except Exception as secondary:life['secondary_cleanup_error']=str(secondary)
        frames=[];tb=original.__traceback__
        while tb:
            frames.append({'file':tb.tb_frame.f_code.co_filename,'line':tb.tb_lineno,'function':tb.tb_frame.f_code.co_name});tb=tb.tb_next
        failure={'status':'failed','error':{'code':getattr(primary,'code','WORKER_FAILED'),'type':type(primary).__name__,'message':str(primary),'winerror':getattr(primary,'winerror',None),'frames':frames},'partial_eligible':False,'lifecycle':life}
        try:publish(job/'session-failure.json',failure)
        except Exception:pass
        if primary is not original:raise primary from original
        raise
