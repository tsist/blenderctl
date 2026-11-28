# SPDX-License-Identifier: GPL-3.0-or-later
"""Historical painted-parent inspection. Never follows original source/code paths."""
from pathlib import Path
import hashlib,ntpath,re
from protocol import Failure
from toon_image_inspect import _Held,_Check,_local,_digest,_need,_keys
from toon_image_public import canonical,implementation_sha256,plain
from toon_painted_image_contract import normalize_params,PROFILE
from toon_painted_image_evidence import split_params,expected_document,expected_from_document,cold_document
from painted_bundle_lease import PaintedWindowsBundleLease,PaintedLeaseLimits
from toon_image_backend import painted_image_receipt_reader as reader

def same(a,b,message):_need(canonical(a)==canonical(b),message)

def inspect_parent(params,inspector_job,*,stack,checkpoint,expected_implementation):
    _keys(params,('parent_result','implementation_sha256'));_keys(params['parent_result'],('file','expected_sha256'))
    implementation=_digest(expected_implementation)
    same(params['implementation_sha256'],implementation,'Inspect implementation differs')
    result_path=_local(params['parent_result']['file']);job=result_path.parent;own=_local(str(inspector_job))
    _need(result_path.name=='result.json' and re.fullmatch('[0-9a-f]{32}',job.name) is not None,'Original parent fixed path required','INVALID_REQUEST')
    _need(own.is_dir() and not own.is_relative_to(job) and not job.is_relative_to(own),'Inspect output overlaps original parent','INVALID_REQUEST')
    check=_Check(checkpoint);held=_Held(stack,check)
    try:return _inspect(params,own,job,result_path,_digest(params['parent_result']['expected_sha256']),held,check,stack,implementation)
    except BaseException:
        if check.error is not None:raise check.error
        raise

def _inspect(params,own,job,result_path,result_sha,held,check,stack,implementation):
    result=held.document(result_path,result_sha)
    _keys(result,('schema_version','cli_version','command','job_id','ok','data','error','artifacts'))
    _need(result['schema_version']=='1.0' and result['command']=='toon-image.run' and result['job_id']==job.name and result['ok'] is True and result['error'] is None,'Original painted parent is not successful')
    root=job/'image-run';bundle=root/'bundle';data=result['data']
    _keys(data,('status','profile','parent_accepted','parent_job_id','source_unchanged','source_mode','quality_accepted','acceptance','result_root','cold_report','qualification'))
    for key,value in dict(status='passed',profile=PROFILE,parent_accepted=True,parent_job_id=job.name,source_unchanged=True,
            source_mode='SAVED_DISK_V1',quality_accepted=False,result_root=str(bundle)).items():same(data[key],value,'Original parent data '+key+' differs')
    same(result['artifacts'],dict(job_directory=str(job),image_acceptance=str(job/'image-acceptance.json'),image_run_directory=str(root)),'Original parent fixed artifacts differ')
    status=held.document(job/'status.json')
    _need(status.get('job_id')==job.name and status.get('state')=='succeeded' and type(status.get('exit_code')) is int and status['exit_code']==0,'Original parent terminal differs')
    a=held.descriptor(data['acceptance'],job/'image-acceptance.json',True)
    _keys(a,('schema','profile','validation','parent_job_id','parent_terminal_required','source_mode','request_sha256','launch_identity_sha256',
        'implementation_sha256','public_context','host_context','export_request','numeric_recipe','expected','numeric_context','backend_request',
        'result_root','result_root_identity','receipt_sha256','cold_report','stages','source_unchanged','geometry_independently_recomputed',
        'quality_accepted','input_guard_scope','dependency_scope'))
    for key,value in dict(schema='toon-painted-image-parent-validation.v1',profile=PROFILE,validation='passed',parent_job_id=job.name,
            parent_terminal_required=True,source_mode='SAVED_DISK_V1',implementation_sha256=implementation,result_root=str(bundle),
            quality_accepted=False,source_unchanged=True,geometry_independently_recomputed=False).items():same(a[key],value,'Original acceptance '+key+' differs')
    same(data['cold_report'],a['cold_report'],'Original cold report descriptor differs')
    request=held.document(job/'request.json');_keys(request,('schema_version','command','params'))
    _need(request['schema_version']=='1.0' and request['command']=='toon-image.run','Original request command differs')
    original=plain(normalize_params(request['params']));same(original,request['params'],'Original request normalization differs')
    same(original['implementation_sha256'],implementation,'Original implementation differs')
    export,numeric_recipe=split_params(original)
    same(held.descriptor(a['export_request'],root/'ORIGINAL-EXPORT-REQUEST.json',True),export,'Original export declaration differs')
    same(held.descriptor(a['numeric_recipe'],root/'ORIGINAL-NUMERIC-RECIPE.json',True),numeric_recipe,'Original producer declaration differs')
    context=held.descriptor(a['host_context'],root/'HOST-CONTEXT.json',True)
    _keys(context,('schema','run_root','nonce','export_request_sha256','numeric_recipe_sha256'))
    same(context['schema'],'toon-painted-image-host-context.v1','Host context version differs');same(context['run_root'],str(root),'Host root differs')
    _need(type(context['nonce']) is str and re.fullmatch('[0-9a-f]{32}',context['nonce']) is not None,'Historical nonce differs')
    same(context['export_request_sha256'],hashlib.sha256(canonical(export)).hexdigest(),'Export context digest differs')
    same(context['numeric_recipe_sha256'],hashlib.sha256(canonical(numeric_recipe)).hexdigest(),'Producer context digest differs')
    launch=held.document(job/'launch.json');identity=launch.get('image_identity')
    _keys(identity,('schema','code','binaries','binary_scope'));same(identity['schema'],'toon-image-launch.v1','Launch version differs')
    code=identity['code'];_keys(code,('schema','sources'));same(code['schema'],'toon-image-code.v1','Code version differs')
    same(implementation_sha256(code),implementation,'Historical code declaration differs')
    request_sha=hashlib.sha256(canonical(request)).hexdigest();launch_sha=hashlib.sha256(canonical(identity)).hexdigest()
    same(launch.get('image_request_sha256'),request_sha,'Queued request digest differs');same(a['request_sha256'],request_sha,'Accepted request differs')
    same(a['launch_identity_sha256'],launch_sha,'Accepted launch differs')
    binaries=identity['binaries'];_need(type(binaries) is list and len(binaries)==2,'Historical binary declaration count differs')
    for row in binaries:_keys(row,('file','expected_sha256'));_local(row['file']);_digest(row['expected_sha256'])
    same(binaries[1]['file'],launch.get('blender'),'Historical Blender declaration differs')
    public=held.descriptor(a['public_context'],root/'PUBLIC-CONTEXT.json',True)
    same(public,dict(schema='toon-painted-image-public-context.v1',job_id=job.name,profile=PROFILE,run_root=str(root),request_sha256=request_sha,
        launch_identity_sha256=launch_sha,implementation_sha256=implementation,source=dict(file=original['file'],sha256=original['expected_sha256']),
        resources=original['resources'],host_context=a['host_context'],public_job=True,parent_terminal_required=True),'Historical public context differs')
    raw=held.descriptor(a['expected'],root/'EXPECTED.json');expected=expected_from_document(raw,check)
    same(expected_document(expected,check),raw,'Historical Expected roundtrip differs')
    numeric=held.descriptor(a['numeric_context'],root/'NUMERIC-CONTEXT.json')
    _keys(numeric,('schema','parent','export_request_sha256','numeric_recipe_sha256','capture_sha256','dependency_audit_sha256',
        'dependency_implementation_sha256','scene_export_sha256','input_sha256','backend_request_sha256','runtime','backend_sources'))
    same(numeric['schema'],'toon-painted-image-numeric-context.v1','Numeric context version differs');same(numeric['parent'],context,'Numeric parent differs')
    for key in ('export_request_sha256','numeric_recipe_sha256'):same(numeric[key],context[key],'Numeric declaration digest differs')
    cli={Path(name).name:pin for name,pin in code['sources'].items() if name.startswith('blenderctl/')}
    import json
    dependency_pin=hashlib.sha256(json.dumps(cli,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    same(numeric['dependency_implementation_sha256'],dependency_pin,'Historical dependency implementation differs')
    same(expected.host_context_sha256,a['numeric_context']['sha256'],'Expected numeric context digest differs')
    same(reader.thaw(expected.code_identity,check),numeric['backend_sources'],'Expected backend code differs')
    backend={k.removeprefix('toon_image_backend/'):v for k,v in code['sources'].items() if k.startswith('toon_image_backend/')}
    pins=reader.thaw(expected.code_identity,check)
    _need(type(pins) is dict and len(pins)==len(backend),'Complete historical backend pins required')
    suffixes={}
    for path,value in pins.items():
        _local(path);suffix=path.replace('\\','/').split('/toon_image_backend/',1)
        _need(len(suffix)==2 and suffix[1] in backend and suffix[1] not in suffixes,'Historical backend path scope differs')
        pin=value if expected.receipt_schema==reader.INTERSECTION else value['sha256']
        same(pin,backend[suffix[1]],'Historical backend pin differs');suffixes[suffix[1]]=True
    backend_request=held.descriptor(a['backend_request'],root/'backend-request.json')
    wanted=dict(numeric_recipe,input=dict(input_path=str(root/'export/geometry/toon-image-input.json'),input_sha256=numeric['input_sha256']))
    same(backend_request,wanted,'Historical backend request differs from original recipe')
    _need(expected.request_utf8==canonical(backend_request) and expected.request_sha256==a['backend_request']['sha256']==numeric['backend_request_sha256'],'Expected original request differs')
    capture=held.document(root/'export/capture.json',_digest(numeric['capture_sha256']))
    for key,value in dict(schema='toon-painted-image-saved-capture.v1',source_mode='SAVED_DISK_V1',declared_request=export,
            inputs_unchanged=True,source_saved=False,numerical_producer_run=False,parent_accepted=False,native_source_authenticated=False,
            runtime=numeric['runtime']).items():same(capture.get(key),value,'Historical capture '+key+' differs')
    for key,value in export['context'].items():same(capture.get('object_names' if key=='occluder_object_names' else key),value,'Historical capture context differs')
    same(capture['object_ids'],export['context']['occluder_object_names'],'Historical capture object order differs')
    same(capture['member_object_ids'],export['context']['member_object_names'],'Historical capture members differ')
    same(capture['input']['file'],expected.source_input_path,'Historical paint input role differs')
    same(capture['input']['expected_sha256'],expected.source_input_sha256,'Historical paint input SHA differs')
    same(capture['scene_export']['expected_sha256'],numeric['scene_export_sha256'],'Historical scene export differs')
    same(list(expected.image_shape),[capture['height'],capture['width'],4],'Historical dimensions differ')
    hashes={expected.source_input_path:expected.source_input_sha256}
    for i,row in enumerate(capture['npz']):
        same(row['object_id'],export['context']['occluder_object_names'][i],'Historical NPZ identity differs')
        same(row['file']['file'],str(root/'export/geometry'/f'export-{i:06d}.npz'),'Historical NPZ path differs')
        hashes[row['file']['file']]=row['file']['expected_sha256']
    same(reader.thaw(expected.input_hashes,check),hashes,'Complete historical source-input declarations differ')
    stages=a['stages'];_need(type(stages) is list and [s['mode'] for s in stages]==['identity','export','numeric'],'Historical stage set differs')
    for s in stages:
        _keys(s,('mode','status','lifecycle'));same(s['status'],'completed','Historical stage status differs')
        life=held.descriptor(s['lifecycle'],root/('stage-'+s['mode'])/'lifecycle.json')
        _need(life.get('status')=='completed' and type(life.get('root_returncode')) is int and life['root_returncode']==0 and
            life.get('tree_handle_closed') is True and life.get('owned_job_zero_active_observed') is True,'Historical stage cleanup differs')
    original_cold=held.descriptor(a['cold_report'],root/'COLD-REPORT.json',True)
    now=held.directory(bundle).identity();same(now,a['result_root_identity'],'Historical bundle identity differs')
    lease=stack.enter_context(PaintedWindowsBundleLease(str(bundle),expected_root_identity=(now['volume'],now['file_index']),
        forbidden_file_identities=frozenset(held.identities),reader_api=reader,limits=PaintedLeaseLimits(),checkpoint=check))
    cold=reader.verify_painted_image_v1(lease,expected_receipt_sha256=_digest(a['receipt_sha256']),expected_run=expected,
        limits=reader.PaintedReaderLimits(members=4096),checkpoint=check)
    _need(cold.parent_accepted is False and cold.quality_accepted is False and cold.geometry_recomputed is False and cold.source_current=='not_checked','Painted cold reader exceeded authority')
    fresh=cold_document(cold,check);same(fresh,original_cold,'Historical cold report differs from fresh verification')
    def recheck():
        check();held.recheck();lease.recheck();check()
    recheck();held.directory(own)
    def write(name,value):
        raw=canonical(value);_need(len(raw)<=8*1024**2,'Inspector output metadata cap','RESOURCE_LIMIT');path=own/name
        with path.open('xb') as stream:stream.write(raw)
        pin=hashlib.sha256(raw).hexdigest();held.file(path,pin,size=len(raw))
        return dict(file=str(path),sha256=pin,bytes=len(raw))
    cold_ref=write('parent-inspect-cold.json',fresh)
    report=dict(schema='toon-image-parent-inspection.v1',profile=PROFILE,validation='pinned_original_parent_and_bundle_verified',
        original_parent_job_id=job.name,parent_result=dict(file=str(result_path),expected_sha256=result_sha),implementation_sha256=implementation,
        nonce=context['nonce'],terminal_observation=dict(state='succeeded',exit_code=0,status_raw_sha256=held.files[ntpath.normcase(str(job/'status.json'))]['sha256']),
        binding_scope=dict(request='canonical_json_sha256',launch='image_identity_and_image_request_sha256_only',status='current_held_terminal_observation'),
        source_current='not_checked',historical_source_unchanged_recorded=True,parent_accepted=False,quality_accepted=False,geometry_recomputed=False,
        cold_report=cold_ref,original_acceptance=data['acceptance'],unverified=['current_source_resources_code_paths','geometry_or_raster_recomputation','display_and_aesthetics'])
    ref=write('parent-inspection.json',report);recheck()
    return dict(report,inspection_report=ref),recheck
