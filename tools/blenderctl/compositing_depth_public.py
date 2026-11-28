"""Owned public depth preparation and cold bundle verification. Windows, no scene/Torch."""
from contextlib import ExitStack
from pathlib import Path
import copy
import hashlib
import json
import math
import os
import sys
import time
import uuid

from compositing_depth_contract import (DepthError, loads_strict, normalize_params,
                                        input_descriptors, signature, CONTRACT_PATH)
from compositing_encode import Inputs, Guard
from compositing_encode_contract import EncodeError
from processes import ProcessTree

HOST_NAMES = ('cli.py', 'protocol.py', 'runner.py', 'compositing_interface.py',
              'execution_contract.py', 'compositing_depth_public.py',
              'compositing_depth_contract.py', 'compositing_encode.py',
              'compositing_encode_contract.py', 'processes.py', 'bootstrap.py')
MODEL_ADMISSION_NAMES = ('compositing_depth_model_admission.py','compositing_depth_estimate_contract.py','compositing_depth_estimate_public.py')
LEGACY_PREPARE_CONTRACT_SHA = 'ca3f9763ef78acd41eec245773141cae23755d0ee3925545bbc7d4742071db2d'
NUMERIC_NAMES = ('depth_contract', 'depth_math', 'depth_io', 'depth_worker')
# Finite exact compatibility candidates. Root must cold-qualify the registered
# profiles after integration; frozen source bytes are still guarded individually.
_PRE_IMPORT_NUMERIC_PROFILE = {
    'depth_contract': 'c1f61f5c334e0968122f2b121a3e353e00cac4fadd8b1430560d931c5ce95d89',
    'depth_math': '98b0669f97cb733a46965f0b96b6fde72f4501fddf1c7bc57d977e6fd797609b',
    'depth_io': '253c177b7bf3329053ad25980570a8dc5d8044debdea3db5012657a5c35fa977',
    'depth_worker': '6a936126b9b71b9069d027fe1f560c5ec1f71c985f4467192c1856d582db9acc',
}
QUALIFIED_LEGACY_NUMERIC_PROFILES = (
    _PRE_IMPORT_NUMERIC_PROFILE,
    _PRE_IMPORT_NUMERIC_PROFILE | {'depth_contract': LEGACY_PREPARE_CONTRACT_SHA},
)


def numeric_profile_admission(normalized, historical, producer_identity, identity):
    roles = [r['role'] for r in historical]
    current_roles = [r['role'] for r in identity['files']]
    need(len(roles) == len(set(roles)) and len(current_roles) == len(set(current_roles)),
         'Duplicate frozen/current source role')
    old_numeric = {r['role']: r['sha256'] for r in historical if r['role'] in NUMERIC_NAMES}
    new_numeric = {r['role']: r['sha256'] for r in identity['files'] if r['role'] in NUMERIC_NAMES}
    need(set(old_numeric) == set(new_numeric) == set(NUMERIC_NAMES), 'Numeric role closure differs')
    numeric_digest = code_signature([r for r in historical if r['role'] in NUMERIC_NAMES])
    need(numeric_digest == producer_identity['numeric_sha256'], 'Frozen numeric digest differs')
    need(code_signature([r for r in identity['files'] if r['role'] in NUMERIC_NAMES]) == identity['numeric_sha256'],
         'Current numeric digest differs')
    current = old_numeric == new_numeric and numeric_digest == identity['numeric_sha256']
    legacy = (normalized['depth_prepare_version'] == '1.0' and
              normalized['normal']['method'] in ('none', 'central_perspective', 'central_heightfield') and
              any(old_numeric == profile for profile in QUALIFIED_LEGACY_NUMERIC_PROFILES))
    need(current or legacy, 'This cold reader has not qualified a different numeric bundle profile', 'UNSUPPORTED')
    legacy_roles = {'host:' + name for name in HOST_NAMES} | set(NUMERIC_NAMES)
    legacy_contract = legacy and old_numeric['depth_contract'] == LEGACY_PREPARE_CONTRACT_SHA
    need(set(roles) == set(current_roles) or legacy_contract and set(roles) == legacy_roles,
         'Frozen source closure differs')
    return legacy
COMPLETE_SHA = '3980996751ac8b805da507ba72f2ebbe10ed7c3a9fc50d718783cc8765918bbe'
QUALITY_SHA = '623dda97087c18c1eee4db6d18ca3aa7842d7264165726e9b2d8b23be78bebe9'


def need(condition, message, code='CONFLICT'):
    if not condition:
        raise DepthError(code, message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def row(path, role=None):
    path = Path(os.path.abspath(path))
    out = {'file': str(path), 'sha256': digest(path), 'bytes': path.stat().st_size}
    if role:
        out['role'] = role
    return out


def descriptor(value):
    return {'file': value['file'], 'expected_sha256': value.get('expected_sha256', value.get('sha256'))}


def key(path):
    return os.path.normcase(os.path.abspath(path))


def read(path):
    need(Path(path).stat().st_size <= 16777216, 'JSON authority exceeds finite document budget', 'RESOURCE_LIMIT')
    return loads_strict(Path(path).read_bytes())


class BoundedInputs(Inputs):
    """Same mature guards, but hash checkpoints remain responsive for finite files."""
    def checked_digest(self, path):
        need(Path(path).stat().st_size <= 536870912, 'Depth profile individual input exceeds512MiB', 'RESOURCE_LIMIT')
        h = hashlib.sha256()
        with Path(path).open('rb') as stream:
            for block in iter(lambda: stream.read(1048576), b''):
                self.checkpoint()
                h.update(block)
        return h.hexdigest()

    def protect(self, d):
        path = Path(os.path.abspath(d['file']))
        k, sha = key(path), d['expected_sha256']
        if k in self.files:
            need(self.files[k]['sha256'] == sha, 'Conflicting guarded input SHA')
            return path
        for parent in reversed(path.parents):
            pk = key(parent)
            if pk not in self.dirs:
                self.stack.enter_context(Guard(parent, True))
                self.dirs.add(pk)
        self.stack.enter_context(Guard(path))
        need(self.checked_digest(path) == sha, 'SHA mismatch:' + str(path))
        self.files[k] = {'file': str(path), 'sha256': sha}
        return path

    def recheck(self):
        for r in self.files.values():
            need(self.checked_digest(r['file']) == r['sha256'], 'Guarded input changed')


def publish(path, value):
    """Atomic no-clobber publication; partial temp is preserved on failure."""
    path = Path(path)
    temp = path.with_name('.' + uuid.uuid4().hex + '.json.partial')
    with temp.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    # Windows rename fails if destination exists. No os.replace of evidence.
    os.rename(temp, path)


def code_rows():
    base = Path(__file__).parent
    rows = [row(base / name, 'host:' + name) for name in (*HOST_NAMES,*MODEL_ADMISSION_NAMES)]
    rows.append(row(CONTRACT_PATH.with_name('depth_estimate_contract.py'), 'host:depth_estimate_contract_source'))
    for role in NUMERIC_NAMES:
        path = CONTRACT_PATH.with_name(role + '.py') if role in ('depth_contract', 'depth_math') else base / ('compositing_' + role + '.py')
        rows.append(row(path, role))
    return rows


def code_signature(rows):
    return signature(sorted([{'role': r['role'], 'sha256': r['sha256']} for r in rows], key=lambda r: r['role']))


def launch_identity():
    codes = code_rows()
    return {'profile': 'depth-public-v1', 'files': codes, 'sha256': code_signature(codes),
            'numeric_sha256': code_signature([r for r in codes if r['role'] in NUMERIC_NAMES])}


def protect_read(inputs, d):
    path = inputs.protect(descriptor(d))
    return read(path)


def provenance(params, inputs):
    p = params['provenance']
    if p['kind'] == 'declared_external':
        return {'kind': p['kind'], 'statement': p['statement'], 'model_completion_verified': False,
                'polarity_evidence': 'explicit caller declaration', 'consumer_eligible': False}
    if p['kind'] == 'verified_public_dav2':
        from compositing_depth_model_admission import admit
        return admit(params, inputs)
    need(p['completion']['expected_sha256'] == COMPLETE_SHA and p['quality']['expected_sha256'] == QUALITY_SHA,
         'This profile only admits the registered real single CG completion/quality identity')
    complete = protect_read(inputs, p['completion'])
    quality = protect_read(inputs, p['quality'])
    need(complete.get('schema') == 'root-single-CG-complete-registration-v1' and
         complete.get('status') == 'root_registered_actual_complete_numeric_producer' and
         type(complete.get('execution_exit_code')) is int and complete['execution_exit_code'] == 0,
         'Registered producer is not actual completed numeric execution')
    records = {}
    for role in ('execution_registration', 'producer_frozen', 'request', 'release', 'run_owner', 'terminal', 'sidecar'):
        records[role] = protect_read(inputs, complete[role])
    inputs.protect(descriptor(complete['raw']))
    need(key(complete['raw']['file']) == key(params['depth']['file']) and
         complete['raw']['sha256'] == params['depth']['expected_sha256'], 'Registered raw differs')
    sidecar, terminal = records['sidecar'], records['terminal']
    need(sidecar.get('status') == 'complete' and terminal.get('state') == 'complete' and
         terminal.get('guard', {}).get('producer_integrity_pass') is True and
         terminal.get('sidecar') == complete['sidecar'], 'Real producer terminal/sidecar/integrity incomplete')
    rgb = sidecar['RGB']['rgb']
    need(key(rgb['file']) == key(params['rgb']['file']) and rgb['sha256'] == params['rgb']['expected_sha256'],
         'Registered RGB differs')
    need(sidecar['raw']['sha256'] == complete['raw']['sha256'] and
         sidecar['request'] == complete['request'] and records['request']['input']['rgb'] == rgb,
         'Registered sidecar/raw/request chain differs')
    need([params['domain']['width'], params['domain']['height']] == [sidecar['RGB']['width'], sidecar['RGB']['height']],
         'Registered CG domain differs')
    need(quality.get('status') == 'diagnostic_pass' and quality.get('ordering_gate') is True and
         quality.get('direction') == params['semantics']['near_order'] == 'larger_nearer' and
         params['semantics']['measure'] == 'relative_score', 'CG quality polarity evidence differs')
    return {'kind': p['kind'], 'model_completion_verified': True, 'scope': 'fixed registered CG only',
            'completion': p['completion'], 'quality': p['quality'], 'direction': quality['direction'],
            'metric_depth_proven': False, 'calibration_proven': False, 'consumer_eligible': False,
            'reexecution': False, 'guard_reexecution': False,
            'evidence_policy': 'read complete registration and fixed terminal/sidecar bytes; not rehashing old model payload'}


def protect_data(params, operation, inputs):
    for d in input_descriptors(params, operation):
        inputs.protect(d)


def resources_used(params, used):
    """Direct resources exact; authority-derived additions are discovered, never unused extras."""
    actual = {key(r['file']): r['sha256'] for r in used}
    for r in params['resources']:
        need(actual.get(key(r['file'])) == r['expected_sha256'], 'Declared extra resource is not used authority/input')


def runtime_identity(checkpoint):
    # Existing trusted bundled runtime only. No import of bpy/Torch, download or install.
    import numpy
    import OpenImageIO
    base = Path(sys.executable).resolve().parents[1]
    need(base.name == 'python' and (base / 'lib/site-packages').is_dir(), 'Existing Blender bundled Python is required', 'UNSUPPORTED')
    paths = {Path(sys.executable).resolve()}
    for folder in (base, base / 'bin', base / 'DLLs'):
        paths.update(p.resolve() for p in folder.glob('*') if p.is_file() and p.suffix.lower() in ('.dll', '.pyd', '.zip'))
    for module in (numpy, OpenImageIO):
        folder = Path(module.__file__).resolve().parent
        need(folder.is_relative_to(base), 'Numeric module outside bundled Python', 'UNSUPPORTED')
        paths.update(p.resolve() for p in folder.rglob('*') if p.is_file() and p.suffix.lower() in ('.py', '.pyd', '.dll'))
    for folder in (base / 'lib/site-packages').glob('*.libs'):
        paths.update(p.resolve() for p in folder.rglob('*') if p.is_file() and p.suffix.lower() in ('.dll', '.pyd'))
    files = []
    for p in sorted(paths):
        checkpoint()
        files.append(row(p))
    return {'python': row(sys.executable), 'files': files,
            'versions': {'python': sys.version, 'numpy': numpy.__version__, 'OpenImageIO': OpenImageIO.VERSION_STRING},
            'scope': 'trusted installed bundled numeric packages and native closure; not an OS sandbox'}


def freeze_codes(codes, target, inputs):
    folder = target / 'source'
    folder.mkdir(exist_ok=False)
    snapshots = []
    for r in codes:
        inputs.protect(descriptor(r))
        path = folder / (r['role'].replace(':', '-') + ('' if r['role'].endswith('.py') else '.py'))
        with path.open('xb') as stream:
            stream.write(Path(r['file']).read_bytes())
        snapshot = row(path, r['role'])
        need(snapshot['sha256'] == r['sha256'], 'Frozen source copy differs')
        inputs.protect(descriptor(snapshot))
        snapshot['source_file'] = r['file']
        snapshots.append(snapshot)
    return snapshots


def parent_authority(bundle_path, bundle, inputs):
    parent = bundle_path.parent
    need(bundle_path.name == 'depth-bundle.json' and len(parent.name) == 32 and
         all(c in '0123456789abcdef' for c in parent.name), 'Bundle must be in its public owned parent job')
    need(bundle.get('schema') == 'compositing-depth-bundle-v1' and bundle.get('status') == 'complete' and
         bundle.get('parent_job_id') == parent.name, 'Bundle incomplete or parent differs')
    docs = {name: protect_read(inputs, row(parent / name)) for name in ('status.json', 'result.json', 'request.json', 'launch.json')}
    state, result = docs['status.json'], docs['result.json']
    need(state.get('job_id') == parent.name and state.get('state') == 'succeeded' and
         type(state.get('exit_code')) is int and state['exit_code'] == 0 and
         type(state.get('finished_at')) in (float, int) and math.isfinite(state['finished_at']) and state['finished_at'] > 0,
         'Public bundle parent has not succeeded; a complete partial cannot advance authority')
    need(result.get('ok') is True and result.get('error') is None and result.get('job_id') == parent.name and
         result.get('command') == 'compositing.depth-prepare' and result.get('schema_version') == '1.0',
         'Parent result is not a matching successful depth-prepare envelope')
    request = docs['request.json']
    need(request.get('command') == 'compositing.depth-prepare' and request.get('schema_version') == '1.0', 'Parent request differs')
    normalized = normalize_params(request.get('params'), 'depth-prepare')
    need(normalized == bundle['request']['params'] and signature(normalized) == bundle['request_signature'] ==
         result.get('data', {}).get('request_signature'), 'Parent request/result/bundle signatures differ')
    for name in ('request.json', 'launch.json'):
        need(digest(parent / name) == bundle['parent_documents'][name]['sha256'] and
             key(parent / name) == key(bundle['parent_documents'][name]['file']), 'Bound parent document changed')
    need(docs['launch.json'].get('depth_identity') == bundle['producer_identity'] and
         docs['launch.json'].get('depth_request_signature') == bundle['request_signature'], 'Parent launch identity differs')
    actual = row(bundle_path)
    need(result['data'].get('bundle') == actual and result.get('artifacts', {}).get('depth_bundle') == str(bundle_path),
         'Parent result does not bind exact complete bundle')
    report_path = parent / 'depth-report.json'
    report = protect_read(inputs, bundle['prepare_report'])
    need(key(report_path) == key(bundle['prepare_report']['file']) and report.get('status') == 'passed' and
         report.get('request_signature') == bundle['request_signature'] and report.get('members') == bundle['members'],
         'Bound prepare report differs')
    need(report.get('stats') == bundle.get('stats') and report.get('stage_parameters') == bundle.get('stage_parameters') and
         bundle.get('stage_keys') == stage_keys(normalized, bundle['producer_identity']), 'Bundle statistics or stage keys differ')
    if normalized['normal']['method'] == 'import_geometry':
        imported = bundle['stage_parameters'].get('normal_import')
        need(type(imported) is dict and imported.get('producer_verified') is False and
             imported.get('source') == normalized['normal']['source'] and
             imported.get('surface_relation') == normalized['normal']['surface_relation'] and
             bundle.get('normal_import') == imported and
             bundle.get('normal_frame') == 'camera_Xright_Yup_Zbackward',
             'Frozen imported Normal declaration/frame differs')
    return normalized


def inspect_admission(params, inputs, identity):
    bundle_path = Path(params['bundle']['file'])
    bundle = protect_read(inputs, params['bundle'])
    normalized = parent_authority(bundle_path, bundle, inputs)
    historical = bundle['frozen_sources']
    need(code_signature(historical) == bundle['producer_identity']['sha256'], 'Frozen producer sources/signature differ')
    numeric_profile_admission(normalized, historical, bundle['producer_identity'], identity)
    for r in historical:
        need(Path(r['file']).parent == bundle_path.parent / 'depth/source', 'Source snapshot outside owned folder')
        inputs.protect(descriptor(r))
    protect_data(normalized, 'depth-prepare', inputs)
    evidence = provenance(normalized, inputs)
    need(evidence == bundle['provenance'], 'Frozen provenance interpretation differs')
    need(type(bundle.get('input_files')) is list and len(bundle['input_files']) <= 128,
         'Bundle data inventory exceeds the supported profile')
    for r in bundle['input_files']:
        inputs.protect(descriptor(r))
    # Exact consumed inventory, including dynamic authority, is bound by complete bundle.
    need({key(r['file']): r['sha256'] for r in bundle['input_files']} ==
         {key(r['file']): r['sha256'] for r in data_inventory(normalized, bundle['provenance'], inputs)},
         'Historical data inventory differs')
    needed = {'scalar', 'validity', 'scalar_preview', 'validity_preview'}
    if normalized['normal']['method'] != 'none':
        needed |= {'normal', 'normal_validity', 'normal_preview'}
    members = bundle['members']
    need(len(members) == len(needed) and {r['role'] for r in members} == needed, 'Bundle member roles incomplete/duplicated')
    for r in members:
        need(Path(r['file']).parent == bundle_path.parent / 'depth' and type(r.get('bytes')) is int and
             Path(r['file']).stat().st_size == r['bytes'], 'Bundle member path/size differs')
        inputs.protect(descriptor(r))
    return bundle


def data_inventory(params, evidence, inputs):
    paths = {key(r['file']) for r in input_descriptors(params, 'depth-prepare')}
    if evidence['kind'] == 'verified_dav2_single_CG':
        complete = read(params['provenance']['completion']['file'])
        paths.update(key(complete[k]['file']) for k in ('execution_registration', 'producer_frozen', 'request', 'release', 'run_owner', 'terminal', 'sidecar', 'raw'))
    elif evidence['kind']=='verified_public_dav2':
        # Exact estimator authority collected by admission; no model/payload byte reread.
        producer = Path(params['provenance']['producer']['file']); parent = producer.parent
        report = read(producer)
        paths.update(key(parent/n) for n in ('status.json','result.json','request.json','launch.json'))
        paths.update(key(r['file']) for r in report['frozen_sources'])
        paths.update(key(report[n]['file']) for n in ('runtime_profile','guarded_input_inventory','worker_report','raw'))
        result = read(parent/'result.json'); paths.add(key(result['data']['prepare_params']['file']))
    return sorted([r for k, r in inputs.files.items() if k in paths], key=lambda r: key(r['file']))


def owned_worker(job, binding, checkpoint):
    target = job / 'depth'
    gate = target / 'gate'
    gate.mkdir(exist_ok=False)
    pycache = target / 'empty-pycache'
    pycache.mkdir(exist_ok=False)
    python = binding['runtime']['python']['file']
    worker = next(r['file'] for r in binding['codes'] if r['role'] == 'depth_worker')
    bootstrap = next(r['file'] for r in binding['codes'] if r['role'] == 'host:bootstrap.py')
    command = [python, '-I', '-B', '-X', 'pycache_prefix=' + str(pycache), bootstrap, str(gate),
               python, '-I', '-B', '-X', 'pycache_prefix=' + str(pycache), worker, str(job)]
    started = time.monotonic()
    lifetime = {'ownership': 'new gated ProcessTree JobObject', 'command_profile': 'existing bundled Python -I -B, empty pycache prefix',
                'timeout_seconds': binding['timeout_seconds'], 'cancel_grace_seconds': 2, 'partial_eligible': False}
    tree = None
    first = None
    cancelled_at = None
    secondary_errors = []
    def secondary(exc, stage):
        nonlocal first
        secondary_errors.append({'stage': stage, 'type': type(exc).__name__, 'message': str(exc)})
        if first is None:
            first = exc
    try:
        with (target / 'worker.stdout.log').open('x', encoding='utf-8') as stdout, (target / 'worker.stderr.log').open('x', encoding='utf-8') as stderr:
            tree = ProcessTree(command, job_name='blenderctl-depth-' + uuid.uuid4().hex, stdin=None,
                               stdout=stdout, stderr=stderr, env=os.environ.copy(), shell=False)
            lifetime['bootstrap_pid'] = tree.process.pid
            publish(target / 'owned-process.json', lifetime)
            (gate / 'worker.gate').touch(exist_ok=False)
            while tree.process.poll() is None:
                if first is None:
                    try:
                        checkpoint()
                        need(time.monotonic() - started <= binding['timeout_seconds'], 'Owned depth worker timed out', 'TIMEOUT')
                    except Exception as exc:
                        first, cancelled_at = exc, time.monotonic()
                        (job / 'depth.cancel.request').touch(exist_ok=True)
                elif time.monotonic() - cancelled_at >= 2:
                    tree.terminate()
                    lifetime['terminated_owned_tree'] = True
                    break
                time.sleep(.1)
            lifetime['returncode'] = tree.process.poll()
            if first is None and lifetime['returncode'] in (6, 7):
                first = DepthError('TIMEOUT' if lifetime['returncode'] == 6 else 'CANCELLED',
                                   'Owned numerical child returned cancellation/timeout')
            try:
                lifetime['usage'] = tree.usage()
            except Exception as exc:
                secondary(exc, 'usage')
    except Exception as exc:
        secondary(exc, 'execution/termination')
    finally:
        if tree is not None:
            try:
                if tree.process.poll() is None:
                    tree.terminate()
            except Exception as exc:
                secondary(exc, 'cleanup termination')
            finally:
                try:
                    tree.close()
                except Exception as exc:
                    secondary(exc, 'JobObject close')
        lifetime['elapsed_seconds'] = time.monotonic() - started
        if first:
            lifetime['first_error'] = {'code': getattr(first, 'code', 'WORKER_FAILED'), 'message': str(first)}
        lifetime['secondary_errors'] = secondary_errors
        try:
            publish(job / 'depth-lifecycle.json', lifetime)
        except Exception as exc:
            secondary(exc, 'lifecycle diagnostic publication')
            try:
                publish(job / 'depth-lifecycle-secondary.json', lifetime)
            except Exception:
                pass
    if first:
        raise first
    report_path = target / 'worker-report.json'
    need(report_path.is_file(), 'Owned numeric worker has no report', 'WORKER_FAILED')
    report = read(report_path)
    error_code = report.get('error', {}).get('code', 'WORKER_FAILED')
    if error_code not in ('INVALID_REQUEST', 'NOT_FOUND', 'CONFLICT', 'WORKER_FAILED', 'TIMEOUT', 'CANCELLED',
                          'VALIDATION_FAILED', 'IO_ERROR', 'UNSUPPORTED', 'RESOURCE_LIMIT'):
        error_code = 'VALIDATION_FAILED'
    need(lifetime['returncode'] == 0 and report.get('status') == 'passed',
         report.get('error', {}).get('message', 'Numeric worker failed'), error_code)
    need(report.get('request_signature') == binding['request_signature'], 'Worker request binding differs')
    checkpoint()
    return report, lifetime


def stage_keys(params, identity):
    scalar = signature({k: params[k] for k in ('depth', 'domain', 'semantics', 'validity', 'normalization', 'edits')} |
                       {'numeric_sha256': identity['numeric_sha256']})
    return {'scalar': scalar, 'normal': signature({'scalar_key': scalar, 'normal': params['normal']}),
            'RGB': signature({'rgb': params['rgb'], 'encoding': params['rgb_encoding'], 'domain': params['domain']}),
            'scope': 'content/parameter keys; this version creates a new owned bundle and does not claim a cache hit'}


def run(params, job, operation, cancel_parent=None, expected_identity=None):
    """Prepare publishes only after worker readback/input protection; inspect never advances producer."""
    job = Path(job).resolve()
    op = operation.removeprefix('compositing.')
    params = normalize_params(params, op)
    started = time.monotonic()
    try:
        need(job.is_dir() and not (job / 'depth').exists(), 'Fresh public owned depth job required')
        target = job / 'depth'
        target.mkdir(exist_ok=False)
        identity = launch_identity()
        launch = read(job / 'launch.json')
        expected = expected_identity or launch.get('depth_identity')
        need(expected == identity and launch.get('depth_request_signature') == signature(params), 'Queued depth implementation/request changed')
        def checkpoint():
            if cancel_parent:
                cancel_parent()
            need(not (job / 'cancel.request').exists(), 'Public cancellation requested', 'CANCELLED')
        with ExitStack() as stack:
            inputs = BoundedInputs(stack, checkpoint)
            for name in ('request.json', 'launch.json'):
                inputs.protect(descriptor(row(job / name)))
            for r in identity['files']:
                inputs.protect(descriptor(r))
            protect_data(params, op, inputs)
            if op == 'depth-prepare':
                evidence = provenance(params, inputs)
                data_files = data_inventory(params, evidence, inputs)
                resources_used(params, data_files)
                bundle = None
                timeout = params['budget']['timeout_seconds']
            else:
                bundle = inspect_admission(params, inputs, identity)
                data_files = list(inputs.files.values())
                resources_used(params, data_files)
                evidence = bundle['provenance']
                timeout = min(1200, math.floor(launch['timeout_seconds'] - 10))
            runtime = runtime_identity(checkpoint)
            if op == 'depth-inspect':
                need(runtime == bundle['runtime'], 'A different numerical runtime has not qualified this cold bundle', 'UNSUPPORTED')
            for r in runtime['files']:
                inputs.protect(descriptor(r))
            codes = freeze_codes(identity['files'], target, inputs)
            binding = {'schema': 'depth-worker-binding-v1', 'operation': op, 'request': {'params': params},
                       'request_signature': signature(params), 'codes': codes, 'runtime': runtime,
                       'timeout_seconds': timeout, 'inspect_bundle': bundle, 'input_files': list(inputs.files.values())}
            publish(target / 'binding.json', binding)
            inputs.protect(descriptor(row(target / 'binding.json')))
            report, lifetime = owned_worker(job, binding, checkpoint)
            if op == 'depth-inspect':
                need(report.get('members') == bundle['members'] and report.get('cold_read_only') is True,
                     'Cold reader changed member inventory or recomputed producer')
                need(report.get('stats', {}).get('readback_statistics') == bundle['stats'].get('readback_statistics'),
                     'Cold numerical readback statistics differ')
            for r in report.get('members', []):
                if op == 'depth-prepare':
                    need(Path(r['file']).parent == target, 'Worker output outside owned depth directory')
                inputs.protect(descriptor(r))
                need(Path(r['file']).stat().st_size == r['bytes'], 'Worker output size differs')
            inputs.recheck()
            report.update(schema='compositing-depth-report-v1', operation=op, provenance=evidence,
                          producer_identity=identity, source_inputs=data_files,
                          input_protection='Windows deny-write/delete plus ancestor guards and pre/post SHA',
                          public_qualification='experimental numeric bundle; lighting/model generalization not qualified',
                          consumer_eligible=False, host_elapsed_seconds=time.monotonic() - started)
            publish(job / 'depth-report.json', report)
            if op == 'depth-prepare':
                published = {'schema': 'compositing-depth-bundle-v1', 'status': 'complete', 'parent_job_id': job.name,
                             'parent_documents': {name: row(job / name) for name in ('request.json', 'launch.json')},
                             'request': {'params': params}, 'request_signature': signature(params),
                             'producer_identity': identity, 'frozen_sources': codes, 'runtime': runtime,
                             'provenance': evidence, 'input_files': data_files, 'members': report['members'],
                             'stats': report['stats'], 'stage_parameters': report['stage_parameters'],
                             'stage_keys': stage_keys(params, identity), 'prepare_report': row(job / 'depth-report.json'),
                             'scalar_semantics': 'positive axial camera depth' if params['semantics']['measure'] == 'axial_z' else 'edited artistic near-one scalar; not metric',
                             'normal_frame': ('camera_Xright_Yup_Zbackward' if params['normal']['method'] == 'import_geometry' else
                                              'camera-X-right-Y-up-Z-toward-camera' if params['normal']['method'] == 'central_perspective' else
                                              'artistic-image-height-proxy-X-right-Y-up-Z-toward-viewer' if params['normal']['method'] == 'central_heightfield' else None),
                             'normal_encoding': 'signed-float32-XYZ' if params['normal']['method'] != 'none' else None,
                             'normal_invalid': 'zero vector with separate binary mask; never a surface normal',
                             'source_RGB_is_albedo': False, 'validity_is_confidence': False,
                             'consumer_eligible': False, 'public_parent_success_required': True}
                if params['normal']['method'] == 'import_geometry':
                    # Numerical preparation is qualified by this owned worker;
                    # caller provenance remains an explicit unverified declaration.
                    normal_import = report['stage_parameters']['normal_import']
                    need(type(normal_import) is dict and
                         normal_import.get('producer_verified') is False and
                         normal_import.get('source') == params['normal']['source'] and
                         normal_import.get('surface_relation') == params['normal']['surface_relation'],
                         'Imported Normal declaration/stage interpretation differs')
                    published['normal_import'] = copy.deepcopy(normal_import)
                publish(job / 'depth-bundle.json', published)
                report = copy.deepcopy(report)
                report['bundle'] = row(job / 'depth-bundle.json')
            else:
                report['inspected_bundle'] = params['bundle']
                report['cold_read_only'] = True
            checkpoint()
            return report
    except EncodeError as exc:
        error = DepthError(exc.code, str(exc))
        _failure(job, op, params, error)
        raise error from exc
    except Exception as exc:
        _failure(job, op, params, exc)
        raise


def _failure(job, op, params, error):
    if job.is_dir() and not (job / 'depth-report.json').exists():
        try:
            publish(job / 'depth-report.json', {'schema': 'compositing-depth-report-v1', 'status': 'failed',
                                               'operation': op, 'request_signature': signature(params),
                                               'error': {'code': getattr(error, 'code', 'WORKER_FAILED'), 'message': str(error)},
                                               'partial_eligible': False, 'consumer_eligible': False})
        except OSError:
            pass
