"""Public owned shot producer and strict complete-shot cold reader.

All original single-frame producer gates remain unchanged. The CG/relative RGB
profiles share their deny-write/delete guards, runtime identity, source freezing,
owned bootstrap/Job teardown, lossless numerical readers and lighting kernel.
"""
from contextlib import ExitStack
import copy
from pathlib import Path
import time

from compositing_depth_public import (BoundedInputs, descriptor, row, read, publish,
    need, key, runtime_identity, freeze_codes, resources_used,
    launch_identity as depth_identity, code_signature)
from compositing_relight_public import owned_stage
from compositing_relight_public import gpu_admission, protect_native_body, NATIVE_PYTHON
from compositing_depth_estimate_public import ModelInputs, profile_admission
from compositing_relight_sequence_contract import (normalize_params, signature,
    input_descriptors, shot_key, DepthError)


def launch_identity():
    base = Path(__file__).parent
    sources = depth_identity()['files'] + [
        row(base / 'compositing_relight_sequence_contract.py', 'sequence:contract'),
        row(base / 'compositing_relight_sequence_public.py', 'sequence:public'),
        row(base / 'compositing_relight_sequence_worker.py', 'sequence:worker'),
        row(base / 'compositing_relight_sequence_gpu_worker.py', 'sequence:GPU_worker'),
        row(base / 'compositing_relight_kernel.py', 'sequence:kernel'),
        row(base / 'compositing_relight_color_io.py', 'sequence:color'),
        row(base / 'compositing_relight_public.py', 'sequence:owner'),
        row(base / 'compositing_relight_contract.py', 'sequence:relight_contract'),
        row(base / 'compositing_relight_session_owned_worker.py', 'sequence:actual_owner'),
        row(base / 'compositing_depth_estimate_worker.py', 'sequence:import_boundary'),
    ]
    return {'profile': 'depth-sequence-public-v1', 'files': sources, 'sha256': code_signature(sources)}


def stage_keys(params, identity, raw_members=None):
    # Reordering or changing ANY raw/validity invalidates the common statistics
    # and every derived/lighting frame. Recipe-only changes do not change raw keys.
    raw = [{'frame': {k: f[k] for k in ('source_frame', 'scene_frame', 'stage_frame', 'time')},
            'RGB': f['rgb'], 'depth': f['depth'], 'validity': f['validity']} for f in params['frames']]
    statistics = signature({'raw': raw, 'statistics': params['statistics'], 'implementation': identity['sha256']})
    frames = [signature({'frame': f, 'statistics': statistics, 'normal_source': params['normal_source'],
                         'jump': params['max_relative_jump'], 'config': params['config'],
                         'implementation': identity['sha256']}) for f in params['frames']]
    if params['source']['kind'] == 'RGB_DAV2_LARGE' and raw_members is not None:
        rows = sorted([{'stage_frame': r['stage_frame'], 'sha256': r['sha256']} for r in raw_members if r['role'] == 'raw'],
                      key=lambda r: r['stage_frame'])
        need([r['stage_frame'] for r in rows] == list(range(len(params['frames']))), 'Realized raw inventory incomplete or duplicated')
        statistics = signature({'dependency_key': statistics, 'raw_members': rows, 'validity': 'all_frame_artistic_declared'})
        frames = [signature({'dependency_frame_key': frame, 'statistics_key': statistics}) for frame in frames]
    return statistics, frames


def expected_roles(original):
    cg = original['source']['kind'] == 'declared_CG_geometry_pass'
    roles = {'raw', 'source_rgba', 'scalar', 'validity', 'normal_derived', 'normal_derived_validity',
             'light_mask', 'highlight_mask', 'line_mask', 'output', 'delta', 'linear_straight_exr', 'display_srgb_png'}
    if cg:
        roles |= {'normal_imported', 'normal_imported_validity'}
    else:
        roles.add('model_rgb8')
    lights = original['config']['lights'] if not original['config']['bypass'] and original['config']['mix'] > 0 else []
    roles |= {'light:%s:%s' % (light['id'], name) for light in lights if light['intensity'] > 0
              for name in ('diffuse', 'specular', 'visibility', 'delta')}
    return roles


def execute_stages(params, target, inputs, runtime, frozen, checkpoint, bundle, identity, resume, reuse):
    """One CPU shot, or owned pack + ONE actual GPU batch + owned encode."""
    original = bundle['request']['params'] if bundle else params
    statistics, keys = stage_keys(original, identity)
    worker = next(r for r in frozen if r['role'] == 'sequence:worker')
    selected = [r for r in frozen if r['role'] != 'depth_worker']
    selected.append(dict(worker, role='depth_worker'))
    packed, computed, reports = [], [], []
    gpu = None
    namespace = trusted = None
    if params['mode'] == 'create' and params['compute']['device'] == 'CUDA0_FP32':
        if params['source']['kind'] == 'RGB_DAV2_LARGE':
            gpu, qualification, namespace, trusted = profile_admission({'runtime_profile': params['source']['runtime_profile']}, inputs)
            # Capture trusted package + official source members for pre-import gate.
            inventory_files = list(inputs.files.values())
        else:
            gpu, inventory_files, namespace, trusted = gpu_admission(params['compute']['runtime_profile'], inputs, checkpoint)
        native_protection = protect_native_body(inputs)
        inventory_path = target / 'gpu-runtime-inventory.json'
        publish(inventory_path, inventory_files)
        inv = row(inventory_path)
        inputs.protect(descriptor(inv))

    def stage(mode, folder_name, gpu_compute=False):
        nonlocal packed, computed
        folder = target / folder_name
        out = folder / 'depth'
        out.mkdir(parents=True, exist_ok=False)
        codes = selected
        if gpu_compute:
            codes = [r for r in selected if r['role'] != 'depth_worker'] + [dict(next(r for r in frozen if r['role'] == 'sequence:GPU_worker'), role='depth_worker')]
        # Host retains the ENTIRE immutable source/runtime/model closure across
        # all stages and performs its full post-SHA once. Child stages rehash
        # only actual consumed numeric/code files, as the mature single-image
        # consumer does; OIIO stages do not import or consume16k CUDA payloads.
        if mode == 'pack' or mode == 'create':
            needed = [row(d['file']) for d in input_descriptors(params['frames'])]
            if mode == 'pack':
                needed += reuse['raw']
            if mode == 'create':
                needed += [r for entry in resume['frames'] for r in entry['members']]
        elif mode == 'compute':
            needed = packed + resume['raw'] + reuse['raw'] + [r for entry in resume['frames'] for r in entry['members']] + [inv]
        elif mode == 'encode':
            needed = [r for r in packed if r['role'] == 'source_rgba'] + [r for r in computed if r['role'] == 'output']
        else:
            needed = bundle['members']
        needed = needed + codes + ([] if gpu_compute else runtime['files'])
        needed = list({key(r['file']): r for r in needed}.values())
        for r in needed:
            need(inputs.files.get(key(r['file']), {}).get('sha256') == r['sha256'], 'Stage input absent from live Host guards')
        binding = {'mode': mode, 'params': params, 'domain': original['domain'], 'bundle': bundle,
            'request_signature': signature(params), 'codes': codes, 'runtime': runtime,
            'timeout_seconds': original['budget']['timeout_seconds'], 'input_files': needed,
            'statistics_key': statistics, 'frame_keys': keys, 'shot_key': shot_key(original, identity['sha256']),
            'packed': packed, 'computed': computed, 'resumed_raw': resume['raw'], 'resumed_frames': resume['frames'],
            'reused_raw': reuse['raw'], 'raw_reuse_authority': reuse['authority']}
        if gpu_compute:
            binding.update(profile=gpu, inventory=inv, native_python=NATIVE_PYTHON,
                           native_body_protection=native_protection,
                           runtime={'python': row(gpu['python']['file'])})
            if params['source']['kind'] == 'RGB_DAV2_LARGE':
                from compositing_depth_estimate_contract import expected_tensor_shape
                binding['expected_tensor_shape'] = expected_tensor_shape(params['domain'])
        publish(out / 'binding.json', binding)
        inputs.protect(descriptor(row(out / 'binding.json')))
        report, lifecycle = owned_stage(folder, binding, checkpoint, gpu_compute)
        need(lifecycle.get('tree_handle_closed') and lifecycle.get('root_returncode_after_reap') == 0,
             'Owned sequence stage was not actually reaped')
        for r in report['members']:
            need(Path(r['file']).is_relative_to(out) and type(r['bytes']) is int and
                 Path(r['file']).stat().st_size == r['bytes'], 'Sequence member escaped or size differs')
            inputs.protect(descriptor(r))
        inputs.protect(descriptor(row(out / 'worker-report.json')))
        reports.append({'mode': mode, 'report': row(out / 'worker-report.json'), 'lifecycle': row(folder / 'relight-lifecycle.json')})
        if mode == 'pack':
            packed = report['members']
        if mode == 'compute':
            computed = report['members']
        return report

    if gpu:
        stage('pack', 'pack')
        numeric = stage('compute', 'owned', True)
        encoded = stage('encode', 'encode')
        need(namespace() == trusted, 'Guarded GPU namespace changed')
        all_members = packed + computed + encoded['members']
        # Keep exact frame-major order independent of storage stage.
        numeric['members'] = [r for i in range(len(params['frames'])) for r in all_members if r['stage_frame'] == i]
        for f in numeric['frames']:
            f['member_sha256'] = [r['sha256'] for r in numeric['members'] if r['stage_frame'] == f['stage_frame']]
        return numeric, reports
    report = stage(params['mode'], 'owned')
    return report, reports


def protect(inputs, d):
    return read(inputs.protect(descriptor(d)))


def resume_admission(params, inputs, identity):
    """Read only terminal owned partials; never promote them to a consumer."""
    d = params.get('resume')
    if d is None:
        return {'raw': [], 'frames': [], 'authority': None}
    path = Path(d['file'])
    need(path.name == 'sequence-resume.json', 'Resume authority filename differs')
    authority = protect(inputs, d)
    parent = path.parent
    need(authority.get('schema') == 'compositing-depth-sequence-resume-v1' and
         authority.get('consumer_eligible') is False and authority.get('parent_job_id') == parent.name and
         len(parent.name) == 32, 'Invalid partial resume authority')
    status = protect(inputs, row(parent / 'status.json'))
    need(status.get('job_id') == parent.name and status.get('state') in ('failed', 'cancelled', 'timed_out', 'succeeded') and
         type(status.get('exit_code')) is int and status.get('finished_at', 0) > 0, 'Resume parent must be terminal')
    request = protect(inputs, authority['request'])
    launch = protect(inputs, authority['launch'])
    need(Path(authority['request']['file']) == parent / 'request.json' and
         Path(authority['launch']['file']) == parent / 'launch.json', 'Resume parent documents escaped')
    original = normalize_params(request.get('params'))
    need(request.get('command') == 'compositing.depth-sequence' and original['mode'] == 'create' and
         launch.get('sequence_identity') == identity == authority['producer_identity'] and
         launch.get('sequence_request_signature') == signature(original) and
         authority['shot_key'] == shot_key(original, identity['sha256']) == shot_key(params, identity['sha256']),
         'Resume input, recipe, model, source identity or shot mapping changed')
    raw, frames = [], []
    root = parent / 'sequence/owned'
    lifecycle_path = root / 'relight-lifecycle.json'
    if not lifecycle_path.is_file():
        return {'raw': raw, 'frames': frames, 'authority': d}
    life = protect(inputs, row(lifecycle_path))
    need(life.get('tree_handle_closed') is True and type(life.get('root_returncode_after_reap')) is int,
         'Partial actual owned process was not reaped')
    actual = life.get('actual_worker')
    if actual is not None:
        need(actual.get('exit_confirmed') is True and actual.get('handle_closed') is True,
             'Partial actual worker remains unconfirmed')
    statistics, keys = stage_keys(original, identity)
    for i in range(len(original['frames'])):
        folder = root / 'depth' / ('f%03d' % i)
        raw_checkpoint = folder / 'raw-complete.json'
        if raw_checkpoint.is_file():
            cp = protect(inputs, row(raw_checkpoint))
            r = cp.get('raw', {})
            need(cp.get('schema') == 'sequence-raw-checkpoint-v1' and
                 cp.get('shot_key') == authority['shot_key'] and cp.get('statistics_key') == statistics and
                 cp.get('stage_frame') == r.get('stage_frame') == i and r.get('role') == 'raw' and
                 Path(r['file']).parent == folder and Path(r['file']).stat().st_size == r['bytes'], 'Partial raw identity differs')
            inputs.protect(descriptor(r))
            raw.append(r)
    if original['source']['kind'] == 'RGB_DAV2_LARGE' and len(raw) == len(original['frames']):
        statistics, keys = stage_keys(original, identity, raw)
    for i in range(len(original['frames'])):
        folder = root / 'depth' / ('f%03d' % i)
        frame_checkpoint = folder / 'frame-complete.json'
        if frame_checkpoint.is_file():
            cp = protect(inputs, row(frame_checkpoint))
            frame = cp.get('frame', {})
            need(cp.get('shot_key') == authority['shot_key'] and cp.get('consumer_eligible') is False and
                 frame.get('stage_frame') == i and frame.get('frame_key') == keys[i] and frame.get('statistics_key') == statistics,
                 'Partial frame identity differs')
            members = cp.get('members')
            need(type(members) is list and len(members) <= 64 and len({r['role'] for r in members}) == len(members), 'Partial roles differ')
            cg = original['source']['kind'] == 'declared_CG_geometry_pass'
            if original['compute']['device'] == 'CPU_NUMPY':
                roles = expected_roles(original)
            else:
                roles = {'output', 'delta'} | {r for r in expected_roles(original) if r.startswith('light:')}
                if not cg:
                    roles |= {'scalar', 'validity', 'normal_derived', 'normal_derived_validity'}
            need({r['role'] for r in members} == roles, 'Partial frame lacks full expected stage role closure')
            for r in members:
                need(Path(r['file']).parent == folder and r['stage_frame'] == i and Path(r['file']).stat().st_size == r['bytes'],
                     'Partial frame member escaped or size differs')
                inputs.protect(descriptor(r))
            frames.append({'frame': frame, 'members': members})
    return {'raw': raw, 'frames': frames, 'authority': d}


def inspect_admission(params, inputs, identity):
    path = Path(params['bundle']['file'])
    bundle = protect(inputs, params['bundle'])
    parent = path.parent
    need(path.name == 'sequence-bundle.json' and len(parent.name) == 32 and
         all(c in '0123456789abcdef' for c in parent.name), 'Shot must belong to a public job')
    need(bundle.get('schema') == 'compositing-depth-sequence-bundle-v1' and
         bundle.get('status') == 'complete' and bundle.get('parent_job_id') == parent.name,
         'Incomplete shot cannot be consumed')
    docs = {name: protect(inputs, row(parent / name)) for name in ('status.json', 'result.json', 'request.json', 'launch.json')}
    state, result, request, launch = [docs[n] for n in ('status.json', 'result.json', 'request.json', 'launch.json')]
    need(state.get('job_id') == parent.name and state.get('state') == 'succeeded' and
         type(state.get('exit_code')) is int and state['exit_code'] == 0 and
         type(state.get('finished_at')) in (int, float) and state['finished_at'] > 0,
         'Public sequence parent has not succeeded')
    need(result.get('ok') is True and result.get('error') is None and result.get('job_id') == parent.name and
         result.get('command') == request.get('command') == 'compositing.depth-sequence' and
         result.get('schema_version') == request.get('schema_version') == '1.0', 'Public parent envelope differs')
    original = normalize_params(request.get('params'))
    need(original['mode'] == 'create' and original == bundle['request']['params'] and
         signature(original) == bundle['request_signature'] == result.get('data', {}).get('request_signature'),
         'Shot request/result signatures differ')
    historical = bundle['producer_identity']
    need(launch.get('sequence_identity') == historical and launch.get('sequence_request_signature') == bundle['request_signature'],
         'Parent launch differs')
    need(historical['sha256'] == identity['sha256'] and code_signature(bundle['frozen_sources']) == historical['sha256'],
         'Cold reader has not qualified this producer source profile', 'UNSUPPORTED')
    need(result['data'].get('bundle') == row(path) and
         result.get('artifacts', {}).get('sequence_bundle') == str(path), 'Public result does not bind complete shot')
    for name in ('request.json', 'launch.json'):
        need(row(parent / name) == bundle['parent_documents'][name], 'Bound parent authority changed')
    report = protect(inputs, bundle['report'])
    need(report.get('status') == 'passed' and report.get('request_signature') == bundle['request_signature'] and
         report.get('members') == bundle['members'] and report.get('frames') == bundle['frames'], 'Bound report differs')
    statistics, frames = stage_keys(original, historical, bundle['members'])
    need(bundle['statistics_key'] == statistics and bundle['frame_keys'] == frames and
         bundle['shot_key'] == shot_key(original, historical['sha256']), 'Frozen shot/stage identities differ')
    need(len(bundle['frames']) == len(original['frames']) and [f['stage_frame'] for f in bundle['frames']] == list(range(len(frames))),
         'Shot frame inventory incomplete or reordered')
    for i in params['frame_order']:
        need(i < len(frames), 'Cold visit is outside actual shot')
    roles = expected_roles(original)
    for i, frame in enumerate(bundle['frames']):
        selected = [r for r in bundle['members'] if r['stage_frame'] == i]
        need(len(selected) == len(roles) and {r['role'] for r in selected} == roles and
             frame['frame_key'] == frames[i] and frame['statistics_key'] == statistics and
             frame['member_sha256'] == [r['sha256'] for r in selected], 'Frame member role/identity closure differs')
    need(len(bundle['members']) == len(frames) * len(roles), 'Unexpected shot members')
    seen = set()
    for r in bundle['members']:
        p = Path(r['file'])
        allowed = [parent / ('sequence/' + stage + '/depth') / ('f%03d' % r['stage_frame']) for stage in ('owned', 'pack', 'encode')]
        need(p.parent in allowed and key(p) not in seen and
             type(r['bytes']) is int and p.stat().st_size == r['bytes'], 'Member escaped, duplicated or truncated')
        seen.add(key(p))
        inputs.protect(descriptor(r))
    for r in bundle['frozen_sources']:
        need(Path(r['file']).parent == parent / 'sequence/source', 'Frozen source escaped')
        inputs.protect(descriptor(r))
    for d in input_descriptors(original):
        inputs.protect(d)
    return bundle, original


def raw_reuse_admission(params, inputs, identity):
    """A new create consumes a successful full producer, never a partial."""
    authority = params.get('reuse_raw')
    if authority is None:
        return {'raw': [], 'authority': None}
    bundle, original = inspect_admission({'bundle': authority, 'frame_order': []}, inputs, identity)
    need(original['source']['kind'] == params['source']['kind'] == 'RGB_DAV2_LARGE' and
         original['compute']['device'] == params['compute']['device'] == 'CUDA0_FP32',
         'Completed raw source must be qualified RGB CUDA', 'UNSUPPORTED')

    def raw_dependency(p):
        q = copy.deepcopy(p)
        # These only affect fresh derived/lighting stages or execution limits.
        for name in ('config', 'budget', 'resume', 'reuse_raw', 'resources', 'max_relative_jump', 'normal_source'):
            q.pop(name, None)
        for frame in q['frames']:
            frame.pop('masks')
        return q

    need(raw_dependency(original) == raw_dependency(params),
         'Completed raw source, ordered RGB/SHA/time/domain/model/profile/statistics differs')
    raw = sorted([r for r in bundle['members'] if r['role'] == 'raw'], key=lambda r: r['stage_frame'])
    need([r['stage_frame'] for r in raw] == list(range(len(params['frames']))) and
         all(r['shape'] == [params['domain']['height'], params['domain']['width']] and
             r['dtype'] == 'float32' for r in raw), 'Completed raw inventory/domain/FP32 differs')
    # inspect_admission already holds every member under live guards. The owned
    # pack stage independently decodes every raw before any GPU model decision.
    return {'raw': raw, 'authority': authority}


def run(params, job, cancel_parent, expected_identity):
    params = normalize_params(params)
    job = Path(job).resolve()
    target = job / 'sequence'
    started = time.monotonic()
    try:
        need(not target.exists(), 'Fresh owned sequence job required')
        target.mkdir(exist_ok=False)
        identity = launch_identity()
        launch = read(job / 'launch.json')
        need(identity == expected_identity == launch.get('sequence_identity') and
             launch.get('sequence_request_signature') == signature(params), 'Queued shot source/request changed')

        def checkpoint():
            cancel_parent()
            need(not (job / 'cancel.request').exists(), 'Public shot cancelled', 'CANCELLED')
        with ExitStack() as stack:
            inputs = ModelInputs(stack, checkpoint)
            for name in ('request.json', 'launch.json'):
                inputs.protect(descriptor(row(job / name)))
            for r in identity['files']:
                inputs.protect(descriptor(r))
            for d in input_descriptors(params):
                inputs.protect(d)
            bundle = None
            if params['mode'] == 'inspect':
                bundle, original = inspect_admission(params, inputs, identity)
                domain = original['domain']
                budget = original['budget']
            else:
                original, domain, budget = params, params['domain'], params['budget']
                # Producer evidence is declared, guarded source material. Never
                # label it independently verified model/geometry qualification.
                evidence = protect(inputs, params['source']['producer_evidence'])
                need(type(evidence) is dict, 'CG producer evidence must be strict JSON object')
            resources_used(params, list(inputs.files.values()))
            runtime = runtime_identity(checkpoint)
            for r in runtime['files']:
                inputs.protect(descriptor(r))
            if bundle:
                need(runtime == bundle['runtime'], 'Cold numeric runtime differs', 'UNSUPPORTED')
            frozen = freeze_codes(identity['files'], target, inputs)
            statistics, frame_keys = stage_keys(original, identity)
            resume = resume_admission(params, inputs, identity) if params['mode'] == 'create' else {'raw': [], 'frames': [], 'authority': None}
            reuse = raw_reuse_admission(params, inputs, identity) if params['mode'] == 'create' else {'raw': [], 'authority': None}
            if params['mode'] == 'create':
                publish(job / 'sequence-resume.json', {'schema': 'compositing-depth-sequence-resume-v1',
                    'consumer_eligible': False, 'parent_job_id': job.name, 'producer_identity': identity,
                    'request': row(job / 'request.json'), 'launch': row(job / 'launch.json'),
                    'shot_key': shot_key(params, identity['sha256']), 'statistics_key': statistics})
            report, stages = execute_stages(params, target, inputs, runtime, frozen, checkpoint, bundle, identity, resume, reuse)
            if params['mode'] == 'create':
                statistics, frame_keys = stage_keys(params, identity, report['members'])
                need(report['stats']['whole_shot']['statistics_key'] == statistics and
                     [f['frame_key'] for f in report['frames']] == frame_keys,
                     'Realized whole-shot raw/statistics/frame identities differ')
            inputs.recheck()
            checkpoint()
            final = {'schema': 'compositing-depth-sequence-report-v1', 'status': 'passed',
                     'operation': 'depth-sequence', 'mode': params['mode'], 'request_signature': signature(params),
                     'frame_count': len(bundle['frames']) if bundle else len(report['frames']),
                     'members': report['members'], 'frames': report['frames'], 'stats': report['stats'],
                     'runtime': runtime, 'producer_identity': identity, 'frozen_sources': frozen,
                     'stages': stages,
                     'input_protection': 'live deny-write/delete plus ancestor guards and pre/post SHA',
                     'host_elapsed_seconds': time.monotonic() - started,
                     'CG_source_producer_verified': False, 'model_executed': report['stats'].get('model_executed', False),
                     'display_quality_qualified': False, 'temporal_quality_qualified': False,
                     'recovery_qualified': False, 'public_parent_success_required': True}
            final['recovery'] = {'authority': resume['authority'], 'admitted_raw_frames': len(resume['raw']),
                                 'admitted_complete_frames': len(resume['frames']), 'partial_consumer_eligible': False}
            final['raw_reuse'] = {'authority': reuse['authority'], 'admitted_raw_frames': len(reuse['raw']),
                'source_raw_members': reuse['raw'], 'ownership': 'byte-exact copies in new job; original guarded through post-SHA',
                'new_inference_claimed': False if reuse['authority'] else report['stats'].get('model_executed', False)}
            publish(job / 'sequence-report.json', final)
            if params['mode'] == 'create':
                complete = {'schema': 'compositing-depth-sequence-bundle-v1', 'status': 'complete',
                    'parent_job_id': job.name, 'request': {'params': params}, 'request_signature': signature(params),
                    'parent_documents': {n: row(job / n) for n in ('request.json', 'launch.json')},
                    'report': row(job / 'sequence-report.json'), 'members': final['members'], 'frames': final['frames'],
                    'statistics_key': statistics, 'frame_keys': frame_keys,
                    'shot_key': shot_key(params, identity['sha256']), 'runtime': runtime,
                    'producer_identity': identity, 'frozen_sources': frozen,
                    'public_parent_success_required': True, 'temporal_quality_qualified': False,
                    'display_quality_qualified': False, 'recovery_qualified': False}
                publish(job / 'sequence-bundle.json', complete)
                checkpoint()
                final['bundle'] = row(job / 'sequence-bundle.json')
            return final
    except Exception as exc:
        if not (job / 'sequence-failure.json').exists():
            publish(job / 'sequence-failure.json', {'status': 'failed', 'error': {
                'code': getattr(exc, 'code', 'WORKER_FAILED'), 'message': str(exc)}, 'partial_eligible': False})
        raise
