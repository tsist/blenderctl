"""No-bpy parent acceptance for owned portable template/request artifacts.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from pathlib import Path
import os
from protocol import Failure,read_json
from filesystem import FileGuard
from toon_workflow_addon import contract,templates,state_contract
from copy import deepcopy
from toon_templates import resolve_request

def accept(job,request,data,stack,checkpoint):
    job=Path(job).resolve();params=request['params'];command=request['command'];checked={}
    def document(row):
        if not isinstance(row,dict) or set(row)!={'file','sha256','bytes'}:
            raise Failure('VALIDATION_FAILED','Exact template artifact descriptor required')
        contract.validate(row['sha256'],contract.SHA_SCHEMA,'artifact.sha256')
        if type(row['bytes']) is not int or row['bytes']<=0:raise Failure('VALIDATION_FAILED','Positive artifact size required')
        if not isinstance(row['file'],str) or not Path(row['file']).is_absolute():
            raise Failure('VALIDATION_FAILED','Absolute template artifact path required')
        p=Path(os.path.abspath(row['file']))
        if not p.is_absolute() or p.parent!=job or not p.is_file() or p.resolve()!=p:
            raise Failure('VALIDATION_FAILED','Template artifact must be a regular owned job file')
        if p.is_symlink() or (p.stat().st_file_attributes&0x400):raise Failure('CONFLICT','Template artifact uses a reparse path')
        key=str(p).casefold()
        if key in checked:
            if checked[key]!=row:raise Failure('CONFLICT','Conflicting template artifact descriptor')
            return p
        guard=stack.enter_context(FileGuard(p))
        if guard.sha256(checkpoint)!=row['sha256'] or p.stat().st_size!=row['bytes']:
            raise Failure('CONFLICT','Template artifact changed before parent acceptance')
        checked[key]=dict(row);return p
    if (not isinstance(data,dict) or data.get('status')!='passed' or not isinstance(data.get('outputs'),list)
            or data.get('operation')!=command.split('.')[1]):
        raise Failure('VALIDATION_FAILED','Successful template worker evidence required')
    if len(data['outputs'])!=2 or len({row.get('file') for row in data['outputs'] if isinstance(row,dict)})!=2:
        raise Failure('VALIDATION_FAILED','Exact unique template output inventory required')
    for row in data['outputs']:document(row)
    report_path=document(data['report']);output=document(data['output'])
    expected_output='toon-resolved-request.json' if command=='toon.template-apply' else 'toon-template.json'
    if report_path!=job/'toon-template-report.json' or output!=job/expected_output or set(checked)!={str(report_path).casefold(),str(output).casefold()}:
        raise Failure('VALIDATION_FAILED','Fixed template output paths differ')
    report=read_json(report_path);value=read_json(output)
    if (report.get('schema')!='toon-template-report-v1' or report.get('status')!='passed'
            or report.get('operation')!=command.split('.')[1] or report.get('implementation_sha256')!=params['implementation_sha256']
            or report.get('source_saved') is not False or report.get('output')!=data['output']):
        raise Failure('VALIDATION_FAILED','Template report operation/status/pin/output differs')
    expected_source=({'file':params['file'],'expected_sha256':params['expected_sha256']} if command=='toon.template-save'
                     else params['request'] if command=='toon.template-apply' else params['template'])
    if report.get('source')!=expected_source:raise Failure('VALIDATION_FAILED','Template report source differs')
    inputs=([{'file':params['file']}] if 'file' in params else [])+list(params.get('resources',[]))+[params[k] for k in ('template','request') if k in params]
    if any(Path(row['file']).resolve().is_relative_to(job) for row in inputs):raise Failure('CONFLICT','Template input aliases its owned output job')
    if command=='toon.template-apply':
        library=templates.normalize_document(read_json(params['template']['file']))
        expected=resolve_request(read_json(params['request']['file']),library,params['bindings'],params['implementation_sha256'])
        if (value!=expected or report.get('resolved_request')!=expected
                or report.get('template')!=params['template'] or report.get('application_status')!='prepared_not_applied'
                or data.get('application_status')!='prepared_not_applied'):
            raise Failure('VALIDATION_FAILED','Prepared run request differs from protected library/bindings')
        if report.get('checks')!={'scene_mutation':'not_performed','ordinary_run_required':True}:
            raise Failure('VALIDATION_FAILED','Prepared request scene mutation evidence differs')
        # Independently preserve all original request fields and assign only the
        # explicitly mapped full settings; do not rely solely on resolve_request.
        original=contract.normalize_request(read_json(params['request']['file']))
        independent=deepcopy(original);sources={r['template_id']:r for r in library['templates']}
        targets={r['template_id']:r for r in independent['params']['manifest']['shading']['templates']}
        for binding in params['bindings']:
            targets[binding['target_template_id']]['settings']=deepcopy(sources[binding['library_template_id']]['settings'])
        if independent!=value:raise Failure('VALIDATION_FAILED','Template application changed unbound request/source/scene fields')
        canonical=contract.canonical(expected)
    else:
        value=templates.normalize_document(value)
        if command=='toon.template-edit':
            expected=templates.edit_document(read_json(params['template']['file']),params['expected_revision'],params['edits'])
            if report.get('checks')!={'original_revision':params['expected_revision'],'new_revision':expected['revision'],
                    'original_file_unchanged':True,'scope_bindings_included':False}:
                raise Failure('VALIDATION_FAILED','Template revision/source-preservation evidence differs')
        else:
            checks=report.get('checks')
            if not isinstance(checks,dict) or set(checks)!={'source_content','scope_id','source_manifest_version',
                    'source_state_schema','source_records_sha256','checked_source_records','wrapper_policy',
                    'selection_values','resources','source_saved'}:
                raise Failure('VALIDATION_FAILED','Exact native export checks required')
            rows=checks['selection_values']
            if (not isinstance(rows,list) or len(rows)!=len(params['selections'])
                    or len(value['templates'])!=len(rows) or report['checks'].get('source_content')!='exact_before_after'
                    or report['checks'].get('source_saved') is not False):
                raise Failure('VALIDATION_FAILED','Native export observations are incomplete')
            if (checks['scope_id']!=params['scope_id'] or checks['source_manifest_version'] not in contract.MANIFEST_VERSIONS
                    or checks['source_state_schema']!=state_contract.state_schema({'schema_version':checks['source_manifest_version']})
                    or checks['wrapper_policy']!='single_core_output_link' or type(checks['checked_source_records']) is not int
                    or checks['checked_source_records']<1):
                raise Failure('VALIDATION_FAILED','Native scope/state/wrapper preservation evidence differs')
            contract.validate(checks['source_records_sha256'],contract.SHA_SCHEMA,'checks.source_records_sha256')
            closure=checks['resources'];declared={str(Path(r['file']).resolve()).casefold():r['expected_sha256'] for r in params['resources']}
            if (not isinstance(closure,dict) or set(closure)!={'status','external','limitations'}
                    or closure['status']!='closed_for_enumerated_static_paths' or not isinstance(closure['external'],list)):
                raise Failure('VALIDATION_FAILED','Static export resource closure is incomplete')
            for resource in closure['external']:
                if (not isinstance(resource,dict) or not {'file','sha256'}<=set(resource)
                        or not isinstance(resource['file'],str) or not Path(resource['file']).is_absolute()):
                    raise Failure('VALIDATION_FAILED','Malformed native resource descriptor')
                contract.validate(resource['sha256'],contract.SHA_SCHEMA,'checks.resources.sha256')
                if declared.get(str(Path(resource['file']).resolve()).casefold())!=resource['sha256']:
                    raise Failure('VALIDATION_FAILED','Exported native resource is outside protected declarations')
            if {k:value[k] for k in ('library_id','name','revision')}!={k:params[k] for k in ('library_id','name','revision')}:
                raise Failure('VALIDATION_FAILED','Exported library header differs')
            for entry,selected,observation in zip(value['templates'],params['selections'],rows):
                if ({k:entry[k] for k in ('template_id','name','tags')}!={k:selected[k] for k in ('template_id','name','tags')}
                        or observation.get('material_id')!=selected['material_id'] or observation.get('template_id')!=selected['template_id']
                        or observation.get('engine')!=entry['engine'] or observation.get('actual_settings')!=entry['settings']
                        or type(observation.get('wrapper_node_count')) is not int or observation['wrapper_node_count']!=1
                        or type(observation.get('wrapper_link_count')) is not int or observation['wrapper_link_count']!=1):
                    raise Failure('VALIDATION_FAILED','Exported native parameter selection differs')
            expected=value
        if value!=expected or report.get('document')!=value:raise Failure('VALIDATION_FAILED','Template document/report differs')
        canonical=templates.canonical_document(expected)
    if output.read_text(encoding='utf-8')!=canonical+'\n':raise Failure('VALIDATION_FAILED','Canonical template artifact encoding differs')
    checkpoint();data['artifact_accepted']=True;data['parent_output_validation']='passed';return data
