"""Candidate compact projection: supervisor lifecycle and domain success stay distinct."""
from pathlib import Path
from protocol import read_json,digest

REPORTS={'compositing.batch':'batch-report.json','compositing.study':'study-report.json',
         'compositing.template-save':'template-report.json','compositing.template-run':'template-run-report.json'}

def descriptor(path):return {'file':str(path),'sha256':digest(path)}

def status_projection(domain,state,error):
    code=(error or {}).get('code')
    if state in ('cancelled','timed_out','failed'):return state
    if code:return {'CANCELLED':'cancelled','TIMEOUT':'timed_out'}.get(code,'failed')
    if state in ('queued','running'):return {'queued':'pending','running':'running'}[state]
    # A succeeded worker is transport completion, never proof every item passed.
    if domain:return domain
    return 'not_observed' if state=='succeeded' else 'pending'

def compact(result,root,command):
    path=root/REPORTS[command] if root else None
    report=read_json(path) if path and path.is_file() else {}
    statusfile=root/'status.json' if root else None
    authority=read_json(statusfile) if statusfile and statusfile.is_file() else {}
    job_data=result.get('data') or {}
    state=authority.get('state') or (job_data.get('state') if result.get('command')!=command else None)
    output={k:result[k] for k in ('schema_version','cli_version','command','job_id','ok','error') if k in result}
    data={k:report[k] for k in ('operation','implementation_sha256','request_fingerprint','template','producer',
          'producer_validation','normal_run_report','normal_run_signature','recovery','resume','ancestor_roots',
          'conditions','comparison','decision','metrics','progress','error','evidence_kind','certified_producer','domain_status_observed',
          'guard_validation','parent_guard_validation','closure_guard_validation') if k in report}
    domain=report.get('status');projected=status_projection(domain,state,result.get('error'));data.update(status=projected,
           domain_status=domain or 'not_observed',supervisor_state=state or 'not_observed',
           transport_ok=bool(result.get('ok')),domain_all_passed=projected=='passed' and domain=='passed' and (report.get('certified_producer') is True or command=='compositing.template-run' and report.get('producer_validation')=='passed'),
           scope='Bounded projection only; transport ok/succeeded does not convert partial into all-passed')
    members=report.get('items',report.get('cases',[]))
    nested=None
    if command=='compositing.study' and not members and isinstance(report.get('batch_report'),dict):
        anchor=report['batch_report'];nested_path=Path(anchor['file'])
        # Only read the same owned attempt, with the stored checkpoint SHA.
        if root and nested_path.resolve().is_relative_to(root.resolve()) and nested_path.is_file():
            if digest(nested_path)==anchor.get('expected_sha256',anchor.get('sha256')):
                nested=read_json(nested_path);members=nested.get('items',[])
                data['nested_batch_status']=nested.get('status','not_observed')
    request=read_json(root/'request.json') if root and (root/'request.json').is_file() else {}
    params=request.get('params',{})
    planned=[row['id'] for row in params.get('manifest',{}).get('items',[])] if command=='compositing.batch' else ['baseline',*[row['id'] for row in params.get('cases',[])]] if command=='compositing.study' else []
    completed={row['id'] for row in members};passed=sum(row.get('status')=='passed' for row in members)
    missing=[key for key in planned if key not in completed]
    data['counts']={'planned':len(planned),'completed':len(members),'passed':passed,
                    'failed':sum(row.get('status')=='failed' for row in members),'reused':sum(bool(row.get('reused')) for row in members),
                    'not_started':len(missing)}
    data['not_started_ids']=missing[:16]
    data['items']=[{k:row[k] for k in ('id','status','reused','error','report','candidate','params_fingerprint') if k in row} for row in members[:16]]
    if report.get('resolved_run_params'):data['context']={k:report['resolved_run_params'].get('manifest',{}).get(k) for k in ('scene','view_layer','workflow_id','reference_frame')}
    output.update(output_format='compact',data=data,artifacts={'job_directory':str(root)} if root else {})
    if root:
        for name in ('request.json','result.json','status.json',REPORTS[command],'compositing-report.json'):
            item=root/name
            if item.is_file():output['artifacts'][name]=descriptor(item)
    return output
