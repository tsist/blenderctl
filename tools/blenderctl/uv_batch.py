"""UV batch admission and evidence; scheduling belongs to the public pipeline."""
import copy
from pathlib import Path

from protocol import Failure, read_json
from scene_contract import obj, array, enum, validate
import pipeline_contract as pipeline

COMMANDS = ('uv.batch', 'uv.batch-resume')
STEP = copy.deepcopy(pipeline.STEP)
STEP['properties']['command'] = enum('uv.run', 'uv.inspect')
POLICY = copy.deepcopy(pipeline.POLICY)
# Quality-first, bounded memory: one owned Blender tree at a time in this release.
POLICY['properties']['max_workers'] = {'const': 1}
MANIFEST = obj({'uv_batch_version': {'const': '1.0'}, 'policy': POLICY,
                'steps': array(STEP, 1, 32)})


def normalize(value):
    return pipeline.normalize(value, schema=MANIFEST)


def normalize_request(command, value):
    value = copy.deepcopy(value)
    allowed = {'job_id'} if command == 'uv.batch-resume' else {'manifest', 'reuse_job'}
    if not isinstance(value, dict) or set(value) - allowed:
        raise Failure('INVALID_REQUEST', 'Unknown UV batch request fields')
    if command == 'uv.batch-resume':
        if set(value) != {'job_id'}:
            raise Failure('INVALID_REQUEST', 'UV batch resume requires job_id')
    elif 'manifest' not in value:
        raise Failure('INVALID_REQUEST', 'UV batch requires manifest')
    else:
        value['manifest'] = normalize(value['manifest'])
    import re
    for field in ('job_id', 'reuse_job'):
        if field in value and (not isinstance(value[field], str) or
                              not re.fullmatch('[0-9a-f]{32}', value[field])):
            raise Failure('INVALID_REQUEST', 'Prior UV batch job ID must be 32 lowercase hex characters')
    return value


def verify_parent(root, prior, guards):
    """Only a settled public UV batch can supply reusable completed children."""
    from filesystem import FileGuard
    for name in ('status.json', 'request.json', 'result.json', 'pipeline-report.json'):
        guards.enter_context(FileGuard(root / name))
    status, request, result, report = [read_json(root / name) for name in
                                      ('status.json', 'request.json', 'result.json', 'pipeline-report.json')]
    if (request.get('command') not in COMMANDS or status.get('job_id') != root.name or
            status.get('state') not in ('succeeded', 'failed', 'cancelled', 'timed_out') or
            not status.get('finished_at') or prior != report or report.get('job_id') != root.name or
            report.get('complete') is not True or report.get('uv_batch_version') != '1.0' or
            result.get('job_id') != root.name or result.get('command') != request['command']):
        raise Failure('CONFLICT', 'UV batch recovery requires an intact terminal parent')
    from protocol import CODES
    error = result.get('error')
    if status['state'] == 'succeeded':
        valid = result.get('ok') is True and error is None and status.get('exit_code') == 0 and result.get('data') == report
    else:
        valid = (result.get('ok') is False and isinstance(error, dict) and
                 error.get('code') in CODES and status.get('exit_code') == CODES[error['code']] and
                 report.get('error') == error)
        if valid:
            wanted = {'cancelled': 'CANCELLED', 'timed_out': 'TIMEOUT'}.get(status['state'])
            valid = error['code'] == wanted if wanted else error['code'] not in ('CANCELLED', 'TIMEOUT')
    if not valid:
        raise Failure('CONFLICT', 'UV batch parent result/status/report disagree')
    if request['command'] == 'uv.batch' and normalize(request['params']['manifest']) != report['manifest']:
        raise Failure('CONFLICT', 'UV batch parent manifest differs from original request')


def qualify(row, params=None):
    """Read the protected child authority, then separate hard checks from review.

    Called only after collect_outputs / verify_reuse holds the complete output
    closure. A numeric failure remains an inspectable candidate, never reusable
    success. This does not approve a model's visual or purpose baseline.
    """
    from uv_workflow_contract import signature
    directory = Path(row['child_directory']).resolve()
    required = ['request.json', 'result.json', 'uv-report.json']
    if row['command'] == 'uv.run':
        required.extend(('uv-candidate.blend', 'uv-reopen.json'))
    if any(str(directory / name) not in row.get('output_hashes', {}) for name in required):
        raise Failure('CONFLICT', 'UV batch reusable closure omits a required child artifact')
    result = read_json(directory / 'result.json')
    status = read_json(directory / 'status.json')
    request = read_json(directory / 'request.json')
    data = row.get('data') or {}
    if (status.get('state') != 'succeeded' or status.get('exit_code') != 0 or
            status.get('job_id') != directory.name or result.get('job_id') != directory.name or
            result.get('command') != row['command'] or request.get('command') != row['command'] or
            result.get('ok') is not True or result.get('error') is not None or result.get('data') != data):
        raise Failure('CONFLICT', 'UV batch child has no matching successful public result')
    actual = request['params']
    if params is not None and actual != params:
        raise Failure('CONFLICT', 'UV batch child request differs')
    ref = data.get('report') or {}
    report_path = directory / 'uv-report.json'
    if (Path(ref.get('file', '')).resolve() != report_path or
            row['output_hashes'].get(str(report_path)) != ref.get('sha256')):
        raise Failure('CONFLICT', 'UV batch child report must be a verified owned output')
    report = read_json(report_path)
    if (report.get('uv_report_version') != '1.0' or report.get('error') or
            report.get('signature') != signature(actual) or
            report.get('implementation_sha256') != actual['implementation_sha256'] or
            report.get('source') != {'file': actual['file'], 'sha256': actual['expected_sha256']} or
            report.get('checks', {}).get('source_sha256') != 'passed' or
            report.get('resources', {}).get('status') != 'checked_static_and_explicit_udim_paths'):
        raise Failure('CONFLICT', 'UV batch child input/closure proof differs')
    operation = actual['manifest']['operation']
    expected_status = {'generate': 'candidates_created', 'inspect': 'inspected',
                       'validate': 'validated', 'edit': 'candidate_edited',
                       'recipe': 'recipe_bound', 'settings': 'rules_updated', 'seams': 'rules_updated',
                       'copy_uv': 'candidate_copied', 'transfer': 'candidate_transferred',
                       'adopt': 'adopted_for_editing', 'restore': 'restored'}.get(operation)
    if (not expected_status or report.get('status') != expected_status or
            data.get('status') != expected_status or report.get('checks', {}).get('scene_view_layer') != 'passed'):
        raise Failure('VALIDATION_FAILED', 'UV batch operation lacks its complete expected status')
    if operation == 'inspect':
        if report.get('checks', {}).get('saves') != 'none':
            raise Failure('VALIDATION_FAILED', 'UV inspection lacks its no-save proof')
        if 'surface' in actual['manifest']:
            surface=report.get('surface')
            if not isinstance(surface,dict) or surface!=data.get('inspection',{}).get('surface') or surface.get('request')!=actual['manifest']['surface']:
                raise Failure('VALIDATION_FAILED','UV surface inspection lacks its exact requested samples')
            expected={(f,m) for f in actual['manifest']['surface']['frames'] for m in actual['manifest']['surface']['modes']}
            samples=surface.get('samples',[])
            if len(samples)!=len(expected) or surface.get('sample_count')!=len(samples) or {(s.get('frame'),s.get('mode')) for s in samples}!=expected:
                raise Failure('VALIDATION_FAILED','UV surface sample set is incomplete')
            _surface_proof(surface,actual['manifest'])
            status='fail' if any(s.get('status')=='fail' for s in samples) else 'unknown' if any(s.get('status')!='pass' or not s.get('complete') for s in samples) else 'pass'
            if surface.get('status')!=status:raise Failure('VALIDATION_FAILED','UV surface aggregate status differs')
            return {'status':'hard_pass_needs_review' if status=='pass' else status,
                    'checks':[{'frame':s['frame'],'mode':s['mode'],'status':s['status'],'reason':s.get('reason')} for s in samples],
                    'quality_acceptance':'requires_surface_purpose_and_visual_review','reusable':status=='pass'}
        return {'status': 'inspected', 'quality_acceptance': 'not_applicable_read_only', 'reusable': True}
    if (report.get('verification', {}).get('status') != 'passed' or
            report.get('checks', {}).get('independent_reopen') != 'passed' or
            report.get('non_target_check', {}).get('status') != 'unchanged' or
            report.get('checks', {}).get('non_target') != 'passed_unchanged'):
        raise Failure('VALIDATION_FAILED', 'UV batch child needs independent reopen and protection evidence')
    candidate = report.get('candidate') or {}
    if (candidate != data.get('candidate') or
            Path(candidate.get('file', '')).resolve() != directory / 'uv-candidate.blend' or
            row['output_hashes'].get(candidate.get('file')) != candidate.get('sha256')):
        raise Failure('CONFLICT', 'UV batch candidate is not an intact owned output')
    records = report.get('candidates') if operation == 'generate' else [report['record']] if report.get('record') else []
    if records:
        checks = [{'candidate': r.get('candidate', actual['manifest'].get('candidate')),
                   'status': r.get('metrics', {}).get('status', 'fail' if r.get('status') == 'rejected' else 'unknown'),
                   'hard_checks_status': r.get('metrics', {}).get('hard_checks_status', 'unknown'),
                   'reason': r.get('reason'),
                   'rule_violations': r.get('metrics', {}).get('recipe_checks', {}).get('violations', [])} for r in records]
        usable = [r['candidate'] for r in checks if r['status'] == 'pass']
        state = 'hard_pass_needs_review' if usable else 'unknown' if any(r['status'] == 'unknown' for r in checks) else 'fail'
        return {'status': state, 'checks': checks, 'usable_candidates': usable,
                'selection_rule': 'Any numeric-pass candidate permits review; failed/unknown siblings remain isolated and are never selected implicitly',
                'quality_acceptance': 'requires_visual_and_purpose_review', 'reusable': bool(usable)}
    # Adopt/restore/seams may be valid state operations without new quality data.
    if operation not in ('adopt', 'restore'):
        return {'status': 'fail', 'usable_candidates': [], 'reusable': False,
                'quality_acceptance': 'no_usable_candidate'}
    return {'status': 'completed_state_operation', 'reusable': True,
            'quality_acceptance': report['quality_acceptance']}


def _surface_proof(surface,manifest):
    """Cross-check the worker's proof once at the batch reuse boundary."""
    spec=manifest['surface']
    if (spec.get('usage') not in ('EXCLUSIVE','CANDIDATE_EXACT') or
            surface.get('usage')!=spec['usage'] or surface.get('controls_restored') is not True):
        raise Failure('VALIDATION_FAILED','UV surface usage or restoration proof is missing')
    reuse_groups=None
    for sample in surface['samples']:
        if sample.get('status')!='pass':continue
        q=sample.get('metrics',{});objects=sample.get('objects',[])
        if (len(objects)!=len(manifest['objects']) or {o.get('object') for o in objects}!=set(manifest['objects']) or
                any(o.get('layer')!=spec['layers'][o['object']] for o in objects) or
                q.get('status')!='pass' or q.get('measurement_surface')!='evaluated_mesh' or
                q.get('evaluation_mode')!=sample['mode'] or q.get('usage',{}).get('mode')!='EXCLUSIVE' or
                q.get('total_triangles')!=sum(o.get('triangles',0) for o in objects) or
                not 0<q.get('total_triangles',0)<=spec['triangle_limit'] or
                q.get('coverage',{}).get('pair_budget_per_phase')!=manifest['pair_budget'] or
                q.get('coverage',{}).get('numeric_complete') is not True or
                q.get('overlap',{}).get('complete') is not True or q.get('gap',{}).get('complete') is not True):
            raise Failure('VALIDATION_FAILED','UV surface numeric proof differs from the requested evaluation')
        if spec['usage']=='CANDIDATE_EXACT':
            groups=_surface_exact_proof(sample,manifest)
            if reuse_groups is not None and groups!=reuse_groups:
                raise Failure('VALIDATION_FAILED','UV exact surface reuse groups changed between samples')
            reuse_groups=groups


def _surface_exact_proof(sample,manifest):
    """Require the worker's complete finite identity receipt, not a usage label.

    Digests are format/equality checked here; the protected worker report is the
    authority for their calculation and candidate settings binding.
    """
    import re
    spec=manifest['surface'];names=manifest['objects']
    identity=sample.get('exact_identity');objects=sample.get('objects')
    def digest(value):
        return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None
    if (sample.get('reuse_used') is not True or sample.get('candidate')!=spec.get('candidate') or
            not isinstance(spec.get('candidate'),str) or not spec['candidate'] or
            type(sample.get('complete')) is not bool or type(sample['metrics'].get('total_triangles')) is not int or
            not isinstance(identity,dict) or identity.get('comparison')!='finite_numeric_values_no_tolerance' or
            'first_mismatch' not in identity or identity['first_mismatch'] is not None or
            not isinstance(identity.get('objects'),list) or
            [r.get('object') if isinstance(r,dict) else None for r in identity['objects']]!=names or
            [r.get('object') for r in objects]!=names):
        raise Failure('VALIDATION_FAILED','UV exact surface lacks complete ordered candidate identity')
    for row,actual in zip(identity['objects'],objects):
        if (not digest(row.get('base_sha256')) or not digest(row.get('evaluated_sha256')) or
                row['base_sha256']!=row['evaluated_sha256'] or row.get('match') is not True or
                'first_mismatch' not in row or row['first_mismatch'] is not None or
                not digest(actual.get('world_geometry_sha256')) or not digest(actual.get('uv_sha256')) or
                type(actual.get('triangles')) is not int or actual['triangles']<0):
            raise Failure('VALIDATION_FAILED','UV exact surface finite identity receipt is incomplete')
    groups=sample['metrics']['usage'].get('reuse_groups')
    if not isinstance(groups,list) or not groups:
        raise Failure('VALIDATION_FAILED','UV exact surface requires explicit nonempty reuse groups')
    ids=set();members=set()
    for group in groups:
        if (not isinstance(group,dict) or not isinstance(group.get('id'),str) or not group['id'] or
                group['id'] in ids or not isinstance(group.get('members'),list) or len(group['members'])<2):
            raise Failure('VALIDATION_FAILED','UV exact surface reuse group proof is invalid')
        ids.add(group['id'])
        for member in group['members']:
            if (not isinstance(member,dict) or member.get('object') not in names or
                    type(member.get('island')) is not int or member['island']<0):
                raise Failure('VALIDATION_FAILED','UV exact surface reuse member proof is invalid')
            key=(member['object'],member['island'])
            if key in members:
                raise Failure('VALIDATION_FAILED','UV exact surface reuse members are duplicated')
            members.add(key)
    return groups


def summary(rows):
    return {'execution': {state: sum(r['state'] == state for r in rows) for state in sorted({r['state'] for r in rows})},
            'quality': [{'id': r['id'], **r.get('uv_result', {'status': 'not_evaluated'})} for r in rows],
            'scope': 'Independent saved-file work units; same-engineering-file joint atlas is one multi-object step. No automatic visual approval.'}


def execute(job, checkpoint=None):
    from pipeline import execute as schedule
    return schedule(job, checkpoint, manifest_normalizer=normalize, uv_batch_mode=True)


def compact(result):
    if not isinstance(result, dict):
        raise Failure('INVALID_REQUEST', 'UV batch result must be an object')
    directory = result.get('artifacts', {}).get('job_directory')
    root = Path(directory) if isinstance(directory, str) and directory else None
    report = {}
    for name in ('pipeline-report.json', 'pipeline-state.json'):
        if root is not None and (root / name).is_file():
            report = read_json(root / name)
            break
    value = {k: result[k] for k in ('schema_version', 'cli_version', 'command', 'job_id', 'ok', 'error', 'artifacts') if k in result}
    value['data'] = {k: report[k] for k in ('complete', 'elapsed_seconds', 'uv_summary') if k in report}
    if isinstance(result.get('data'), dict) and 'state' in result['data']:
        value['data']['state'] = result['data']['state']
    value['data']['steps'] = [{k: r[k] for k in ('id', 'state', 'child_job', 'reused_from', 'error', 'uv_result') if k in r} for r in report.get('steps', [])]
    for item, row in zip(value['data']['steps'], report.get('steps', [])):
        item.update({k: row['data'][k] for k in ('candidate', 'report') if isinstance(row.get('data'), dict) and k in row['data']})
    return value
