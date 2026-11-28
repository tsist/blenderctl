# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in CPU line host. Runner retains all guards through terminal publication.

The receipt proves a complete bound image bundle. It is deliberately not the
older per-cell mathematical oracle, nor a claim of commercial visual parity.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

from protocol import Failure
from toon_image_public import canonical, digest, implementation_sha256, source_files, TOOLS, plain
from toon_cpu_image_contract import PROFILE, normalize_params, split_params, RENDER_HARD
# inspect's pure normalizer intentionally does not import provider contracts.
# Establish the fixed installed sibling root before shared guarded IO imports.
if str(TOOLS) not in sys.path:sys.path.insert(0,str(TOOLS))
from toon_image_prepared_run import HeldFiles, local_path

DOC_CAP=8*1024**2
FILE_CAP=512*1024**2

def need(value,message,code='CONFLICT'):
    if not value:raise Failure(code,message)

def same(a,b,message):
    if type(b) in (set,frozenset):need(type(a) in (set,frozenset) and a==b,message)
    else:need(canonical(a)==canonical(b),message)
def sha_value(value):return hashlib.sha256(canonical(value)).hexdigest()
def keys(value,required):need(type(value) is dict and set(value)==set(required),'Exact fields required: '+','.join(required))

def _ref(path,held,expected=None,maximum=DOC_CAP):
    path=Path(path);row=held.file(path,expected or digest(path),maximum=maximum)
    return dict(file=str(path),sha256=row['sha256'],bytes=row['bytes'])

def _document(ref,path,held,maximum=DOC_CAP):
    keys(ref,('file','sha256','bytes'))
    need(ref['file']==str(path),'Descriptor escaped fixed role')
    need(type(ref['bytes']) is int and 0<=ref['bytes']<=maximum,'Descriptor extent','RESOURCE_LIMIT')
    row=held.file(path,ref['sha256'],maximum=maximum,size=ref['bytes'])
    return held.document(path,row['sha256'],maximum)

def _write(path,value,held):
    from toon_cpu_image_evidence import write_document
    ref=dict(write_document(path,value),file=str(path))
    held.file(path,ref['sha256'],maximum=DOC_CAP,size=ref['bytes'])
    return ref

def _guard_queue(request,job,launch,stack,checkpoint,inspect=False):
    sys.dont_write_bytecode=True
    cache=job/('cpu-inspect-empty-pycache' if inspect else 'cpu-empty-pycache')
    cache.mkdir(exist_ok=False);sys.pycache_prefix=str(cache)
    if str(TOOLS) not in sys.path:sys.path.insert(0,str(TOOLS))
    params=request['params'];identity=launch['image_identity'];code=identity['code']
    same(identity['schema'],'toon-image-inspect-launch.v2' if inspect else 'toon-image-launch.v2','Queued launch schema differs')
    same(code['schema'],'toon-image-code.v2','CPU code schema differs')
    same(implementation_sha256(code),params['implementation_sha256'],'Queued implementation differs')
    same(launch['image_request_sha256'],sha_value(request),'Queued request differs')
    held=HeldFiles(stack,checkpoint);paths=source_files(PROFILE)
    same(sorted(paths),sorted(code['sources']),'Code membership changed since queue')
    for name,path in paths.items():held.file(path,code['sources'][name],maximum=FILE_CAP if path.suffix=='.dll' else DOC_CAP)
    if inspect:
        binaries=[identity['python']]
        same(binaries[0]['file'],str(Path(sys.executable).resolve()),'Inspector Python changed')
    else:
        binaries=identity['binaries']
        same([r['file'] for r in binaries],[str(Path(sys.executable).resolve()),launch['blender']],'Executable selection changed')
        for row in [params,*params['resources']]:held.file(row['file'],row['expected_sha256'],maximum=FILE_CAP)
    for row in binaries:held.file(row['file'],row['expected_sha256'],maximum=FILE_CAP)
    for path,wanted in ((job/'request.json',request),(job/'launch.json',launch)):
        same(held.document(path,digest(path),DOC_CAP),wanted,'Queued document changed')
    return held,paths,code

def _provider_pin(code):
    pins={name.removeprefix('toon_image_cpu/'):pin for name,pin in code['sources'].items() if name.startswith('toon_image_cpu/')}
    need(bool(pins),'Provider code closure missing')
    return sha_value(dict(sorted(pins.items())))

def _capture(root,export_request,capture_ref,audit_ref,held,code,*,historical=False):
    """Validate protected saved-capture roles without invoking a line producer."""
    from toon_selected_image_prepared_run import _source_inputs,_audit,_descriptor,_export_members,EXPORT_HARD_LIMITS
    export=root/'export';geometry=export/'geometry';_export_members(export,held)
    capture=_document(capture_ref,export/'capture.json',held)
    context=export_request['context'];runtime=capture.get('runtime')
    need(type(runtime) is dict and set(runtime)=={'python','numpy','blender','blender_build'} and
        all(type(v) is str and 0<len(v)<=4096 for v in runtime.values()),'Capture runtime differs')
    for key,value in dict(schema='toon-painted-image-saved-capture.v1',source_mode='SAVED_DISK_V1',declared_request=export_request,
            inputs_unchanged=True,source_saved=False,numerical_producer_run=False,parent_accepted=False,native_source_authenticated=False,
            object_ids=context['occluder_object_names'],member_object_ids=context['member_object_names'],camera_type='ORTHO').items():
        same(capture.get(key),value,'Capture '+key+' differs')
    for key,value in context.items():same(capture.get('object_names' if key=='occluder_object_names' else key),value,'Capture context differs')
    if historical:
        wanted=[dict(file=export_request['file'],expected_sha256=export_request['expected_sha256']),*export_request['resources']]
        rows=[capture.get('source'),*capture.get('resources',[])]
        same(capture.get('verified_inputs_after'),rows,'Recorded input recheck differs')
        need(len(rows)==len(wanted),'Recorded input count differs');total=0
        for actual,want in zip(rows,wanted):
            keys(actual,('file','expected_sha256','bytes','volume','file_index','links','attributes'))
            same({k:actual[k] for k in want},want,'Recorded original input differs')
            local_path(actual['file'])
            need(all(type(actual[k]) is int and actual[k]>=0 for k in ('bytes','volume','file_index','links','attributes')) and
                actual['links']==1 and not actual['attributes']&0x400,'Recorded input metadata differs')
            total+=actual['bytes']
    else:wanted,total=_source_inputs(capture,export_request,held)
    audit=_document(audit_ref,export/'resources.json',held)
    cli={Path(name).name:pin for name,pin in code['sources'].items() if name.startswith('blenderctl/')}
    audit_pin=hashlib.sha256(json.dumps(cli,ensure_ascii=True,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    _audit(audit,wanted,total,context,runtime,audit_pin)
    _descriptor(capture.get('scene_export'),geometry/'EXPORT.json',held)
    scene_export_ref=_ref(geometry/'EXPORT.json',held,capture['scene_export']['expected_sha256'])
    exported=_document(scene_export_ref,geometry/'EXPORT.json',held)
    same(exported,capture.get('scene_export_record'),'Scene export record differs')
    for key,value in dict(schema='toon-selected-scene-export.v2',scene=context['scene'],view_layer=context['view_layer'],frame=context['frame'],
            object_ids=context['occluder_object_names'],source_saved=False,numerical_producer_run=False,parent_accepted=False,
            native_source_authenticated=False,concurrent_writer_safe=False,runtime={k:runtime[k] for k in ('blender','blender_build')}).items():
        same(exported.get(key),value,'EXPORT '+key+' differs')
    limits=dict(EXPORT_HARD_LIMITS);limits.update(export_request['export_limits'])
    same(exported.get('limits'),limits,'Export limits differ')
    input_path=geometry/'toon-image-input.json'
    input_row=_descriptor(capture.get('input'),input_path,held,maximum=4*1024**2)
    _descriptor(exported.get('input'),input_path,held,low=True,maximum=4*1024**2)
    document=held.document(input_path,input_row['sha256'],4*1024**2)
    same(document.get('schema'),'toon-image-input.v2','Paint input schema differs')
    same(document.get('frame'),context['frame'],'Input frame differs')
    same(document.get('camera'),capture.get('camera'),'Capture camera differs')
    same(document.get('camera'),exported.get('camera'),'Export camera differs')
    w,h=capture.get('width'),capture.get('height')
    need(type(w) is int and type(h) is int and 0<w<=min(2048,limits['max_dimension']) and 0<h<=min(2048,limits['max_dimension']) and
        w*h<=limits['max_pixels'],'Image dimensions exceed declared limits','RESOURCE_LIMIT')
    same([document['camera']['height'],document['camera']['width']],[h,w],'Input dimensions differ')
    rows=capture.get('npz');low=exported.get('objects');objects=document.get('objects');names=context['occluder_object_names']
    need(type(rows) is list and type(low) is list and type(objects) is list and len(rows)==len(low)==len(objects)==len(names),'Complete ordered capture required')
    members={'EXPORT.json','toon-image-input.json'};refs=[]
    for i,(name,row,item,obj) in enumerate(zip(names,rows,low,objects)):
        held.checkpoint();path=geometry/('export-%06d.npz'%i);members.add(path.name)
        same([row['object_id'],item['object_id'],obj['id'],obj['name']],[name]*4,'Captured object identity differs')
        actual=_descriptor(row['file'],path,held,maximum=FILE_CAP)
        _descriptor(item['file'],path,held,low=True,maximum=FILE_CAP)
        same(obj['file'],str(path),'Input NPZ role differs');same(obj['sha256'],actual['sha256'],'Input NPZ digest differs')
        for key,field in (('evaluated_matrix_world','matrix_world'),('material_culling','material_culling'),('paint','paint')):
            same(row[key],item[key],'Export NPZ declaration differs');same(obj[field],row[key],'Input NPZ declaration differs')
        same(row['arrays'],item['arrays'],'NPZ array declaration differs')
        refs.append(dict(file=str(path),sha256=actual['sha256'],bytes=actual['bytes']))
    same(held.flat_names(geometry),members,'Geometry inventory differs')
    return dict(capture=capture,input_ref=dict(file=str(input_path),sha256=input_row['sha256'],bytes=input_row['bytes']),
        npz_refs=refs,scene_export_ref=scene_export_ref,shape=[h,w,4])

def _stage(root,mode,extra,launch,held,paths,code,checkpoint):
    from toon_image_owned_stage import run_stage
    same(sorted(source_files(PROFILE)),sorted(paths),'Code membership changed before stage')
    stage=root/('stage-'+mode);stage.mkdir(exist_ok=False)
    cache=root/('empty-pycache-'+mode);cache.mkdir(exist_ok=False)
    profile=root/('profile-'+mode);profile.mkdir(exist_ok=False)
    env={k:v for k,v in os.environ.items() if not k.startswith('BLENDER_') and k not in ('PYTHONPATH','PYTHONHOME','PYTHONSTARTUP')}
    for field in ('CONFIG','SCRIPTS','EXTENSIONS','DATAFILES'):
        target=profile/field.lower();target.mkdir();env['BLENDER_USER_'+field]=str(target)
    env.update(BLENDER_USER_RESOURCES=str(profile),PYTHONIOENCODING='utf-8',PYTHONUNBUFFERED='1')
    binding=dict(schema='toon-cpu-image-stage-binding.v1',profile=PROFILE,mode=mode,run_root=str(root),stage_dir=str(stage),empty_pycache=str(cache),**extra)
    ref=_write(root/(mode+'-binding.json'),binding,held)
    script=Path(__file__).with_name('toon_cpu_image_stage_worker.py');python=Path(sys.executable).resolve()
    if mode=='export':
        command=(launch['blender'],'--background','--factory-startup','--disable-autoexec','--offline-mode','--threads','2',
            '--python-exit-code','23','--python',str(script),'--',ref['file'],ref['sha256'])
    else:command=(str(python),'-I','-B',str(script),'--',ref['file'],ref['sha256'])
    documents={str(path):code['sources'][name] for name,path in paths.items()}
    documents.update({row['file']:row['expected_sha256'] for row in launch['image_identity']['binaries']})
    documents[ref['file']]=ref['sha256']
    life=run_stage(stage,command=command,checkpoint=checkpoint,bootstrap_path=Path(__file__).with_name('bootstrap.py'),
        python_executable=python,env=env,expected_documents=tuple(dict(file=k,expected_sha256=v) for k,v in documents.items()))
    need(life['status']=='completed','CPU stage did not complete')
    life_ref=_ref(stage/'lifecycle.json',held)
    checkpoint()
    return stage,dict(mode=mode,status='completed',lifecycle=life_ref)

def run(request,job,launch,stack,checkpoint):
    job=Path(job);held,paths,code=_guard_queue(request,job,launch,stack,checkpoint)
    params=request['params'];export_request,numeric_recipe=split_params(params)
    root=job/'image-run';root.mkdir(exist_ok=False);held.directory(root)
    context=dict(schema='toon-cpu-image-public-context.v1',profile=PROFILE,job_id=job.name,nonce=uuid.uuid4().hex,run_root=str(root),
        request_sha256=sha_value(request),launch_identity_sha256=sha_value(launch['image_identity']),implementation_sha256=params['implementation_sha256'],
        source=dict(file=params['file'],sha256=params['expected_sha256']),resources=params['resources'],public_job=True,parent_terminal_required=True)
    context_ref=_write(root/'PUBLIC-CONTEXT.json',context,held)
    export_ref=_write(root/'ORIGINAL-EXPORT-REQUEST.json',export_request,held)
    recipe_ref=_write(root/'ORIGINAL-NUMERIC-RECIPE.json',numeric_recipe,held)
    stage,life_export=_stage(root,'export',dict(export_request=export_request),launch,held,paths,code,checkpoint)
    export_return_ref=_ref(stage/'EXPORT-RETURN.json',held)
    returned=_document(export_return_ref,stage/'EXPORT-RETURN.json',held)
    need(returned.get('schema')=='toon-painted-image-saved-export.v1' and returned.get('inputs_unchanged') is True,'Export return differs')
    capture_ref=_ref(root/'export/capture.json',held,returned['capture']['expected_sha256'])
    audit_ref=_ref(root/'export/resources.json',held,returned['dependency_audit']['expected_sha256'])
    captured=_capture(root,export_request,capture_ref,audit_ref,held,code)
    backend_request=dict(numeric_recipe,input=dict(input_path=captured['input_ref']['file'],input_sha256=captured['input_ref']['sha256']))
    need(len(canonical(backend_request))<=1048576,'Bound numerical request size exceeded','RESOURCE_LIMIT')
    request_ref=_write(root/'backend-request.json',backend_request,held)
    output_cap=numeric_recipe.get('budgets',{}).get('render',{}).get('max_output_bytes',RENDER_HARD['max_output_bytes'])
    expectation=dict(schema='toon-cpu-image-expected.v1',profile=PROFILE,job_id=job.name,nonce=context['nonce'],implementation_sha256=params['implementation_sha256'],
        provider_implementation_sha256=_provider_pin(code),public_context_ref=context_ref,request_ref=request_ref,capture_ref=capture_ref,
        dependency_audit_ref=audit_ref,scene_export_ref=captured['scene_export_ref'],input_ref=captured['input_ref'],npz_refs=captured['npz_refs'],
        shape=captured['shape'],dtype='<f4',origin='bottom_left',alpha='premultiplied',max_output_bytes=output_cap)
    need(not os.path.lexists(root/'bundle'),'Numerical output exists before expectation')
    expected_ref=_write(root/'EXPECTED.json',expectation,held)
    _write(root/'PRE-NUMERIC.json',dict(schema='toon-cpu-image-pre-numeric.v1',expectation_ref=expected_ref,request_ref=request_ref,
        public_context_ref=context_ref,output_absent=True),held)
    stage,life_numeric=_stage(root,'numeric',dict(expectation_ref=expected_ref,request_ref=request_ref),launch,held,paths,code,checkpoint)
    numeric_return_ref=_ref(stage/'NUMERIC-RETURN.json',held)
    numeric=_document(numeric_return_ref,stage/'NUMERIC-RETURN.json',held)
    keys(numeric,('ok','receipt'));need(numeric['ok'] is True,'Numerical stage failed')
    need(numeric['receipt']['file']==str(root/'bundle/receipt.json'),'Receipt role differs')
    from toon_cpu_image_evidence import verify_bundle
    cold=verify_bundle(root/'bundle',receipt_sha256=numeric['receipt']['sha256'],expectation_ref=expected_ref,expectation=expectation,held=held,checkpoint=checkpoint)
    cold_ref=_write(root/'COLD-REPORT.json',cold,held)
    acceptance=dict(schema='toon-cpu-image-parent-validation.v1',profile=PROFILE,validation='passed',parent_job_id=job.name,
        implementation_sha256=params['implementation_sha256'],request_sha256=context['request_sha256'],launch_identity_sha256=context['launch_identity_sha256'],
        public_context=context_ref,export_request=export_ref,numeric_recipe=recipe_ref,expected=expected_ref,
        result_root=str(root/'bundle'),result_root_identity=held.directory(root/'bundle').identity(),receipt_sha256=numeric['receipt']['sha256'],
        cold_report=cold_ref,stages=[life_export,life_numeric],stage_returns=dict(export=export_return_ref,numeric=numeric_return_ref),
        parent_terminal_required=True,source_unchanged=True,
        geometry_independently_recomputed=False,quality_accepted=False,validation_scope='bound complete NPY payloads and capture chain; no per-cell oracle')
    acceptance_ref=_write(job/'image-acceptance.json',acceptance,held)
    def recheck():
        checkpoint();same(sorted(source_files(PROFILE)),sorted(paths),'CPU code membership changed')
        same(held.flat_names(root/'bundle'),{'v.npy','h.npy','both.npy','off.npy','METADATA.json','receipt.json'},'Bundle membership changed')
        held.recheck();checkpoint()
    recheck()
    return dict(status='passed',profile=PROFILE,parent_accepted=True,parent_job_id=job.name,source_unchanged=True,
        source_mode='SAVED_DISK_V1',quality_accepted=False,acceptance=acceptance_ref,result_root=str(root/'bundle'),cold_report=cold_ref,
        qualification='complete bound pure-line bundle; requires this parent succeeded result/status; no visual or commercial parity acceptance'),recheck

def inspect_run(request,job,launch,stack,checkpoint):
    """Read the original fixed job evidence; never open its current source paths."""
    job=Path(job);held,paths,code=_guard_queue(request,job,launch,stack,checkpoint,inspect=True)
    params=request['params'];result_path=local_path(params['parent_result']['file']);parent=result_path.parent;root=parent/'image-run'
    need(re.fullmatch('[0-9a-f]{32}',parent.name) is not None and result_path.name=='result.json','Original parent fixed path differs')
    need(not job.is_relative_to(parent) and not parent.is_relative_to(job),'Inspector overlaps original parent')
    inspector_context=_write(job/'image-inspect-context.json',dict(schema='toon-cpu-image-inspector-context.v1',profile=PROFILE,
        job_id=job.name,request_sha256=sha_value(request),implementation_sha256=params['implementation_sha256'],parent_result=params['parent_result'],
        source_current='not_checked',current_blender_used=False),held)
    result=held.document(result_path,params['parent_result']['expected_sha256'],DOC_CAP)
    keys(result,('schema_version','cli_version','command','job_id','ok','data','error','artifacts'))
    need(result['schema_version']=='1.0' and result['command']=='toon-image.run' and result['job_id']==parent.name and result['ok'] is True and result['error'] is None,'Original parent was not successful')
    status=held.document(parent/'status.json',digest(parent/'status.json'),DOC_CAP)
    need(status.get('job_id')==parent.name and status.get('state')=='succeeded' and type(status.get('exit_code')) is int and status['exit_code']==0,'Original parent terminal required')
    data=result['data']
    keys(data,('status','profile','parent_accepted','parent_job_id','source_unchanged','source_mode','quality_accepted','acceptance','result_root','cold_report','qualification'))
    for key,value in dict(status='passed',profile=PROFILE,parent_accepted=True,parent_job_id=parent.name,source_unchanged=True,
            source_mode='SAVED_DISK_V1',quality_accepted=False,result_root=str(root/'bundle')).items():same(data[key],value,'Original parent '+key+' differs')
    same(result['artifacts'],dict(job_directory=str(parent),image_acceptance=str(parent/'image-acceptance.json'),image_run_directory=str(root)),'Original artifacts differ')
    a=_document(data['acceptance'],parent/'image-acceptance.json',held)
    keys(a,('schema','profile','validation','parent_job_id','implementation_sha256','request_sha256','launch_identity_sha256','public_context',
        'export_request','numeric_recipe','expected','result_root','result_root_identity','receipt_sha256','cold_report','stages',
        'stage_returns','parent_terminal_required','source_unchanged','geometry_independently_recomputed','quality_accepted','validation_scope'))
    for key,value in dict(schema='toon-cpu-image-parent-validation.v1',profile=PROFILE,validation='passed',parent_job_id=parent.name,
            implementation_sha256=params['implementation_sha256'],parent_terminal_required=True,source_unchanged=True,
            geometry_independently_recomputed=False,quality_accepted=False,result_root=str(root/'bundle')).items():same(a[key],value,'Original acceptance differs')
    original_request=held.document(parent/'request.json',digest(parent/'request.json'),DOC_CAP)
    keys(original_request,('schema_version','command','params'))
    same([original_request['schema_version'],original_request['command']],['1.0','toon-image.run'],'Original request command differs')
    normalized=plain(normalize_params(original_request['params']));same(normalized,original_request['params'],'Original normalization differs')
    same(normalized['implementation_sha256'],params['implementation_sha256'],'Original implementation differs')
    original_launch=held.document(parent/'launch.json',digest(parent/'launch.json'),DOC_CAP);identity=original_launch.get('image_identity')
    keys(identity,('schema','code','binaries','binary_scope'));same(identity['schema'],'toon-image-launch.v2','Historical launch schema differs')
    same(identity['code'],code,'Same implementation closure required')
    need(type(identity['binaries']) is list and len(identity['binaries'])==2,'Historical binaries differ')
    for row in identity['binaries']:
        keys(row,('file','expected_sha256'));local_path(row['file']);need(re.fullmatch('[0-9a-f]{64}',row['expected_sha256']) is not None,'Historical binary digest differs')
    same(identity['binaries'][1]['file'],original_launch.get('blender'),'Historical Blender selection differs')
    same(original_launch.get('image_request_sha256'),sha_value(original_request),'Historical queued request differs')
    same(a['request_sha256'],sha_value(original_request),'Accepted original request differs')
    same(a['launch_identity_sha256'],sha_value(identity),'Accepted launch differs')
    context=_document(a['public_context'],root/'PUBLIC-CONTEXT.json',held)
    keys(context,('schema','profile','job_id','nonce','run_root','request_sha256','launch_identity_sha256','implementation_sha256','source','resources','public_job','parent_terminal_required'))
    need(type(context['nonce']) is str and re.fullmatch('[0-9a-f]{32}',context['nonce']) is not None,'Historical nonce differs')
    same(context,dict(schema='toon-cpu-image-public-context.v1',profile=PROFILE,job_id=parent.name,nonce=context['nonce'],run_root=str(root),
        request_sha256=sha_value(original_request),launch_identity_sha256=sha_value(identity),implementation_sha256=params['implementation_sha256'],
        source=dict(file=normalized['file'],sha256=normalized['expected_sha256']),resources=normalized['resources'],public_job=True,parent_terminal_required=True),'Historical context differs')
    export,recipe=split_params(normalized)
    same(_document(a['export_request'],root/'ORIGINAL-EXPORT-REQUEST.json',held),export,'Historical export declaration differs')
    same(_document(a['numeric_recipe'],root/'ORIGINAL-NUMERIC-RECIPE.json',held),recipe,'Historical producer declaration differs')
    expected=_document(a['expected'],root/'EXPECTED.json',held)
    captured=_capture(root,export,expected['capture_ref'],expected['dependency_audit_ref'],held,code,historical=True)
    keys(a['stage_returns'],('export','numeric'))
    returned=_document(a['stage_returns']['export'],root/'stage-export/EXPORT-RETURN.json',held)
    same(returned.get('capture'),dict(file=expected['capture_ref']['file'],expected_sha256=expected['capture_ref']['sha256'],bytes=expected['capture_ref']['bytes']),'Historical export return capture differs')
    numeric=_document(a['stage_returns']['numeric'],root/'stage-numeric/NUMERIC-RETURN.json',held)
    keys(numeric,('ok','receipt'));need(numeric['ok'] is True,'Historical numeric return failed')
    same(numeric['receipt']['file'],str(root/'bundle/receipt.json'),'Historical numeric receipt role differs')
    same(numeric['receipt']['sha256'],a['receipt_sha256'],'Historical numeric receipt digest differs')
    backend=dict(recipe,input=dict(input_path=captured['input_ref']['file'],input_sha256=captured['input_ref']['sha256']))
    same(_document(expected['request_ref'],root/'backend-request.json',held),backend,'Historical numerical request differs')
    wanted=dict(schema='toon-cpu-image-expected.v1',profile=PROFILE,job_id=parent.name,nonce=context['nonce'],implementation_sha256=params['implementation_sha256'],
        provider_implementation_sha256=_provider_pin(code),public_context_ref=a['public_context'],request_ref=expected['request_ref'],
        capture_ref=expected['capture_ref'],dependency_audit_ref=expected['dependency_audit_ref'],scene_export_ref=captured['scene_export_ref'],
        input_ref=captured['input_ref'],npz_refs=captured['npz_refs'],shape=captured['shape'],dtype='<f4',origin='bottom_left',alpha='premultiplied',
        max_output_bytes=recipe.get('budgets',{}).get('render',{}).get('max_output_bytes',RENDER_HARD['max_output_bytes']))
    same(expected,wanted,'Historical expectation differs from original source declarations')
    pre=held.document(root/'PRE-NUMERIC.json',digest(root/'PRE-NUMERIC.json'),DOC_CAP)
    same(pre,dict(schema='toon-cpu-image-pre-numeric.v1',expectation_ref=a['expected'],request_ref=expected['request_ref'],public_context_ref=a['public_context'],output_absent=True),'Historical pre-numeric seal differs')
    same([s['mode'] for s in a['stages']],['export','numeric'],'Historical stage set differs')
    for row in a['stages']:
        keys(row,('mode','status','lifecycle'));same(row['status'],'completed','Historical stage status differs')
        life=_document(row['lifecycle'],root/('stage-'+row['mode'])/'lifecycle.json',held)
        need(life.get('status')=='completed' and type(life.get('root_returncode')) is int and life['root_returncode']==0 and
            life.get('tree_handle_closed') is True and life.get('owned_job_zero_active_observed') is True and life.get('source_recheck_passed') is True,'Historical stage cleanup differs')
    same(held.directory(root/'bundle').identity(),a['result_root_identity'],'Historical bundle identity differs')
    from toon_cpu_image_evidence import verify_bundle
    cold=verify_bundle(root/'bundle',receipt_sha256=a['receipt_sha256'],expectation_ref=a['expected'],expectation=expected,held=held,checkpoint=checkpoint)
    same(cold,_document(a['cold_report'],root/'COLD-REPORT.json',held),'Fresh verification differs from original cold report')
    same(a['cold_report'],data['cold_report'],'Parent cold descriptor differs')
    cold_ref=_write(job/'parent-inspect-cold.json',cold,held)
    report=dict(schema='toon-cpu-image-parent-inspection.v1',profile=PROFILE,validation='pinned_original_parent_and_complete_bundle_verified',
        original_parent_job_id=parent.name,parent_result=params['parent_result'],implementation_sha256=params['implementation_sha256'],
        source_current='not_checked',parent_accepted=False,quality_accepted=False,geometry_recomputed=False,cold_report=cold_ref,
        original_acceptance=data['acceptance'],historical_source_unchanged_recorded=True,nonce=context['nonce'])
    ref=_write(job/'parent-inspection.json',report,held)
    def recheck():
        checkpoint();same(sorted(source_files(PROFILE)),sorted(paths),'Inspector code membership changed')
        same(held.flat_names(root/'bundle'),{'v.npy','h.npy','both.npy','off.npy','METADATA.json','receipt.json'},'Historical bundle membership changed')
        held.recheck();checkpoint()
    recheck()
    return dict(report,status='passed',inspection_verified=True,inspector_job_id=job.name,inspection_report=ref,
        inspector_context=inspector_context,inspector_terminal_required=True),recheck
