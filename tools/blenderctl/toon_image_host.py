# SPDX-License-Identifier: GPL-3.0-or-later
"""Saved image host; the existing runner owns final job/result/status publication."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from protocol import Failure
from toon_image_public import canonical, digest, implementation_sha256, source_files, TOOLS


def need(value, message):
    if not value:
        raise Failure('CONFLICT', message)


def run(request, job, launch, stack, checkpoint):
    if request['params']['recipe']['profile'] == 'cpu-boundary-image.v1':
        from toon_cpu_image_host import run as run_cpu
        return run_cpu(request,job,launch,stack,checkpoint)
    if request['params']['recipe']['profile'] == 'painted-mesh-image.v1':
        from toon_painted_image_host import run as run_painted
        return run_painted(request,job,launch,stack,checkpoint)
    if request['params']['recipe']['profile'] == 'selected-mesh-image.v1':
        from toon_selected_image_host import run as run_selected
        return run_selected(request, job, launch, stack, checkpoint)
    need(request['params']['recipe']['profile']=='intersection-image.v1','Unknown explicit image profile')
    # Persistent guards and output lease belong to runner's ExitStack, retained
    # through its result/status publication. No with-stack closes at run return.
    sys.dont_write_bytecode = True
    job = Path(job)
    params = request['params']
    cache = job / 'image-empty-pycache'
    cache.mkdir(exist_ok=False)
    sys.pycache_prefix = str(cache)
    sys.path.insert(0, str(TOOLS))
    from toon_image_prepared_run import HeldFiles, prepare_exported_run
    from toon_image_owned_stage import run_stage
    from toon_image_bundle_lease import WindowsBundleLease
    from toon_image_backend import intersection_image_receipt_reader as reader

    identity = launch['image_identity']
    code = identity['code']
    need(implementation_sha256(code) == params['implementation_sha256'], 'Queued implementation pin differs')
    need(launch['image_request_sha256'] == hashlib.sha256(canonical(request)).hexdigest(), 'Queued request differs')
    held = HeldFiles(stack, checkpoint)
    paths = source_files()
    need(sorted(paths) == sorted(code['sources']), 'Queued code inventory changed')
    for name, path in paths.items():
        held.file(path, code['sources'][name], maximum=8 * 1024**2)
    need([row['file'] for row in identity['binaries']] == [str(Path(sys.executable).resolve()), launch['blender']],
         'Queued executable selection changed')
    for row in identity['binaries']:
        held.file(row['file'], row['expected_sha256'], maximum=512 * 1024**2)
    for row in [params, *params['resources']]:
        held.file(row['file'], row['expected_sha256'], maximum=512 * 1024**2)
    # Request and launch are immutable semantic anchors. status is deliberately
    # mutable and remains under the existing runner, not a worker's authority.
    for path, expected in ((job / 'request.json', request), (job / 'launch.json', launch)):
        actual = held.document(path, digest(path), 8 * 1024**2)
        need(actual == expected, 'Queued document changed before its guard')
    root = job / 'image-run'
    root.mkdir(exist_ok=False)
    held.directory(root)
    context = dict(schema='toon-image-host-context.v1', job_id=job.name,
        nonce=uuid.uuid4().hex, run_root=str(root), params_sha256=hashlib.sha256(canonical(params)).hexdigest(),
        request_sha256=hashlib.sha256(canonical(request)).hexdigest(),
        source=dict(file=params['file'], sha256=params['expected_sha256']), resources=params['resources'],
        implementation_sha256=params['implementation_sha256'], public_job=True,
        launch_identity_sha256=hashlib.sha256(canonical(identity)).hexdigest())

    def write(path, value):
        data = canonical(value)
        with path.open('xb') as stream:
            stream.write(data)
        held.file(path, hashlib.sha256(data).hexdigest(), maximum=8 * 1024**2)
        return dict(file=str(path), sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))

    context_ref = write(root / 'HOST-CONTEXT.json', context)
    lifecycles = []
    def stage(mode, extra=None):
        checkpoint()
        need(sorted(source_files()) == sorted(code['sources']), 'Code membership changed before stage launch')
        stage_dir = root / ('stage-' + mode)
        stage_dir.mkdir(exist_ok=False)
        empty = root / ('empty-pycache-' + mode)
        empty.mkdir(exist_ok=False)
        profile = root / ('profile-' + mode)
        profile.mkdir(exist_ok=False)
        env = {k: v for k, v in os.environ.items() if not k.startswith('BLENDER_') and
               k not in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP')}
        for field in ('CONFIG', 'SCRIPTS', 'EXTENSIONS', 'DATAFILES'):
            target = profile / field.lower()
            target.mkdir()
            env['BLENDER_USER_' + field] = str(target)
        env.update(BLENDER_USER_RESOURCES=str(profile), PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
        binding = dict(mode=mode, run_root=str(root), stage_dir=str(stage_dir), empty_pycache=str(empty),
                       params=params, **(extra or {}))
        binding_path = root / (mode + '-binding.json')
        binding_ref = write(binding_path, binding)
        command = (launch['blender'], '--background', '--factory-startup', '--disable-autoexec',
            '--offline-mode', '--threads', '2', '--python-exit-code', '23',
            '--python', str(Path(__file__).with_name('toon_image_stage_worker.py')),
            '--', str(binding_path), binding_ref['sha256'])
        documents = {str(path): code['sources'][name] for name, path in paths.items()}
        documents.update({row['file']: row['expected_sha256'] for row in identity['binaries']})
        life = run_stage(stage_dir, command=command, checkpoint=checkpoint,
            bootstrap_path=Path(__file__).with_name('bootstrap.py'), python_executable=Path(sys.executable).resolve(),
            env=env, expected_documents=tuple(dict(file=k, expected_sha256=v) for k, v in documents.items()))
        lifecycles.append(dict(mode=mode, status=life['status'],
            lifecycle=dict(file=str(stage_dir / 'lifecycle.json'), sha256=digest(stage_dir / 'lifecycle.json'))))
        checkpoint()
        return stage_dir

    runtime_path = stage('identity') / 'RUNTIME.json'
    runtime = held.document(runtime_path, digest(runtime_path), 1024**2)
    exported = stage('export') / 'EXPORT-RETURN.json'
    exported = held.document(exported, digest(exported), 8 * 1024**2)
    need(exported['capture']['file'] == str(root / 'export' / 'capture.json'), 'Capture return role differs')
    cli_sources = {Path(name).name: sha for name, sha in code['sources'].items() if name.startswith('blenderctl/')}
    dependency_identity = hashlib.sha256(json.dumps(cli_sources, ensure_ascii=True,
        sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    backend_sources = {name.removeprefix('toon_image_backend/'): sha for name, sha in code['sources'].items()
                       if name.startswith('toon_image_backend/')}
    prepared = prepare_exported_run(run_root=root, params=params,
        capture_sha256=exported['capture']['expected_sha256'], dependency_audit=exported['dependency_audit'],
        host_context=context, expected_runtime=runtime, expected_backend_sources=backend_sources,
        expected_dependency_implementation=dependency_identity, held=held)
    need(not (root / 'result').exists(), 'Numerical output exists before presealed expectation')
    write(root / 'PRE-NUMERIC.json', dict(expected_sha256=prepared.expected_sha256,
        request_sha256=prepared.request_sha256, output_absent=True,
        host_context_sha256=prepared.host_context_sha256))
    numeric_path = stage('numeric', dict(request_path=prepared.request_path,
        request_sha256=prepared.request_sha256)) / 'NUMERIC-RETURN.json'
    numeric = held.document(numeric_path, digest(numeric_path), 1024**2)
    need(numeric['ok'] is True, 'Numerical producer failed')
    root_id = held.directory(root / 'result').identity()
    lease = stack.enter_context(WindowsBundleLease(str(root / 'result'),
        expected_root_identity=(root_id['volume'], root_id['file_index']),
        forbidden_file_identities=frozenset(held.identities), reader_api=reader,
        limits=reader.ReaderLimits(), checkpoint=checkpoint))
    cold = reader.verify_intersection_image_v1(lease,
        expected_receipt_sha256=numeric['receipt_sha256'], expected_run=prepared.expected_run,
        limits=reader.ReaderLimits(), checkpoint=checkpoint)
    need(cold.parent_accepted is False and cold.quality_accepted is False, 'Cold reader exceeded authority')
    cold_ref = write(root / 'COLD-REPORT.json', asdict(cold))
    acceptance = dict(schema='toon-image-parent-validation.v1', validation='passed', parent_job_id=job.name,
        parent_terminal_required=True, source_mode='SAVED_DISK_V1',
        request_sha256=context['request_sha256'], launch_identity_sha256=context['launch_identity_sha256'],
        implementation_sha256=params['implementation_sha256'], host_context=context_ref,
        expected=dict(file=prepared.expected_path, sha256=prepared.expected_sha256),
        numeric_context=dict(file=str(root / 'NUMERIC-CONTEXT.json'), sha256=prepared.host_context_sha256),
        backend_request=dict(file=prepared.request_path, sha256=prepared.request_sha256),
        result_root=str(root / 'result'), result_root_identity=root_id,
        receipt_sha256=numeric['receipt_sha256'], cold_report=cold_ref, stages=lifecycles,
        source_unchanged=True, geometry_independently_recomputed=False, quality_accepted=False,
        input_guard_scope='original source/resources/code, export/snapshot and prepared input held through parent terminal publication',
        dependency_scope='finite registered static dependencies; not a preload sandbox')
    acceptance_ref = write(job / 'image-acceptance.json', acceptance)

    def recheck():
        checkpoint()
        need(sorted(source_files()) == sorted(code['sources']), 'Code membership changed during image run')
        held.recheck()
        lease.recheck()
        checkpoint()
    recheck()
    data = dict(status='passed', parent_accepted=True, parent_job_id=job.name,
        source_unchanged=True, source_mode='SAVED_DISK_V1', quality_accepted=False,
        acceptance=acceptance_ref, result_root=str(root / 'result'), cold_report=cold_ref,
        qualification='valid only with this job succeeded status and matching final result; not image-quality acceptance')
    return data, recheck


def inspect_run(request, job, launch, stack, checkpoint):
    """Guard the current verifier and pinned queued request, then cold inspect.

    No original source/resource declaration or current Blender path is opened.
    The same runner lifetime retains both verifier guards and original evidence.
    """
    if request['params'].get('profile') == 'cpu-boundary-image.v1':
        from toon_cpu_image_host import inspect_run as inspect_cpu
        return inspect_cpu(request,job,launch,stack,checkpoint)
    sys.dont_write_bytecode = True
    job = Path(job)
    cache = job / 'image-inspect-empty-pycache'
    cache.mkdir(exist_ok=False)
    sys.pycache_prefix = str(cache)
    sys.path.insert(0, str(TOOLS))
    from toon_image_prepared_run import HeldFiles
    from toon_image_inspect import inspect_parent
    from toon_image_backend import intersection_image_receipt_reader as reader
    from toon_image_backend import selected_chain_image_receipt_reader as selected_reader

    def inspect_call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (reader.ImageReceiptError, selected_reader.ImageReceiptError) as exc:
            code = {'INTEGRITY_CONFLICT': 'CONFLICT', 'INVALID_RECEIPT': 'VALIDATION_FAILED',
                    'RESOURCE_LIMIT': 'RESOURCE_LIMIT', 'UNSUPPORTED_SCHEMA': 'UNSUPPORTED'}[exc.code]
            raise Failure(code, str(exc)) from exc
    params = request['params']
    identity = launch['image_identity']
    need(identity.get('schema') == 'toon-image-inspect-launch.v1', 'Inspect launch identity schema differs')
    code = identity['code']
    request_sha = hashlib.sha256(canonical(request)).hexdigest()
    need(implementation_sha256(code) == params['implementation_sha256'], 'Queued inspect implementation differs')
    need(launch['image_request_sha256'] == request_sha, 'Queued inspect request differs')
    held = HeldFiles(stack, checkpoint)
    paths = source_files()
    need(sorted(paths) == sorted(code['sources']), 'Inspect code inventory changed since queue')
    for name, path in paths.items():
        held.file(path, code['sources'][name], maximum=8 * 1024**2)
    selected_python = identity['python']
    need(selected_python['file'] == str(Path(sys.executable).resolve()), 'Inspect selected Python differs')
    held.file(selected_python['file'], selected_python['expected_sha256'], maximum=512 * 1024**2)
    for path, expected in ((job / 'request.json', request), (job / 'launch.json', launch)):
        need(held.document(path, digest(path), 8 * 1024**2) == expected, 'Inspect queued document changed')
    context = dict(schema='toon-image-inspector-context.v1', job_id=job.name,
        request_sha256=request_sha, implementation_sha256=params['implementation_sha256'],
        launch_identity_sha256=hashlib.sha256(canonical(identity)).hexdigest(),
        parent_result=params['parent_result'], source_current='not_checked',
        current_blender_used=False, parent_terminal_required=True)
    context_path = job / 'image-inspect-context.json'
    encoded = canonical(context)
    with context_path.open('xb') as stream:
        stream.write(encoded)
    context_sha = hashlib.sha256(encoded).hexdigest()
    held.file(context_path, context_sha, maximum=8 * 1024**2)
    need(sorted(source_files()) == sorted(code['sources']), 'Inspect code membership changed before cold call')
    data, inspect_recheck = inspect_call(inspect_parent, params, job, stack=stack, checkpoint=checkpoint,
        expected_implementation=params['implementation_sha256'])
    need(data.get('parent_accepted') is False and data.get('quality_accepted') is False and
         data.get('source_current') == 'not_checked', 'Inspector exceeded qualification boundary')

    def recheck():
        checkpoint()
        need(sorted(source_files()) == sorted(code['sources']), 'Inspect code membership changed during inspection')
        held.recheck()
        inspect_call(inspect_recheck)
        checkpoint()

    recheck()
    return dict(data, status='passed', inspection_verified=True, inspector_job_id=job.name,
        inspector_context=dict(file=str(context_path), sha256=context_sha, bytes=len(encoded)),
        inspector_terminal_required=True), recheck
