"""Protected portable template worker; callers own the source/runtime guards.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from pathlib import Path
from copy import deepcopy
import json
from protocol import Failure,atomic_json,digest,read_json
from toon_identity import implementation_sha256
from toon_template_contract import normalize
from toon_workflow_addon import contract,templates

def descriptor(path):
    p=Path(path);return {'file':str(p),'sha256':digest(p),'bytes':p.stat().st_size}

def _bound(row):
    p=Path(row['file'])
    if not p.is_file() or digest(p)!=row['expected_sha256']:raise Failure('CONFLICT','Template input SHA changed')
    return read_json(p)

def resolve_request(value,document,bindings,pin):
    request=contract.normalize_request(value)
    if request['command']!='toon.run':raise Failure('INVALID_REQUEST','Template application requires an ordinary toon.run request')
    if request['params']['implementation_sha256']!=pin:raise Failure('CONFLICT','Prepared run request implementation differs')
    m=request['params']['manifest']
    if m['schema_version']!='0.7.0':raise Failure('UNSUPPORTED','Explicit region bindings require manifest0.7.0')
    m['shading']=templates.apply_document(m['shading'],document,bindings,m['engine'])
    return contract.normalize_request(request)

def _export(params):
    import bpy
    from toon_workflow import _open
    from toon_workflow_addon import core,materials,regions,state_contract
    closure=_open(params)
    scope=params['scope_id'];roots=[c for c in bpy.data.collections if c.get(core.SCOPE)==scope and c.get(core.ROLE)=='scope_root']
    if len(roots)!=1:raise Failure('CONFLICT','Template source requires exactly one managed scope')
    embedded=json.loads(roots[0].get(core.STATE,'{}'));state_contract.validate_embedded_state(embedded)
    m=embedded['manifest']
    if m['scope_id']!=scope:raise Failure('CONFLICT','Scope state ownership differs')
    scene=bpy.data.scenes.get(m['scene'])
    if scene is None:raise Failure('CONFLICT','Scope scene is missing')
    core.activate(scene);core._check_cache(roots[0],embedded)
    if regions.is_region_mode(m['shading']):
        objects=core.resolve_region_targets({'manifest':m,'targets':m['targets']})
        preparation=regions.plan_update(objects,m['targets'],m['shading'],m['engine'],scope,embedded['materials'],m['shading'])
        snapshot=preparation['current']['managed']
    else:snapshot=materials.managed_snapshot(scope)
    by_id={r['material_id']:r for r in snapshot['materials']}
    before=core.capture_records();entries=[];exported=[]
    for selected in params['selections']:
        row=by_id.get(selected['material_id'])
        if row is None:raise Failure('NOT_FOUND','Selected managed material does not exist in the scope')
        if len(row['wrapper']['nodes'])!=1 or len(row['wrapper']['links'])!=1:
            raise Failure('UNSUPPORTED','Portable parameter templates cannot represent extra wrapper nodes or links')
        entries.append({k:deepcopy(selected[k]) for k in ('template_id','name','tags')})
        entries[-1].update(engine=row['engine'],settings=deepcopy(row['settings']))
        exported.append({'material_id':selected['material_id'],'template_id':selected['template_id'],
                         'engine':row['engine'],'actual_settings':deepcopy(row['settings']),
                         'wrapper_node_count':len(row['wrapper']['nodes']),
                         'wrapper_link_count':len(row['wrapper']['links']),
                         'original_bindings':deepcopy(row['bindings'])})
    document=templates.document_from_entries(params['library_id'],params['name'],params['revision'],entries)
    if core.capture_records()!=before:raise Failure('VALIDATION_FAILED','Template export modified native source records')
    return document,{'source_content':'exact_before_after','scope_id':scope,'source_manifest_version':m['schema_version'],
                     'source_state_schema':embedded['schema'],'source_records_sha256':contract.signature(before),
                     'checked_source_records':len(before),'wrapper_policy':'single_core_output_link',
                     'selection_values':exported,'resources':closure,'source_saved':False}

def run(command,params,job):
    params=normalize(command,params);job=Path(job).resolve();operation=command.split('.')[1]
    inputs=([{'file':params['file']}] if 'file' in params else [])+list(params.get('resources',[]))+[params[k] for k in ('template','request') if k in params]
    if any(Path(row['file']).resolve().is_relative_to(job) for row in inputs):
        raise Failure('CONFLICT','Template inputs must remain outside the fresh owned job')
    report={'schema':'toon-template-report-v1','operation':operation,'status':'failed',
            'implementation_sha256':params['implementation_sha256'],'source_saved':False}
    try:
        if command=='toon.template-save':
            source={'file':params['file'],'expected_sha256':params['expected_sha256']}
            document,checks=_export(params);output=job/'toon-template.json';payload=templates.canonical_document(document)
            report.update(document=document,checks=checks)
        elif command=='toon.template-edit':
            source=deepcopy(params['template']);original=templates.normalize_document(_bound(source))
            document=templates.edit_document(original,params['expected_revision'],params['edits'])
            output=job/'toon-template.json';payload=templates.canonical_document(document)
            report.update(document=document,checks={'original_revision':original['revision'],'new_revision':document['revision'],
                'original_file_unchanged':digest(source['file'])==source['expected_sha256'],'scope_bindings_included':False})
        else:
            source=deepcopy(params['request']);document=templates.normalize_document(_bound(params['template']))
            resolved=resolve_request(_bound(source),document,params['bindings'],params['implementation_sha256'])
            output=job/'toon-resolved-request.json';payload=contract.canonical(resolved)
            report.update(resolved_request=resolved,template=deepcopy(params['template']),
                application_status='prepared_not_applied',checks={'scene_mutation':'not_performed','ordinary_run_required':True})
        if implementation_sha256()!=params['implementation_sha256']:raise Failure('CONFLICT','Template runtime changed')
        if digest(source['file'])!=source['expected_sha256']:raise Failure('CONFLICT','Template source changed')
        with output.open('x',encoding='utf-8',newline='\n') as f:f.write(payload+'\n')
        if read_json(output)!=(report.get('resolved_request') if command=='toon.template-apply' else document):
            raise Failure('VALIDATION_FAILED','Template output JSON readback differs')
        report.update(status='passed',source=source,output=descriptor(output))
        atomic_json(job/'toon-template-report.json',report)
        return {'status':'passed','report':descriptor(job/'toon-template-report.json'),'output':descriptor(output),
                'operation':operation,'application_status':report.get('application_status','portable_content_saved'),
                'outputs':[descriptor(output),descriptor(job/'toon-template-report.json')]}
    except Exception as exc:
        report['error']={'type':type(exc).__name__,'message':str(exc)};atomic_json(job/'toon-template-report.json',report)
        if isinstance(exc,contract.WorkflowError):raise Failure(exc.code if exc.code in ('INVALID_REQUEST','CONFLICT','UNSUPPORTED','NOT_FOUND') else 'INVALID_REQUEST',exc.path+': '+str(exc)) from exc
        raise
