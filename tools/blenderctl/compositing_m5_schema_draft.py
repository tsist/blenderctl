"""Audit schema generator draft; imports pure contracts only, never Blender/Host."""
import argparse,copy,json
from pathlib import Path

SHA={'type':'string','pattern':'^[0-9a-f]{64}$'}
FILE={'type':'object','properties':{'file':{'type':'string'},'sha256':SHA,'expected_sha256':SHA},'required':['file'],'anyOf':[{'required':['sha256']},{'required':['expected_sha256']}],'additionalProperties':False}
STATUSES=['running','passed','partial','failed','cancelled','timed_out','mock_only']
def obj(properties,required=(),extra=False):return {'type':'object','properties':properties,'required':list(required),'additionalProperties':extra}
ERROR=obj({'code':{'type':'string'},'message':{'type':'string'},'details':{}},('code','message'))
GUARDS=obj({**{name:{'type':'integer','minimum':0} for name in ('initial_full_sha','final_full_sha','file_path_identity_checks','ancestor_path_identity_checks','files','ancestors')},'status':{'const':'passed'}},('status','files','ancestors','final_full_sha','file_path_identity_checks','ancestor_path_identity_checks'))
CASE=obj({'id':{'type':'string'},'status':{'enum':['passed','failed']},'reused':{'type':'boolean'},'params_fingerprint':SHA,
          'new_attempt_directory':{'type':'string'},'report':FILE,'candidate':FILE,'outputs':{'type':'array','items':{'type':'object'}},'baselines':{'type':'array','items':{'type':'object'}},'guard_validation':GUARDS,'changes':{'type':'array'},'error':ERROR},('id','status','reused','params_fingerprint','new_attempt_directory'))
CASE['allOf']=[{'if':{'properties':{'status':{'const':'passed'}},'required':['status']},'then':{'required':['report','candidate','outputs','guard_validation']}},
 {'if':{'properties':{'status':{'const':'failed'}},'required':['status']},'then':{'required':['error']}}]

def report_schema(operation):
    properties={'report_version':{'const':'1.0'},'operation':{'const':operation},'status':{'enum':STATUSES},'attempt_directory':{'type':'string'},
      'request':{'type':'object'},'request_fingerprint':SHA,'implementation_sha256':SHA,'runtime':{'type':'object'},'evidence_kind':{'enum':['normal_host','mock_only']},'certified_producer':{'type':'boolean'},
      'scope':{'type':'string'},'error':ERROR,'domain_status_observed':{'enum':['passed','partial','failed']},'guard_validation':GUARDS,'parent_guard_validation':GUARDS,'closure_guard_validation':GUARDS}
    if operation=='compositing.batch':properties.update(items={'type':'array','maxItems':16,'items':CASE},ancestor_roots={'type':'array','items':{'type':'string'}},resume={'type':'object'},progress={'type':'object'},metrics={'type':'object'})
    elif operation=='compositing.study':properties.update(cases={'type':'array','maxItems':9,'items':CASE},batch_report=FILE,decision={'type':'object'},comparison={'type':'object'},conditions={'type':'object'})
    elif operation=='compositing.template-save':properties.update(template=FILE,producer=FILE,source_saved={'const':False})
    else:properties.update(template=FILE,producer_validation={'enum':['not_run','passed']},normal_run_report={'oneOf':[FILE,{'type':'null'}]},normal_run_signature=SHA,resolved_run_params={'type':'object'},stage={'type':'string'},candidate=FILE,outputs={'type':'array','items':{'type':'object'}},recovery={'type':'object'},seam_mock={'type':'boolean'})
    required=['report_version','operation','status','attempt_directory']
    if operation!='compositing.template-run':required+=['evidence_kind','certified_producer']
    result=obj(properties,required)
    result['allOf']=[
      {'if':{'properties':{'status':{'enum':['failed','cancelled','timed_out']}},'required':['status']},'then':{'required':['error']}},
      {'if':{'properties':{'status':{'const':'mock_only'}},'required':['status']},'then':{'properties':{'evidence_kind':{'const':'mock_only'},'certified_producer':{'const':False}},'required':['evidence_kind','certified_producer']}},
      {'if':{'properties':{'status':{'enum':['passed','partial']}},'required':['status']},'then':{'required':['implementation_sha256','runtime','request_fingerprint']}}]
    if operation!='compositing.template-run':
      result['allOf'].append({'if':{'properties':{'status':{'enum':['passed','partial']}},'required':['status']},'then':{'properties':{'evidence_kind':{'const':'normal_host'},'certified_producer':{'const':True}}}})
    if operation=='compositing.template-save':passed=['template','producer','guard_validation']
    elif operation=='compositing.batch':passed=['items','parent_guard_validation','metrics','progress','resume','ancestor_roots']
    elif operation=='compositing.study':passed=['cases','batch_report','comparison','conditions','parent_guard_validation','closure_guard_validation']
    else:passed=['template','normal_run_report','normal_run_signature','candidate','outputs','producer_validation']
    result['allOf'].append({'if':{'properties':{'status':{'enum':['passed','partial']}},'required':['status']},'then':{'required':passed}})
    if operation=='compositing.template-run':result['allOf'].append({'if':{'properties':{'status':{'const':'passed'}},'required':['status']},'then':{'properties':{'producer_validation':{'const':'passed'},'seam_mock':{'const':False}},'required':['seam_mock']}})
    return result

def schemas():
    from compositing_batch_contract import schemas as parent_schemas
    from compositing_template_run_contract import schemas as template_schemas
    params={**parent_schemas(),**template_schemas()};output={}
    commands=['compositing.template-save','compositing.template-run','compositing.batch','compositing.study']
    for command in commands:
        short=command.split('.')[1]
        output[short+'-request.schema.json']=obj({'schema_version':{'const':'1.0'},'command':{'const':command},'params':copy.deepcopy(params[short])},('schema_version','command','params'))
        output[short+'-report.schema.json']=report_schema(command)
    output['template-document.schema.json']=copy.deepcopy(params['template-document'])
    output['compact-data.schema.json']=obj({'status':{'enum':['pending','running','passed','partial','failed','cancelled','timed_out','not_observed','mock_only']},
       'domain_status':{'enum':STATUSES+['not_observed']},'supervisor_state':{'enum':['queued','running','succeeded','failed','cancelled','timed_out','not_observed']},
       'transport_ok':{'type':'boolean'},'domain_all_passed':{'type':'boolean'},'evidence_kind':{'enum':['normal_host','mock_only']},'certified_producer':{'type':'boolean'},
       'counts':obj({name:{'type':'integer','minimum':0,'maximum':16} for name in ('planned','completed','passed','failed','reused','not_started')},('planned','completed','passed','failed','reused','not_started')),
       'items':{'type':'array','maxItems':16,'items':{'type':'object'}},'not_started_ids':{'type':'array','maxItems':16,'items':{'type':'string'}},'scope':{'type':'string'}},('status','domain_status','supervisor_state','transport_ok','domain_all_passed','counts','items','not_started_ids'),extra=True)
    output['compact-data.schema.json']['allOf']=[{'if':{'properties':{'domain_status':{'enum':['partial','mock_only','failed','cancelled','timed_out']}},'required':['domain_status']},'then':{'properties':{'domain_all_passed':{'const':False}}}}]
    output['compact-data.schema.json']['allOf'].append({'if':{'properties':{'status':{'enum':['pending','running','partial','mock_only','failed','cancelled','timed_out','not_observed']}},'required':['status']},'then':{'properties':{'domain_all_passed':{'const':False}}}})
    output['central-request-patch.schema.json']={'oneOf':[copy.deepcopy(output[command.split('.')[1]+'-request.schema.json']) for command in commands],
       '$comment':'Draft branch additions only: root must merge into central command enum/condition generator without changing existing commands or schemas.'}
    return output

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output-dir',type=Path,required=True);args=parser.parse_args()
    output=args.output_dir.resolve();output.mkdir(parents=True,exist_ok=True)
    for name,value in schemas().items():
        path=output/name
        if path.exists():raise RuntimeError('Refuse overwriting generated draft '+str(path))
        path.write_text(json.dumps({'$schema':'https://json-schema.org/draft/2020-12/schema',**value},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
if __name__=='__main__':main()
