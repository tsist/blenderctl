# SPDX-License-Identifier: GPL-3.0-or-later
"""Paint-aware image parent; caller retains guards through terminal publication."""
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
    sys.dont_write_bytecode = True
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    from toon_image_backend import painted_image_receipt_reader as reader
    try:
        return _run(request, job, launch, stack, checkpoint)
    except reader.ImageReceiptError as error:
        code = {'INTEGRITY_CONFLICT': 'CONFLICT', 'INVALID_RECEIPT': 'VALIDATION_FAILED',
                'RESOURCE_LIMIT': 'RESOURCE_LIMIT', 'UNSUPPORTED_SCHEMA': 'UNSUPPORTED'}[error.code]
        raise Failure(code, str(error)) from error


def _run(request, job, launch, stack, checkpoint):
    sys.dont_write_bytecode = True
    job = Path(job)
    params = request['params']
    need(params['recipe']['profile'] == 'painted-mesh-image.v1', 'Painted profile required')
    cache = job/'image-empty-pycache'
    cache.mkdir(exist_ok=False)
    sys.pycache_prefix = str(cache)
    sys.path.insert(0, str(TOOLS))
    from toon_image_prepared_run import HeldFiles
    from toon_painted_image_prepared_run import prepare_exported_painted_run
    from toon_selected_image_prepared_run import _export_members
    from toon_painted_image_evidence import split_params, validate_prepared, cold_document
    from toon_image_owned_stage import run_stage
    from painted_bundle_lease import PaintedWindowsBundleLease, PaintedLeaseLimits
    from toon_image_backend import painted_image_receipt_reader as reader
    identity = launch['image_identity']
    code = identity['code']
    need(implementation_sha256(code) == params['implementation_sha256'], 'Queued implementation pin differs')
    request_sha = hashlib.sha256(canonical(request)).hexdigest()
    need(launch['image_request_sha256'] == request_sha, 'Queued request differs')
    held = HeldFiles(stack, checkpoint)
    paths = source_files()
    need(sorted(paths) == sorted(code['sources']), 'Queued code inventory changed')
    for name, path in paths.items():
        held.file(path, code['sources'][name], maximum=8*1024**2)
    need([row['file'] for row in identity['binaries']] == [str(Path(sys.executable).resolve()), launch['blender']],
         'Queued executable selection changed')
    for row in [*identity['binaries'], params, *params['resources']]:
        held.file(row['file'], row['expected_sha256'], maximum=512*1024**2)
    for path, wanted in ((job/'request.json', request), (job/'launch.json', launch)):
        need(canonical(held.document(path, digest(path), 8*1024**2)) == canonical(wanted),
             'Queued document changed before its guard')
    root = job/'image-run'
    root.mkdir(exist_ok=False)
    held.directory(root)

    def write(path, value):
        data = canonical(value)
        need(len(data) <= 8*1024**2, 'Host document exceeds byte cap')
        pin = hashlib.sha256(data).hexdigest()
        with path.open('xb') as stream:
            stream.write(data)
        held.file(path, pin, maximum=8*1024**2, size=len(data))
        return dict(file=str(path), sha256=pin, bytes=len(data))

    export_request, numeric_recipe = split_params(params)
    export_ref = write(root/'ORIGINAL-EXPORT-REQUEST.json', export_request)
    recipe_ref = write(root/'ORIGINAL-NUMERIC-RECIPE.json', numeric_recipe)
    context = dict(schema='toon-painted-image-host-context.v1', run_root=str(root), nonce=uuid.uuid4().hex,
        export_request_sha256=export_ref['sha256'], numeric_recipe_sha256=recipe_ref['sha256'])
    context_ref = write(root/'HOST-CONTEXT.json', context)
    public = dict(schema='toon-painted-image-public-context.v1', job_id=job.name,
        profile='painted-mesh-image.v1', run_root=str(root), request_sha256=request_sha,
        launch_identity_sha256=hashlib.sha256(canonical(identity)).hexdigest(),
        implementation_sha256=params['implementation_sha256'],
        source=dict(file=params['file'], sha256=params['expected_sha256']), resources=params['resources'],
        host_context=context_ref, public_job=True, parent_terminal_required=True)
    public_ref = write(root/'PUBLIC-CONTEXT.json', public)
    lifecycles = []

    def stage(mode, extra=None):
        checkpoint()
        need(sorted(source_files()) == sorted(code['sources']), 'Code membership changed before stage launch')
        stage_dir = root/('stage-'+mode)
        stage_dir.mkdir(exist_ok=False)
        empty = root/('empty-pycache-'+mode)
        empty.mkdir(exist_ok=False)
        profile = root/('profile-'+mode)
        profile.mkdir(exist_ok=False)
        env = {k: v for k, v in os.environ.items() if not k.startswith('BLENDER_') and
               k not in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONOPTIMIZE')}
        for field in ('CONFIG', 'SCRIPTS', 'EXTENSIONS', 'DATAFILES'):
            target = profile/field.lower()
            target.mkdir()
            env['BLENDER_USER_'+field] = str(target)
        env.update(BLENDER_USER_RESOURCES=str(profile), PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1',
            PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1', PYTHONOPTIMIZE=str(sys.flags.optimize))
        binding = dict(mode=mode, profile='painted-mesh-image.v1', run_root=str(root),
            stage_dir=str(stage_dir), empty_pycache=str(empty), params=params,
            export_request=export_request, optimize=sys.flags.optimize, **(extra or {}))
        binding_path = root/(mode+'-binding.json')
        binding_ref = write(binding_path, binding)
        command = (launch['blender'], '--background', '--factory-startup', '--disable-autoexec',
            '--offline-mode', '--python-use-system-env', '--threads', '2', '--python-exit-code', '23',
            '--python', str(Path(__file__).with_name('toon_painted_image_stage_worker.py')),
            '--', str(binding_path), binding_ref['sha256'])
        documents = {str(path): code['sources'][name] for name, path in paths.items()}
        documents.update({row['file']: row['expected_sha256'] for row in identity['binaries']})
        life = run_stage(stage_dir, command=command, checkpoint=checkpoint,
            bootstrap_path=Path(__file__).with_name('bootstrap.py'), python_executable=Path(sys.executable).resolve(),
            env=env, expected_documents=tuple(dict(file=k, expected_sha256=v) for k, v in documents.items()))
        life_path = stage_dir/'lifecycle.json'
        life_sha = digest(life_path)
        held.document(life_path, life_sha, 8*1024**2)
        observation_path = stage_dir/'STAGE-OBSERVATION.json'
        observation = held.document(observation_path, digest(observation_path), 1024**2)
        need(observation['mode'] == mode and observation['optimization'] == sys.flags.optimize,
             'Target stage optimization or mode differs')
        lifecycles.append(dict(mode=mode, status=life['status'],
            lifecycle=dict(file=str(life_path), sha256=life_sha)))
        checkpoint()
        return stage_dir

    runtime_path = stage('identity')/'RUNTIME.json'
    runtime = held.document(runtime_path, digest(runtime_path), 1024**2)
    exported_path = stage('export')/'EXPORT-RETURN.json'
    exported = held.document(exported_path, digest(exported_path), 8*1024**2)
    need(exported['capture']['file'] == str(root/'export/capture.json'), 'Capture return role differs')
    cli_sources = {Path(name).name: pin for name, pin in code['sources'].items() if name.startswith('blenderctl/')}
    dependency_pin = hashlib.sha256(json.dumps(cli_sources, ensure_ascii=True,
        sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    backend_sources = {name.removeprefix('toon_image_backend/'): pin for name, pin in code['sources'].items()
                       if name.startswith('toon_image_backend/')}
    prepared = prepare_exported_painted_run(run_root=root, export_request=export_request, numeric_recipe=numeric_recipe,
        capture_sha256=exported['capture']['expected_sha256'], dependency_audit=exported['dependency_audit'],
        host_context=context, expected_runtime=runtime, expected_backend_sources=backend_sources,
        expected_dependency_implementation=dependency_pin, held=held)
    disk_expected = validate_prepared(root=root, prepared=prepared, numeric_recipe=numeric_recipe,
        context=context, runtime=runtime, exported=exported, dependency_pin=dependency_pin,
        backend_sources=backend_sources, tools=TOOLS, held=held)
    need(not os.path.lexists(root/'bundle'), 'Numerical output exists before presealed expectation')
    write(root/'PRE-NUMERIC.json', dict(expected_sha256=prepared.expected_sha256,
        request_sha256=prepared.request_sha256, output_absent=True,
        host_context_sha256=prepared.host_context_sha256))
    numeric_path = stage('numeric', dict(request_path=prepared.request_path,
        request_sha256=prepared.request_sha256))/'NUMERIC-RETURN.json'
    numeric = held.document(numeric_path, digest(numeric_path), 1024**2)
    need(numeric['ok'] is True, 'Numerical producer failed')
    root_id = held.directory(root/'bundle').identity()
    lease = stack.enter_context(PaintedWindowsBundleLease(str(root/'bundle'),
        expected_root_identity=(root_id['volume'], root_id['file_index']),
        forbidden_file_identities=frozenset(held.identities), reader_api=reader,
        limits=PaintedLeaseLimits(), checkpoint=checkpoint))
    cold = reader.verify_painted_image_v1(lease, expected_receipt_sha256=numeric['receipt_sha256'],
        expected_run=disk_expected, limits=reader.PaintedReaderLimits(), checkpoint=checkpoint)
    need(cold.parent_accepted is False and cold.quality_accepted is False, 'Cold reader exceeded authority')
    cold_ref = write(root/'COLD-REPORT.json', cold_document(cold, checkpoint))
    acceptance = dict(schema='toon-painted-image-parent-validation.v1', profile='painted-mesh-image.v1',
        validation='passed', parent_job_id=job.name, parent_terminal_required=True, source_mode='SAVED_DISK_V1',
        request_sha256=request_sha, launch_identity_sha256=public['launch_identity_sha256'],
        implementation_sha256=params['implementation_sha256'], public_context=public_ref, host_context=context_ref,
        export_request=export_ref, numeric_recipe=recipe_ref,
        expected=dict(file=prepared.expected_path, sha256=prepared.expected_sha256),
        numeric_context=dict(file=str(root/'NUMERIC-CONTEXT.json'), sha256=prepared.host_context_sha256),
        backend_request=dict(file=prepared.request_path, sha256=prepared.request_sha256),
        result_root=str(root/'bundle'), result_root_identity=root_id, receipt_sha256=numeric['receipt_sha256'],
        cold_report=cold_ref, stages=lifecycles, source_unchanged=True,
        geometry_independently_recomputed=False, quality_accepted=False,
        input_guard_scope='original source/resources/code, export/geometry and prepared input held through parent terminal publication',
        dependency_scope='finite registered static dependencies; not a preload sandbox')
    acceptance_ref = write(job/'image-acceptance.json', acceptance)
    geometry = root/'export/geometry'
    geometry_members = {'EXPORT.json', 'toon-image-input.json',
        *('export-%06d.npz' % i for i in range(len(params['recipe']['occluder_object_names'])))}

    def recheck():
        checkpoint()
        need(sorted(source_files()) == sorted(code['sources']), 'Code membership changed during image run')
        _export_members(root/'export', held)
        need(held.flat_names(geometry) == geometry_members, 'Export geometry membership changed')
        held.recheck()
        lease.recheck()
        checkpoint()
    recheck()
    return dict(status='passed', profile='painted-mesh-image.v1', parent_accepted=True, parent_job_id=job.name,
        source_unchanged=True, source_mode='SAVED_DISK_V1', quality_accepted=False,
        acceptance=acceptance_ref, result_root=str(root/'bundle'), cold_report=cold_ref,
        qualification='valid only with this job succeeded status and matching final result; not image-quality acceptance'), recheck
