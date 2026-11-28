"""Read completed public RGB producer authority, never Torch/reinference/old guard."""
from pathlib import Path
import math

from compositing_depth_estimate_contract import normalize_params, signature
from compositing_depth_estimate_public import PROFILE_SHA, MANIFEST_SHA, raw_stage_key, launch_identity


def admit(params, inputs):
    from compositing_depth_public import need, row, descriptor, read, key, code_signature
    def load(d): return read(inputs.protect(descriptor(d)))
    descriptor_in = params['provenance']['producer']
    path = Path(descriptor_in['file']); parent = path.parent
    need(path.name=='model-report.json' and len(parent.name)==32 and all(c in '0123456789abcdef' for c in parent.name),
         'Public DAV2 producer must be its owned model-report')
    report = load(descriptor_in)
    need(report.get('schema')=='public-dav2-model-report-v1' and report.get('status')=='passed' and
         report.get('parent_job_id')==parent.name and report.get('public_parent_success_required') is True,
         'Model report is not completed numeric producer')
    docs = {n:load(row(parent/n)) for n in ('status.json','result.json','request.json','launch.json')}
    state,result,request,launch = [docs[n] for n in ('status.json','result.json','request.json','launch.json')]
    need(state.get('job_id')==parent.name and state.get('state')=='succeeded' and type(state.get('exit_code')) is int and state['exit_code']==0 and
         type(state.get('finished_at')) in (float,int) and math.isfinite(state['finished_at']) and state['finished_at']>0,
         'Public estimate parent has not succeeded')
    need(result.get('schema_version')=='1.0' and result.get('command')=='compositing.depth-estimate' and result.get('job_id')==parent.name and
         result.get('ok') is True and result.get('error') is None and request.get('schema_version')=='1.0' and request.get('command')=='compositing.depth-estimate',
         'Public estimate result/request differs')
    try: normalized = normalize_params(request.get('params'))
    except Exception: need(False, 'Public estimate normalized request invalid', 'CONFLICT')
    need(report['request']['params']==normalized and signature(normalized)==report['request_signature']==result.get('data',{}).get('request_signature')==launch.get('estimate_request_signature'),
         'Estimate normalized request/report/result/launch mismatch')
    for n in ('request.json','launch.json'):
        current = row(parent/n)
        need(current==report['parent_documents'][n], 'Bound estimate parent document changed')
    need(result['data'].get('model_report')==row(path) and result.get('artifacts',{}).get('model_report')==str(path), 'Parent result does not bind model report')
    need(report['producer_identity']==launch.get('estimate_identity'),'Estimate source launch differs')
    frozen = report['frozen_sources']; roles = [r['role'] for r in frozen]
    expected = launch_identity()
    need(len(roles)==len(set(roles)) and set(roles)=={r['role'] for r in expected['files']} and
         code_signature(frozen)==report['producer_identity']['sha256'],'Estimate frozen source closure differs')
    # Actual model Host/worker/contract profile only, permit shared parser location/version changes.
    critical = ('host:compositing_depth_estimate_public.py','host:compositing_depth_estimate_contract.py','depth_estimate_contract','depth_estimate_worker')
    for role in critical:
        old = next(r['sha256'] for r in frozen if r['role']==role)
        new = next(r['sha256'] for r in expected['files'] if r['role']==role)
        need(old==new,'Different model producer profile has not qualified cold admission','UNSUPPORTED')
    for r in frozen:
        need(Path(r['file']).parent==parent/'model/source','Producer snapshot outside owned model')
        inputs.protect(descriptor(r))
    profile = load(normalized['runtime_profile'])
    need(normalized['runtime_profile']['expected_sha256']==PROFILE_SHA and report['runtime_profile']==normalized['runtime_profile'] and
         profile['trusted_installed_manifest']['expected_sha256']==MANIFEST_SHA, 'Unqualified fixed model profile')
    inventory = load(report['guarded_input_inventory'])
    verification = report['profile_verification']
    need(verification.get('files')==16221 and type(verification['files']) is int and verification.get('packages')==14 and
         type(verification['packages']) is int and verification.get('current_payload_sha_rechecked') is True and
         verification.get('wheel_archive_reconstruction') is False and verification.get('prior_manifest_sha256')==MANIFEST_SHA and
         inventory.get('trusted_expected_manifest')==profile['trusted_installed_manifest'] and inventory.get('verification')==verification,
         'Actual approved-manifest verification differs')
    worker = load(report['worker_report'])
    need(worker.get('status')=='passed' and worker.get('request_signature')==report['request_signature'] and
         worker.get('raw')==report['raw'] and worker.get('preprocess')==report['preprocess'] and
         worker.get('statistics')==report['statistics'] and worker.get('runtime')==report['runtime'], 'Actual numeric worker/report differs')
    inputs.protect(descriptor(report['raw']))
    need(key(report['raw']['file'])==key(params['depth']['file']) and report['raw']['sha256']==params['depth']['expected_sha256'] and
         report['raw']['dtype']=='float32' and report['raw']['shape']==[params['domain']['height'],params['domain']['width']], 'Prepared raw differs from actual estimate')
    need(report['RGB']==normalized['rgb']==params['rgb'] and report['domain']==normalized['domain']==params['domain'], 'Original model RGB/domain differs')
    need(params['semantics']=={'measure':'relative_score','near_order':'larger_nearer','units':'unitless'} and
         report['declared_near_order']==profile['near_order'], 'Relative score/profile direction differs')
    need(report['stage_key']==raw_stage_key(normalized, report['producer_identity'])==result['data'].get('stage_key'), 'Raw model key differs')
    need(report['metric_depth_proven'] is False and report['calibration_proven'] is False and report['finite_is_confidence'] is False and
         report['runtime']['actual_device']=='cuda:0' and report['runtime']['dtype']=='float32' and
         report['runtime']['autocast'] is False and report['runtime']['TF32'] is False and report['runtime']['fallback'] is False,
         'Unqualified model physical/precision assumptions')
    recipe = result['data'].get('prepare_params'); need(type(recipe) is dict, 'Missing root-produced prepare recipe')
    original_recipe = load(recipe)
    need(result.get('artifacts',{}).get('estimate_prepare_params')==recipe['file'] and
         original_recipe['provenance']==params['provenance'] and original_recipe['depth']==params['depth'] and original_recipe['rgb']==params['rgb'],
         'Published recipe origin/raw/RGB differs')
    # Consumers may explicitly revise derived controls. They must keep producer/raw/RGB/domain/semantics.
    for n in ('domain','semantics','rgb_encoding'): need(original_recipe[n]==params[n],'Derived recipe changed model domain/semantics')
    return {'kind':'verified_public_dav2','producer':descriptor_in,'model_completion_verified':True,
            'scope':'actual numeric new RGB from fixed public model profile; per-image geometry and lighting unproven',
            'direction':'larger_nearer','direction_basis':profile['near_order']['basis'],
            'model_reexecuted':False,'guard_reexecuted':False,'payload_rehashed_on_cold_read':False,
            'metric_depth_proven':False,'calibration_proven':False,'consumer_eligible':False}
