# SPDX-License-Identifier: GPL-3.0-or-later
"""One host supervisor, bounded independent Blender trees, immutable resume attempts."""
import copy,hashlib,json,os,re,shutil,subprocess,sys,threading,time,traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack,nullcontext
from pathlib import Path
from protocol import VERSION,ROOT,CODES,Failure,atomic_json,read_json,digest,envelope,validate
from pipeline_contract import normalize,get,put,SUCCESS,disk_usage
from filesystem import FileGuard,PathLocks

CONSTRUCTORS={'scene.prepare','model.prepare','node.prepare','rig.prepare','simulation.prepare','media.prepare','extension.prepare'}
NO_REUSE={'extension.run','simulation.bake','dependency.audit','asset.prepare','asset.index','identity.prepare','sculpt.replay'}
NO_REUSE.update(('tracking.prepare','tracking.inspect','tracking.solve'))
NO_REUSE.update(('inspect','project.verify','project.prepare_copy','project.link.prepare','project.override.prepare','project.override.resync','exchange.analyze'))

def key(v):return hashlib.sha256(json.dumps(v,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()

def runtime_identity(blender,stack,checkpoint):
    # Exact implementation/engine bytes plus content SHA of shipped runtime files.
    files=[*sorted(Path(__file__).parent.glob('*.py')),Path(blender),Path(sys.executable)]
    hashes={}
    for p in files:
        g=stack.enter_context(FileGuard(p));hashes[str(p)]=g.sha256(checkpoint)
    from installation import content_inventory
    inventory=content_inventory(blender,checkpoint)
    version=subprocess.run([str(blender),'--version'],capture_output=True,encoding='utf-8',timeout=15,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    found=re.search(r'^\s*build hash:\s*([0-9a-f]+)\s*$',version.stdout,re.MULTILINE)
    if version.returncode or not found:raise Failure('UNSUPPORTED','Cannot verify Blender build for pipeline runtime identity')
    checkpoint()
    return {'cli_version':VERSION,'hashes':hashes,'blender_build':found.group(1),'runtime_inventory':inventory,'scope':'exact engine/host/CLI bytes and native build; shipped runtime dependency SHA inventory excluding mutable portable user config; not a malicious-installation sandbox'}

def documents(params):
    result=[]
    def walk(v):
        if isinstance(v,dict):
            for name in ('file','other'):
                if isinstance(v.get(name),str):result.append({'file':v[name],'expected_sha256':v.get('expected_sha256' if name=='file' else 'other_sha256')})
            for x in v.values():walk(x)
        elif isinstance(v,list):
            for x in v:walk(x)
    walk(params)
    return result

def protect(params,command,blender,stack,checkpoint):
    if command=='extension.run' and 'review_receipt' in params:
        from reviews import verify_run
        verify_run(params,blender,stack,checkpoint)
    docs=documents(params)
    if command in ('project.link.prepare','project.override.prepare','project.override.resync'):
        if command=='project.override.resync':
            descriptor=params['manifest']['receipt'];guard=stack.enter_context(FileGuard(descriptor['file']))
            if guard.sha256(checkpoint)!=descriptor['expected_sha256']:raise Failure('CONFLICT','Override receipt changed before pipeline expansion')
        from link_contract import input_documents
        docs+=input_documents(params,command)
    if command.startswith('tracking.'):
        from tracking_contract import input_documents
        descriptor=params.get('resources')
        if isinstance(descriptor,dict):
            guard=stack.enter_context(FileGuard(descriptor['file']))
            if guard.sha256(checkpoint)!=descriptor['expected_sha256']:raise Failure('CONFLICT','Tracking resources descriptor changed')
        docs+=input_documents(params)
    if command in ('dependency.audit','dependency.plan-relink','dependency.prepare'):
        anchors=[];closure=params.get('profile',{}).get('closure',{})
        if closure.get('simulation_receipt'):anchors.append(closure['simulation_receipt'])
        if command=='dependency.plan-relink':anchors.append(params['manifest']['baseline'])
        if command=='dependency.prepare':anchors.append(params['plan'])
        for descriptor in anchors:
            g=stack.enter_context(FileGuard(descriptor['file']))
            if g.sha256(checkpoint)!=descriptor['expected_sha256']:raise Failure('CONFLICT','Dependency descriptor changed')
        from dependency_contract import input_documents
        docs+=input_documents(params,command)
    if command=='asset.preview.prepare' and params.get('reuse_receipt'):
        from preview_contract import receipt_inputs
        d=params['reuse_receipt'];g=stack.enter_context(FileGuard(d['file']))
        if g.sha256(checkpoint)!=d['expected_sha256']:raise Failure('CONFLICT','Preview receipt changed')
        docs+=receipt_inputs(read_json(d['file']))
    if command=='extension.prepare':
        from exchange_contract import addon_documents
        docs+=addon_documents(blender)
    receipt=params.get('simulation_receipt') or (params.get('manifest',{}).get('receipt') if command=='simulation.bake' else None)
    if receipt:
        from simulation_contract import receipt_contract
        g=stack.enter_context(FileGuard(receipt['file']));sha=g.sha256(checkpoint)
        if sha!=receipt['expected_sha256']:raise Failure('CONFLICT','Receipt hash changed')
        value=read_json(receipt['file']);receipt_contract(value);docs+=value['files']
    hashes={}
    for d in docs:
        p=Path(d['file'])
        if not p.is_absolute():raise Failure('INVALID_REQUEST','Pipeline file inputs must be absolute')
        name=str(p.resolve())
        if name not in hashes:
            g=stack.enter_context(FileGuard(p));hashes[name]=g.sha256(checkpoint)
        if d.get('expected_sha256') and d['expected_sha256']!=hashes[name]:raise Failure('CONFLICT','Pipeline input hash differs: '+str(p))
    return hashes

def retain_library_inputs(row,stack,checkpoint):
    """Worker locks its native typed closure; retain verified members for later DAG steps.

    This records discovered inputs, but does not grant generic cache reuse.
    """
    from link_contract import DOCUMENTS
    from scene_contract import validate as validate_shape
    documents=(row.get('data') or {}).get('resources');validate_shape(documents,DOCUMENTS)
    for document in documents:
        guard=stack.enter_context(FileGuard(document['file']));sha=guard.sha256(checkpoint)
        if sha!=document['expected_sha256']:raise Failure('CONFLICT','Linked dependency changed before pipeline retained it')
        name=str(Path(document['file']).resolve())
        if name in row['input_hashes'] and row['input_hashes'][name]!=sha:raise Failure('CONFLICT','Linked dependency conflicts with declared input')
        row['input_hashes'][name]=sha

def collect_outputs(job,stack,checkpoint):
    outputs={}
    request=read_json(job/'request.json')
    isolated=request.get('command')=='extension.run' and request.get('params',{}).get('manifest',{}).get('isolation')=='WINDOWS_APPCONTAINER_V1'
    if request.get('command')=='sculpt.replay':
        from sculpt_host import OUTPUTS
        for name in OUTPUTS:
            p=job/name;g=stack.enter_context(FileGuard(p));outputs[str(p)]=g.sha256(checkpoint)
        return outputs
    # All regular worker artifacts, including embedded cache files and reports.
    # Exclude only mutable supervision records. No claimed reusable data may be a link.
    # Isolated jobs publish only host-received top-level outputs. Never walk or
    # hash arbitrary sandbox leftovers, even after the sandbox has terminated.
    for p in sorted(job.iterdir() if isolated else job.rglob('*')):
        if p.is_dir():
            if p.is_symlink() or p.is_junction():raise Failure('UNSUPPORTED','Reusable artifacts cannot contain directory links')
            continue
        if p.name in ('status.json','cancel.request','worker.gate') and p.parent==job:continue
        if p.is_file():
            g=stack.enter_context(FileGuard(p));outputs[str(p)]=g.sha256(checkpoint)
    return outputs

def reusable(step,rows):
    # Closed inputs are a producer property, separate from this run's reuse flag.
    if step['command'] in NO_REUSE or step['command']=='dependency.prepare':return False
    if step['command']=='render.run' and step['params'].get('manifest',{}).get('device',{}).get('backend')!='CPU':return False
    if step['command']=='exchange.import':return step['params']['manifest']['mode']=='STATIC'
    if 'file' not in step['params'] and step['command'] in CONSTRUCTORS:return True
    # File-backed work is reusable only along a known closed producer lineage.
    sources=[b for b in step['bindings'] if b['target']=='/file']
    return bool(sources) and all(rows[b['step']].get('closed_inputs',False) for b in sources) and all(rows[d].get('closed_inputs',False) for d in step['depends_on'])

def dependency_proof(row,runtime,stack,checkpoint):
    """Admit only a verified dependency.prepare candidate and its native proof."""
    if row.get('command')!='dependency.prepare' or row.get('state') not in SUCCESS or not runtime:
        raise Failure('CONFLICT','Typed closure requires a successful dependency.prepare producer')
    data=row.get('data') or {};outputs=row.get('output_hashes') or {}
    directory=Path(row.get('child_directory','')).resolve()
    needed=('candidate','candidate_sha256','closure_report','closure_sha256')
    if any(not isinstance(data.get(k),str) for k in needed):raise Failure('VALIDATION_FAILED','Dependency producer is missing candidate/closure evidence')
    candidate=Path(data['candidate']).resolve();proof_path=Path(data['closure_report']).resolve()
    for path,sha in ((candidate,data['candidate_sha256']),(proof_path,data['closure_sha256'])):
        if not row.get('child_directory') or not path.is_relative_to(directory) or outputs.get(str(path))!=sha:
            raise Failure('CONFLICT','Dependency proof and candidate must be verified child-job outputs')
        guard=stack.enter_context(FileGuard(path))
        if guard.sha256(checkpoint)!=sha:raise Failure('CONFLICT','Dependency producer output changed')
    report=read_json(proof_path)
    if not isinstance(report,dict):raise Failure('CONFLICT','Dependency producer proof must be an object')
    expected_implementation=key({Path(p).name:sha for p,sha in runtime['hashes'].items() if Path(p).parent==Path(__file__).parent and Path(p).suffix=='.py'})
    if report.get('version')!='1.0' or report.get('adapter')!='TYPED_FILES_V1' or report.get('closure')!='closed' or report.get('issues')!=[] or report.get('cycles')!=[]:
        raise Failure('CONFLICT','Dependency producer proof is not a closed TYPED_FILES_V1 report')
    if report.get('implementation_sha256')!=expected_implementation or report.get('blender_build')!=runtime.get('blender_build'):
        raise Failure('CONFLICT','Dependency producer implementation/build differs from current pipeline')
    source=report.get('source') or {}
    if not isinstance(source,dict) or source.get('expected_sha256')!=data['candidate_sha256'] or not isinstance(source.get('file'),str) or Path(source['file']).resolve()!=candidate:
        raise Failure('CONFLICT','Dependency proof does not describe the exact produced candidate')
    resources=report.get('resources')
    if not isinstance(resources,list) or any(not isinstance(r,dict) or r.get('complete') is not True or not isinstance(r.get('kind'),str) for r in resources):raise Failure('CONFLICT','Dependency producer has incomplete resources')
    profile=copy.deepcopy(report.get('profile'))
    from verification_contract import validate_profile
    validate_profile(profile)
    if profile.get('closure',{}).get('adapter')!='TYPED_FILES_V1':raise Failure('CONFLICT','Dependency proof profile has no typed closure contract')
    hashes={};documents=report.get('documents')
    if not isinstance(documents,list) or not documents:raise Failure('CONFLICT','Dependency proof has no closed document set')
    for document in documents:
        from verification_contract import file_input
        if not isinstance(document,dict):raise Failure('CONFLICT','Dependency proof document must be a file descriptor')
        file_input(document);name=str(Path(document['file']).resolve())
        if name in hashes:raise Failure('CONFLICT','Dependency proof has duplicate documents')
        guard=stack.enter_context(FileGuard(name));sha=guard.sha256(checkpoint)
        if sha!=document['expected_sha256']:raise Failure('CONFLICT','Dependency proof document changed: '+name)
        hashes[name]=sha
    if hashes.get(str(candidate))!=data['candidate_sha256']:raise Failure('CONFLICT','Dependency proof document set omits its candidate')
    for resource in resources:
        members=resource.get('members')
        if not isinstance(members,list):raise Failure('CONFLICT','Dependency resource has no exact member list')
        for member in members:
            if not isinstance(member,dict) or not isinstance(member.get('file'),str):raise Failure('CONFLICT','Dependency resource member must be a file descriptor')
            if hashes.get(str(Path(member['file']).resolve()))!=member.get('expected_sha256'):raise Failure('CONFLICT','Resource member is not covered by the closed document set')
    temporal=any(r.get('source')=='SEQUENCE' or 'cache' in r.get('kind','').lower() or r.get('kind') in ('geometry_nodes_bake','image_sequence_binding') for r in resources)
    if temporal and 'timeline' not in profile:raise Failure('UNSUPPORTED','Sequence/cache closure requires an explicit proven timeline')
    return {'report':str(proof_path),'sha256':data['closure_sha256'],'candidate':str(candidate),'candidate_sha256':data['candidate_sha256'],'profile':profile,'documents':hashes,'temporal':temporal,'scope':'input file closure only; render/animation/licensing are not certified'}

def consumer_proof(step,rows):
    """Keep the original proof domain; derived artifacts do not widen it."""
    inherited=[(b,rows[b['step']]['dependency_proof']) for b in step['bindings'] if rows[b['step']].get('dependency_proof')]
    if not inherited:return None
    if len(inherited)!=1:raise Failure('UNSUPPORTED','Typed closure consumers require one direct candidate binding')
    binding,proof=inherited[0]
    if binding['target']!='/file' or Path(step['params'].get('file','')).resolve()!=Path(proof['candidate']).resolve():
        raise Failure('UNSUPPORTED','Typed closure can only follow the original verified candidate file')
    command=step['command'];profile=step['params'].get('profile',{})
    if command=='query':
        if set(profile)-{'hash_resources','require_complete'}:raise Failure('UNSUPPORTED','Typed closure query only supports static inspection without additional profile inputs')
    elif command=='render.run':
        spec=step['params']['manifest'];timeline=proof['profile'].get('timeline')
        if spec['device']['backend']!='CPU':raise Failure('UNSUPPORTED','Typed closure lineage currently supports CPU rendering only')
        if timeline:
            start=timeline['start'];end=timeline['end'];stride=timeline.get('step',1)
            if spec['scene']!=timeline['scene'] or any(f<start or f>end or (f-start)%stride for f in spec['frames']):
                raise Failure('CONFLICT','Render frames/scene lie outside the dependency proof timeline')
        elif proof['temporal']:raise Failure('CONFLICT','Temporal rendering has no dependency proof timeline')
    else:raise Failure('UNSUPPORTED','Typed closure lineage currently supports render.run and unevaluated query only')
    return copy.deepcopy(proof)

def verify_reuse(old,cache_key,stack,checkpoint,runtime=None):
    if old.get('state') not in SUCCESS or old.get('cache_key')!=cache_key or not old.get('closed_inputs') or not old.get('output_hashes'):return False
    # Lock the complete recorded result first, then compare all bytes; retain guards
    # throughout downstream consumption. A mismatch causes fresh candidate work.
    trial=ExitStack()
    try:
        for p,sha in old['output_hashes'].items():
            g=trial.enter_context(FileGuard(p))
            if g.sha256(checkpoint)!=sha:trial.close();return False
        if old.get('command')=='dependency.prepare':old['dependency_proof']=dependency_proof(old,runtime,trial,checkpoint)
        stack.enter_context(trial);return True
    except Failure as ex:
        trial.close()
        if ex.code in ('CANCELLED','TIMEOUT','RESOURCE_LIMIT'):raise
        return False
    except OSError:trial.close();return False

def execute(job,parent_checkpoint=None):
    import runner
    guards=ExitStack()
    try:guards.enter_context(PathLocks([job]))
    except Failure as ex:
        # A fail-closed platform/lock refusal is terminal, including for async
        # clients. Never leave a rejected Linux pipeline queued indefinitely.
        guards.close()
        request=read_json(job/'request.json');status=read_json(job/'status.json')
        code=CODES[ex.code]
        result=envelope(request['command'],job.name,error={'code':ex.code,'message':str(ex)},artifacts={'job_directory':str(job)})
        status.update(state='failed',exit_code=code,finished_at=time.time(),heartbeat_at=time.time())
        atomic_json(job/'result.json',result);atomic_json(job/'status.json',status)
        return result,code
    started=time.monotonic();request=read_json(job/'request.json');launch=read_json(job/'launch.json');status=read_json(job/'status.json');abort=threading.Event();rows={};error=None;manifest=None;runtime=None;futures={};max_active=0;observed_disk=0;last_disk=0;prior_id=None;prior={};pool=None
    status.update(state='running',supervisor_pid=os.getpid(),started_at=time.time());artifacts={'job_directory':str(job),'pipeline_state':str(job/'pipeline-state.json'),'pipeline_report':str(job/'pipeline-report.json')}
    def child_checkpoint():
        if abort.is_set() or (job/'cancel.request').exists():raise Failure('CANCELLED','Parent pipeline cancelled')
        if time.monotonic()-started>launch['timeout_seconds']:raise Failure('TIMEOUT','Parent pipeline timeout')
    def checkpoint():
        nonlocal last_disk,observed_disk
        if parent_checkpoint:parent_checkpoint()
        child_checkpoint()
        if time.time()-status['heartbeat_at']>=1:
            status['heartbeat_at']=time.time();status['progress']={state:sum(r['state']==state for r in rows.values()) for state in set(r['state'] for r in rows.values())};atomic_json(job/'status.json',status)
        if manifest and time.monotonic()-last_disk>=.25:
            size=disk_usage(job);last_disk=time.monotonic();observed_disk=max(size,observed_disk)
            if size>manifest['policy']['disk_mb']*1048576:raise Failure('RESOURCE_LIMIT','Observed pipeline disk budget exceeded; partial jobs retained')
            if shutil.disk_usage(job).free<manifest['policy']['min_free_mb']*1048576:raise Failure('RESOURCE_LIMIT','Free disk space below pipeline floor')
    def record(final=False):
        report={'pipeline_report_version':'1.0','job_id':job.name,'manifest':manifest,'reuse_job':prior_id,'runtime_key':key(runtime) if runtime else None,'steps':list(rows.values()),'max_active_workers':max_active,'observed_disk_bytes':observed_disk,'elapsed_seconds':time.monotonic()-started,'complete':final,'error':error,'execution':'independent workers; no publication/transaction commands; explicit resume creates a new attempt'}
        atomic_json(job/('pipeline-report.json' if final else 'pipeline-state.json'),report)
        if final and any(r['command']=='asset.preview.prepare' for r in rows.values()):
            preview_rows=[]
            for r in rows.values():
                if r['command']!='asset.preview.prepare':continue
                data=r.get('data') or {};preview_rows.append({'id':r['id'],'state':r['state'],'error':r.get('error'),'mode':data.get('mode'),'receipt':data.get('receipt'),'candidate':data.get('candidate'),'outputs':data.get('outputs',[])})
            p=job/'preview-batch-report.json';atomic_json(p,{'preview_batch_report_version':'1.0','job_id':job.name,'items':preview_rows,'error':error,'source_saved':False});artifacts['preview_batch_report']=str(p)
        return report
    def fail_row(row,ex):
        row.update(state='failed',error={'code':ex.code if isinstance(ex,Failure) else 'WORKER_FAILED','message':str(ex)},finished_at=time.time())
    try:
        with nullcontext(guards):
            prior_id=request['params'].get('job_id') if request['command']=='pipeline.resume' else request['params'].get('reuse_job')
            previous=runner.job_path(job.parent,prior_id) if prior_id else None
            if previous:guards.enter_context(PathLocks([previous]))
            for p in (job/'request.json',job/'launch.json'):guards.enter_context(FileGuard(p))
            atomic_json(job/'status.json',status)
            if previous:
                prior_command=read_json(previous/'request.json')['command']
                if not prior_command.startswith('pipeline.') and prior_command!='asset.preview.batch':raise Failure('INVALID_REQUEST','Reuse source must be a pipeline/batch job in this jobs directory')
                for name in ('pipeline-report.json','pipeline-state.json'):
                    if (previous/name).exists():
                        guards.enter_context(FileGuard(previous/name));prior=read_json(previous/name);break
                if not prior:raise Failure('CONFLICT','Prior pipeline has no recoverable definition yet')
            if request['command']=='asset.preview.batch':
                from preview_contract import expand_batch
                manifest=expand_batch(request['params']['manifest'])
            else:manifest=normalize(prior['manifest'] if request['command']=='pipeline.resume' else request['params']['manifest'])
            rows={s['id']:{'id':s['id'],'command':s['command'],'state':'pending','depends_on':s['depends_on'],'child_job':None,'error':None} for s in manifest['steps']};byid={s['id']:s for s in manifest['steps']};oldrows={s['id']:s for s in prior.get('steps',[])};record();checkpoint()
            if request['command']=='pipeline.plan':
                for row in rows.values():row['state']='planned'
            else:
                runtime=runtime_identity(launch['blender'],guards,checkpoint);atomic_json(job/'pipeline-runtime.json',runtime);pool=ThreadPoolExecutor(max_workers=manifest['policy']['max_workers']);policy=manifest['policy'];halt=False
                while any(r['state'] in ('pending','running') for r in rows.values()):
                    checkpoint()
                    for future in list(futures):
                        if not future.done():continue
                        ident,step,step_guards=futures.pop(future);row=rows[ident]
                        keep_inputs=False
                        try:
                            result,code=future.result();row.update(data=result.get('data'),error=result.get('error'),finished_at=time.time(),state='succeeded' if code==0 else 'cancelled' if code==7 else 'timed_out' if code==6 else 'failed')
                            if code==0:
                                row['output_hashes']=collect_outputs(Path(row['child_directory']),guards,checkpoint)
                                if step['command'] in ('project.link.prepare','project.override.prepare','project.override.resync'):
                                    retain_library_inputs(row,guards,checkpoint)
                                if step['command']=='dependency.prepare':
                                    row['dependency_proof']=dependency_proof(row,runtime,guards,checkpoint)
                                    row['closed_inputs']=True;keep_inputs=True
                        except Exception as ex:fail_row(row,ex)
                        finally:
                            if keep_inputs:guards.enter_context(step_guards)
                            else:step_guards.close()
                        if row['state'] not in SUCCESS and policy['failure']=='fail_fast':halt=True
                        record()
                    for ident,row in rows.items():
                        if row['state']!='pending':continue
                        if halt:row.update(state='blocked',reason='fail_fast');continue
                        deps=[rows[d]['state'] for d in row['depends_on']]
                        if any(d in ('failed','blocked','cancelled','timed_out') for d in deps):row.update(state='blocked',reason='dependency_failed');continue
                        if not all(d in SUCCESS for d in deps):continue
                        step=copy.deepcopy(byid[ident]);active=[s for _,s,_ in futures.values()]
                        if len(active)>=policy['max_workers']:continue
                        if any(sum(s['limits'][k] for s in active)+step['limits'][k]>policy[k] for k in ('cpu_threads','memory_mb','gpu_mb')):continue
                        # One admitted GPU task at a time; gpu_mb is a declared reservation.
                        if step['limits']['gpu_mb'] and any(s['limits']['gpu_mb'] for s in active):continue
                        step_guards=ExitStack()
                        try:
                            for b in step['bindings']:
                                src=rows[b['step']];path=get(src['data'],b['artifact'])
                                if not isinstance(path,str) or path not in src.get('output_hashes',{}):raise Failure('INVALID_REQUEST','Binding must select a verified output file, not an arbitrary result value')
                                put(step['params'],b['target'],path);put(step['params'],b['sha_target'],src['output_hashes'][path])
                            child=validate({'schema_version':'1.0','command':step['command'],'params':step['params']});step['params']=child['params']
                            is_gpu=step['command']=='sculpt.replay' or (step['command'] in ('render.run','asset.preview.prepare') and step['params']['manifest']['device']['backend']!='CPU')
                            if is_gpu and not step['limits']['gpu_mb']:raise Failure('INVALID_REQUEST','GPU worker requires a nonzero declared gpu_mb reservation')
                            hashes=protect(child['params'],step['command'],launch['blender'],step_guards,checkpoint)
                            proof=consumer_proof(step,rows)
                            closed=reusable(step,rows);ck=key({'runtime':key(runtime),'command':step['command'],'params':child['params'],'inputs':hashes,'limits':step['limits'],'dependencies':{d:rows[d].get('cache_key') for d in row['depends_on']},**({'dependency_proof':proof} if proof else {})})
                            row.update(cache_key=ck,input_hashes=hashes,closed_inputs=closed)
                            if proof:row['dependency_proof']=proof
                            # Asset-file previews stay outside the generic closed
                            # producer lineage. A matching prior result only
                            # supplies a receipt to a fresh validating worker.
                            old=oldrows.get(ident,{})
                            if step['command']=='asset.preview.prepare' and step['reuse'] and not child['params'].get('reuse_receipt') and (old.get('data') or {}).get('receipt') and verify_reuse({**old,'closed_inputs':True},ck,guards,checkpoint,runtime):
                                prior_data=old['data'];child['params']['reuse_receipt']={'file':prior_data['receipt'],'expected_sha256':prior_data['receipt_sha256']}
                            if step['reuse'] and (closed or step['command']=='dependency.prepare') and verify_reuse(old,ck,guards,checkpoint,runtime):
                                row.update(state='reused',data=old['data'],output_hashes=old['output_hashes'],reused_from=old.get('reused_from') or old.get('child_job'),finished_at=time.time())
                                if old.get('child_directory'):row['child_directory']=old['child_directory']
                                if step['command']=='dependency.prepare':
                                    row.update(closed_inputs=True,dependency_proof=old['dependency_proof']);guards.enter_context(step_guards)
                                else:step_guards.close()
                                record();continue
                            child_job=runner.prepare(child,launch['blender'],job/'children',step['timeout_seconds'],launch['transactions_root'],limits=step['limits'],resource_domain=policy.get('resource_domain'))
                            row.update(state='running',child_job=child_job.name,child_directory=str(child_job),started_at=time.time());record()
                            future=pool.submit(runner.execute_worker,child_job,child_checkpoint);futures[future]=(ident,step,step_guards);max_active=max(max_active,len(futures))
                        except Exception as ex:
                            step_guards.close()
                            if isinstance(ex,Failure) and ex.code in ('CANCELLED','TIMEOUT','RESOURCE_LIMIT'):raise
                            fail_row(row,ex)
                            if policy['failure']=='fail_fast':halt=True
                            record()
                    if futures:time.sleep(.05)
                checkpoint()
                if any(row['state'] not in SUCCESS for row in rows.values()):error={'code':'VALIDATION_FAILED','message':'Pipeline has failed/blocked steps; inspect report and explicitly resume'}
            # Recheck owned outputs and inputs while guards still held. Runtime
            # installation inventory is re-scanned on every new attempt.
            record(True)
    except KeyboardInterrupt:error={'code':'CANCELLED','message':'Pipeline interrupted'}
    except Failure as ex:error={'code':ex.code,'message':str(ex)}
    except OSError as ex:error={'code':'IO_ERROR','message':str(ex)}
    except Exception as ex:
        error={'code':'WORKER_FAILED','message':str(ex)};(job/'supervisor-error.log').write_text(traceback.format_exc(),encoding='utf-8')
    finally:
        abort.set()
        if pool:pool.shutdown(wait=True,cancel_futures=False)
        for future,(ident,step,sg) in futures.items():
            try:
                r,code=future.result();rows[ident].update(state='cancelled' if code==7 else 'timed_out' if code==6 else 'failed',error=r.get('error') or {'code':'CANCELLED','message':'Parent result not accepted'},finished_at=time.time())
            except Exception as ex:fail_row(rows[ident],ex)
            finally:sg.close()
        for row in rows.values():
            if row['state']=='pending':row.update(state='cancelled' if error and error['code']=='CANCELLED' else 'blocked',reason='parent_stopped')
    try:
        report=record(True);code=CODES[error['code']] if error else 0
        status.update(state='cancelled' if code==7 else 'timed_out' if code==6 else 'failed' if code else 'succeeded',exit_code=code,finished_at=time.time(),heartbeat_at=time.time(),elapsed_seconds=round(time.monotonic()-started,3),progress={s:sum(r['state']==s for r in rows.values()) for s in set(r['state'] for r in rows.values())})
        result=envelope(request['command'],job.name,report if not error else None,error,artifacts);atomic_json(job/'result.json',result);atomic_json(job/'status.json',status);return result,code
    finally:guards.close()
