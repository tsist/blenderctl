"""Discover compositing contracts without starting Blender; compact real evidence."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from protocol import Failure,read_json,digest,VERSION

def descriptor(path):return {'file':str(path),'sha256':digest(path)}

def compact_binding(binding):
    out={key:binding[key] for key in ('kind','file','expected_sha256','colorspace','alpha_mode','layer','scene','view_layer','mapping','metadata') if key in binding}
    if isinstance(binding.get('frames'),list):
        rows=binding['frames']
        out['frame_count']=len(rows)
        out['frame_endpoints']=[rows[index] for index in sorted({0,len(rows)-1})] if rows else []
    return out

def sampled_outputs(rows):
    # Full per-frame records remain in the hashed report. Compact carries a
    # bounded first/middle/last selection, keeping legacy <=8-file output intact.
    indices=list(range(len(rows))) if len(rows)<=8 else sorted({0,1,len(rows)//2,len(rows)-2,len(rows)-1})
    return [{k:rows[i][k] for k in ('file','sha256','format','frame','width','height','status','requested_device','used_device','device_fallback','device_scope','denoise_execution','working_space','output_color_space') if k in rows[i]} for i in indices]

def _param_projection(operation,param,operation_schema,source):
    """Exact top-level discovery only; retain alternatives rather than invent a validator."""
    import copy
    branches=operation_schema.get('oneOf')
    branch_kind='oneOf' if branches is not None else None
    if branches is None:
        branches=operation_schema.get('anyOf')
        branch_kind='anyOf' if branches is not None else None
    branches=branches if branches is not None else [operation_schema]
    available=sorted({name for branch in branches for name in branch.get('properties',{})})
    if param not in available:
        raise Failure('INVALID_REQUEST','Unknown compositing operation parameter: '+param+
                      '; available_params='+','.join(available[:24])+
                      '; available_param_count='+str(len(available))+
                      '; full_schema='+source['command'])
    constraint_keys=('type','required','additionalProperties','allOf','anyOf','not','if','then','else',
                     'dependentRequired','dependentSchemas','propertyNames','minProperties','maxProperties',
                     'unevaluatedProperties')
    variants=[]
    escaped=param.replace('~','~0').replace('/','~1')
    for index,branch in enumerate(branches):
        pointer=('/'+branch_kind+'/'+str(index)) if branch_kind else ''
        present=param in branch.get('properties',{})
        row={'parent_pointer':pointer or '', 'present':present,
             'parent_constraints':{key:copy.deepcopy(branch[key]) for key in constraint_keys if key in branch}}
        if present:
            row.update(schema_pointer=pointer+'/properties/'+escaped,
                       schema=copy.deepcopy(branch['properties'][param]))
        variants.append(row)
    out={'operation':operation,'param':param,'complete_schema':source,
         'scope':'Exact top-level field discovery; not a complete request validator. Parent alternatives and semantic/runtime validation still apply.'}
    if branch_kind:
        out.update(branch_kind=branch_kind,variants=variants,
                   root_constraints={key:copy.deepcopy(operation_schema[key]) for key in constraint_keys if key in operation_schema})
    else:
        out.update(variants[0]);out.pop('present')
    return out

def _describe_param(operation,param):
    from compositing_workflow_contract import schemas
    known=schemas()
    module_names={'run':'compositing_workflow_addon.contract','inspect':'compositing_workflow_addon.contract',
                  'manifest':'compositing_workflow_addon.contract','preview':'compositing_workflow_addon.contract',
                  'template-save':'compositing_batch_contract','batch':'compositing_batch_contract',
                  'study':'compositing_batch_contract','template-run':'compositing_template_run_contract',
                  'template-document':'compositing_template_run_contract','encode':'compositing_encode_public',
                  'depth-prepare':'compositing_depth_contract','depth-inspect':'compositing_depth_contract',
                  'depth-estimate':'compositing_depth_estimate_contract','depth-relight':'compositing_relight_contract',
                  'depth-session':'compositing_relight_session_contract',
                  'depth-sequence':'compositing_relight_sequence_contract'}
    if operation=='depth-sequence':
        from compositing_relight_sequence_contract import params_schema
        operation_schema=params_schema()
    elif operation=='depth-session':
        from compositing_relight_session_contract import params_schema
        operation_schema=params_schema()
    elif operation=='depth-relight':
        from compositing_relight_contract import params_schema
        operation_schema=params_schema()
    elif operation=='depth-estimate':
        from compositing_depth_estimate_contract import params_schema
        operation_schema=params_schema()
    elif operation in ('depth-prepare','depth-inspect'):
        from compositing_depth_contract import params_schema
        operation_schema=params_schema(operation)
    elif operation=='encode':
        from compositing_encode_public import PARAMS_SCHEMA
        operation_schema=PARAMS_SCHEMA
    elif operation in known:
        operation_schema=known[operation]
    else:
        raise Failure('INVALID_REQUEST','--param requires a valid --operation')
    import importlib
    module=importlib.import_module(module_names[operation])
    source={'operation':operation,'command':'compositing describe --operation '+operation+' --schema',
            'schema_root_pointer':'/schema',
            'defining_source':descriptor(Path(module.__file__)),
            'discovery_source':descriptor(Path(__file__)),
            'resolver_source':descriptor(Path(module.__file__) if operation in ('depth-prepare','depth-inspect','depth-estimate','depth-relight','depth-session','depth-sequence') else Path(sys.modules['compositing_workflow_contract'].__file__))}
    return _param_projection(operation,param,operation_schema,source)

def describe(operation=None,schema=False,section=None,param=None):
    if param is not None:
        if schema or section is not None:
            raise Failure('INVALID_REQUEST','--param is mutually exclusive with --schema and --section')
        if not operation:
            raise Failure('INVALID_REQUEST','--param requires --operation')
        return _describe_param(operation,param)
    if operation=="encode":
        from compositing_encode_public import describe as describe_encode
        return describe_encode(schema,section)
    if operation=='depth-sequence':
        from compositing_relight_sequence_contract import params_schema
        if section is not None:return _describe_param(operation,section)
        if schema:return {'operation':operation,'schema':params_schema(),'support_level':'experimental'}
        return {'cli_version':VERSION,'operation':'compositing.depth-sequence','support_level':'experimental',
                'execution':'Finite owned shot with explicit frame/time identity, frozen shot statistics and complete immutable output inventory',
                'schema_discovery':'compositing describe --operation depth-sequence --schema',
                'authority_scope':'Complete succeeded parent required; partial files do not authorize a consumer',
                'evidence':'docs/cli/深度补光序列.md',
                'validation':'Consult matching implementation runtime evidence; registration alone is not sequence qualification'}
    if operation=='depth-session':
        from compositing_relight_session_contract import params_schema
        if section is not None:return _describe_param(operation,section)
        if schema:return {'operation':operation,'schema':params_schema(),'support_level':'experimental'}
        return {'cli_version':VERSION,'operation':'compositing.depth-session','support_level':'experimental',
                'execution':'One bounded owned Host job with resident fixed CUDA0_FP32 and latest-revision preview transport; outside DAG',
                'schema_discovery':'compositing describe --operation depth-session --schema',
                'authority_scope':'Guarded preview requires running parent; final public success requires succeeded parent plus matching closed complete receipt',
                'revision_schema':'docs/cli/schemas/compositing-depth-session-revision.schema.json',
                'live_cli':'compositing depth-control {inspect,submit,read,stop} JOB_ID under the same --jobs-dir',
                'local_edit_recipe':'v1/v2 explicit-Apply ellipse/box masks, prepared-depth affine and regional roughness/receiver/caster data; independent derived identity',
                'evidence':'docs/cli/GPU热预览与画面拖灯.md',
                'unverified':['formal3+1 visual quality','video temporal stability','cross-machine','extension-manager install','per-region roughness','receiver-plane shadows','hot final EXR']}
    if operation=='depth-relight':
        from compositing_relight_contract import params_schema
        if section is not None:return _describe_param(operation,section)
        if schema:return {'operation':operation,'schema':params_schema(),'support_level':'experimental'}
        return {'cli_version':VERSION,'operation':'compositing.depth-relight','support_level':'experimental',
                'execution':'Owned single-batch consumer of succeeded depth bundle, fixed GPU or explicit CPU, no model/scene',
                'schema_discovery':'compositing describe --operation depth-relight --schema',
                'scope':'Bound metric or artistic frame; straight scene-linear floats, per-light contributions and separate SDR display',
                'unverified':['owned hot session','Blender dragging','formal visual quality','controlled ground receiver','sequence','cross-machine']}
    if operation=='depth-estimate':
        from compositing_depth_estimate_contract import params_schema,signature
        import compositing_depth_estimate_contract as estimate
        selected=params_schema()
        if section is not None:return _describe_param(operation,section)
        if schema:return {'operation':operation,'schema':selected,'support_level':'experimental'}
        return {'cli_version':VERSION,'operation':'compositing.depth-estimate','support_level':'experimental',
                'execution':'Owned Host fixed DAV2 Large CUDA0 FP32 inference; isolated existing venv; no Blender scene',
                'contract_identity':{'contract':descriptor(Path(estimate.__file__)),'schema_signature':signature(selected)},
                'consumer_eligible':False,'profile':'Root-qualified fixed source/checkpoint/approved14 payload expected manifest',
                'scope':'New PNG8 RGB or opaque RGBA; native tensor pixel budget; relative raw plus separate prepare recipe',
                'schema_discovery':'compositing describe --operation depth-estimate --schema',
                'unverified':['generic geometry or metric quality','lighting consumer','CPU/MPS/half fallback','sequence','cross-machine']}
    if operation in ('depth-prepare','depth-inspect'):
        from compositing_depth_contract import params_schema,implementation_identity
        selected=params_schema(operation)
        if section is not None:
            if section not in selected.get('properties',{}):raise Failure('INVALID_REQUEST','Unknown depth params section')
            return _describe_param(operation,section)
        if schema:return {'operation':operation,'schema':selected,'support_level':'experimental'}
        return {'cli_version':VERSION,'operation':'compositing.'+operation,'support_level':'experimental',
                'execution':'Host-only existing depth preparation/cold inspection; no Blender scene or automatic RGB estimation',
                'contract_identity':implementation_identity(),'consumer_eligible':False,
                'scope':'Explicit axial-Z geometry or declared relative-score artistic proxy; no automatic metric/model/lighting certification',
                'schema_discovery':'compositing describe --operation '+operation+' --schema',
                'unverified':['public runtime acceptance pending matching evidence','visible GUI','RGB inference','sequence','cross-machine']}
    from compositing_workflow_addon import contract as c
    from compositing_workflow_contract import schemas
    if section:
        branches=c.MANIFEST_SCHEMA.get('oneOf',[c.MANIFEST_SCHEMA])
        matches=[branch.get('properties',{}).get(section) for branch in branches]
        matches=[value for value in matches if value is not None]
        if not matches:raise Failure('INVALID_REQUEST','Unknown compositing manifest section')
        out={'section':section,'schema':matches[-1]}
        if any(value!=matches[-1] for value in matches[:-1]):
            out['legacy_schemas']=matches[:-1]
            out['scope']='schema is newest workflow branch; legacy_schemas retain earlier contracts'
        if section=='components':out['component_parameters']=getattr(c,'COMPONENT_PARAM_SCHEMAS',{})
        return out
    if schema:
        known=schemas()
        if operation not in known:raise Failure('INVALID_REQUEST','--schema requires a described operation')
        return {'operation':operation,'schema':known[operation]}
    from compositing_workflow_addon import bl_info
    versions=[]
    for branch in c.MANIFEST_SCHEMA.get('oneOf',[c.MANIFEST_SCHEMA]):
        field=branch.get('properties',{}).get('workflow_version',{})
        versions.extend(field.get('enum',[field['const']] if 'const' in field else []))
    versions=list(dict.fromkeys(versions))
    return {'cli_version':VERSION,'plugin_version':'.'.join(map(str,bl_info['version'])),'workflow_version':'1.0','workflow_versions':versions,
            'operations':['compositing.run','compositing.inspect','compositing.encode','compositing.template-save','compositing.template-run','compositing.batch','compositing.study','compositing.depth-prepare','compositing.depth-inspect','compositing.depth-estimate','compositing.depth-relight','compositing.depth-session','compositing.depth-sequence'],'actions':['create','update','adopt','detach'],
            'state_source':'SAVED_DISK_V1; GUI edits handed off through an explicit saved snapshot',
            'milestone':'Historical M3b development: explicit full-CFR SDR8 Movie conversion in workflow 1.3; legacy 1.0/1.1/1.2 retained',
            'schema_operations':list(schemas())+['encode','depth-prepare','depth-inspect','depth-estimate','depth-relight','depth-session','depth-sequence'],
            'experimental_operations':['compositing.depth-prepare','compositing.depth-inspect','compositing.depth-estimate','compositing.depth-relight','compositing.depth-session','compositing.depth-sequence'],
            'param_discovery':'compositing describe --operation OPERATION --param FIELD; exact field and parent constraints, not a complete request validator',
            'current_interface':'Thirteen public operation names; six depth operations are experimental. Discovery does not certify runtime or visual quality',
            'validation':'Current implementation support must be checked against matching runtime evidence; historical M2 checkpoint is CLI0.59/plugin0.2',
            'components':list(c.COMPONENT_SCHEMA['properties']['kind']['enum']),
            'component_parameter_discovery':'compositing describe --section components','contract':descriptor(Path(c.__file__)),
            'support_levels':'Node discovery/creation, validated graph operation, numeric output and visual acceptance are distinct',
            'later':['HDR device viewing and complete normative certification'],
            'unverified':['all node modes','exact EEVEE pass reconstruction','complete HDR10','visible GUI installation'],
            'compact':'--compact request and job result/wait; hashed full report remains on disk'}

def compact(result,requested_command=None):
    artifacts=result.get('artifacts') or {};root=Path(artifacts['job_directory']) if artifacts.get('job_directory') else None
    command=requested_command or result.get('command')
    if root and (root/'request.json').is_file():command=read_json(root/'request.json').get('command',command)
    if command=='compositing.encode':
        from compositing_encode_public import compact as compact_encode
        return compact_encode(result,command)
    if command=='compositing.depth-sequence':
        report=read_json(root/'sequence-report.json') if root and (root/'sequence-report.json').is_file() else {}
        state=read_json(root/'status.json') if root and (root/'status.json').is_file() else {}
        out={k:result[k] for k in ('schema_version','cli_version','command','job_id','ok','error') if k in result}
        out.update(output_format='compact',artifacts=artifacts,data={'parent_state':state.get('state'),
            'report_status':report.get('status'),'frame_count':report.get('frame_count'),
            'support_level':'experimental','report':descriptor(root/'sequence-report.json') if root and (root/'sequence-report.json').is_file() else None})
        return out
    if command=='compositing.depth-session':
        summary=(result.get('data') or {}) if result.get('command')==command else {}
        report=read_json(root/'session-report.json') if root and (root/'session-report.json').is_file() else summary
        state=read_json(root/'status.json') if root and (root/'status.json').is_file() else {}
        ready=read_json(root/'session-ready.json') if root and (root/'session-ready.json').is_file() else {}
        complete=read_json(root/'session-complete.json') if root and (root/'session-complete.json').is_file() else {}
        latest_path=root/'session'/'latest.json' if root else None
        latest=read_json(latest_path) if latest_path and latest_path.is_file() else report.get('last_passed')
        parent_state=state.get('state',(result.get('data') or {}).get('state'))
        closed=(parent_state=='succeeded' and report.get('status')=='closed' and complete.get('status')=='complete'
                and complete.get('schema')=='relight-session-closed-v1' and root is not None
                and complete.get('parent_job_id')==root.name)
        report_path=root/'session-report.json' if root else None
        closed=closed and report_path.is_file() and complete.get('report',{}).get('file')==str(report_path) and complete.get('report',{}).get('sha256')==digest(report_path)
        live=(parent_state=='running' and ready.get('schema')=='relight-session-ready-v1')
        out={k:result[k] for k in ('schema_version','cli_version','command','job_id','ok','error') if k in result}
        out.update(output_format='compact',artifacts=artifacts,data={'parent_state':parent_state,
            'status':'closed' if closed else 'live_guarded_preview' if live else 'closed_unverified' if parent_state=='succeeded' else parent_state or report.get('status','pending'),
            'report_status':report.get('status'),'transport_ok':result.get('ok') is True,'support_level':'experimental',
            'public_success':bool(closed),'live_guarded_preview':bool(live),
            'authority_scope':'Running ready session may expose guarded preview; only succeeded parent plus exact closed report receipt is final public success',
            'runtime':{'ready':bool(live),'ready_file_exists':bool(ready),'session_id':ready.get('session_id'),'domain':ready.get('domain'),
                       'preview_divisor':ready.get('preview_divisor'),'ready_seconds':ready.get('ready_seconds')},
            'verified_revision_count':len(report['verified_revisions']) if 'verified_revisions' in report else None,
            'event_count':len(report['events']) if 'events' in report else None,
            'data_generation_count':report.get('data_generation_count'),'tensor_upload_count':report.get('tensor_upload_count')})
        if isinstance(latest,dict):
            request=latest.get('request',latest)
            out['data']['last_passed']={k:latest[k] for k in ('status','domain','output','display','timings','derived_cache_id','generation') if k in latest}
            out['data']['last_passed'].update({k:request[k] for k in ('revision','parameters_sha256') if k in request})
            out['data']['last_passed']['authority']='guarded_preview' if live else 'closed_session_record' if closed else 'diagnostic_only'
        out['data']['references']={n:descriptor(root/n) for n in ('request.json','launch.json','status.json','result.json','session-ready.json','session-report.json','session-complete.json','session-failure.json') if root and (root/n).is_file()}
        if latest_path and latest_path.is_file():out['data']['references']['latest.json']=descriptor(latest_path)
        return out
    if command=='compositing.depth-relight':
        summary=(result.get('data') or {}) if result.get('command')==command else {}
        report=read_json(root/'relight-report.json') if root and (root/'relight-report.json').is_file() else summary
        state=read_json(root/'status.json') if root and (root/'status.json').is_file() else {}
        out={k:result[k] for k in ('schema_version','cli_version','command','job_id','ok','error') if k in result}
        out.update(output_format='compact',artifacts=artifacts,data={k:summary.get(k,report.get(k)) for k in ('status','bundle','domain','compute','host_elapsed_seconds','display_quality_qualified','error') if k in summary or k in report})
        out['data'].update(parent_state=state.get('state'),transport_ok=result.get('ok') is True,support_level='experimental',
                           authority_scope='Only succeeded parent plus exact complete report/output binding authorizes consumption')
        out['data']['outputs']=[r for r in report.get('members',[]) if r['role'] in ('output','linear_straight_exr','display_srgb_png')]
        out['data']['references']={n:descriptor(root/n) for n in ('request.json','launch.json','status.json','result.json','relight-report.json','relight-bundle.json','relight-failure.json') if root and (root/n).is_file()}
        return out
    if command=='compositing.depth-estimate':
        summary=(result.get('data') or {}) if result.get('command')==command else {}
        report=read_json(root/'model-report.json') if root and (root/'model-report.json').is_file() else summary
        state=read_json(root/'status.json') if root and (root/'status.json').is_file() else {}
        out={key:result[key] for key in ('schema_version','cli_version','command','job_id','ok','error') if key in result}
        out.update(output_format='compact',artifacts=artifacts,data={key:summary.get(key,report.get(key)) for key in
                   ('status','raw','model_report','prepare_params','consumer_eligible','producer_identity','request_signature','error') if key in summary or key in report})
        out['data'].update(parent_state=state.get('state'),transport_ok=result.get('ok') is True,support_level='experimental',
                           authority_scope='Only succeeded public estimate parent authorizes its raw and exact prepare recipe; estimate is not a depth bundle')
        out['data']['references']={name:descriptor(root/name) for name in ('request.json','launch.json','status.json','result.json','model-report.json','depth-prepare-params.json','model-lifecycle.json') if root and (root/name).is_file()}
        return out
    if command in ('compositing.depth-prepare','compositing.depth-inspect'):
        report=read_json(root/'depth-report.json') if root and (root/'depth-report.json').is_file() else (result.get('data') or {}) if result.get('command')==command else {}
        state=read_json(root/'status.json') if root and (root/'status.json').is_file() else {}
        out={key:result[key] for key in ('schema_version','cli_version','command','job_id','ok','error') if key in result}
        out.update(output_format='compact',artifacts=artifacts,data={key:report[key] for key in
                   ('status','operation','depth_report_version','implementation','semantics','domain','normalization','normal_scope','stage_key','checks','protection','consumer_eligible','error') if key in report})
        out['data'].update(parent_state=state.get('state'),transport_ok=result.get('ok') is True,
                           authority_scope='Only a succeeded public parent can authorize cold bundle inspection',support_level='experimental')
        out['data']['references']={name:descriptor(root/name) for name in ('request.json','launch.json','status.json','result.json','depth-report.json') if root and (root/name).is_file()}
        if command=='compositing.depth-prepare' and root and (root/'depth-bundle.json').is_file():
            out['data']['references']['depth-bundle.json']=descriptor(root/'depth-bundle.json')
        return out
    if not command or not command.startswith('compositing.'):raise Failure('INVALID_REQUEST','Not a compositing job')
    if command in ('compositing.template-save','compositing.template-run','compositing.batch','compositing.study'):
        from compositing_orchestration_interface import compact as compact_parent
        return compact_parent(result,root,command)
    report=read_json(root/'compositing-report.json') if root and (root/'compositing-report.json').is_file() else ((result.get('data') or {}) if command==result.get('command') else {})
    out={k:result[k] for k in ('schema_version','cli_version','command','job_id','ok','error') if k in result}
    out.update(output_format='compact',data={k:report[k] for k in ('status','source','candidate','scene','workflow_id','implementation_sha256','inspection','device','denoise_execution','preview_runtime_recovery','protection','verification','recovery','checks','metrics','support','error') if k in report})
    if isinstance(report.get('input_bindings'),dict):
        inputs=report['input_bindings'].get('inputs',[])
        out['data']['input_bindings']={'status':report['input_bindings'].get('status'),'input_count':len(inputs),
                                      'inputs':[{**{k:r[k] for k in ('node_id','required_outputs') if k in r},
                                                 **({'binding':compact_binding(r['binding'])} if isinstance(r.get('binding'),dict) else {})} for r in inputs[:8]]}
    if isinstance(report.get('temporal'),dict):
        temporal=report['temporal']
        out['data']['temporal']={key:temporal[key] for key in ('mode','reference_frame','scene_frame','frames_checked','fps','mapping_scope','source_mapping') if key in temporal}
        frames=temporal.get('scene_frames',[])
        if frames:out['data']['temporal']['scene_frame_range']=[frames[0],frames[-1]]
    if isinstance(report.get('frame_receipts'),list):
        receipts=report['frame_receipts'];statuses={}
        for row in receipts:
            status=row.get('status','not_observed');statuses[status]=statuses.get(status,0)+1
        out['data']['frame_receipts']={'count':len(receipts),'statuses':statuses,
            'reference_restored_count':sum(row.get('reference_restoration')=='passed' for row in receipts),
            'scope':'Summary only; complete per-role/frame dependency and output evidence in hashed report'}
    if isinstance(report.get('video'),dict):
        out['data']['video']={key:report['video'][key] for key in ('status','file','sha256','kind','alpha_policy','frames','scene_fps','color_metadata','color_scope','validation') if key in report['video']}
    if isinstance(report.get('immutable_media_digest_scope'),dict):
        out['data']['immutable_media_digest_scope']=report['immutable_media_digest_scope']
    color=report.get('source_color_identity') or report.get('color_identity')
    if isinstance(color,dict):
        out['data']['color_identity']={'working_space':color.get('working_space'),'scope':color.get('scope'),
            'ocio':{k:color.get('ocio',{})[k] for k in ('status','file','sha256','file_transform_scan') if k in color.get('ocio',{})}}
    # Submission/wait/status describe job state even before a domain report
    # exists. A last checkpoint report is not terminal authority after cancel.
    job_data=result.get('data') or {}
    if command!=result.get('command'):
        out['data']['job_state']={k:job_data[k] for k in ('state','status','exit_code','finished_at','elapsed_seconds','pending','status_file','result_file') if k in job_data}
        state=job_data.get('state')
        if state:
            out['data']['status']={'queued':'pending','running':'running','succeeded':'passed'}.get(state,state)
        elif not report:
            out['data']['status']='pending' if result.get('ok') else 'failed'
    if isinstance(out['data'].get('inspection'),dict):
        conflicts=out['data']['inspection'].get('conflicts',[])
        out['data']['inspection']['conflict_count']=len(conflicts)
        out['data']['inspection']['conflicts']=conflicts[:8]
    for key in ('inspection','verification','protection'):
        if isinstance(out['data'].get(key),dict):
            keep=('status','scene','workflow_id','fingerprint','managed_group','root_group','bridge_node','conflicts','conflict_count','non_target_protection','error','blender','build')
            out['data'][key]={k:v for k,v in out['data'][key].items() if k in keep}
    for key in ('outputs','baselines'):
        rows=report.get(key,[])
        out['data'][key]=sampled_outputs(rows)
        if len(rows)>8:
            out['data'][key+'_count']=len(rows)
            out['data'][key+'_scope']='first/middle/last samples; all records in hashed full report'
    if result.get('error'):out['data']['status']={'CANCELLED':'cancelled','TIMEOUT':'timed_out'}.get(result['error'].get('code'),'failed')
    out['artifacts']={'job_directory':str(root)} if root else {}
    if root:
        for name in ('request','result','compositing-report','compositing-progress','compositing-reopen-saved','compositing-reopen-outputs'):
            path=root/(name+'.json')
            if path.is_file():out['artifacts'][name]=descriptor(path)
    return out
