# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only pinned original-parent inspection; runner owns this job's terminal.

Historical source/resource/code path declarations are never followed. No producer,
Blender, current-source check or new acceptance authority is invoked here.
"""
from dataclasses import asdict,fields
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import math
import ntpath
import re

from filesystem import FileGuard,kernel
import directory_handles
from protocol import Failure
from toon_image_public import canonical,implementation_sha256,normalize
from toon_image_bundle_lease import WindowsBundleLease
from toon_image_backend import intersection_image_receipt_reader as reader

_DOC_CAP=8*1024**2
_INNER=frozenset(('intersections/image_pipeline.py','intersections/paged_candidates.py','intersections/certifier_f64.py',
 'intersections/policy_facts.py','intersections/policy_rules.py','intersections/screen_index.py','intersections/curtain_filter_f64.py',
 'intersections/geometry_exact.py','intersections/numeric_f64.py','intersections/camera_exact.py','intersections/camera_sections.py',
 'intersections/relations.py','intersections/exact_utils.py','intersections/visibility_kernel.py','intersections/prepared_sections.py','raster.py'))

def _need(ok,message,code='CONFLICT'):
    if not ok:raise Failure(code,message)

def _keys(value,required,optional=()):
    _need(type(value) is dict and set(required)<=set(value)<=set(required)|set(optional),'Unknown or missing document fields','VALIDATION_FAILED')

def _digest(value):
    _need(type(value) is str and re.fullmatch('[0-9a-f]{64}',value) is not None,'Invalid digest','INVALID_REQUEST')
    return value

def _local(value):
    _need(type(value) is str and value,'Absolute native local path required','INVALID_REQUEST')
    try:value.encode('utf-8',errors='strict')
    except UnicodeEncodeError:raise Failure('INVALID_REQUEST','Strict UTF-8 path required')
    text=value.replace('/','\\')
    _need(re.match(r'^[A-Za-z]:\\',text) is not None and ':' not in text[2:],'Local path without UNC/device/ADS required','INVALID_REQUEST')
    parts=text[3:].split('\\')
    _need(all(p and p not in ('.','..') and not p.endswith((' ','.')) and
        not any(ord(c)<32 or ord(c)==127 or c in '<>:"|?*' for c in p) for p in parts),'Noncanonical path component','INVALID_REQUEST')
    return Path(text)

def _json(data):
    def pairs(items):
        out={}
        for k,v in items:
            _need(k not in out,'Duplicate JSON key','VALIDATION_FAILED');out[k]=v
        return out
    def constant(_):raise Failure('VALIDATION_FAILED','Nonfinite JSON number')
    try:value=json.loads(data.decode('utf-8'),object_pairs_hook=pairs,parse_constant=constant)
    except (ValueError,UnicodeError) as error:raise Failure('VALIDATION_FAILED','Invalid UTF-8 JSON') from error
    todo=[(value,0)];count=0
    while todo:
        item,depth=todo.pop();count+=1
        _need(depth<=64 and count<=1048576,'JSON metadata extent exceeded','RESOURCE_LIMIT')
        if type(item) is dict:todo.extend((x,depth+1) for kv in item.items() for x in kv)
        elif type(item) is list:todo.extend((x,depth+1) for x in item)
        elif type(item) is str:
            try:item.encode('utf-8',errors='strict')
            except UnicodeEncodeError:raise Failure('VALIDATION_FAILED','JSON string is not Unicode scalar data')
        elif type(item) is float:_need(math.isfinite(item),'Nonfinite JSON value','VALIDATION_FAILED')
    return value

class _Check:
    def __init__(self,fn):self.fn=fn;self.error=None
    def __call__(self):
        self.error=None
        try:self.fn()
        except BaseException as error:self.error=error;raise

class _Held:
    def __init__(self,stack,check):self.stack=stack;self.check=check;self.dirs={};self.files={};self.identities=set()
    def directory(self,path):
        for p in (*reversed(path.parents),path):
            key=ntpath.normcase(str(p))
            if key in self.dirs:continue
            self.check();g=self.stack.enter_context(FileGuard(p,directory=True));info=g._information()
            _need(info.attributes&0x10 and not info.attributes&0x400,'Ordinary directory required')
            self.dirs[key]=(p,g,g.identity())
        return self.dirs[ntpath.normcase(str(path))][1]
    def file(self,path,expected=None,maximum=_DOC_CAP,size=None):
        _need(path.is_absolute(),'Fixed absolute role required')
        key=ntpath.normcase(str(path))
        if key in self.files:
            row=self.files[key]
            _need((expected is None or row['sha256']==expected) and (size is None or row['bytes']==size) and row['bytes']<=maximum,'Conflicting fixed descriptor')
            return row
        self.directory(path.parent);self.check();g=self.stack.enter_context(FileGuard(path));i=g._information()
        identity=(i.volume,(i.index_high<<32)|i.index_low);n=(i.size_high<<32)|i.size_low
        _need(not i.attributes&(0x400|0x10) and i.links==1 and identity not in self.identities,'Input identity alias or nonregular role')
        _need(n<=maximum,'Fixed role byte cap exceeded','RESOURCE_LIMIT')
        _need(size is None or n==size,'Fixed role size differs')
        actual=g.sha256(self.check)
        _need(expected is None or actual==_digest(expected),'Fixed role SHA differs: '+str(path))
        row=dict(file=str(path),sha256=actual,bytes=n,identity=identity,guard=g)
        self.files[key]=row;self.identities.add(identity);return row
    def document(self,path,expected=None,size=None):
        row=self.file(path,expected,size=size);data=b''.join(row['guard'].chunks(self.check))
        _need(len(data)==row['bytes'] and hashlib.sha256(data).hexdigest()==row['sha256'],'Held JSON changed')
        return _json(data)
    def descriptor(self,value,path,require_size=False):
        _keys(value,('file','sha256'),('bytes',))
        _need(value['file']==str(path),'Descriptor is outside its fixed job role')
        expected=_digest(value['sha256']);size=value.get('bytes')
        _need(not require_size or size is not None,'Size descriptor missing')
        _need(size is None or type(size) is int and 0<=size<=_DOC_CAP,'Invalid descriptor size')
        return self.document(path,expected,size)
    def flat(self,path):
        guard=self.directory(path);h=directory_handles.open_root(path);names=set();marker=_Check(self.check)
        try:
            info=FileGuard._information(SimpleNamespace(handle=h,k=kernel()))
            _need((info.volume,(info.index_high<<32)|info.index_low)==self._identity(guard),'Snapshot enumeration handle identity differs')
            for name,attrs,size in directory_handles.entries(h,marker):
                self.check();_need(not attrs&(0x400|0x10),'Snapshot contains directory/reparse')
                _need(name not in names,'Duplicate snapshot member');names.add(name)
                _need(len(names)<=3074,'Snapshot entry limit exceeded','RESOURCE_LIMIT')
            return names
        except BaseException:
            if marker.error is not None:raise marker.error
            raise
        finally:kernel().CloseHandle(h)
    @staticmethod
    def _identity(guard):
        i=guard.identity();return (i['volume'],i['file_index'])
    def recheck(self):
        for path,g,identity in self.dirs.values():
            self.check();_need(g.identity()==identity,'Held directory identity changed')
            with FileGuard(path,directory=True) as current:_need(current.identity()==identity,'Directory path identity changed')
        for row in self.files.values():
            self.check();g=row['guard'];i=g._information()
            _need(i.links==1 and ((i.size_high<<32)|i.size_low)==row['bytes'] and self._identity(g)==row['identity'],'Held role metadata changed')
            _need(g.sha256(self.check)==row['sha256'],'Held role content changed')
            with FileGuard(row['file']) as current:_need(self._identity(current)==row['identity'],'Fixed role path identity changed')

def _expected(raw):
    wanted={f.name for f in fields(reader.ExpectedRun)};_keys(raw,wanted)
    raw=dict(raw)
    for key in ('policy_canonical_json','style_canonical_json'):
        _need(type(raw[key]) is str,'Expected JSON bytes require their original UTF-8 serialization');raw[key]=raw[key].encode('utf-8')
    for key in ('producer_outer_code_sha256','producer_inner_code_sha256','recorded_input_locks'):
        _need(type(raw[key]) is list,'Expected immutable sequence declaration required');raw[key]=tuple(tuple(x) for x in raw[key])
    _need(type(raw['image_shape']) is list,'Expected image shape required');raw['image_shape']=tuple(raw['image_shape'])
    return reader.ExpectedRun(**raw)

def _same(a,b):return canonical(a)==canonical(b)

def inspect_parent(params,inspector_job,*,stack,checkpoint,expected_implementation):
    """Return data,recheck; caller's stack remains live through runner publication."""
    _keys(params,('parent_result','implementation_sha256'));_keys(params['parent_result'],('file','expected_sha256'))
    _need(_digest(params['implementation_sha256'])==_digest(expected_implementation),'Inspect implementation differs','CONFLICT')
    result_path=_local(params['parent_result']['file']);result_sha=_digest(params['parent_result']['expected_sha256'])
    job=result_path.parent;_need(result_path.name=='result.json' and re.fullmatch('[0-9a-f]{32}',job.name) is not None,'Pinned parent result must be a fixed 32hex job/result.json','INVALID_REQUEST')
    own=_local(str(inspector_job));_need(own.is_dir() and not own.is_relative_to(job) and not job.is_relative_to(own),'Inspector output overlaps original parent job','INVALID_REQUEST')
    check=_Check(checkpoint);held=_Held(stack,check)
    try:return _inspect(params,own,job,result_path,result_sha,held,check,stack,expected_implementation)
    except BaseException:
        if check.error is not None:raise check.error
        raise

def _inspect(params,own,job,result_path,result_sha,held,check,stack,implementation):
    check();result=held.document(result_path,result_sha)
    _keys(result,('schema_version','cli_version','command','job_id','ok','data','error','artifacts'))
    _need(result['schema_version']=='1.0' and result['command']=='toon-image.run' and result['job_id']==job.name and result['ok'] is True and result['error'] is None,'Original parent is not a successful image run')
    data=result['data'];_need(type(data) is dict and data.get('status')=='passed' and data.get('parent_accepted') is True and data.get('parent_job_id')==job.name and data.get('source_mode')=='SAVED_DISK_V1' and data.get('quality_accepted') is False,'Original result has no qualifying pinned parent acceptance')
    if data.get('profile') == 'painted-mesh-image.v1':
        from toon_painted_image_inspect import inspect_parent as inspect_painted
        return inspect_painted(params, own, stack=stack, checkpoint=check, expected_implementation=implementation)
    if data.get('profile') == 'selected-mesh-image.v1':
        from toon_selected_image_inspect import inspect_parent as inspect_selected
        return inspect_selected(params, own, stack=stack, checkpoint=check, expected_implementation=implementation)
    root=job/'image-run';bundle=root/'result';snapshot=root/'export'/'snapshot'
    _need(result['artifacts']==dict(job_directory=str(job),image_acceptance=str(job/'image-acceptance.json'),image_run_directory=str(root)),'Original result path roles differ')
    _need(data.get('result_root')==str(bundle),'Original bundle role differs')
    status=held.document(job/'status.json')
    _need(status.get('job_id')==job.name and status.get('state')=='succeeded' and type(status.get('exit_code')) is int and status['exit_code']==0,'Original current status is not succeeded/exit0')
    acceptance=held.descriptor(data['acceptance'],job/'image-acceptance.json',True)
    _need(acceptance.get('schema')=='toon-image-parent-validation.v1' and acceptance.get('validation')=='passed' and acceptance.get('parent_job_id')==job.name and acceptance.get('parent_terminal_required') is True and acceptance.get('source_mode')=='SAVED_DISK_V1' and acceptance.get('quality_accepted') is False,'Original acceptance shape/identity differs')
    _need(acceptance.get('implementation_sha256')==implementation and acceptance.get('result_root')==str(bundle),'Original implementation or bundle role differs')
    _need(_same(data['cold_report'],acceptance['cold_report']),'Original cold report descriptors differ')
    context=held.descriptor(acceptance['host_context'],root/'HOST-CONTEXT.json',True)
    _need(context.get('schema')=='toon-image-host-context.v1' and context.get('job_id')==job.name and context.get('run_root')==str(root) and context.get('public_job') is True and type(context.get('nonce')) is str and re.fullmatch('[0-9a-f]{32}',context['nonce']) is not None,'Original host job/root/nonce differs')
    request=held.document(job/'request.json');_keys(request,('schema_version','command','params'))
    _need(request['schema_version']=='1.0' and request['command']=='toon-image.run','Original request command differs')
    original=normalize(request['params']);_need(_same(original,request['params']),'Original request changed during normalization')
    request_sha=hashlib.sha256(canonical(request)).hexdigest()
    _need(context.get('request_sha256')==acceptance.get('request_sha256')==request_sha and context.get('params_sha256')==hashlib.sha256(canonical(original)).hexdigest(),'Original canonical request/params binding differs')
    _need(original['implementation_sha256']==context.get('implementation_sha256')==implementation,'Original request implementation differs')
    _need(context.get('source')==dict(file=original['file'],sha256=original['expected_sha256']) and context.get('resources')==original['resources'],'Original historical input declarations differ')
    launch=held.document(job/'launch.json');identity=launch.get('image_identity')
    _need(type(identity) is dict and identity.get('schema')=='toon-image-launch.v1' and type(identity.get('code')) is dict and identity['code'].get('schema')=='toon-image-code.v1','Original launch identity schema differs')
    _need(implementation_sha256(identity['code'])==implementation and launch.get('image_request_sha256')==request_sha,'Original launch code/request binding differs')
    launch_sha=hashlib.sha256(canonical(identity)).hexdigest()
    _need(launch_sha==context.get('launch_identity_sha256')==acceptance.get('launch_identity_sha256'),'Original launch image_identity digest differs')
    binaries=identity.get('binaries');_need(type(binaries) is list and len(binaries)==2,'Original executable declarations differ')
    for b in binaries:_keys(b,('file','expected_sha256'));_local(b['file']);_digest(b['expected_sha256'])
    _need(binaries[1]['file']==launch.get('blender'),'Original selected Blender declaration differs')
    sources=identity['code'].get('sources');_need(type(sources) is dict and len(sources)<=4096,'Original code inventory differs')
    backend={name.removeprefix('toon_image_backend/'):h for name,h in sources.items() if name.startswith('toon_image_backend/')}
    for h in sources.values():_digest(h)
    # The presealed expectation is consumed before looking at any result receipt.
    expected_raw=held.descriptor(acceptance['expected'],root/'EXPECTED.json');expected=_expected(expected_raw)
    numeric=held.descriptor(acceptance['numeric_context'],root/'NUMERIC-CONTEXT.json')
    backend_request=held.descriptor(acceptance['backend_request'],root/'backend-request.json')
    numeric_sha=acceptance['numeric_context']['sha256'];backend_sha=acceptance['backend_request']['sha256']
    _need(numeric.get('schema')=='toon-image-numeric-context.v1' and _same(numeric.get('parent'),context) and numeric.get('producer_sources')==backend,'Numeric parent nonce or producer inventory differs')
    _need(expected.host_context_sha256==numeric_sha and numeric.get('backend_request_sha256')==expected.backend_request_sha256==backend_sha,'Presealed context/request digests differ')
    _need(expected.recorded_request_path==str(root/'backend-request.json') and expected.recorded_snapshot_manifest_path==str(snapshot/'manifest.json'),'Expected historical roles are outside this parent job')
    _need(numeric.get('snapshot_id')==expected.snapshot_id and numeric.get('snapshot_manifest_sha256')==expected.snapshot_manifest_sha256,'Expected snapshot declarations differ')
    _need(expected.runtime_python==numeric['runtime']['python'] and expected.runtime_numpy==numeric['runtime']['numpy'],'Expected runtime differs from sealed runtime')
    _need(backend_request.get('schema')=='toon-intersection-image-request.v1' and backend_request.get('snapshot')==dict(manifest_path=str(snapshot/'manifest.json'),manifest_sha256=expected.snapshot_manifest_sha256),'Backend request snapshot role differs')
    recipe=original['recipe'];policy=dict(recipe['policy'],member_object_ids=recipe['member_object_names'])
    _need(_same(backend_request.get('policy'),policy) and _same(backend_request.get('style'),recipe['style']) and _same(backend_request.get('budgets'),recipe['budgets']),'Original backend policy/style/budget differs')
    _need(expected.policy_canonical_json==canonical(policy) and expected.style_canonical_json==canonical(recipe['style']),'Expected policy/style bytes differ')
    camera=backend_request['camera_exact'];camera_hash=hashlib.sha256(json.dumps(camera,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    _need(expected.camera_id==camera_hash and expected.image_shape==(camera['height'],camera['width'],4),'Expected camera identity/shape differs')
    outer={k.replace('\\','/'):v for k,v in expected.producer_outer_code_sha256}
    wanted_outer={k:v for k,v in backend.items() if k in ('intersection_image_runner.py','raster.py') or re.fullmatch(r'intersections/[A-Za-z_][A-Za-z_0-9]*\.py',k)}
    _need(outer==wanted_outer,'Expected outer producer closure differs from sealed launch')
    inner={}
    for path,h in expected.producer_inner_code_sha256:
        normalized=path.replace('\\','/');matches=[name for name in _INNER if normalized.endswith('/'+name)]
        _need(len(matches)==1 and matches[0] not in inner,'Expected inner producer path relation differs');inner[matches[0]]=h
    _need(inner=={name:backend[name] for name in _INNER},'Expected inner closure differs from sealed launch')
    capture=held.document(root/'export'/'capture.json',_digest(numeric['capture_sha256']))
    _need(capture.get('schema')=='toon-image-saved-capture.v1' and capture.get('declared_recipe')==recipe and capture.get('runtime')==numeric['runtime'],'Pinned capture recipe/runtime differs')
    captured=[capture['source'],*capture['resources']]
    # Saved export records native canonical paths; request/context retain caller text.
    # Match preparation's lexical spelling only, without opening current inputs.
    wanted_capture=[dict(file=str(_local(x['file'])),expected_sha256=x['expected_sha256'])
        for x in [{'file':original['file'],'expected_sha256':original['expected_sha256']},*original['resources']]]
    _need(capture.get('verified_inputs_after')==captured and [{k:x[k] for k in ('file','expected_sha256')} for x in captured]==wanted_capture,'Pinned historical input capture differs')
    _need(capture.get('parent_accepted') is False and capture.get('source_saved') is False and capture.get('numerical_producer_run') is False,'Capture authority differs')
    audit=held.document(root/'export'/'resources.json',_digest(numeric['dependency_audit_sha256']))
    _need(audit.get('implementation_sha256')==numeric.get('dependency_implementation_sha256') and audit.get('closure')=='closed' and audit.get('issues')==[],'Pinned audit declaration differs')
    exported=capture['snapshot'];_need(exported['path']==str(snapshot/'manifest.json') and exported['receipt_path']==str(snapshot/'receipt.json') and exported['sha256']==expected.snapshot_manifest_sha256 and exported['snapshot_id']==expected.snapshot_id,'Capture snapshot role differs')
    manifest=held.document(snapshot/'manifest.json',expected.snapshot_manifest_sha256)
    _need(manifest.get('snapshot_id')==expected.snapshot_id and manifest.get('schema')=='toon-world-triangle-snapshot.arrays.f64.v1','Snapshot identity/schema differs')
    receipt=held.document(snapshot/'receipt.json',_digest(exported['receipt_sha256']))
    _need(receipt.get('snapshot_id')==expected.snapshot_id and receipt.get('status')=='complete' and receipt.get('manifest')==dict(file='manifest.json',sha256=expected.snapshot_manifest_sha256,bytes=held.files[ntpath.normcase(str(snapshot/'manifest.json'))]['bytes']),'Snapshot receipt differs')
    objects=manifest.get('objects');_need(type(objects) is list and 0<len(objects)<=1024,'Snapshot object count differs')
    expected_members={'manifest.json','receipt.json'};locks=[(str(snapshot/'manifest.json'),expected.snapshot_manifest_sha256,held.files[ntpath.normcase(str(snapshot/'manifest.json'))]['bytes']),
        (str(snapshot/'receipt.json'),exported['receipt_sha256'],held.files[ntpath.normcase(str(snapshot/'receipt.json'))]['bytes'])]
    total=0
    for index,obj in enumerate(objects):
        for role in ('world','triangles','polygon'):
            path=snapshot/(f'{index:06d}.{role}.npy');ref=obj['arrays'][role]
            _need(ref['path']==str(path),'Snapshot array descriptor outside fixed role')
            size=ref['bytes'];_need(type(size) is int and 0<=size<=536870912,'Snapshot array size invalid')
            held.file(path,_digest(ref['sha256']),maximum=536870912,size=size);total+=size
            _need(total<=536870912,'Snapshot total byte cap exceeded','RESOURCE_LIMIT')
            expected_members.add(path.name);locks.append((str(path),ref['sha256'],size))
    _need(tuple(locks)==expected.recorded_input_locks and held.flat(snapshot)==expected_members,'Expected snapshot locks or complete members differ')
    stages=acceptance.get('stages');_need(type(stages) is list and [x.get('mode') for x in stages]==['identity','export','numeric'],'Original stage set differs')
    for stage in stages:
        _need(stage.get('status')=='completed','Original stage is not completed')
        lifecycle=held.descriptor(stage['lifecycle'],root/('stage-'+stage['mode'])/'lifecycle.json')
        _need(lifecycle.get('status')=='completed' and type(lifecycle.get('root_returncode')) is int and lifecycle['root_returncode']==0 and lifecycle.get('tree_handle_closed') is True and lifecycle.get('owned_job_zero_active_observed') is True,'Original owned lifecycle differs')
    original_cold=held.descriptor(acceptance['cold_report'],root/'COLD-REPORT.json',True)
    root_guard=held.directory(bundle);identity_now=root_guard.identity();_need(identity_now==acceptance.get('result_root_identity'),'Original bundle directory identity differs')
    lease=stack.enter_context(WindowsBundleLease(str(bundle),expected_root_identity=(identity_now['volume'],identity_now['file_index']),
        forbidden_file_identities=frozenset(held.identities),reader_api=reader,limits=reader.ReaderLimits(),checkpoint=check))
    cold=reader.verify_intersection_image_v1(lease,expected_receipt_sha256=_digest(acceptance['receipt_sha256']),expected_run=expected,limits=reader.ReaderLimits(),checkpoint=check)
    _need(cold.parent_accepted is False and cold.quality_accepted is False and cold.source_current=='not_checked','Cold reader authority differs')
    fresh=json.loads(canonical(asdict(cold)));old=dict(original_cold);fresh.pop('lease_id');old.pop('lease_id')
    _need(_same(fresh,old),'Original cold report and current held cold verification differ')
    def recheck():
        try:
            check();held.recheck();_need(held.flat(snapshot)==expected_members,'Snapshot membership changed');lease.recheck();check()
        except BaseException:
            if check.error is not None:raise check.error
            raise
    recheck()
    # Protect the runner-owned output directory before any report creation.
    held.directory(own)
    def write(name,value):
        path=own/name;data=canonical(value)
        with path.open('xb') as stream:stream.write(data)
        held.file(path,hashlib.sha256(data).hexdigest())
        return dict(file=str(path),sha256=hashlib.sha256(data).hexdigest(),bytes=len(data))
    cold_ref=write('parent-inspect-cold.json',asdict(cold))
    report=dict(schema='toon-image-parent-inspection.v1',validation='pinned_original_parent_and_bundle_verified',
        original_parent_job_id=job.name,parent_result=dict(file=str(result_path),expected_sha256=result_sha),
        implementation_sha256=implementation,nonce=context['nonce'],
        terminal_observation=dict(state='succeeded',exit_code=0,status_raw_sha256=held.files[ntpath.normcase(str(job/'status.json'))]['sha256']),
        binding_scope=dict(request='canonical_json_sha256',launch='image_identity_and_image_request_sha256_only',status='current_held_terminal_observation'),
        source_current='not_checked',historical_source_unchanged_recorded=acceptance.get('source_unchanged'),
        parent_accepted=False,quality_accepted=False,geometry_recomputed=False,cold_report=cold_ref,
        original_acceptance=data['acceptance'],
        unverified=['current_source_resources_code_paths','entire_launch_status_historical_bytes','new_inspector_runner_terminal','display_and_aesthetics'])
    report_ref=write('parent-inspection.json',report);recheck()
    return dict(report,inspection_report=report_ref),recheck
