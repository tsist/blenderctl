"""M5 prototype: SHA-bound full producer recipe, detached explicit bindings."""
import copy
from pathlib import Path
from contextlib import ExitStack
from filesystem import FileGuard
from protocol import atomic_json,read_json,digest
# The canonical Host contract initializes the local addon path in fresh workers.
import compositing_workflow_contract as _host_contract
from compositing_workflow_addon import contract as c
from compositing_batch_contract import checked,descriptor,fail,TEMPLATE_SAVE_SCHEMA,FRAME,normalize_template_params
from compositing_batch import child_artifacts

def guard(row,stack):
    from compositing_m5_guards import scope
    return scope(stack).add(descriptor(row))

def report_artifacts(report):
    return child_artifacts(report)

def producer_recipe(report):
    if report.get('compositing_report_version')!='1.0' or report.get('status')!='passed':fail('Template requires a successful compositing producer report')
    if report.get('verification',{}).get('status')!='passed' or report.get('output_verification',{}).get('status')!='passed' or report.get('protection',{}).get('status')!='pass':fail('Producer must have complete fresh/outputs/protection evidence','VALIDATION_FAILED')
    checks=report.get('checks',{})
    if not report.get('candidate') or not report.get('outputs') or any(checks.get(k)!='passed' for k in ('candidate_bytes_unchanged','fresh_saved_graph_protection','independent_png_exr_decode')):fail('Producer needs complete candidate/output identities and fresh checks','VALIDATION_FAILED')
    original=report.get('original_request',{});manifest=copy.deepcopy(original.get('manifest',{}))
    if manifest.get('operation')=='create':
        if 'source' not in manifest or bool('graph' in manifest)==bool('components' in manifest):fail('Producer create lacks complete recipe')
    elif manifest.get('operation')=='update' and manifest.get('replace_graph') is True and 'graph' in manifest:
        if not report.get('inspection',{}).get('source'):fail('replace_graph producer lacks recorded primary source','VALIDATION_FAILED')
    else:fail('Patch/adopt/detach is not a full template recipe','UNSUPPORTED')
    if manifest.get('tracking_imports'):fail('Prototype template requires caller tracking-slot design before temporal tracking export','UNSUPPORTED')
    # Temporal tracks are retained exactly; callers must explicitly resupply
    # source media/bindings and preview, with no implicit frame remapping.
    for key in ('operation','scene','view_layer','workflow_id','source','frame','reference_frame','replace_graph','patches','animation_patches'):manifest.pop(key,None)
    slots={}
    for node in manifest.get('graph',{}).get('nodes',[]):
        if 'binding' in node:
            binding=node.pop('binding');slots[node['id']]={'kind':binding['kind'],'node_type':node['type'],'requires_layer':'layer' in binding}
    return manifest,slots

def save_template(params,job,*,cancel_parent=None,_seam=False,_seam_runtime=None):
    from compositing_batch import cancel,original,fingerprint,error_row,default_runtime
    from compositing_m5_guards import scope
    from compositing_batch_contract import input_documents
    from compositing_identity import implementation_sha256
    job=Path(job).resolve();job.mkdir(parents=True,exist_ok=True);checkpoint=job/'template-report.json'
    if checkpoint.exists():fail('Template-save requires a new attempt directory','CONFLICT')
    out={'report_version':'1.0','operation':'compositing.template-save','status':'running','attempt_directory':str(job),'source_saved':False,'evidence_kind':'mock_only' if _seam else 'normal_host','certified_producer':False,'scope':'SHA-bound full recipe export; mock fixtures never certify producer'}
    atomic_json(checkpoint,out)
    try:
        params=normalize_template_params(params);producer=descriptor(params['producer']);cancel(job,cancel_parent)
        if _seam_runtime is not None and not _seam:fail('Mock runtime is test-only','INVALID_REQUEST')
        out.update(request=original(params),request_fingerprint=fingerprint(original(params)),implementation_sha256=params['implementation_sha256'],runtime=_seam_runtime if _seam_runtime is not None else default_runtime(),producer=producer)
        atomic_json(checkpoint,out)
        with ExitStack() as stack:
            guards=scope(stack)
            for row in input_documents(params,'compositing.template-save'):guards.add(row)
            report=guards.read(producer)
            if report.get('evidence_kind')=='mock_only' and not _seam:fail('Mock report cannot certify a real template export','VALIDATION_FAILED')
            if not _seam:
                import bpy
                guards.add({'file':bpy.app.binary_path,'expected_sha256':out['runtime']['binary_sha256']})
            recipe,slots=producer_recipe(report)
            for row in report_artifacts(report):cancel(job,cancel_parent);guards.add(row)
            template={'template_version':'1.0','template_id':params['template_id'],'recipe':recipe,'source_slot':{'required':True},'binding_slots':slots,'provenance':{'producer':producer,'implementation_sha256':report['implementation_sha256'],'runtime':report['runtime'],'candidate':report['candidate'],'scope':('MOCK_ONLY: audit fixture, never a real producer' if _seam else 'Full verified producer recipe; historical identity retained; new instance must be validated by current normal Host')}}
            path=job/'template.json'
            if path.exists():fail('Template destination already exists','CONFLICT')
            guards.protect_output_parent(path)
            cancel(job,cancel_parent);atomic_json(path,template);output=guards.capture(path)
            if fingerprint(guards.read(output))!=fingerprint(template):fail('Generated and persisted template contents differ','CONFLICT')
            out['guard_validation']=guards.recheck()
            if implementation_sha256()!=params['implementation_sha256']:fail('Implementation changed before template acceptance','CONFLICT')
            if not _seam and default_runtime()!=out['runtime']:fail('Runtime changed before template acceptance','CONFLICT')
            cancel(job,cancel_parent);out.update(status='mock_only' if _seam else 'passed',domain_status_observed='passed',template=output,certified_producer=not _seam)
            atomic_json(checkpoint,out)
    except BaseException as error:
        out.update(status={'CANCELLED':'cancelled','TIMEOUT':'timed_out'}.get(getattr(error,'code',None),'failed'),error=error_row(error),certified_producer=False)
        atomic_json(checkpoint,out);raise
    return out

def instantiate(template,*,file,expected_sha256,context,source,bindings,resources,preview):
    if not isinstance(template,dict) or template.get('template_version')!='1.0':fail('Unknown template version')
    checked(context,c.obj({'scene':c.NAME,'view_layer':c.NAME,'workflow_id':c.ID,'reference_frame':FRAME},('scene','view_layer','workflow_id','reference_frame')))
    slots=template['binding_slots']
    if set(bindings)!=set(slots):fail('Every binding slot requires an explicit new binding, with no extras')
    manifest=copy.deepcopy(template['recipe']);manifest.update(operation='create',scene=context['scene'],view_layer=context['view_layer'],workflow_id=context['workflow_id'],source=copy.deepcopy(source))
    frame=context['reference_frame'];manifest['frame']=frame
    if manifest['workflow_version'] in ('1.2','1.3'):manifest['reference_frame']=frame
    for node in manifest.get('graph',{}).get('nodes',[]):
        if node['id'] in slots:
            binding=copy.deepcopy(bindings[node['id']]);slot=slots[node['id']]
            if binding.get('kind')!=slot['kind'] or slot['requires_layer'] and 'layer' not in binding:fail('New binding does not satisfy explicit slot kind/layer')
            node['binding']=binding
    try:return c.normalize_run_params({'file':file,'expected_sha256':expected_sha256,'resources':copy.deepcopy(resources),'manifest':manifest,'preview':copy.deepcopy(preview)})
    except c.WorkflowError as error:fail(str(error),error.code,error.details)

def instantiate_from_file(template_descriptor,**kwargs):
    row=descriptor(template_descriptor)
    with ExitStack() as stack:
        guard(row,stack);params=instantiate(read_json(row['file']),**kwargs)
    return {'params':params,'template':row}
