"""Single public template consumer candidate; existing Host is the only producer."""
import copy
import hashlib
import json
import os
from pathlib import Path
from contextlib import ExitStack
from protocol import Failure,atomic_json,read_json,digest
from filesystem import FileGuard
from compositing_template_run_contract import (normalize_host_params,descriptor,anchor_documents,input_documents,
    validate_template_document,fail)

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()

def original(params):return {key:value for key,value in params.items() if key!='resume'}

def _guard_rows(rows,stack,checked,parents):
    for row in rows:
        row=descriptor(row);path=Path(row['file']);key=os.path.normcase(str(path))
        if key in checked:
            if checked[key]['descriptor']!=row:fail('CONFLICT','Template-run input SHA differs for the same path')
            continue
        for parent in reversed(path.parents):
            name=os.path.normcase(str(parent))
            if name not in parents:
                guarded=stack.enter_context(FileGuard(parent,directory=True));parents[name]=(guarded,guarded.identity())
        guarded=stack.enter_context(FileGuard(path));actual=guarded.sha256()
        if actual!=row['expected_sha256']:fail('CONFLICT','Template-run guarded input SHA differs',row)
        checked[key]={'descriptor':row,'guard':guarded,'identity':guarded.identity()}

def _recheck(checked,parents):
    for guarded,identity in parents.values():
        if not guarded.handle or guarded.identity()!=identity:fail('CONFLICT','Template-run ancestor identity changed')
        with FileGuard(guarded.path,directory=True) as opened:
            if opened.identity()!=identity:fail('CONFLICT','Template-run ancestor path identity changed')
    for row in checked.values():
        guarded=row['guard'];descriptor=row['descriptor']
        if not guarded.handle or guarded.identity()!=row['identity'] or guarded.sha256()!=descriptor['expected_sha256']:
            fail('CONFLICT','Template-run live input identity/SHA changed')
        with FileGuard(descriptor['file']) as reopened:
            if reopened.identity()!=row['identity']:fail('CONFLICT','Template-run input path identity changed')

def _resolve_params(params,stack,checked,parents,runtime=None):
    """Resolve outer authority to an exact native checkpoint in the caller scope."""
    from compositing_templates import instantiate_from_file
    from compositing_workflow_contract import normalize_params as normalize_single,input_documents as single_inputs
    params=normalize_host_params(params)
    _guard_rows(anchor_documents(params),stack,checked,parents)
    _guard_rows(input_documents(params),stack,checked,parents)
    prior=None;native_resume=None
    if 'resume' in params:
        from compositing_batch import verify_parent_attempt,default_runtime
        prior=read_json(params['resume']['file']);root=Path(params['resume']['file']).resolve().parent
        verify_parent_attempt(root,prior,stack,expected_command='compositing.template-run',expected_request=original(params),expected_identity=params['implementation_sha256'],expected_runtime=runtime if runtime is not None else default_runtime())
        try:native_resume=descriptor(prior['normal_run_report'])
        except (KeyError,Failure) as exc:fail('CONFLICT','Template-run resume lacks an explicit native report descriptor',{'reason':str(exc)})
        if Path(native_resume['file'])!=root/'compositing-report.json':fail('CONFLICT','Template-run native resume report is not the exact owned path')
        _guard_rows([native_resume],stack,checked,parents)
        native=read_json(native_resume['file'])
        if type(native) is not dict or native.get('compositing_report_version')!='1.0':fail('CONFLICT','Template-run native resume report type differs')
    validate_template_document(read_json(params['template']['file']))
    instance=instantiate_from_file(params['template'],file=params['file'],expected_sha256=params['expected_sha256'],
        context=params['context'],source=params['source'],bindings=params['bindings'],
        resources=params['resources'],preview=params['preview'])
    if instance['template']!=params['template']:fail('CONFLICT','Instantiator returned a different template identity')
    inner=copy.deepcopy(instance['params']);inner['implementation_sha256']=params['implementation_sha256']
    if native_resume is not None:inner['resume']=native_resume
    inner=normalize_single(inner)
    signature=fingerprint(original(inner))
    if prior is not None and prior.get('normal_run_signature')!=signature:fail('CONFLICT','Template-run resolved native signature differs from the outer checkpoint')
    template_rows=[r for r in inner['resources'] if Path(r['file']).resolve()==Path(params['template']['file']).resolve()]
    if template_rows!=[params['template']]:fail('CONFLICT','Normal run lost exact template resource/SHA')
    _guard_rows(single_inputs(inner),stack,checked,parents)
    _recheck(checked,parents)
    from compositing_m5_guards import scope
    scope(stack).recheck()
    return {'params':inner,'template':copy.deepcopy(params['template']),
        'signature':signature,'outer_fingerprint':fingerprint(original(params))}

def resolve_params(params):
    """Standalone protected resolution; run() retains this scope until publishing."""
    with ExitStack() as stack:
        return _resolve_params(params,stack,{}, {})

def _default_runner(params,job,*,cancel_parent):
    from compositing_workflow import run
    return run(params,job,cancel_parent=cancel_parent)

def _cancel(job,parent):
    if any((Path(p)/'cancel.request').exists() for p in (job,parent) if p):fail('CANCELLED','Owned template-run cancelled')

def _validated_producer(child,inner,job,stack,checked,parents,runtime):
    from compositing_batch import validate_child,child_artifacts
    validate_child(child,inner,runtime)
    path=Path(job)/'compositing-report.json'
    if not path.is_file():fail('VALIDATION_FAILED','Normal Host did not persist its complete report')
    row={'file':str(path.resolve()),'expected_sha256':digest(path)}
    _guard_rows([row],stack,checked,parents)
    if read_json(path)!=child:fail('CONFLICT','Normal Host returned and persisted producer reports differ')
    _guard_rows(child_artifacts(child),stack,checked,parents)
    return row

def run(params,job,*,cancel_parent=None,_seam_runner=None):
    """Same-job normal Host execution, no submit/scheduler or child supervisor.

    _seam_runner is test-only and can NEVER produce a passed consumer receipt.
    The public JSON contract has no callback/runtime/validation bypass fields.
    """
    params=normalize_host_params(params);job=Path(job).resolve();job.mkdir(parents=True,exist_ok=True)
    receipt_path=job/'template-run-report.json'
    if receipt_path.exists():fail('CONFLICT','Template-run needs a fresh attempt directory')
    report={'report_version':'1.0','operation':'compositing.template-run','status':'running',
        'attempt_directory':str(job),'template':params['template'],'request':original(params),
        'request_fingerprint':fingerprint(original(params)),'implementation_sha256':params['implementation_sha256'],
        'producer_validation':'not_run','normal_run_report':None,
        'scope':'Explicit template descriptor consumed into unchanged single compositing Host run; no new scheduler',
        'seam_mock':_seam_runner is not None}
    atomic_json(receipt_path,report)
    stack=ExitStack()
    try:
        from compositing_verifier import runtime_identity
        from compositing_identity import implementation_sha256
        import bpy
        runtime=copy.deepcopy(runtime_identity());report['runtime']=runtime
        atomic_json(receipt_path,report)
        checked={};parents={};_cancel(job,cancel_parent)
        _guard_rows([{'file':bpy.app.binary_path,'expected_sha256':runtime['binary_sha256']}],stack,checked,parents)
        _guard_rows(anchor_documents(params),stack,checked,parents)
        _guard_rows(input_documents(params),stack,checked,parents)
        resolved=_resolve_params(params,stack,checked,parents,runtime);inner=resolved['params']
        report.update(resolved_run_params=inner,normal_run_signature=resolved['signature'],stage='normal_host_run')
        atomic_json(receipt_path,report);_cancel(job,cancel_parent)
        child=(_seam_runner or _default_runner)(copy.deepcopy(inner),job,cancel_parent=cancel_parent)
        if _seam_runner is not None:fail('VALIDATION_FAILED','Seam mock cannot certify a passed producer or consumer')
        row=_validated_producer(child,inner,job,stack,checked,parents,runtime)
        if runtime_identity()!=runtime or implementation_sha256()!=params['implementation_sha256']:fail('CONFLICT','Runtime/implementation changed before template acceptance')
        _recheck(checked,parents)
        from compositing_m5_guards import scope
        scope(stack).recheck();_cancel(job,cancel_parent)
        report.update(status='passed',stage='complete',normal_run_report=row,producer_validation='passed',
            candidate=copy.deepcopy(child['candidate']),outputs=copy.deepcopy(child['outputs']),
            recovery=copy.deepcopy(child.get('recovery',{})))
    except BaseException as exc:
        report.update(status={'CANCELLED':'cancelled','TIMEOUT':'timed_out'}.get(getattr(exc,'code',None),'failed'),
            error={'code':getattr(exc,'code','WORKER_FAILED'),'message':str(exc),'details':getattr(exc,'details',None)})
        child_path=job/'compositing-report.json'
        if child_path.is_file():report['normal_run_report']={'file':str(child_path),'expected_sha256':digest(child_path)}
        raise
    finally:
        try:atomic_json(receipt_path,report)
        finally:stack.close()
    return report
