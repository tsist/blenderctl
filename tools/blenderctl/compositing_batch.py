"""Serial bounded M5 candidate: live closure guards and verified parent authority."""
import copy,hashlib,json,time
from contextlib import ExitStack
from pathlib import Path
from protocol import Failure,atomic_json,digest,read_json,CODES
from compositing_batch_contract import normalize_params,input_documents,descriptor,fail
from compositing_workflow_contract import input_documents as single_inputs
from compositing_m5_guards import scope

def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
def original(params):return {k:v for k,v in params.items() if k!='resume'}
def reference(path):return {'file':str(Path(path).resolve()),'expected_sha256':digest(path)}
def error_row(error):return {'code':getattr(error,'code','WORKER_FAILED'),'message':str(error),'details':getattr(error,'details',None)}
def guard(row,stack):return scope(stack).add(descriptor(row))
def recheck(stack):return scope(stack).recheck()
def default_runtime():
    from compositing_verifier import runtime_identity
    return runtime_identity()
def default_runner(params,job,*,cancel_parent):
    from compositing_workflow import run
    return run(params,job,cancel_parent=cancel_parent)
def cancel(job,parent=None):
    if any((Path(p)/'cancel.request').exists() for p in (job,parent) if p):fail('Owned M5 cancelled','CANCELLED')

def child_artifacts(child):
    found={};nodes=0
    def walk(value,ancestors=(),depth=0):
        nonlocal nodes
        nodes+=1
        if nodes>2000000 or depth>128:fail('Child JSON closure exceeds bounded traversal','UNSUPPORTED')
        if isinstance(value,(dict,list)):
            if id(value) in ancestors:fail('Child JSON closure is cyclic','CONFLICT')
            ancestors=(*ancestors,id(value))
        if isinstance(value,dict):
            if isinstance(value.get('file'),str) and isinstance(value.get('sha256',value.get('expected_sha256')),str):
                row=descriptor({'file':value['file'],'expected_sha256':value.get('sha256',value.get('expected_sha256'))});key=row['file'].casefold()
                if key in found and found[key]['expected_sha256']!=row['expected_sha256']:fail('Child closure has conflicting SHA','CONFLICT')
                found[key]=row
                if len(found)>8192:fail('Child closure exceeds8192 descriptors','UNSUPPORTED')
            for value in value.values():walk(value,ancestors,depth+1)
        elif isinstance(value,list):
            for value in value:walk(value,ancestors,depth+1)
    walk(child);return list(found.values())

def validate_child(child,params,runtime,*,allow_mock=False):
    if child.get('evidence_kind')=='mock_only' and not allow_mock:fail('Mock cannot certify real producer','VALIDATION_FAILED')
    if child.get('compositing_report_version')!='1.0' or child.get('status')!='passed' or child.get('runtime')!=runtime:fail('Child complete status/runtime required','VALIDATION_FAILED')
    if child.get('implementation_sha256')!=params['implementation_sha256'] or fingerprint(original(params))!=fingerprint(child.get('original_request',{})):fail('Child recipe/implementation differs','CONFLICT')
    checks=child.get('checks',{})
    if child.get('verification',{}).get('status')!='passed' or child.get('output_verification',{}).get('status')!='passed' or child.get('protection',{}).get('status')!='pass' or any(checks.get(k)!='passed' for k in ('candidate_bytes_unchanged','fresh_saved_graph_protection','independent_png_exr_decode')):fail('Child requires complete fresh/outputs/protection evidence','VALIDATION_FAILED')
    if not child.get('candidate') or not child.get('outputs') or not child.get('source'):fail('Child lacks candidate/source/outputs','VALIDATION_FAILED')
    if child['source'].get('file')!=params['file'] or child['source'].get('sha256')!=params['expected_sha256']:fail('Child source differs from explicit caller','CONFLICT')

_AUTHORITY_TOKEN=object()
class _ParentAuthority:
    __slots__=('root','guards','command','identity','runtime','request_fingerprint','_token')
    def __init__(self,token,root,guards,command,identity,runtime,request_fingerprint):
        if token is not _AUTHORITY_TOKEN:fail('Parent authority must be minted by protected Host preflight','CONFLICT')
        self.root=root;self.guards=guards;self.command=command;self.identity=identity;self.runtime=runtime;self.request_fingerprint=request_fingerprint;self._token=token

def verify_parent_attempt(root,prior,stack,*,expected_command,expected_request,expected_identity,expected_runtime,allow_mock=False):
    root=Path(root).resolve();guards=scope(stack)
    if prior.get('evidence_kind')=='mock_only' and not allow_mock:fail('Mock parent cannot be real resume proof','CONFLICT')
    allowed={'running','passed','partial','failed','cancelled','timed_out'}|({'mock_only'} if allow_mock else set())
    if prior.get('status') not in allowed or prior.get('implementation_sha256')!=expected_identity or prior.get('runtime')!=expected_runtime:fail('Parent status/implementation/runtime differs','CONFLICT')
    if 'attempt_directory' in prior and Path(prior['attempt_directory']).resolve()!=root:fail('Parent report owning attempt mismatch','CONFLICT')
    status,_=guards.capture_json(root/'status.json');request,_=guards.capture_json(root/'request.json')
    result,_=guards.capture_json(root/'result.json')
    report_names={'compositing.batch':'batch-report.json','compositing.study':'study-report.json','compositing.template-run':'template-run-report.json'}
    if expected_command not in report_names:fail('Unknown parent domain report command','CONFLICT')
    pinned,_=guards.capture_json(root/report_names[expected_command])
    if any(type(value) is not dict for value in (status,request,result,pinned)) or not {'ok','error','data'}<=set(result):fail('Parent terminal documents/envelope shape differs','CONFLICT')
    if fingerprint(pinned)!=fingerprint(prior):fail('Parent proof differs from pinned domain report','CONFLICT')
    states={'succeeded':0,'failed':None,'cancelled':7,'timed_out':6}
    state=status.get('state')
    if state not in states or status.get('job_id')!=root.name or type(status.get('exit_code')) is not int or type(status.get('finished_at')) not in (int,float) or status['finished_at']<=0:fail('Parent supervisor not terminal or job identity differs','CONFLICT')
    if state!='failed' and status.get('exit_code')!=states[state] or state=='failed' and status.get('exit_code') in (None,0):fail('Parent supervisor state/exit code differs','CONFLICT')
    if request.get('schema_version')!='1.0' or request.get('command')!=expected_command or type(request.get('params')) is not dict:fail('Parent request command/shape differs','CONFLICT')
    wanted=fingerprint(original(expected_request));actual=fingerprint(original(request['params']))
    if actual!=wanted or request['params'].get('implementation_sha256')!=expected_identity:fail('Parent request fingerprint/implementation differs','CONFLICT')
    if prior.get('request_fingerprint',wanted)!=wanted:fail('Parent report/request fingerprints differ','CONFLICT')
    if prior.get('request') is not None and fingerprint(original(prior['request']))!=wanted:fail('Parent normalized report request differs','CONFLICT')
    if state=='succeeded' and prior.get('status') not in ({'passed','partial'}|({'mock_only'} if allow_mock else set())):fail('Succeeded supervisor lacks a completed domain report','CONFLICT')
    if result.get('schema_version')!='1.0' or result.get('command')!=expected_command or result.get('job_id')!=root.name or type(result.get('ok')) is not bool:fail('Parent result envelope identity/shape differs','CONFLICT')
    if state=='succeeded':
        if not result['ok'] or result.get('error') is not None or type(result.get('data')) is not dict:fail('Successful parent result requires ok/data and no error','CONFLICT')
    else:
        error=result.get('error')
        if result['ok'] or type(error) is not dict or type(error.get('message')) is not str or error.get('code') not in CODES or CODES[error['code']]!=status['exit_code']:fail('Parent result error/exit code differs','CONFLICT')
        wanted_error={'cancelled':'CANCELLED','timed_out':'TIMEOUT'}.get(state)
        if wanted_error and error['code']!=wanted_error or state=='failed' and error['code'] in ('CANCELLED','TIMEOUT'):fail('Parent result error/terminal state differs','CONFLICT')
        if result.get('data') is not None and type(result['data']) is not dict:fail('Parent result data shape differs','CONFLICT')
    if result.get('data') is not None:
        data=copy.deepcopy(result['data'])
        # Only these three fields are added by the supervisor after domain receipt.
        # Validate their values before comparing every remaining domain field.
        if 'source_sha256' in data or 'source_unchanged' in data:
            if data.get('source_sha256')!=request['params'].get('expected_sha256') or data.get('source_unchanged') is not True or not request['params'].get('file'):fail('Parent supervisor source proof differs','CONFLICT')
        if 'input_hashes' in data:
            hashes=data['input_hashes']
            docs=single_inputs(request['params'],command=expected_command)
            docs=[*docs,*[request['params'][key] for key in ('resume','template','producer') if key in request['params']]]
            if request['params'].get('file'):docs=[*docs,{'file':request['params']['file'],'expected_sha256':request['params']['expected_sha256']}]
            expected={descriptor(doc)['file'].casefold():descriptor(doc)['expected_sha256'] for doc in docs}
            if type(hashes) is not dict or any(type(path) is not str or not Path(path).is_absolute() or expected.get(str(Path(path).resolve()).casefold())!=sha for path,sha in hashes.items()):fail('Parent supervisor input hash proof differs','CONFLICT')
        for key in ('source_sha256','source_unchanged','input_hashes'):
            if key not in prior:data.pop(key,None)
        if fingerprint(data)!=fingerprint(prior):fail('Parent returned data differs from pinned domain report','CONFLICT')
    guards.recheck()
    return _ParentAuthority(_AUTHORITY_TOKEN,root,guards,expected_command,expected_identity,expected_runtime,wanted)

def check_resume_parent(root,prior,*,_authority=None,stack=None,expected_command=None,expected_request=None,expected_runtime=None,allow_mock=False):
    root=Path(root).resolve()
    if _authority is not None:
        if not isinstance(_authority,_ParentAuthority) or _authority._token is not _AUTHORITY_TOKEN or _authority.command!='compositing.study' or root!=_authority.root/'batch':fail('Nested resume requires exact protected study parent proof','CONFLICT')
        if prior.get('implementation_sha256')!=_authority.identity or prior.get('runtime')!=_authority.runtime:fail('Nested batch parent identity/runtime differs','CONFLICT')
        _authority.guards.recheck();return _authority
    if stack is None or expected_request is None or expected_runtime is None:fail('Resume requires protected request/status/runtime identity','CONFLICT')
    return verify_parent_attempt(root,prior,stack,expected_command=expected_command or prior.get('operation'),expected_request=expected_request,expected_identity=expected_request['implementation_sha256'],expected_runtime=expected_runtime,allow_mock=allow_mock)

def load_resume(params,report,stack,*,_authority=None,allow_mock=False):
    if 'resume' not in params:return None
    guards=scope(stack);prior=guards.read(params['resume']);root=Path(params['resume']['file']).resolve().parent
    if prior.get('operation')!='compositing.batch' or prior.get('report_version')!='1.0':fail('Not a batch report','CONFLICT')
    if prior.get('request_fingerprint')!=report['request_fingerprint'] or prior.get('implementation_sha256')!=report['implementation_sha256'] or prior.get('runtime')!=report['runtime']:fail('Resume request/implementation/runtime changed','CONFLICT')
    if Path(prior.get('attempt_directory','')).resolve()!=root:fail('Resume report owning attempt differs','CONFLICT')
    check_resume_parent(root,prior,_authority=_authority,stack=stack,expected_command='compositing.batch',expected_request=original(params),expected_runtime=report['runtime'],allow_mock=allow_mock)
    old={r['id']:r for r in prior.get('items',[])}
    if len(old)!=len(prior.get('items',[])) or not set(old)<=set(i['id'] for i in params['manifest']['items']):fail('Resume member identity differs','CONFLICT')
    report['ancestor_roots']=list(dict.fromkeys([str(root),*prior.get('ancestor_roots',[])]));report['resume'].update(loaded=True,report=params['resume'])
    return old

def run(params,job,*,single_runner=None,runtime=None,cancel_parent=None,observer=None,_resume_authority=None):
    params=normalize_params(params);job=Path(job).resolve();job.mkdir(parents=True,exist_ok=True)
    if (job/'batch-report.json').exists():fail('New attempt requires a new directory','CONFLICT')
    mock=single_runner is not None or runtime is not None;runner=single_runner or default_runner;runtime=runtime or default_runtime();start=time.monotonic()
    report={'report_version':'1.0','operation':'compositing.batch','status':'running','attempt_directory':str(job),'request':original(params),'request_fingerprint':fingerprint(original(params)),'implementation_sha256':params['implementation_sha256'],'runtime':runtime,'evidence_kind':'mock_only' if mock else 'normal_host','certified_producer':False,'items':[],'ancestor_roots':[],'resume':{'requested':'resume' in params,'loaded':False,'reused':0,'rerun':0},'progress':{},'scope':'Single serial worker; partial is not all passed; mock callback/runtime can never certify public producer'}
    def publish(stage,current=None):
        report['progress']={'stage':stage,'current_item':current,'planned':len(params['manifest']['items']),'completed':len(report['items']),'wall_seconds':time.monotonic()-start};atomic_json(job/'batch-report.json',report)
        if observer:observer(copy.deepcopy(report))
    lifecycle=ExitStack();parents=lifecycle.enter_context(ExitStack());accepted=[]
    try:
        publish('parent_preflight')
        for row in input_documents(params):guard(row,parents)
        if not mock:
            import bpy
            guard({'file':bpy.app.binary_path,'expected_sha256':runtime['binary_sha256']},parents)
        prior=load_resume(params,report,parents,_authority=_resume_authority,allow_mock=mock) or {};(job/'items').mkdir(exist_ok=False)
        for item in params['manifest']['items']:
            cancel(job,cancel_parent);key=item['id'];p=item['params'];directory=job/'items'/key;directory.mkdir(exist_ok=False);publish('member_preflight',key)
            row={'id':key,'status':'failed','reused':False,'params_fingerprint':fingerprint(original(p)),'new_attempt_directory':str(directory)}
            members=ExitStack();retained=False
            try:
                for doc in [{'file':p['file'],'expected_sha256':p['expected_sha256']},*single_inputs(p),*([item['template']] if 'template' in item else [])]:guard(doc,members)
                old=prior.get(key)
                if old and old.get('status')=='passed':
                    if old.get('params_fingerprint')!=row['params_fingerprint']:fail('Resume member recipe changed','CONFLICT')
                    doc=old['report'];path=Path(doc['file']).resolve()
                    if path.name!='compositing-report.json' or not any(path.is_relative_to(Path(a)/'items'/key) for a in report['ancestor_roots']):fail('Resume child outside owned member ancestry','CONFLICT')
                    child=scope(members).read(doc)
                else:
                    atomic_json(directory/'request-params.json',p);publish('member_run',key)
                    child=runner(copy.deepcopy(p),directory,cancel_parent=cancel_parent or job)
                    childpath=directory/'compositing-report.json';doc=scope(members).capture(childpath)
                    if read_json(childpath)!=child:fail('Returned and persisted child reports differ','CONFLICT')
                validate_child(child,p,runtime,allow_mock=mock)
                for artifact in child_artifacts(child):guard(artifact,members)
                row['guard_validation']=recheck(members);cancel(job,cancel_parent)
                row.update(status='passed',reused=bool(old and old.get('status')=='passed'),report=doc,candidate=child['candidate'],outputs=child['outputs'],baselines=child.get('baselines',[]))
                if row['reused']:
                    atomic_json(directory/'reuse-reference.json',{'prior_report':doc,'ancestor_dependency':str(Path(doc['file']).parent),'no_original_bytes_modified':True});report['resume']['reused']+=1
                else:report['resume']['rerun']+=1
                lifecycle.callback(members.close);accepted.append((row,members));retained=True
            except Exception as error:
                row.update(status='failed',error=error_row(error));atomic_json(directory/'member-failure.json',row)
                if row['error']['code'] in ('CANCELLED','TIMEOUT'):report['items'].append(row);raise
            finally:
                if not retained:members.close()
            report['items'].append(row);publish('member_complete',key)
        report['parent_guard_validation']=recheck(parents)
        from compositing_identity import implementation_sha256
        if implementation_sha256()!=params['implementation_sha256']:fail('Implementation changed before batch acceptance','CONFLICT')
        if not mock and default_runtime()!=runtime:fail('Runtime changed before batch acceptance','CONFLICT')
        for row,members in accepted:
            try:row['guard_validation']=recheck(members)
            except Exception as error:row.update(status='failed',error=error_row(error));members.close()
        report['resume']['reused']=sum(row['status']=='passed' and row['reused'] for row in report['items'])
        report['resume']['rerun']=sum(row['status']=='passed' and not row['reused'] for row in report['items'])
        cancel(job,cancel_parent);passed=sum(row['status']=='passed' for row in report['items'])
        status='passed' if passed==len(params['manifest']['items']) else 'partial' if passed else 'failed'
        report.update(status='mock_only' if mock else status,domain_status_observed=status,certified_producer=not mock and status in ('passed','partial'))
    except Exception as error:
        report.update(status={'CANCELLED':'cancelled','TIMEOUT':'timed_out'}.get(getattr(error,'code',None),'failed'),error=error_row(error),certified_producer=False)
    finally:
        try:
            report['metrics']={'wall_seconds':time.monotonic()-start,'passed':sum(r['status']=='passed' for r in report['items']),'failed':sum(r['status']=='failed' for r in report['items']),'planned':len(params['manifest']['items'])};publish(report['status'])
        finally:lifecycle.close()
    if report['status'] not in ('passed','partial','mock_only'):
        error=report.get('error',{'code':'VALIDATION_FAILED','message':'No member passed'});fail(error['message'],error['code'],error.get('details'))
    return report
