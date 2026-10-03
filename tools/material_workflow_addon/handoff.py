# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent GUI snapshot bundles. No source save, background wait, or auto-import."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from .contract import WorkflowError, normalize_manifest, image_documents
from .batch_contract import normalize_manifest as normalize_batch
from . import job_bridge

def sha(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream,'sha256').hexdigest()

def write_json(path, value):
    path=Path(path); temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with temp.open('x',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temp,path)

def _ids():
    import bpy
    seen=set()
    for prop in bpy.data.bl_rna.properties:
        if prop.type!='COLLECTION': continue
        for item in getattr(bpy.data,prop.identifier):
            if isinstance(item,bpy.types.ID) and item.as_pointer() not in seen:
                seen.add(item.as_pointer()); yield item

def _session(context):
    import bpy
    return {'file':bpy.data.filepath,'dirty':bpy.data.is_dirty,'scene':context.scene.name,
            'view_layer':context.view_layer.name,'frame':context.scene.frame_current,
            'mode':context.mode,'active':context.view_layer.objects.active.name if context.view_layer.objects.active else None,
            'selected':sorted(o.name for o in context.selected_objects)}

def _manifest(value, mode):
    if mode=='BATCH': return normalize_batch(value)
    if mode!='SINGLE': raise WorkflowError('INVALID_REQUEST','Unknown handoff mode')
    result=normalize_manifest(value)
    if result['schema_version']!='1.1':
        raise WorkflowError('UNSUPPORTED','快照封包需要 v2；请先显式升级旧材质草稿')
    return result

def prepare(context, value, mode, output_root):
    """Synchronously seal a copy and static dependencies, then return without launch."""
    import bpy
    manifest=_manifest(value,mode)
    if context.mode!='OBJECT': raise WorkflowError('UNSUPPORTED','交接前请切换 Object Mode；不会自动改变编辑模式')
    expected={'scene':context.scene.name,'view_layer':context.view_layer.name,'frame':context.scene.frame_current}
    if manifest['context']!=expected: raise WorkflowError('INVALID_REQUEST','草稿场景/视图层/帧与当前上下文不同')
    if bpy.data.libraries or bpy.data.sounds or bpy.data.movieclips or bpy.data.cache_files or bpy.data.volumes:
        raise WorkflowError('UNSUPPORTED','当前快照入口支持本地静态材质工程；链接库/媒体/缓存需专门封包')
    root=Path(output_root).expanduser()
    if not root.is_absolute(): raise WorkflowError('INVALID_REQUEST','请选择持久快照根目录的绝对路径')
    root=root.resolve()
    images=[]
    for image in bpy.data.images:
        if image.type in ('RENDER_RESULT','COMPOSITING'): continue
        if image.is_dirty: raise WorkflowError('UNSUPPORTED','贴图像素尚未保存，请先保存贴图：'+image.name)
        if image.source not in ('FILE','GENERATED'): raise WorkflowError('UNSUPPORTED','不支持序列/UDIM图像：'+image.name)
        if image.source=='FILE' and not image.packed_file:
            images.append(image)
    fonts=[font for font in bpy.data.fonts if font.filepath!='<builtin>' and not font.packed_file]
    # Establish source hashes before native save; all bundle writes use a unique folder.
    before=_session(context); source_sha=sha(before['file']) if before['file'] and Path(before['file']).is_file() else None
    folder=root/('handoff-'+uuid.uuid4().hex); folder.mkdir(parents=True,exist_ok=False)
    (folder/'dependencies').mkdir()
    receipt={'handoff_version':'1.0','id':folder.name,'mode':mode,'state':'preparing',
             'folder':str(folder),'receipt':str(folder/'handoff.json'),'origin':before,
             'origin_sha256':source_sha,'source_saved':False,'job_ref':None}
    write_json(folder/'handoff.json',receipt)
    path_changes=[]; retained=[]; mapping={};resources={}
    try:
        def stage(path, expected_sha=None):
            path=str(Path(path).resolve()); key=os.path.normcase(path)
            if not Path(path).is_file(): raise WorkflowError('NOT_FOUND','缺少依赖：'+path)
            digest=sha(path)
            if expected_sha and digest!=expected_sha: raise WorkflowError('CONFLICT','依赖内容已改变：'+path)
            if key in mapping: return mapping[key]
            suffix=Path(path).suffix.lower()
            if not re.fullmatch(r'\.[a-z0-9]{1,12}',suffix): suffix='.data'
            dest=folder/'dependencies'/(digest+suffix)
            if not dest.exists():
                with open(path,'rb') as src,dest.open('xb') as dst: shutil.copyfileobj(src,dst)
            if sha(dest)!=digest or sha(path)!=digest: raise WorkflowError('CONFLICT','复制期间依赖发生变化：'+path)
            mapping[key]=str(dest);resources[str(dest)]=digest
            return str(dest)
        # Remap both live source references and declared, possibly unapplied textures.
        for item in [*images,*fonts]:
            staged=stage(bpy.path.abspath(item.filepath,library=item.library))
            path_changes.append((item,item.filepath,staged))
        manifests=[manifest] if mode=='SINGLE' else [{'layers':a['layers']} for a in manifest['assignments']]
        for spec in manifests:
            for image in image_documents(spec): image['file']=stage(image['file'],image['expected_sha256'])
        snapshot=folder/'snapshot.blend'
        # Save Copy operates synchronously on Blender's main thread. Only these
        # temporary serialization references/retention flags are changed; always restore.
        try:
            for item,raw,staged in path_changes: item.filepath='//dependencies/'+Path(staged).name
            for item in list(_ids()):
                if not item.library and not item.is_embedded_data and item.users==0 and not item.use_fake_user:
                    item.use_fake_user=True; retained.append(item)
            result=bpy.ops.wm.save_as_mainfile(filepath=str(snapshot),copy=True,relative_remap=False,check_existing=False)
            if result!={'FINISHED'}: raise WorkflowError('IO_ERROR','保存独立快照未完成')
        finally:
            restore_errors=[]
            for item in reversed(retained):
                try: item.use_fake_user=False
                except Exception as error: restore_errors.append(str(error))
            for item,raw,staged in reversed(path_changes):
                try: item.filepath=raw
                except Exception as error: restore_errors.append(str(error))
            if restore_errors: raise WorkflowError('VALIDATION_FAILED','会话引用恢复失败：'+str(restore_errors))
        after=_session(context)
        if any(before[k]!=after[k] for k in before if k!='dirty') or before['dirty'] and not after['dirty']:
            raise WorkflowError('VALIDATION_FAILED','快照保存改变了当前会话状态；未提交后台')
        if any(item.filepath!=raw for item,raw,_ in path_changes) or any(item.use_fake_user for item in retained):
            raise WorkflowError('VALIDATION_FAILED','快照临时引用未完整恢复')
        if source_sha and sha(before['file'])!=source_sha: raise WorkflowError('CONFLICT','源工程文件在交接期间发生变化')
        params={'file':str(snapshot),'expected_sha256':sha(snapshot),'manifest':manifest,
                'resources':[{'file':p,'expected_sha256':h} for p,h in sorted(resources.items())]}
        request={'schema_version':'1.0','command':'material.batch' if mode=='BATCH' else 'material.run','params':params}
        request_path=folder/'request.json';write_json(request_path,request)
        receipt.update(state='prepared',snapshot={'file':str(snapshot),'expected_sha256':params['expected_sha256']},
                       request={'file':str(request_path),'expected_sha256':sha(request_path)},
                       resources=params['resources'],retained_ids=[{'type':i.bl_rna.identifier,'name':i.name} for i in retained],
                       session_after=after,checks={'source_file_unchanged':True,'live_references_restored':True},
                       limitations=['Object Mode/local static dependencies; unsaved image pixels rejected',
                                    'Snapshot is retained independently; CLI performs reopen and render validation',
                                    'GUI state is not replaced with the background candidate'])
        write_json(folder/'handoff.json',receipt);return receipt
    except Exception as error:
        receipt.update(state='prepare_failed',error=str(error));write_json(folder/'handoff.json',receipt)
        error.handoff_receipt=str(folder/'handoff.json')
        raise

def load_receipt(path, verify_inputs=False):
    path=Path(path).resolve();receipt=json.loads(path.read_text(encoding='utf-8'))
    if receipt.get('handoff_version')!='1.0' or Path(receipt.get('folder','')).resolve()!=path.parent or receipt.get('id')!=path.parent.name:
        raise WorkflowError('INVALID_REQUEST','Invalid handoff receipt ownership')
    if receipt.get('state') in ('preparing','prepare_failed'):
        if receipt.get('job_ref'): raise WorkflowError('INVALID_REQUEST','Failed preparation cannot own a job')
        receipt['receipt']=str(path);return receipt
    for key in ('snapshot','request'):
        doc=receipt.get(key,{})
        if Path(doc.get('file','')).resolve().parent!=path.parent or not re.fullmatch('[0-9a-f]{64}',doc.get('expected_sha256','')):
            raise WorkflowError('INVALID_REQUEST','Receipt artifact outside owned directory')
        if (key=='request' or verify_inputs) and sha(doc['file'])!=doc['expected_sha256']: raise WorkflowError('CONFLICT','Receipt artifact SHA mismatch: '+key)
    request=json.loads(Path(receipt['request']['file']).read_text(encoding='utf-8'))
    if request.get('command')!=('material.batch' if receipt['mode']=='BATCH' else 'material.run') or any(request['params'].get(k)!=receipt['snapshot'][k] for k in ('file','expected_sha256')):
        raise WorkflowError('INVALID_REQUEST','Receipt request does not reference its own snapshot')
    ref=receipt.get('job_ref')
    if ref:
        request_sha=hashlib.sha256(json.dumps(request,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()
        if (Path(ref.get('jobs_dir','')).resolve()!=path.parent/'jobs'
                or Path(ref.get('request_file','')).resolve()!=Path(receipt['request']['file']).resolve()
                or ref.get('command')!=request['command'] or ref.get('submitted_request_sha256')!=request_sha):
            raise WorkflowError('INVALID_REQUEST','Job reference belongs to another handoff')
    receipt['receipt']=str(path)
    return receipt

def submit(receipt,config,timeout=300):
    receipt=load_receipt(receipt['receipt'],verify_inputs=True)
    if receipt['state'] not in ('prepared','submit_failed') or receipt.get('job_ref'):
        raise WorkflowError('CONFLICT','此快照已提交或提交状态不确定，请读取原作业状态')
    receipt.update(state='submitting',config=config,timeout_seconds=timeout);write_json(receipt['receipt'],receipt)
    try:
        result=job_bridge.submit(receipt['request']['file'],str(Path(receipt['folder'])/'jobs'),config,timeout=timeout)
        receipt.update(state='submitted',job_ref=result['job_ref'],submission=result)
    except Exception as error:
        # The CLI may have accepted before its response was lost. Never silently resubmit.
        receipt.update(state='submission_unknown',error=str(error));write_json(receipt['receipt'],receipt);raise
    write_json(receipt['receipt'],receipt);return receipt

def refresh(path):
    receipt=load_receipt(path)
    if not receipt.get('job_ref'): return receipt
    status=job_bridge.status(receipt['job_ref'],receipt['config'])
    if not status.get('ok'): raise job_bridge.BridgeError('无法读取作业状态',status)
    receipt['job_status']=status['data'];state=status['data']['state'];receipt['state']=state
    if state not in ('queued','running'):
        result=job_bridge.result(receipt['job_ref'],receipt['config'])
        receipt['result']=result
        if result.get('ok'):
            data=result.get('data') or {};receipt['workflow_status']=data.get('status')
            receipt['candidate']=data.get('candidate');receipt['sheet']=data.get('sheet')
    write_json(receipt['receipt'],receipt);return receipt

def cancel(path):
    receipt=load_receipt(path)
    if not receipt.get('job_ref'): raise WorkflowError('INVALID_REQUEST','未提交作业')
    response=job_bridge.cancel(receipt['job_ref'],receipt['config'])
    receipt['cancel_request']=response;write_json(receipt['receipt'],receipt);return receipt

def recover_submission(path):
    receipt=load_receipt(path)
    if receipt.get('job_ref'): return refresh(path)
    if receipt['state'] not in ('submitting','submission_unknown'):
        raise WorkflowError('INVALID_REQUEST','此记录不处于提交未知态')
    prior=[]
    for attempt in receipt.get('attempts',[]):
        previous=attempt['job_ref']
        if Path(previous['jobs_dir']).resolve()!=Path(receipt['folder'])/'jobs':
            raise WorkflowError('INVALID_REQUEST','Previous attempt belongs to another handoff')
        job_bridge._owned(previous,receipt['config']);prior.append(previous['job_id'])
    ref=job_bridge.recover(receipt['request']['file'],str(Path(receipt['folder'])/'jobs'),receipt['config'],exclude_job_ids=prior)
    receipt.update(job_ref=ref,state='submitted');write_json(path,receipt)
    return refresh(path)

def retry(path):
    receipt=refresh(path)
    if receipt['state'] not in ('succeeded','failed','cancelled','timed_out'):
        raise WorkflowError('CONFLICT','仅终态作业可以重试；先刷新或找回提交')
    request=json.loads(Path(receipt['request']['file']).read_text(encoding='utf-8'))
    request['params'].pop('resume',None)
    if receipt['mode']=='BATCH':
        report=Path(receipt['job_ref']['jobs_dir'])/receipt['job_ref']['job_id']/'batch-report.json'
        if report.is_file(): request['params']['resume']={'file':str(report),'expected_sha256':sha(report)}
    new_request=Path(receipt['folder'])/('request-'+uuid.uuid4().hex+'.json');write_json(new_request,request)
    receipt.setdefault('attempts',[]).append({'job_ref':receipt['job_ref'],'state':receipt['state'],'workflow_status':receipt.get('workflow_status'),'request':receipt['request']})
    for key in ('job_status','result','candidate','sheet','workflow_status','cancel_request','submission','error'):receipt.pop(key,None)
    receipt.update(state='prepared',job_ref=None,request={'file':str(new_request),'expected_sha256':sha(new_request)})
    write_json(path,receipt)
    return submit(receipt,receipt['config'],receipt.get('timeout_seconds',300))
