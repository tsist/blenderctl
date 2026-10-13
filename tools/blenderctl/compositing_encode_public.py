"""Draft public facade for unchanged isolated M4; no bpy or producer execution."""
from pathlib import Path
from contextlib import ExitStack
import copy
from protocol import Failure,CODES,read_json,digest,SCHEMA,VERSION,atomic_json
from compositing_encode_contract import normalize_params,EncodeError,signature
from compositing_encode import run,implementation_identity,Inputs,describe as file_identity

PARAMS_SCHEMA = {'oneOf': [{'type': 'object', 'additionalProperties': False, 'properties': {'encode_version': {'const': '1.0-candidate'}, 'producer': {'type': 'object', 'additionalProperties': False, 'properties': {'file': {'type': 'string', 'pattern': '^(?:[A-Za-z]:[\\\\/]|\\\\\\\\)'}, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, 'required': ['file', 'expected_sha256']}, 'select': {'oneOf': [{'type': 'object', 'additionalProperties': False, 'properties': {'mode': {'const': 'produced_frames'}, 'frames': {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 1, 'maxItems': 300, 'uniqueItems': True}}, 'required': ['mode', 'frames']}, {'type': 'object', 'additionalProperties': False, 'properties': {'mode': {'const': 'still_repeat'}, 'frames': {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 1, 'maxItems': 1}, 'repeat_count': {'type': 'integer', 'minimum': 1, 'maximum': 300}}, 'required': ['mode', 'frames', 'repeat_count']}]}, 'fps': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1, 'maximum': 120000}, 'minItems': 2, 'maxItems': 2}, 'alpha': {'oneOf': [{'type': 'object', 'additionalProperties': False, 'properties': {'mode': {'const': 'require_opaque'}}, 'required': ['mode']}, {'type': 'object', 'additionalProperties': False, 'properties': {'mode': {'const': 'composite_matte'}, 'association': {'const': 'PREMUL'}, 'matte_rgb_output_linear': {'type': 'array', 'items': {'type': 'number', 'minimum': 0}, 'minItems': 3, 'maxItems': 3}}, 'required': ['mode', 'association', 'matte_rgb_output_linear']}]}, 'runtime': {'type': 'object', 'additionalProperties': False, 'properties': {'python': {'type': 'object', 'additionalProperties': False, 'properties': {'file': {'type': 'string', 'pattern': '^(?:[A-Za-z]:[\\\\/]|\\\\\\\\)'}, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, 'required': ['file', 'expected_sha256']}, 'ffmpeg': {'type': 'object', 'additionalProperties': False, 'properties': {'file': {'type': 'string', 'pattern': '^(?:[A-Za-z]:[\\\\/]|\\\\\\\\)'}, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, 'required': ['file', 'expected_sha256']}, 'ffprobe': {'type': 'object', 'additionalProperties': False, 'properties': {'file': {'type': 'string', 'pattern': '^(?:[A-Za-z]:[\\\\/]|\\\\\\\\)'}, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, 'required': ['file', 'expected_sha256']}, 'modules': {'type': 'object', 'additionalProperties': False, 'properties': {'PyOpenColorIO': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'properties': {'file': {'type': 'string', 'pattern': '^(?:[A-Za-z]:[\\\\/]|\\\\\\\\)'}, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, 'required': ['file', 'expected_sha256']}, 'minItems': 1}, 'OpenImageIO': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'properties': {'file': {'type': 'string', 'pattern': '^(?:[A-Za-z]:[\\\\/]|\\\\\\\\)'}, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, 'required': ['file', 'expected_sha256']}, 'minItems': 1}, 'numpy': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'properties': {'file': {'type': 'string', 'pattern': '^(?:[A-Za-z]:[\\\\/]|\\\\\\\\)'}, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, 'required': ['file', 'expected_sha256']}, 'minItems': 1}, 'python_native': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'properties': {'file': {'type': 'string', 'pattern': '^(?:[A-Za-z]:[\\\\/]|\\\\\\\\)'}, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, 'required': ['file', 'expected_sha256']}, 'minItems': 1}}, 'required': ['PyOpenColorIO', 'OpenImageIO', 'numpy', 'python_native']}}, 'required': ['python', 'ffmpeg', 'ffprobe', 'modules']}, 'quality': {'type': 'object', 'additionalProperties': False, 'properties': {'crf': {'type': 'integer', 'minimum': 0, 'maximum': 18}, 'preset': {'enum': ['medium', 'slow']}, 'gray_rois': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'properties': {'label': {'type': 'string'}, 'rect': {'type': 'array', 'items': {'type': 'integer', 'minimum': 0}, 'minItems': 4, 'maxItems': 4}}, 'required': ['label', 'rect']}, 'minItems': 0, 'maxItems': 128}}, 'required': ['crf', 'preset', 'gray_rois']}, 'budget': {'type': 'object', 'additionalProperties': False, 'properties': {'max_pixel_frames': {'type': 'integer', 'minimum': 1, 'maximum': 2147483648}, 'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 7200}}, 'required': ['max_pixel_frames', 'timeout_seconds']}, 'resume': {'type': 'object', 'additionalProperties': False, 'properties': {'file': {'type': 'string', 'pattern': '^(?:[A-Za-z]:[\\\\/]|\\\\\\\\)'}, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, 'required': ['file', 'expected_sha256']}, 'encode_color': {'type': 'object', 'additionalProperties': False, 'properties': {'view': {'const': 'Standard'}, 'source_display_policy': {'const': 'EXPLICIT_OVERRIDE'}, 'look': {'const': 'None'}, 'white_balance': {'const': False}, 'curves': {'const': False}, 'gamma': {'type': 'number', 'const': 1}, 'exposure': {'type': 'number', 'minimum': -20, 'maximum': 20}, 'gamut_policy': {'const': 'RETAIN_REFERENCE'}, 'reference_white_nits': {'enum': [100, 203]}, 'mapping_policy': {'type': 'object', 'additionalProperties': False, 'properties': {'kind': {'const': 'LINEAR_RETAIN_REFERENCE'}}, 'required': ['kind']}}, 'required': ['view', 'source_display_policy', 'look', 'white_balance', 'curves', 'gamma', 'exposure', 'gamut_policy', 'reference_white_nits']}, 'mastering': {'type': 'object', 'additionalProperties': False, 'properties': {'kind': {'const': 'technical_target'}, 'primaries': {'const': 'BT2020'}, 'white_point': {'const': [0.3127, 0.329]}, 'min_nits': {'const': 0.0001}, 'max_nits': {'const': 10000}, 'statement_source': {'type': 'string', 'minLength': 1}}, 'required': ['kind', 'primaries', 'white_point', 'min_nits', 'max_nits', 'statement_source']}}, 'required': ['encode_version', 'producer', 'select', 'fps', 'alpha', 'runtime', 'quality', 'budget', 'encode_color', 'mastering']}, {'type': 'object', 'additionalProperties': False, 'properties': {'encode_version': {'const': '1.0-candidate'}, 'producer': {'type': 'object', 'additionalProperties': False, 'properties': {'file': {'type': 'string', 'pattern': '^(?:[A-Za-z]:[\\\\/]|\\\\\\\\)'}, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, 'required': ['file', 'expected_sha256']}, 'select': {'oneOf': [{'type': 'object', 'additionalProperties': False, 'properties': {'mode': {'const': 'produced_frames'}, 'frames': {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 1, 'maxItems': 300, 'uniqueItems': True}}, 'required': ['mode', 'frames']}, {'type': 'object', 'additionalProperties': False, 'properties': {'mode': {'const': 'still_repeat'}, 'frames': {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 1, 'maxItems': 1}, 'repeat_count': {'type': 'integer', 'minimum': 1, 'maximum': 300}}, 'required': ['mode', 'frames', 'repeat_count']}]}, 'fps': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1, 'maximum': 120000}, 'minItems': 2, 'maxItems': 2}, 'alpha': {'oneOf': [{'type': 'object', 'additionalProperties': False, 'properties': {'mode': {'const': 'require_opaque'}}, 'required': ['mode']}, {'type': 'object', 'additionalProperties': False, 'properties': {'mode': {'const': 'composite_matte'}, 'association': {'const': 'PREMUL'}, 'matte_rgb_output_linear': {'type': 'array', 'items': {'type': 'number', 'minimum': 0}, 'minItems': 3, 'maxItems': 3}}, 'required': ['mode', 'association', 'matte_rgb_output_linear']}]}, 'runtime': {'type': 'object', 'additionalProperties': False, 'properties': {'python': {'type': 'object', 'additionalProperties': False, 'properties': {'file': {'type': 'string', 'pattern': '^(?:[A-Za-z]:[\\\\/]|\\\\\\\\)'}, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, 'required': ['file', 'expected_sha256']}, 'ffmpeg': {'type': 'object', 'additionalProperties': False, 'properties': {'file': {'type': 'string', 'pattern': '^(?:[A-Za-z]:[\\\\/]|\\\\\\\\)'}, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, 'required': ['file', 'expected_sha256']}, 'ffprobe': {'type': 'object', 'additionalProperties': False, 'properties': {'file': {'type': 'string', 'pattern': '^(?:[A-Za-z]:[\\\\/]|\\\\\\\\)'}, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, 'required': ['file', 'expected_sha256']}, 'modules': {'type': 'object', 'additionalProperties': False, 'properties': {'PyOpenColorIO': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'properties': {'file': {'type': 'string', 'pattern': '^(?:[A-Za-z]:[\\\\/]|\\\\\\\\)'}, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, 'required': ['file', 'expected_sha256']}, 'minItems': 1}, 'OpenImageIO': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'properties': {'file': {'type': 'string', 'pattern': '^(?:[A-Za-z]:[\\\\/]|\\\\\\\\)'}, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, 'required': ['file', 'expected_sha256']}, 'minItems': 1}, 'numpy': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'properties': {'file': {'type': 'string', 'pattern': '^(?:[A-Za-z]:[\\\\/]|\\\\\\\\)'}, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, 'required': ['file', 'expected_sha256']}, 'minItems': 1}, 'python_native': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'properties': {'file': {'type': 'string', 'pattern': '^(?:[A-Za-z]:[\\\\/]|\\\\\\\\)'}, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, 'required': ['file', 'expected_sha256']}, 'minItems': 1}}, 'required': ['PyOpenColorIO', 'OpenImageIO', 'numpy', 'python_native']}}, 'required': ['python', 'ffmpeg', 'ffprobe', 'modules']}, 'quality': {'type': 'object', 'additionalProperties': False, 'properties': {'crf': {'type': 'integer', 'minimum': 0, 'maximum': 18}, 'preset': {'enum': ['medium', 'slow']}, 'gray_rois': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False, 'properties': {'label': {'type': 'string'}, 'rect': {'type': 'array', 'items': {'type': 'integer', 'minimum': 0}, 'minItems': 4, 'maxItems': 4}}, 'required': ['label', 'rect']}, 'minItems': 0, 'maxItems': 128}}, 'required': ['crf', 'preset', 'gray_rois']}, 'budget': {'type': 'object', 'additionalProperties': False, 'properties': {'max_pixel_frames': {'type': 'integer', 'minimum': 1, 'maximum': 2147483648}, 'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 7200}}, 'required': ['max_pixel_frames', 'timeout_seconds']}, 'resume': {'type': 'object', 'additionalProperties': False, 'properties': {'file': {'type': 'string', 'pattern': '^(?:[A-Za-z]:[\\\\/]|\\\\\\\\)'}, 'expected_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}}, 'required': ['file', 'expected_sha256']}, 'encode_color': {'type': 'object', 'additionalProperties': False, 'properties': {'view': {'const': 'AgX - HDR 1000 nits'}, 'source_display_policy': {'const': 'EXPLICIT_OVERRIDE'}, 'look': {'const': 'None'}, 'white_balance': {'const': False}, 'curves': {'const': False}, 'gamma': {'type': 'number', 'const': 1}, 'exposure': {'type': 'number', 'minimum': -20, 'maximum': 20}, 'gamut_policy': {'const': 'OCIO_VIEW'}, 'mapping_policy': {'type': 'object', 'additionalProperties': False, 'properties': {'kind': {'const': 'OCIO_DISPLAY_VIEW'}, 'display': {'const': 'Rec.2100-PQ'}, 'view': {'const': 'AgX - HDR 1000 nits'}, 'ocio_config_sha256': {'const': 'df5714c85d5afb5e9762281503a0c48a9f65dadb22d32a3aee50c454a0821f62'}, 'declared_display_reference_white_nits': {'const': 100}, 'extra_gain': {'const': 'NONE'}}, 'required': ['kind', 'display', 'view', 'ocio_config_sha256', 'declared_display_reference_white_nits', 'extra_gain']}}, 'required': ['view', 'source_display_policy', 'look', 'white_balance', 'curves', 'gamma', 'exposure', 'gamut_policy', 'mapping_policy']}, 'mastering': {'type': 'object', 'additionalProperties': False, 'properties': {'kind': {'const': 'technical_target'}, 'primaries': {'const': 'P3D65'}, 'white_point': {'const': [0.3127, 0.329]}, 'min_nits': {'const': 0.0001}, 'max_nits': {'const': 1000}, 'statement_source': {'type': 'string', 'minLength': 1}}, 'required': ['kind', 'primaries', 'white_point', 'min_nits', 'max_nits', 'statement_source']}}, 'required': ['encode_version', 'producer', 'select', 'fps', 'alpha', 'runtime', 'quality', 'budget', 'encode_color', 'mastering']}]}

HOST_FILES=('cli.py','protocol.py','runner.py','compositing_interface.py','execution_contract.py','compositing_encode_public.py','compositing_encode.py','compositing_encode_contract.py','processes.py','bootstrap.py')
def launch_identity():
    rows=[file_identity(Path(__file__).with_name(name)) for name in HOST_FILES]
    return {'host_files':rows,'host_sha256':signature(rows),'encoder':implementation_identity()}
def normalize(value):
    from scene_contract import validate
    validate(value,PARAMS_SCHEMA)
    try:return normalize_params(value)
    except EncodeError as exc:raise Failure(exc.code if exc.code in CODES else 'INVALID_REQUEST',str(exc)) from exc
def describe(schema=False,section=None):
    if schema:return {'operation':'encode','schema':PARAMS_SCHEMA}
    if section:
        branches=[x['properties'].get(section) for x in PARAMS_SCHEMA['oneOf']]
        if any(x is None for x in branches):raise Failure('INVALID_REQUEST','Unknown encode params section')
        return {'operation':'encode','section':section,'profiles':branches}
    return {'cli_version':VERSION,'operation':'compositing.encode','execution':'Host only; no Blender producer or scene worker','encode_version':'1.0-candidate','contract_identity':launch_identity(),'profiles':['LINEAR_RETAIN_REFERENCE Standard100/203','OCIO_DISPLAY_VIEW exact AgX HDR1000'], 'processing_profile':{'reference':'explicit CPU OCIO/NumPy','encoder':'explicit CPU libx265 Main10','fallback':'not_applicable_fixed_profile','GPU_encoding':'not_qualified'}, 'qualification':'isolated audit evidence; fresh public route validation required after source freeze','fidelity':'named mapped reference fixed3%/.05nit/black.01nit; old linear comparator remains separate','strict_decoded1000nit':'diagnostic only, no achieved hard cap claim','resume':'complete passed same request/runtime/source/implementation/artifacts only; cancelled partial not reusable','real_mastering':'not_run','device_viewing':'not_run','complete_normative_HDR10':'not_run'}
def run_public(params,job,launch,checkpoint):
    identity=launch.get('encode_identity')
    if identity is None or launch_identity()!=identity:raise Failure('CONFLICT','Encode public/worker implementation differs from queued launch')
    if signature(params)!=launch.get('encode_request_signature'):raise Failure('CONFLICT','Encode queued request changed')
    try:
        with ExitStack() as stack:
            guards=Inputs(stack,checkpoint)
            for row in producer_authority_documents(params):guards.protect(row)
            for row in identity['host_files']:guards.protect({'file':row['file'],'expected_sha256':row['sha256']})
            for name in ('request.json','launch.json'):
                path=job/name;guards.protect({'file':str(path),'expected_sha256':digest(path)})
            current=read_json(job/'request.json')
            if current['command']!='compositing.encode' or signature(current['params'])!=signature(params):raise Failure('CONFLICT','Locked public request differs')
            if 'resume' in params:
                old=params['resume'];receipt=Path(old['file']).parent/'encode-public-receipt.json'
                if not receipt.is_file():raise Failure('CONFLICT','Public resume requires prior public identity receipt')
                guards.protect(old);guards.protect({'file':str(receipt),'expected_sha256':digest(receipt)})
                previous=read_json(receipt)
                validate_public_resume_authority(params,old,previous,guards)
                if previous.get('host_identity')!=identity or previous.get('report',{}).get('file')!=old['file'] or previous.get('report',{}).get('sha256')!=old['expected_sha256']:
                    raise Failure('CONFLICT','Public resume host identity/report receipt differs')
            report=run(params,job,cancel_parent=checkpoint)
            guards.recheck()
            atomic_json(job/'encode-public-receipt.json',{'public_encode_receipt_version':'1.0-candidate','host_identity':identity,'report':file_identity(job/'encode-report.json'),'request_signature':report['request_signature'],'status':'passed','scope':'Public host code plus core exact-reference implementation; no real mastering/viewing/certification'})
            return report
    except EncodeError as exc:
        # Full encode-report preserves original domain error; public supervisor uses known CODES.
        raise Failure(exc.code if exc.code in CODES else 'WORKER_FAILED','M4 '+exc.code+': '+str(exc)) from exc

def compact(result,requested_command=None):
    artifacts=result.get('artifacts') or {};root=Path(artifacts['job_directory']) if artifacts.get('job_directory') else None
    status=read_json(root/'status.json') if root and (root/'status.json').is_file() else {}
    running=status.get('state') in ('queued','running')
    report=read_json(root/'encode-report.json') if root and not running and (root/'encode-report.json').is_file() else (result.get('data') or {}) if not running and result.get('command')=='compositing.encode' else {}
    out={k:result[k] for k in ('schema_version','cli_version','command','job_id','ok','error') if k in result}
    data={k:report[k] for k in ('encode_report_version','status','producer','producer_candidate','encode_color','mapping_policy','mastering','mastering_provenance','runtime_versions','implementation','request_signature','timing','checks','final_video','reused_validated_output','resumed_from','strict_decoded_1000nit_diagnostic','viewing','real_mastering','complete_hdr10_normative_certification','limits','error') if k in report}
    data['domain_report_status']=report.get('status','not_observed')
    if status:
        data['job_state']={k:status[k] for k in ('state','exit_code','worker_exit_code','elapsed_seconds','finished_at','process_tree_root_pid') if k in status}
        data['status']={'queued':'pending','running':'running','succeeded':'passed'}.get(status['state'],status['state'])
    if result.get('error'):data['status']={'CANCELLED':'cancelled','TIMEOUT':'timed_out'}.get(result['error']['code'],'failed')
    frames=report.get('frames',[]);indices=sorted({0,len(frames)//2,len(frames)-1}) if frames else []
    data['frames']={'count':len(frames),'scope':'first/middle/last only; full all-frame proof in hashed encode report','samples':[frames[i] for i in indices]}
    data['reference_count']=len(report.get('references',[]));data['command_count']=len(report.get('commands',[]));data['partial_file_count']=len(report.get('partial_files',[]))
    out.update(output_format='compact',data=data,artifacts={'job_directory':str(root)} if root else {})
    if root:
        for name in ('request.json','encode-report.json','encode-lifecycle.json','encode-public-receipt.json','result.json'):
            path=root/name
            if path.is_file() and (name=='request.json' or not running):out['artifacts'][name]=file_identity(path)
        out['artifacts']['status']={'file':str(root/'status.json'),'scope':'mutable supervisor metadata; terminal authority, not immutable producer proof'}
    return out

def producer_input_documents(params):
    from compositing_encode_contract import producer_documents
    from filesystem import FileGuard
    desc=params['producer']
    with FileGuard(desc['file']) as guard:
        if guard.sha256()!=desc['expected_sha256']:raise Failure('CONFLICT','Producer report changed')
        producer=read_json(desc['file'])
        try:documents,_selected=producer_documents(producer,params)
        except EncodeError as exc:raise Failure(exc.code if exc.code in CODES else 'INVALID_REQUEST',str(exc)) from exc
        return documents+producer_authority_documents(params)

def producer_authority_documents(params):
    """Existing standalone passed domain reports retain core eligibility.
    A report in a public job directory must also have true supervisor success.
    """
    from filesystem import FileGuard
    directory=Path(params['producer']['file']).parent;status_path=directory/'status.json'
    if not status_path.is_file():return []
    documents=[];values=[]
    for path in (status_path,directory/'result.json'):
        if not path.is_file():raise Failure('CONFLICT','Public producer authority receipt absent')
        with FileGuard(path) as guard:
            documents.append({'file':str(path),'expected_sha256':guard.sha256()});values.append(read_json(path))
    state,result=values
    if state.get('job_id')!=directory.name or state.get('state')!='succeeded' or state.get('exit_code')!=0 or state.get('worker_exit_code')!=0 or not state.get('finished_at') or result.get('job_id')!=directory.name or result.get('command')!='compositing.run' or result.get('ok') is not True or result.get('error') is not None:
        raise Failure('CONFLICT','Public producer supervisor/domain envelope is not true succeeded; checkpoint alone is insufficient')
    return documents

def validate_public_resume_authority(params,old,receipt,guards):
    import math,re
    from compositing_encode_contract import normalize_params
    directory=Path(old['file']).parent
    if Path(old['file']).name!='encode-report.json':raise Failure('CONFLICT','Public resume expects prior owned encode-report')
    if not re.fullmatch('[0-9a-f]{32}',directory.name):raise Failure('CONFLICT','Public resume parent job id invalid')
    values={}
    for name in ('status.json','request.json','result.json'):
        path=directory/name
        if not path.is_file():raise Failure('CONFLICT','Public resume authority document absent:'+name)
        guards.protect({'file':str(path),'expected_sha256':digest(path)});values[name]=read_json(path)
    state=values['status.json'];request=values['request.json'];result=values['result.json'];finished=state.get('finished_at')
    if state.get('job_id')!=directory.name or state.get('state')!='succeeded' or type(state.get('exit_code')) is not int or state['exit_code']!=0 or ('worker_exit_code' in state and (type(state['worker_exit_code']) is not int or state['worker_exit_code']!=0)) or type(finished) not in (int,float) or not math.isfinite(finished) or finished<=0:
        raise Failure('CONFLICT','Prior encode supervisor is not successful terminal authority')
    if request.get('schema_version')!='1.0' or request.get('command')!='compositing.encode' or result.get('schema_version')!='1.0' or result.get('job_id')!=directory.name or result.get('command')!='compositing.encode' or result.get('ok') is not True or 'error' not in result or result['error'] is not None or result.get('data',{}).get('status')!='passed':
        raise Failure('CONFLICT','Prior encode request/result is not matching successful public command')
    try:normalized=normalize_params(request.get('params'))
    except Exception as exc:raise Failure('CONFLICT','Prior encode normalized request invalid') from exc
    old_signature=signature({k:v for k,v in normalized.items() if k!='resume'});new_signature=signature({k:v for k,v in params.items() if k!='resume'})
    if old_signature!=new_signature or receipt.get('request_signature')!=old_signature or result['data'].get('request_signature')!=old_signature:
        raise Failure('CONFLICT','Prior normalized public request signature differs')
    report=read_json(Path(old['file']))
    if report.get('status')!='passed' or report.get('request_signature')!=old_signature or report.get('implementation')!=result['data'].get('implementation'):
        raise Failure('CONFLICT','Prior worker report/result signature or implementation differs')
