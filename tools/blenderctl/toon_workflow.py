"""Protected Toon worker: sealed preflight, native cache, persistence and cold read."""
import json
import math
import os
from pathlib import Path
import subprocess
import time

import bpy

from protocol import Failure, atomic_json, digest, read_json
from toon_identity import implementation_sha256
from toon_workflow_contract import normalize_params, normalize_inspect_params, _domain_error
from toon_workflow_addon import core, contract


def descriptor(path):
    path = Path(path)
    return {'file': str(path), 'sha256': digest(path), 'bytes': path.stat().st_size}


def resource_check(resources):
    declared = {os.path.normcase(str(Path(r['file']).resolve())): r['expected_sha256'] for r in resources}
    rows = []
    for kind in ('images', 'fonts', 'libraries', 'volumes', 'sounds', 'movieclips', 'cache_files'):
        for item in getattr(bpy.data, kind, ()):
            raw = getattr(item, 'filepath', '')
            if not raw or raw == '<builtin>' or getattr(item, 'packed_file', None) or getattr(item, 'packed_files', ()): continue
            if kind in ('volumes', 'cache_files') or getattr(item, 'source', '') in ('TILED', 'SEQUENCE', 'MOVIE') or any(token in raw for token in ('<UDIM>', '####')):
                raise Failure('UNSUPPORTED', 'Dynamic/cached input needs a future immutable member receipt: ' + item.name)
            path = str(Path(bpy.path.abspath(raw, library=item.library)).resolve())
            wanted = declared.get(os.path.normcase(path))
            if wanted is None: raise Failure('CONFLICT', 'Undeclared static resource: ' + path)
            if not Path(path).is_file() or digest(path) != wanted: raise Failure('CONFLICT', 'Static resource SHA differs: ' + path)
            rows.append({'kind': kind, 'owner': item.name, 'file': path, 'sha256': wanted, 'stored_path': raw})
    for tree in bpy.data.node_groups:
        for node in tree.nodes:
            for socket in node.inputs:
                if socket.bl_idname != 'NodeSocketStringFilePath': continue
                if socket.is_linked: raise Failure('UNSUPPORTED', 'Linked file-path socket needs explicit evaluation')
                raw = socket.default_value
                if not raw: continue
                path = str(Path(bpy.path.abspath(raw)).resolve())
                wanted = declared.get(os.path.normcase(path))
                if wanted is None or not Path(path).is_file() or digest(path) != wanted:
                    raise Failure('CONFLICT', 'Node file resource is undeclared or changed: ' + path)
                rows.append({'kind': 'node_path', 'owner': tree.name + '/' + node.name,
                             'file': path, 'sha256': wanted, 'stored_path': raw})
    return {'status': 'closed_for_enumerated_static_paths', 'external': rows,
            'limitations': ['Dynamic caches and sequences reject; opaque plugin state is outside this receipt.']}


def render_frame(scene, frame, path, manifest):
    core.activate(scene); scene.frame_set(frame); bpy.context.view_layer.update()
    settings_check = core.validate_render_contract(scene, manifest)
    if scene.frame_current != frame: raise Failure('VALIDATION_FAILED', 'Render frame context differs')
    shading_sample = None
    if contract.is_animated(manifest):
        from toon_workflow_addon import animation_bpy
        shading_sample = animation_bpy.sample_scope(manifest, frame)
    before = scene.render.image_settings.file_format
    try:
        scene.render.image_settings.file_format = 'PNG'
        bpy.ops.render.render(write_still=False, scene=scene.name)
        if scene.frame_current != frame: raise Failure('VALIDATION_FAILED', 'Render changed the requested scene frame')
        image = bpy.data.images.get('Render Result')
        if image is None: raise Failure('VALIDATION_FAILED', 'Render Result is absent')
        image.save_render(filepath=str(path), scene=scene)
    finally:
        scene.render.image_settings.file_format = before
    from array import array
    decoded_image = bpy.data.images.load(str(path), check_existing=False)
    try:
        pixels = array('f', [0]) * len(decoded_image.pixels)
        decoded_image.pixels.foreach_get(pixels)
        if not pixels or not all(math.isfinite(v) for v in pixels):
            raise Failure('VALIDATION_FAILED', 'Actual PNG decode contains missing/nonfinite pixels')
        decoded = {'images': [{'width': int(decoded_image.size[0]), 'height': int(decoded_image.size[1]),
                               'channels': decoded_image.channels, 'finite': True}]}
    finally:
        bpy.data.images.remove(decoded_image)
    if len(decoded['images']) != 1 or [decoded['images'][0]['width'], decoded['images'][0]['height']] != manifest['render']['resolution']:
        raise Failure('VALIDATION_FAILED', 'Actual decoded PNG dimensions differ from request')
    result = {'frame': frame, **descriptor(path), 'decoded': decoded, 'settings': settings_check}
    if contract.is_animated(manifest):
        if animation_bpy.sample_scope(manifest, frame) != shading_sample:
            raise Failure('VALIDATION_FAILED', 'Render changed native shading parameter evaluation')
        result['shading_sample'] = shading_sample
    return result


def _identity(params):
    if params['implementation_sha256'] != implementation_sha256():
        raise Failure('CONFLICT', 'Toon implementation changed inside worker')


def _open(params):
    _identity(params)
    if digest(params['file']) != params['expected_sha256']: raise Failure('CONFLICT', 'Source SHA changed')
    bpy.ops.wm.open_mainfile(filepath=params['file'], load_ui=False, use_scripts=False)
    return resource_check(params['resources'])


def inspect(params, job):
    params = normalize_inspect_params(params); job = Path(job)
    closure = _open(params)
    try:
        observed = core.inspect_scene(params.get('scene'), params.get('object_names'))
    except contract.WorkflowError as exc:
        _domain_error(exc)
    report = {'toon_report_version': '1.0', 'operation': 'inspect', 'status': 'passed',
              'source': {'file': params['file'], 'sha256': params['expected_sha256']},
              'implementation_sha256': params['implementation_sha256'], 'resources': closure,
              'blender': {'version': bpy.app.version_string, 'build_hash': bpy.app.build_hash.decode()},
              'observed': observed, 'source_saved': False}
    path = job / 'toon-report.json'; atomic_json(path, report)
    return {'status': 'passed', 'report': descriptor(path), 'observed': observed, 'outputs': [descriptor(path)]}


def execution_context(argv=None):
    """Read the explicit launcher CPU budget; never infer it from Scene RNA."""
    import sys
    values = list(sys.argv if argv is None else argv)
    prefix = values[:values.index('--')] if '--' in values else values
    if any(value.startswith('-t') or value.startswith('--threads=') for value in prefix):
        raise Failure('CONFLICT', 'Toon requires canonical explicit --threads N before --')
    positions = [i for i, value in enumerate(prefix) if value == '--threads']
    if len(positions) != 1 or positions[0] + 1 >= len(prefix):
        raise Failure('CONFLICT', 'Toon requires one explicit --threads N before --')
    raw = prefix[positions[0] + 1]
    if not raw.isascii() or not raw.isdecimal() or int(raw) <= 0:
        raise Failure('CONFLICT', 'Toon CPU thread budget must be a positive decimal integer')
    return {'cpu_threads': int(raw)}


def _progress(job, row):
    atomic_json(Path(job) / 'toon-progress.json',
                {'status': 'passed' if row.get('stage') == 'complete' else 'running', **row})


def _checked_document(row, job, name):
    path = Path(row['file'])
    expected = Path(job).resolve() / name
    if (path.resolve() != expected or not path.is_file()
            or digest(path) != row['sha256'] or path.stat().st_size != row['bytes']):
        raise Failure('CONFLICT', 'Pinned phase document changed: ' + str(path))
    return path


def produce(params, job):
    """Only this short-lived process mutates the source working copy."""
    params = normalize_params(params); job = Path(job); started = time.monotonic()
    cpu_context = execution_context()
    report = {'toon_report_version': '1.1', 'operation': 'run', 'status': 'produced',
              'finalization': {'status': 'pending_independent_reopen'}, 'manifest': params['manifest'],
              'source': {'file': params['file'], 'sha256': params['expected_sha256']},
              'implementation_sha256': params['implementation_sha256'], 'execution_context': cpu_context,
              'recovery': {'strategy': 'restart_from_original_source_and_request_in_new_job',
                           'partial_candidate_accepted': False, 'resume': 'unsupported'},
              'blender': {'version': bpy.app.version_string, 'build_hash': bpy.app.build_hash.decode()}}
    closure = _open(params); report['resources'] = closure
    baseline = None
    if 'baseline' in params:
        anchor = params['baseline']
        if digest(anchor['file']) != anchor['expected_sha256']: raise Failure('CONFLICT', 'Baseline SHA changed')
        baseline = read_json(anchor['file'])
        if baseline.get('implementation_sha256') != params['implementation_sha256']:
            raise Failure('CONFLICT', 'Baseline implementation changed; preserve the old baseline and rebuild from original input with a reviewed new request')
    plan, expectation = core.prepare_plan(params['manifest'], baseline)
    for name, body in (('toon-manifest.json', params['manifest']), ('toon-plan.json', plan),
                       ('toon-expectation.json', expectation), ('toon-resources.json', closure)):
        atomic_json(job / name, body)
    report['expectation'] = descriptor(job / 'toon-expectation.json')
    report['plan'] = descriptor(job / 'toon-plan.json')
    _progress(job, {'stage': 'preflight_sealed'})
    provenance = []
    evidence_folder = job / 'toon-chain-provenance'; evidence_folder.mkdir()
    def frame_evidence(frame, chains, fingerprint):
        path = evidence_folder / ('frame-%04d.json' % frame)
        payload = contract.canonical(chains).encode('utf-8')
        with path.open('xb') as stream: stream.write(payload)
        row = {'frame': frame, 'schema': fingerprint['schema'], **descriptor(path)}
        if row['sha256'] != fingerprint['sha256']:
            raise Failure('VALIDATION_FAILED', 'Canonical chain provenance differs from its frame fingerprint')
        provenance.append(row)
    result = core.apply_plan(plan, expectation, baseline,
                             lambda row: _progress(job, row), frame_evidence=frame_evidence)
    report['chain_provenance'] = provenance
    report['checks'] = {'source_content': result['protection'], 'target_content': result['target_content']}
    report['shading'] = result['shading']; report['frames'] = result['frames']
    candidate = job / 'toon-candidate.blend'
    if candidate.exists(): raise Failure('CONFLICT', 'Candidate output already exists')
    _progress(job, {'stage': 'saving_candidate'})
    bpy.ops.wm.save_as_mainfile(filepath=str(candidate), check_existing=False, relative_remap=False)
    report['candidate'] = descriptor(candidate)
    state = {'schema': result['embedded_state']['schema'], 'embedded_state': result['embedded_state'],
             'candidate': report['candidate'], 'source': report['source'],
             'implementation_sha256': params['implementation_sha256'], 'expectation': report['expectation']}
    state_path = job / 'toon-state.json'; atomic_json(state_path, state)
    report['state'] = descriptor(state_path)
    report['metrics'] = {'production_seconds': time.monotonic() - started,
                         'cache_seconds': sum(r['seconds'] for r in result['frames']),
                         'frames': len(result['frames']), 'rendered_frames': 0}
    _identity(params)
    if digest(params['file']) != params['expected_sha256']: raise Failure('CONFLICT', 'Source changed after production')
    report_path = job / 'toon-produce-report.json'; atomic_json(report_path, report)
    return {'report': descriptor(report_path)}


def render_candidate(params, job, report_descriptor):
    """Fresh renderer consumes the sealed producer files; no new baseline."""
    from contextlib import ExitStack
    from filesystem import FileGuard
    params = normalize_params(params); job = Path(job); started = time.monotonic()
    with ExitStack() as stack:
        report_path = _checked_document(report_descriptor, job, 'toon-produce-report.json')
        stack.enter_context(FileGuard(report_path))
        _checked_document(report_descriptor, job, 'toon-produce-report.json')
        report = read_json(report_path)
        if (report['status'] != 'produced' or report['manifest'] != params['manifest']
                or report['implementation_sha256'] != params['implementation_sha256']
                or report['source'] != {'file': params['file'], 'sha256': params['expected_sha256']}
                or report['execution_context'] != execution_context()):
            raise Failure('CONFLICT', 'Renderer does not bind the original producer report')
        documents = {}
        for key, name in (('candidate', 'toon-candidate.blend'), ('state', 'toon-state.json'),
                          ('plan', 'toon-plan.json'), ('expectation', 'toon-expectation.json')):
            path = _checked_document(report[key], job, name)
            stack.enter_context(FileGuard(path)); _checked_document(report[key], job, name); documents[key] = path
        plan = read_json(documents['plan']); state = read_json(documents['state'])
        if (plan['manifest'] != params['manifest'] or state['candidate'] != report['candidate']
                or state['expectation'] != report['expectation'] or state['source'] != report['source']
                or state['implementation_sha256'] != params['implementation_sha256']):
            raise Failure('CONFLICT', 'Renderer candidate/original expectation binding differs')
        from toon_workflow_addon import state_contract
        state_contract.validate_embedded_state(state['embedded_state'], params['manifest'])
        bpy.ops.wm.open_mainfile(filepath=str(documents['candidate']), load_ui=False, use_scripts=False)
        scene = bpy.data.scenes[params['manifest']['scene']]; core.activate(scene)
        core.validate_render_contract(scene, params['manifest'])
        if contract.is_animated(params['manifest']):
            from toon_workflow_addon import animation_bpy
            scene.frame_set(plan['reference_frame']); bpy.context.view_layer.update()
            witness = state['embedded_state']['shading_animation']
            if (animation_bpy.capture_scope(params['manifest']) != witness['native']
                    or animation_bpy.sample_scope(params['manifest'], plan['reference_frame']) != witness['reference']):
                raise Failure('CONFLICT', 'Warm renderer native Action/reference witness differs')
        frames = []
        folder = job / 'toon-frames'; folder.mkdir()
        for frame in range(params['manifest']['time']['start'], params['manifest']['time']['end'] + 1):
            _progress(job, {'stage': 'render', 'frame': frame,
                            'total': params['manifest']['time']['end'] - params['manifest']['time']['start'] + 1})
            rendered = render_frame(scene, frame, folder / ('frame-%04d.png' % frame), params['manifest'])
            if contract.is_animated(params['manifest']) and rendered['shading_sample'] != state['embedded_state']['frames'][frame-params['manifest']['time']['start']]['shading_sample']:
                raise Failure('CONFLICT', 'Warm shading sample differs from the original producer frame')
            frames.append(rendered)
        _identity(params)
        for key, name in (('candidate', 'toon-candidate.blend'), ('state', 'toon-state.json'),
                          ('plan', 'toon-plan.json'), ('expectation', 'toon-expectation.json')):
            _checked_document(report[key], job, name)
    return {'images': frames, 'candidate': report['candidate'], 'expectation': report['expectation'],
            'render_seconds': time.monotonic() - started}


def _phase(phase, params, job, cpu_context, report_descriptor=None):
    from filesystem import FileGuard
    request = {'phase': phase, 'params': params, 'job': str(job),
               'implementation_sha256': params['implementation_sha256'], 'execution_context': cpu_context,
               'output': str(job / ('toon-%s-receipt.json' % phase))}
    if report_descriptor is not None: request['report'] = report_descriptor
    path = job / ('toon-%s-request.json' % phase); atomic_json(path, request)
    command = [bpy.app.binary_path, '--background', '--factory-startup', '--disable-autoexec',
               '--threads', str(cpu_context['cpu_threads']), '--python-exit-code', '1',
               '--python', str(Path(__file__).with_name('toon_phase_worker.py')), '--', str(path), digest(path)]
    atomic_json(job / ('toon-%s-command.json' % phase), command)
    with FileGuard(path), (job / ('toon-%s.log' % phase)).open('w', encoding='utf-8') as log:
        child = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=3300,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    # A structured failure is trusted only after the same identity and execution
    # binding checks as success; process diagnostics never determine its code.
    try:
        _identity(params)
    except Exception as exc:
        raise Failure('VALIDATION_FAILED', 'Toon phase implementation changed after execution') from exc
    if type(child.returncode) is not int or not Path(request['output']).is_file():
        raise Failure('VALIDATION_FAILED', 'Toon %s process failed; see its receipt/log' % phase)
    try:
        receipt = read_json(request['output'])
    except Exception as exc:
        raise Failure('VALIDATION_FAILED', 'Toon phase receipt is unreadable') from exc
    if (type(receipt) is not dict or receipt.get('phase') != phase
            or receipt.get('implementation_sha256') != params['implementation_sha256']
            or type(receipt.get('execution_context')) is not dict
            or set(receipt['execution_context']) != {'cpu_threads'}
            or type(receipt['execution_context']['cpu_threads']) is not int
            or receipt['execution_context']['cpu_threads'] <= 0
            or receipt['execution_context'] != cpu_context):
        raise Failure('VALIDATION_FAILED', 'Toon phase receipt does not bind this execution')
    if receipt.get('status') == 'failed':
        from protocol import CODES
        detail = receipt.get('error_detail')
        if (child.returncode == 0 or set(receipt) != {'schema', 'version', 'status', 'phase',
                'implementation_sha256', 'execution_context', 'error_detail', 'error', 'traceback'}
                or receipt.get('schema') != 'toon-phase-failure' or receipt.get('version') != '1.0'
                or type(receipt.get('error')) is not str or type(receipt.get('traceback')) is not str
                or type(detail) is not dict
                or set(detail) != {'public_code', 'domain_code', 'path', 'message', 'type'}
                or any(type(value) is not str or not value for value in detail.values())
                or detail['public_code'] not in CODES or not detail['type'].isidentifier()
                or (detail['type'] not in ('Failure', 'WorkflowError')
                    and (detail['public_code'] != 'WORKER_FAILED' or detail['domain_code'] != 'WORKER_FAILED' or detail['path'] != '$'))
                or (detail['type'] == 'WorkflowError'
                    and detail['public_code'] != (detail['domain_code'] if detail['domain_code'] in CODES else 'WORKER_FAILED'))):
            raise Failure('VALIDATION_FAILED', 'Toon failed phase receipt is invalid or exit status differs')
        error = Failure(detail['public_code'], detail['message'])
        error.domain_code = detail['domain_code']; error.domain_path = detail['path']
        raise error
    if (child.returncode != 0 or receipt.get('status') != 'passed'
            or set(receipt) != {'status', 'phase', 'implementation_sha256', 'execution_context', 'result'}
            or type(receipt.get('result')) is not dict):
        raise Failure('VALIDATION_FAILED', 'Toon successful phase receipt is invalid or exit status differs')
    return receipt['result']


def run(params, job):
    """Factory coordinator; every native phase exits before the next starts."""
    params = normalize_params(params); job = Path(job); started = time.monotonic()
    report_path = job / 'toon-report.json'; report = {'toon_report_version': '1.1', 'operation': 'run',
        'status': 'failed', 'finalization': {'status': 'pending'}, 'manifest': params['manifest'],
        'source': {'file': params['file'], 'sha256': params['expected_sha256']},
        'implementation_sha256': params['implementation_sha256'],
        'recovery': {'strategy': 'restart_from_original_source_and_request_in_new_job',
                     'partial_candidate_accepted': False, 'resume': 'unsupported'}}
    try:
        if not bpy.app.background or bpy.data.filepath:
            raise Failure('CONFLICT', 'Toon coordinator must own a factory background Main')
        cpu_context = execution_context()
        _progress(job, {'stage': 'native_producer'})
        produced = _phase('produce', params, job, cpu_context)
        producer_path = _checked_document(produced['report'], job, 'toon-produce-report.json')
        report = read_json(producer_path)
        if (report.get('manifest') != params['manifest'] or report.get('source') != {'file': params['file'], 'sha256': params['expected_sha256']}
                or report.get('implementation_sha256') != params['implementation_sha256'] or report.get('execution_context') != cpu_context):
            raise Failure('CONFLICT', 'Producer report does not bind this request')
        frames = []
        if params['manifest']['render']['enabled']:
            _progress(job, {'stage': 'native_renderer'})
            rendered = _phase('render', params, job, cpu_context, produced['report'])
            if rendered.get('candidate') != report['candidate'] or rendered.get('expectation') != report['expectation']:
                raise Failure('CONFLICT', 'Renderer used a different candidate/original expectation')
            frames = rendered['images']; report['metrics']['render_seconds'] = rendered['render_seconds']
        report['images'] = frames
        verify_request = {'candidate': report['candidate'], 'state': report['state'],
                          'plan': report['plan'], 'expectation': report['expectation'],
                          'implementation_sha256': params['implementation_sha256'], 'images': frames,
                          'execution_context': cpu_context, 'chain_provenance': report['chain_provenance']}
        from filesystem import FileGuard
        for mode in ('content', 'images'):
            suffix = 'content-' if mode == 'content' else ''
            output = job / ('toon-%sreopen.json' % suffix)
            request = {**verify_request, 'verification_mode': mode, 'output': str(output)}
            if mode == 'images': request['content_receipt'] = descriptor(job / 'toon-content-reopen.json')
            verify_path = job / ('toon-%sverification-request.json' % suffix)
            atomic_json(verify_path, request)
            _progress(job, {'stage': 'independent_' + mode})
            command = [bpy.app.binary_path, '--background', '--factory-startup', '--disable-autoexec',
                       '--threads', str(cpu_context['cpu_threads']), '--python-exit-code', '1',
                       '--python', str(Path(__file__).with_name('toon_verifier.py')), '--', str(verify_path)]
            atomic_json(job / ('toon-%sreopen-command.json' % suffix), command)
            phase_started = time.monotonic()
            with FileGuard(verify_path), (job / ('toon-%sreopen.log' % suffix)).open('w', encoding='utf-8') as log:
                cold = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=300,
                                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            report['metrics']['cold_%s_seconds' % mode] = time.monotonic() - phase_started
            wanted = 'content_passed' if mode == 'content' else 'passed'
            if cold.returncode != 0 or not output.is_file() or read_json(output).get('status') != wanted:
                raise Failure('VALIDATION_FAILED', 'Independent Toon %s failed; see its reopen receipt/log' % mode)
            _identity(params)
        reopen_path = job / 'toon-reopen.json'
        report['verification'] = {'status': 'passed', 'reopen': descriptor(reopen_path),
                                  'content': descriptor(job / 'toon-content-reopen.json')}
        report['execution_phases'] = {'mode': 'sequential_fresh_native_processes',
            'order': ['produce', 'render', 'cold_content', 'cold_images'] if params['manifest']['render']['enabled'] else ['produce', 'cold_content', 'cold_images'],
            'coordinator_opened_source': False}
        _identity(params)
        if digest(params['file']) != params['expected_sha256']: raise Failure('CONFLICT', 'Source changed after execution')
        report['status'] = 'passed'; report['finalization'] = {'status': 'passed'}
        report['metrics'].update(wall_seconds=time.monotonic() - started, rendered_frames=len(frames))
        atomic_json(report_path, report); _progress(job, {'stage': 'complete'})
        outputs = [descriptor(p) for p in job.rglob('*') if p.is_file() and
                   any(part.startswith('toon-') for part in p.relative_to(job).parts)]
        return {'status': 'passed', 'candidate': report['candidate']['file'],
                'candidate_sha256': report['candidate']['sha256'], 'report': descriptor(report_path),
                'state': report['state'], 'verification': report['verification'], 'outputs': outputs}
    except Exception as exc:
        report['status'] = 'failed'; report['error'] = {'code': getattr(exc, 'code', 'WORKER_FAILED'), 'message': str(exc)}
        report['finalization'] = {'status': 'failed'}
        report['failed_observed_context'] = 'coordinator_factory_main_only; native phase diagnostics remain in receipts/logs'
        report['metrics'] = {**report.get('metrics', {}), 'wall_seconds': time.monotonic() - started}
        atomic_json(report_path, report)
        atomic_json(job / 'toon-progress.json', {'status': 'failed', 'error': report['error']})
        if isinstance(exc, contract.WorkflowError): _domain_error(exc)
        raise
