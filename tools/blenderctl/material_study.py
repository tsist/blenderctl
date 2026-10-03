# SPDX-License-Identifier: GPL-3.0-or-later
"""One owned worker for bounded material candidates, diagnostics and evidence."""
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import re
import time

from protocol import Failure, atomic_json, digest, read_json
from filesystem import FileGuard
from material_study_contract import normalize_params, expand_cases
import material_batch


def reference(path):
    path = Path(path).resolve()
    return {'file': str(path), 'expected_sha256': digest(path)}


def changes(base, case):
    assignment = next(a for a in base['manifest']['assignments'] if a['id'] == base['study']['assignment_id'])
    layer = next(l for l in assignment['layers'] if l['id'] == base['study']['layer_id'])
    rows = [{'field': key, 'before': layer['values'][key], 'after': value}
            for key, value in case['values'].items() if layer['values'][key] != value]
    if case['color_mode'] == 'white':
        rows.append({'field': 'base_color', 'before': 'texture' if 'base_color' in layer['channels'] else layer['values']['base_color'],
                     'after': [.65, .65, .65, 1], 'scope': 'target layer only'})
    return rows


def parameter_warnings(layer, case):
    """Report why a requested knob may not influence the named surface."""
    warnings = []
    if not layer['enabled'] or layer['opacity'] == 0:
        warnings.append({'code': 'target_layer_inactive', 'recommendation': 'Check layer enabled/opacity before interpreting comparisons'})
    for field in case['values']:
        if case['values'][field] == layer['values'][field]: continue
        if field in ('roughness', 'metallic') and field in layer['channels']:
            warnings.append({'code': 'fallback_overridden_by_texture', 'field': field,
                             'recommendation': 'This constant is a fallback. Use texture/adjustment controls instead of repeating this numeric sweep'})
        if field == 'normal_strength' and 'normal' not in layer['channels']:
            warnings.append({'code': 'normal_strength_without_normal_channel', 'field': field,
                             'recommendation': 'No NormalMap uses this knob; check channel intent before interpreting images'})
    return warnings


def run(params, job):
    from material_workflow_addon.texture_analysis import analyze_manifest
    from material_workflow_addon import preview
    params = normalize_params(params)
    job = Path(job).resolve()
    started = time.monotonic()
    report = {'report_version': '1.0', 'operation': 'material.study', 'status': 'running',
              'report': str(job / 'study-report.json'), 'source': {k: params[k] for k in ('file', 'expected_sha256')},
              'source_saved': False, 'study': params['study'], 'cases': [], 'semantic_analysis': None,
              'progress': {}, 'timings': {}, 'sheets': [], 'limitations': [
                  'Bounded candidates, no automatic artistic scoring or parameter selection',
                  'White mode changes only the named layer; other layers remain active',
                  'Semantic statistics are sampled warnings, not physical calibration or alignment proof',
                  'Each candidate preserves batch guards and independently verified checkpoints',
                  'Study report references are not relocated resumable jobs']}
    durations = {'semantic_analysis': 0.0, 'candidate_batches': 0.0, 'contact_sheets': 0.0}
    current = None
    recovery_snapshots = {}
    planned_cases = len(params['study']['cases'])
    views_per_case = len(params['manifest']['preview']['views'])
    assignment = next(a for a in params['manifest']['assignments'] if a['id'] == params['study']['assignment_id'])
    target_layer = next(l for l in assignment['layers'] if l['id'] == params['study']['layer_id'])
    report['baseline'] = {'target': assignment['target'], 'material': assignment['material'],
                          'layer_id': target_layer['id'], 'enabled': target_layer['enabled'],
                          'opacity': target_layer['opacity'], 'texture_roles': list(target_layer['channels']),
                          'values': {key: target_layer['values'][key] for key in ('normal_strength', 'roughness', 'metallic',
                              'ior', 'coat_weight', 'coat_roughness', 'coat_ior')},
                          'value_scope': 'Requested constants; image channels override fallback roughness/metallic'}

    def publish(stage, child=None):
        if current and child:
            case_dir = job / 'cases' / current
            checkpoint = child.get('checkpoint')
            signature = (checkpoint.get('expected_sha256') if checkpoint else None,
                         child.get('completed_assignments'), child.get('completed_views'))
            previous = recovery_snapshots.get(current)
            if checkpoint and (previous is None or previous['signature'] != signature):
                index = previous['index'] + 1 if previous else 1
                snapshot = case_dir / ('batch-recovery-%03d.json' % index)
                if snapshot.exists(): raise Failure('CONFLICT', 'Recovery snapshot already exists')
                atomic_json(snapshot, read_json(case_dir / 'batch-report.json'))
                recovery_snapshots[current] = {'signature': signature, 'index': index, 'report': reference(snapshot)}
            # Before the first checkpoint this reference is informational; no work is reusable yet.
            cached = recovery_snapshots.get(current)
            report['active_case'] = {'id': current, 'report': cached['report'] if cached else reference(case_dir / 'batch-report.json'),
                                     'checkpoint_verified': bool(cached)}
        elif current is None:
            report['active_case'] = None
        complete_views = sum(c['counts']['completed_views'] for c in report['cases'])
        child_views = child.get('completed_views', 0) if child else 0
        elapsed = time.monotonic() - started
        report['timings'] = {'elapsed_seconds': elapsed, 'phase_seconds': dict(durations),
                             'unallocated_or_active_seconds': max(0, elapsed - sum(durations.values())),
                             'scope': 'Disjoint completed outer phases; active batch has its own disjoint timings'}
        report['seconds'] = elapsed
        report['progress'] = {'stage': stage, 'status': report['status'], 'current_case': current,
                              'planned_cases': planned_cases, 'completed_cases': len(report['cases']),
                              'planned_views': planned_cases * views_per_case,
                              'completed_views': complete_views + child_views,
                              'elapsed_seconds': elapsed, 'current_batch': deepcopy(child),
                              'next_action': 'Review sheets, parameter differences and warnings; choose a candidate manually'
                              if report['status'] in ('pass', 'partial') else 'Wait for owned work; inspect failures before retry'
                              if report['status'] == 'running' else 'Inspect error and retained reports before retry',
                              'retry_case_ids': [c['id'] for c in report['cases'] if c['status'] != 'pass']}
        atomic_json(job / 'study-report.json', report)
        atomic_json(job / 'study-progress.json', report['progress'])

    try:
        publish('preflight')
        with ExitStack() as guards:
            source_guard = guards.enter_context(FileGuard(params['file']))
            if source_guard.sha256() != params['expected_sha256']:
                raise Failure('CONFLICT', 'Study source SHA differs')
            prior_cases = {}
            prior_root = None
            if 'resume' in params:
                resume = params['resume']
                guard = guards.enter_context(FileGuard(resume['file']))
                if guard.sha256() != resume['expected_sha256']:
                    raise Failure('CONFLICT', 'Study recovery report SHA differs')
                prior = read_json(resume['file'])
                if prior.get('operation') != 'material.study' or prior.get('report_version') != '1.0':
                    raise Failure('INVALID_REQUEST', 'Study resume requires a study report')
                prior_root = Path(resume['file']).resolve().parent
                prior_cases = {c['id']: c for c in prior.get('cases', [])}
                active = prior.get('active_case')
                if isinstance(active, dict) and active.get('checkpoint_verified') and active.get('report'):
                    prior_cases.setdefault(active['id'], active)
            tick = time.monotonic()
            if params['study']['semantic_analysis']:
                report['semantic_analysis'] = analyze_manifest(params['manifest'])
                atomic_json(job / 'texture-analysis.json', report['semantic_analysis'])
                report['semantic_analysis_ref'] = reference(job / 'texture-analysis.json')
            durations['semantic_analysis'] += time.monotonic() - tick
            publish('semantic_analysis_complete')
            (job / 'cases').mkdir()
            carry_resume = None
            for case, batch_params in expand_cases(params):
                current = case['id']
                case_dir = job / 'cases' / current
                case_dir.mkdir()
                old = prior_cases.get(current)
                resume_origin = {'kind': 'none'}
                if old and old.get('report'):
                    document = old['report']
                    path = Path(document['file']).resolve()
                    valid_name = path.name == 'batch-report.json' or re.fullmatch(r'batch-recovery-[0-9]+\.json', path.name)
                    if not path.is_relative_to(prior_root) or path == prior_root or not valid_name:
                        raise Failure('INVALID_REQUEST', 'Recovery batch report must belong to the prior study')
                    old_guard = guards.enter_context(FileGuard(path))
                    if old_guard.sha256() != document['expected_sha256']:
                        raise Failure('CONFLICT', 'Recovery batch report SHA differs')
                    batch_params['resume'] = document
                    resume_origin = {'kind': 'prior_study_case', 'case_id': current}
                elif carry_resume:
                    batch_params['resume'] = carry_resume['report']
                    resume_origin = {'kind': 'previous_case', 'case_id': carry_resume['id']}
                atomic_json(case_dir / 'request-params.json', batch_params)
                tick = time.monotonic()
                try:
                    result = material_batch.run(batch_params, case_dir,
                                                observer=lambda p: publish(current + ':' + p['stage'], p))
                except Failure as error:
                    # Batch writes its complete failure evidence before raising.
                    # Preserve ordinary candidate failures and continue independent candidates.
                    result = read_json(case_dir / 'batch-report.json')
                    result['status'] = 'failed'
                    result['error'] = {'code': error.code, 'message': str(error)}
                durations['candidate_batches'] += time.monotonic() - tick
                outputs = [{k: o[k] for k in ('id', 'status', 'file', 'sha256', 'bytes', 'reused', 'error', 'code') if k in o}
                           for o in result['outputs']]
                row = {'id': current, 'status': result['status'], 'values': case['values'],
                       'color_mode': case['color_mode'], 'changes': changes(params, case),
                       'parameter_warnings': parameter_warnings(target_layer, case),
                       'counts': {'assignments': len(result['assignments']), 'completed_views': len(outputs),
                                  'successful_views': sum(o['status'] == 'pass' for o in outputs)},
                       'outputs': outputs, 'resume': result['resume'], 'resume_origin': resume_origin, 'timings': result.get('timings'),
                       'report': reference(case_dir / 'batch-report.json')}
                if result.get('candidate'):
                    row['candidate'] = {'file': result['candidate'], 'expected_sha256': result['candidate_sha256']}
                if result.get('error'): row['error'] = result['error']
                report['cases'].append(row)
                if result['status'] == 'pass' and result.get('checkpoint'):
                    carry_resume = {'id': current, 'report': row['report']}
                current = None
                publish('case_complete:' + case['id'])
                if result.get('error', {}).get('code') in ('CONFLICT', 'CANCELLED', 'TIMEOUT'):
                    raise Failure(result['error']['code'], result['error']['message'])
            tick = time.monotonic()
            images = [{**o, 'id': c['id'] + '/' + o['id']} for c in report['cases'] for o in c['outputs']]
            for offset in range(0, len(images), 24):
                report['sheets'].append(preview.compose_sheet(images[offset:offset+24],
                    job / ('study-contact-%02d.png' % (offset // 24 + 1)), max_edge=1536))
            durations['contact_sheets'] += time.monotonic() - tick
            if source_guard.sha256() != params['expected_sha256']:
                raise Failure('CONFLICT', 'Study source changed')
            passed = sum(c['status'] == 'pass' for c in report['cases'])
            report['status'] = 'pass' if passed == planned_cases else 'partial' if passed else 'failed'
            if any(s['status'] != 'pass' for s in report['sheets']) and passed:
                report['status'] = 'partial'
            if report['semantic_analysis'] and report['semantic_analysis']['status'] == 'failed' and passed:
                report['status'] = 'partial'
            report['source_unchanged'] = True
            publish(report['status'])
    except Exception as error:
        report['status'] = 'failed'
        report['error'] = {'code': getattr(error, 'code', 'WORKER_FAILED'), 'message': str(error)}
    finally:
        publish(report['status'])
    if report['status'] == 'failed':
        err = report.get('error', {'code': 'VALIDATION_FAILED', 'message': 'No study candidate passed'})
        raise Failure(err['code'], err['message'])
    return report
