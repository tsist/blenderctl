"""Bounded sequential lighting batches with per-item checkpoints and explicit partial failure."""
import copy,hashlib,json,time
from pathlib import Path
import bpy
from protocol import Failure,atomic_json,digest,read_json
from lighting_workflow_contract import normalize_batch_params,pending_report_descriptor,_read_descriptor
import lighting_workflow as single

def _plan(params):
    body={k:params[k] for k in ('version','items','implementation_sha256')}
    return hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def run(params,job):
    params=normalize_batch_params(params);job=Path(job);started=time.monotonic()
    runtime={'version':bpy.app.version_string,'build':bpy.app.build_hash.decode(),'binary_sha256':digest(bpy.app.binary_path)}
    report={'lighting_batch_report_version':'1.0','operation':'lighting.batch','status':'running',
        'plan_sha256':_plan(params),'implementation_sha256':params['implementation_sha256'],'runtime':runtime,
        'items':[],'previews':[],'checks':{'visual_quality':'not_received'}}
    def progress(stage,**extra):
        atomic_json(job/'lighting-report.json',report)
        atomic_json(job/'lighting-progress.json',{'stage':stage,'status':report['status'],'seconds':time.monotonic()-started,**extra})
        if (job/'cancel.request').exists():raise Failure('CANCELLED','Lighting batch cancelled at item checkpoint')
    prior={}
    try:
        if 'resume' in params:
            d=params['resume']
            if digest(d['file'])!=d['expected_sha256']:raise Failure('CONFLICT','Batch resume report changed')
            old=read_json(d['file'])
            if old.get('operation')!='lighting.batch' or old.get('lighting_batch_report_version')!='1.0':raise Failure('INVALID_REQUEST','Not a lighting batch report')
            if any(old.get(k)!=report[k] for k in ('plan_sha256','implementation_sha256','runtime')):raise Failure('CONFLICT','Batch plan, implementation or runtime changed')
            prior={r['id']:r for r in old['items']};report['resumed_from']=d
        progress('begin',total=len(params['items']))
        for item in params['items']:
            progress('item',id=item['id'],completed=len(report['items']),total=len(params['items']))
            directory=job/'items'/item['id'];directory.mkdir(parents=True,exist_ok=False)
            row={'id':item['id'],'status':'running','reused':False,
                'pending_report_path':str(directory/'lighting-report.json')};report['items'].append(row)
            progress('item_started',id=item['id'],completed=len(report['items'])-1,total=len(params['items']))
            try:
                previous=prior.get(item['id']);old_result=None;resume_desc=None
                if previous and previous.get('report'):
                    desc=previous['report']
                    resume_desc={'file':desc['file'],'expected_sha256':desc['sha256']};old_result=_read_descriptor(resume_desc)
                elif previous and previous.get('pending_report_path'):
                    resume_desc=pending_report_descriptor(params['resume'],previous);old_result=_read_descriptor(resume_desc)
                if previous and previous.get('status')=='passed':
                    row['reuse_verification']=single.verify_saved(item['params'],old_result,directory)
                    row.update(status='passed',reused=True,report=previous['report'],candidate=old_result['candidate'],verification=old_result['verification'])
                    report['previews'].extend(old_result.get('previews',[]))
                else:
                    request=copy.deepcopy(item['params'])
                    if old_result and old_result.get('candidate'):
                        request['resume']=resume_desc
                    result=single.run(request,directory,cancel_parent=job)
                    row.update(status='passed',candidate=result['candidate'],report=single.descriptor(directory/'lighting-report.json'),verification=result['verification'],recovery=result['recovery'])
                    report['previews'].extend(result['previews'])
            except Exception as e:
                row.update(status='failed',error={'code':getattr(e,'code','WORKER_FAILED'),'message':str(e)})
                if (directory/'lighting-report.json').is_file():
                    row['report']=single.descriptor(directory/'lighting-report.json')
                    child=read_json(directory/'lighting-report.json')
                    for key in ('candidate','recovery','verification'):
                        if key in child:row[key]=child[key]
                if getattr(e,'code',None)=='CANCELLED':raise
            progress('item_finished',id=item['id'],completed=len(report['items']),total=len(params['items']))
        passed=sum(r['status']=='passed' for r in report['items']);failed=len(report['items'])-passed
        report['status']='passed' if not failed else 'partial' if passed else 'failed'
        report['metrics']={'wall_seconds':time.monotonic()-started,'passed':passed,'failed':failed,'preview_count':len(report['previews'])}
        progress('complete',completed=len(report['items']),total=len(params['items']))
        if failed:raise Failure('VALIDATION_FAILED','Lighting batch has failed items; independent successes and checkpoints are preserved')
        return report
    except Exception as e:
        if report['status']=='running':report['status']='cancelled' if getattr(e,'code',None)=='CANCELLED' else 'failed'
        report['error']={'code':getattr(e,'code','WORKER_FAILED'),'message':str(e)}
        atomic_json(job/'lighting-report.json',report)
        atomic_json(job/'lighting-progress.json',{'stage':'stopped','status':report['status'],'completed':len(report['items'])})
        raise
