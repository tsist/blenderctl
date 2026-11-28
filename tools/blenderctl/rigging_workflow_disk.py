"""Saved-file adapter: use the same product service, write only a new job file."""
from pathlib import Path
import json
import sys
import bpy
from protocol import Failure,atomic_json,digest
from rigging_workflow_contract import PRODUCT,implementation_sha256,resolve_parameters


def _dependencies(params):
    declared={Path(r['file']).resolve() for r in params['resources']}
    external=[]
    for group in (bpy.data.libraries,bpy.data.images,bpy.data.sounds,bpy.data.fonts,bpy.data.movieclips):
        for item in group:
            raw=getattr(item,'filepath','')
            if not raw or raw=='<builtin>' or getattr(item,'packed_file',None) or getattr(item,'packed_files',None):
                continue
            if hasattr(item,'source') and item.source in {'GENERATED','VIEWER'}:
                continue
            path=Path(bpy.path.abspath(raw,library=getattr(item,'library',None))).resolve()
            if path not in declared:
                external.append(str(path))
    if external:
        raise Failure('CONFLICT','Undeclared external input dependencies: '+repr(sorted(set(external))))


def _step_failure(job,step,exc,completed):
    """Keep a bounded domain diagnosis in this job, without serializing handles."""
    details=getattr(exc,'details',None)
    try:
        raw=json.dumps(details,ensure_ascii=False,allow_nan=False)
        if len(raw.encode('utf-8'))>2097152:
            details={'omitted':'domain details exceed2MiB','bytes':len(raw.encode('utf-8'))}
    except (TypeError,ValueError):
        details={'omitted':'domain details are not finite JSON','type':type(details).__name__}
    report=Path(job)/'rigging-workflow-failure.json'
    try:
        atomic_json(report,{'schema':'1.0','step_id':step['id'],'operation':step['operation'],
            'completed_steps':list(completed),'exception_type':type(exc).__name__,
            'domain_code':getattr(exc,'code',None),'message':str(exc),'details':details,
            'candidate_saved':False,'source_overwritten':False})
    except OSError:
        return Failure('WORKER_FAILED',f"Rigging step {step['id']} failed: {exc}; diagnosis could not be saved")
    return Failure('WORKER_FAILED',f"Rigging step {step['id']} ({step['operation']}) failed: {exc}; diagnosis: {report}")


def run(params,job):
    if implementation_sha256()!=params['implementation_sha256']:
        raise Failure('CONFLICT','Rigging product changed before worker import')
    sys.path.insert(0,str(PRODUCT.parent))
    import rigging_workflow
    rigging_workflow.register()
    from rigging_workflow.core import service,work_context,scene_time,domains
    execution=work_context.WorkContext(job)
    try:
        bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
        _dependencies(params)
        manifest=params['manifest']
        scene=bpy.data.scenes.get(manifest['scene'])
        if scene is None or manifest['view_layer'] not in scene.view_layers:
            raise Failure('NOT_FOUND','Explicit scene/view_layer missing')
        if bpy.context.window:
            bpy.context.window.scene=scene
            bpy.context.window.view_layer=scene.view_layers[manifest['view_layer']]
        if bpy.context.scene!=scene or bpy.context.view_layer.name!=manifest['view_layer']:
            raise Failure('CONFLICT','Cannot bind disk worker context')
        if manifest['steps'][0]['operation'] in {'character.create_work_copy','character.diagnose'}:
            for obj in bpy.context.selected_objects:
                obj.select_set(False)
            for name in manifest['source_objects']:
                obj=scene.objects.get(name)
                if not obj or obj.type!='MESH' or obj.name not in bpy.context.view_layer.objects:
                    raise Failure('NOT_FOUND','Missing selected source mesh: '+name)
                obj.select_set(True)
            bpy.context.view_layer.objects.active=scene.objects[manifest['active_body']]
        else:
            records=scene.rw_characters
            matches=[i for i,c in enumerate(records) if c.character_id==manifest['character_id']]
            if len(matches)!=1:
                raise Failure('CONFLICT','Character identity is not unique in scene')
            scene.rw_active_character=matches[0]
            if records[matches[0]].revision!=manifest['base_revision']:
                raise Failure('CONFLICT','Saved character revision differs')
            bpy.context.view_layer.objects.active=records[matches[0]].body
        results={}
        for step in manifest['steps']:
            op=step['operation']
            parameters=resolve_parameters(step,results)
            request={'schema':'1.0','operation':op,'parameters':parameters,'actor':'disk'}
            if op in {'character.create_work_copy','character.diagnose'}:
                parameters.update(source_objects=manifest['source_objects'],active_body=manifest['active_body'])
            else:
                record=service.current(bpy.context)
                character_id=record.character_id
                if 'character_from_step' in step:
                    expected=results[step['character_from_step']].get('character_id')
                    if expected!=character_id:
                        raise Failure('CONFLICT','Character binding differs from created result')
                elif manifest.get('character_id') and manifest['character_id']!=character_id:
                    raise Failure('CONFLICT','Plan character differs')
                state=service.dispatch({'schema':'1.0','operation':'character.inspect','character_id':character_id})
                request.update(character_id=character_id,expected_revision=state['revision'],expected_signature=state['signature'])
                if 'candidate_from_step' in step:
                    previous=results[step['candidate_from_step']]
                    if previous.get('status')!='candidate' or not previous.get('candidate_id'):
                        raise Failure('CONFLICT','Candidate binding is not a generated result')
                    parameters['candidate_id']=previous['candidate_id']
                bpy.context.view_layer.objects.active=record.rig or record.metarig or record.body
            try:
                with work_context.bind(execution), execution.step(step['id'],op), scene_time.owned_snapshot(bpy.context,job,params['file']):
                    if step.get('export_rebuild_packet'):
                        # Graph signatures include live ID pointers. Rebind the
                        # inspected immutable snapshot in this worker rather
                        # than reusing its author's process-local signature.
                        inspected=service.dispatch({'schema':'1.0','operation':'skeleton.rebuild.inspect',
                            'character_id':character_id,'parameters':{'settings':parameters.get('settings',{})},'actor':'disk'})
                        parameters.setdefault('settings',{})['expected_graph_signature']=inspected['graph_signature']
                    if step.get('export_hair_author'):
                        from rigging_workflow.hair import author_packet
                        author_before=author_packet.capture_before(bpy.context,record,parameters['settings'])
                    results[step['id']]=service.dispatch(request)
                    if step.get('export_hair_author'):
                        results[step['id']]['hair_author_packet']=author_packet.export_result(
                            bpy.context,record,author_before,results[step['id']],Path(job))
                    if step.get('export_skin_packet'):
                        from rigging_workflow.skin import scoped_packet as job_packet
                        token=results[step['id']].get('candidate_id')
                        handle=rigging_workflow.core._handles.get(token)
                        if handle is None or handle.get('_domain')!='skin':
                            raise Failure('CONFLICT','Skin export requires the current live Core candidate')
                        results[step['id']]['skin_packet']=job_packet.export_packet(bpy.context,record,handle,Path(job))
                    if step.get('export_hair_packet'):
                        from rigging_workflow.hair import job_packet
                        token=results[step['id']].get('candidate_id')
                        handle=rigging_workflow.core._handles.get(token)
                        if handle is None or handle.get('_domain')!='hair' or handle.get('subtype')!='bake':
                            raise Failure('CONFLICT','Hair export requires the current live Bake candidate')
                        packet=job_packet.export_packet(bpy.context,record,handle,Path(job))
                        # The opt-in task is numerical output only. Restore its
                        # snapshot writer before the normal disk save boundary.
                        current=service.dispatch({'schema':'1.0','operation':'character.inspect','character_id':character_id})
                        restored=service.dispatch({'schema':'1.0','operation':'candidate.discard','character_id':character_id,
                            'expected_revision':current['revision'],'expected_signature':current['signature'],
                            'parameters':{'candidate_id':token}})
                        results[step['id']].update(status=restored.get('status','restored'),
                            hair_packet=packet,worker_candidate_discarded=restored)
                    if step.get('export_rebuild_packet'):
                        from rigging_workflow.core.rebuild import job_packet
                        token=results[step['id']].get('candidate_id')
                        handle=domains._handles.get(token)
                        if handle is None or handle.get('_domain')!='rebuild':
                            raise Failure('CONFLICT','Rebuild export requires the current live Core candidate')
                        packet_base=Path(job)/'rebuild-packets'/step['id']
                        packet_base.mkdir(parents=True,exist_ok=False)
                        results[step['id']]['rebuild_packet']=job_packet.export_candidate(
                            bpy.context,record,handle,packet_base)
            except Exception as exc:
                raise _step_failure(job,step,exc,results) from exc
        candidate=Path(job)/'output-rigging-workflow.blend'
        if any(h.get('kind') in {'face-preview','hair-preview'} for h in rigging_workflow.core._handles.values()):
            raise Failure('CONFLICT','Disk plan has a temporary preview; apply or restore it before saving output')
        if candidate.exists():
            raise Failure('CONFLICT','Job candidate already exists')
        execution.checkpoint()
        execution.report('saving_output')
        bpy.ops.wm.save_as_mainfile(filepath=str(candidate),copy=True,relative_remap=True)
        execution.checkpoint()
        report={'schema':'1.0','state_source':'SAVED_DISK_V1','source':{'file':params['file'],'sha256':params['expected_sha256']},
                'implementation_sha256':params['implementation_sha256'],'steps':results,
                'candidate':{'file':str(candidate),'sha256':digest(candidate)},
                'source_overwritten':False,'live_session_modified':False,
                'dependencies':'declared external ID file dependencies checked; complex native simulation/cache migration is not implemented',
                'scope':'necessary development output; no automatic live application or complete product quality claim'}
        atomic_json(Path(job)/'rigging-workflow-report.json',report)
        execution.report('output_written')
        return report
    finally:
        rigging_workflow.unregister()
