"""Refresh an owned Material Workflow submission or explicitly retry its saved snapshot."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import sys
import uuid

from filesystem import FileGuard, PathLocks
from protocol import CODES, DEFAULT_BLENDER, ROOT, Failure, atomic_json, digest, envelope, read_json, validate

sys.path.insert(0, str(ROOT / 'tools'))
from material_workflow_addon import handoff, job_bridge
from material_workflow_addon.contract import WorkflowError


COMMAND = 'material.recover-submission'
RETRY = 'material.retry-submission'
MODES = {'SINGLE': 'material.run', 'BATCH': 'material.batch'}
PENDING = {'queued', 'running'}
TERMINAL = {'succeeded', 'failed', 'cancelled', 'timed_out'}


def require(condition, code, message):
    if not condition:
        raise Failure(code, message)


def absolute(value):
    require(isinstance(value, (str, Path)) and Path(value).is_absolute(),
            'INVALID_REQUEST', 'Expected an absolute file or directory path')
    return Path(os.path.abspath(value))


def canonical(value):
    path = absolute(value)
    require(path == path.resolve(strict=True), 'CONFLICT', f'Redirected path: {path}')
    return path


def document(value, directory, stack):
    require(isinstance(value, dict) and set(value) == {'file', 'expected_sha256'}
            and isinstance(value['expected_sha256'], str)
            and re.fullmatch(r'[0-9a-f]{64}', value['expected_sha256']),
            'INVALID_REQUEST', 'Expected a file descriptor with lowercase SHA-256')
    path = absolute(value['file'])
    require(path.parent == directory, 'CONFLICT', f'File is outside its owned directory: {path}')
    canonical(path)
    guard = stack.enter_context(FileGuard(path))
    require(guard.sha256() == value['expected_sha256'], 'CONFLICT', f'SHA-256 mismatch: {path}')
    return path


def bound_request(descriptor, snapshot, resources, command, folder, stack):
    path = document(descriptor, folder, stack)
    original = read_json(path)
    require(isinstance(original, dict) and original.get('command') == command
            and isinstance(original.get('params'), dict), 'INVALID_REQUEST', 'Invalid material handoff request')
    params = original['params']
    required = {'file', 'expected_sha256', 'manifest', 'resources'}
    allowed = required | ({'resume'} if command == MODES['BATCH'] else set())
    require(required <= set(params) <= allowed, 'INVALID_REQUEST', 'Expected an inline material handoff request')
    require(all(params.get(key) == value for key, value in snapshot.items())
            and params.get('resources') == resources, 'CONFLICT', 'Request does not own this handoff snapshot or resources')
    return path, original, validate(original)


def verify_ref(ref, request, jobs, trusted, stack):
    path, original, normalized = request
    require(isinstance(ref, dict) and isinstance(ref.get('job_id'), str)
            and re.fullmatch(r'[0-9a-f]{32}', ref['job_id']), 'INVALID_REQUEST', 'Invalid job reference')
    require(Path(ref.get('jobs_dir', '')) == jobs and Path(ref.get('request_file', '')) == path
            and ref.get('submitted_request_sha256') == job_bridge._sha(original)
            and ref.get('request_sha256') == job_bridge._sha(normalized),
            'CONFLICT', 'Job belongs to a different handoff request')
    job = canonical(jobs / ref['job_id'])
    stack.enter_context(FileGuard(job, directory=True))
    for name in ('request.json', 'launch.json'):
        stack.enter_context(FileGuard(job / name))
    job_bridge._owned(ref, trusted)
    return job


def refresh(receipt, ref, trusted, job, snapshot, stack):
    status = job_bridge.status(ref, trusted)
    if not status['ok']:
        raise job_bridge.BridgeError('Could not read the recovered job status', status)
    observed = status.get('data')
    require(status.get('command') == 'job.status' and status.get('job_id') == ref['job_id']
            and isinstance(observed, dict) and observed.get('job_id') == ref['job_id']
            and observed.get('state') in PENDING | TERMINAL,
            'CONFLICT', 'Job status does not match the recovered job')
    state = observed['state']
    receipt.update(job_ref=ref, job_status=observed, state=state)
    for key in ('result', 'workflow_status', 'candidate', 'sheet'):
        receipt.pop(key, None)
    if state not in TERMINAL:
        return None, None

    result_path = job / 'result.json'
    stack.enter_context(FileGuard(result_path))
    result = job_bridge.result(ref, trusted)
    require(result.get('job_id') == ref['job_id'] and result.get('command') == ref['command']
            and result['ok'] == (state == 'succeeded'), 'CONFLICT', 'Terminal result does not match the recovered job')
    receipt['result'] = result
    if not result['ok']:
        return None, None
    data = result.get('data') or {}
    require(isinstance(data, dict) and data.get('source_sha256') == snapshot['expected_sha256'],
            'CONFLICT', 'Result belongs to a different snapshot')
    receipt['workflow_status'] = data.get('status')
    if data.get('status') == 'pass' and data.get('candidate'):
        descriptor = {'file': data['candidate'], 'expected_sha256': data.get('candidate_sha256')}
        try:
            candidate = document(descriptor, job, stack)
        except (Failure, OSError) as exc:
            code = getattr(exc, 'code', 'NOT_FOUND' if isinstance(exc, FileNotFoundError) else 'IO_ERROR')
            return None, {'code': code, 'message': str(exc)}
        receipt.update(candidate=str(candidate), sheet=data.get('sheet'))
        return {'file': str(candidate), 'sha256': descriptor['expected_sha256']}, None
    return None, None


def retry(path, receipt, request, job, trusted, jobs, stack):
    require(receipt['state'] in TERMINAL, 'CONFLICT', 'Retry requires a terminal job')
    timeout = receipt.get('timeout_seconds', 300)
    require(type(timeout) is int and 30 <= timeout <= 3600, 'INVALID_REQUEST', 'Invalid original panel timeout')
    if receipt['resources']:
        dependencies = canonical(path.parent / 'dependencies')
        stack.enter_context(FileGuard(dependencies, directory=True))
        for resource in receipt['resources']:
            document(resource, dependencies, stack)
    original = request[1]
    params = dict(original['params'])
    params.pop('resume', None)
    report = job / 'batch-report.json'
    if receipt['mode'] == 'BATCH' and report.is_file():
        guard = stack.enter_context(FileGuard(report))
        params['resume'] = {'file': str(report), 'expected_sha256': guard.sha256()}
    next_request = {**original, 'params': params}
    validate(next_request)
    new_path = path.parent / ('request-' + uuid.uuid4().hex + '.json')
    with new_path.open('x', encoding='utf-8') as out:
        json.dump(next_request, out, ensure_ascii=False, allow_nan=False, indent=2)
    descriptor = {'file': str(new_path), 'expected_sha256': digest(new_path)}
    bound = bound_request(descriptor, receipt['snapshot'], receipt['resources'], MODES[receipt['mode']], path.parent, stack)

    previous = receipt['job_ref']
    receipt.setdefault('attempts', []).append({'job_ref': previous, 'request': receipt['request'],
                'state': receipt['state'], 'workflow_status': receipt.get('workflow_status')})
    for key in ('job_status', 'result', 'workflow_status', 'candidate', 'sheet', 'submission', 'cancel_request', 'error'):
        receipt.pop(key, None)
    receipt.update(job_ref=None, state='submitting', request=descriptor)
    atomic_json(path, receipt)
    pending_sha = digest(path)
    try:
        ack = job_bridge.submit(new_path, jobs, trusted, timeout=timeout)
        new_ref = ack['job_ref']
        new_job = verify_ref(new_ref, bound, jobs, trusted, stack)
        require(new_job != job, 'CONFLICT', 'Retry returned the previous job')
    except Exception as exc:
        if digest(path) == pending_sha:
            receipt.update(state='submission_unknown', error=str(exc))
            atomic_json(path, receipt)
        raise
    require(digest(path) == pending_sha, 'CONFLICT', 'Receipt changed after retry submission; recover the existing job')
    receipt.update(job_ref=new_ref, state='submitted', submission=ack)
    atomic_json(path, receipt)
    return envelope(RETRY, new_job.name, {'receipt': {'file': str(path), 'sha256': digest(path)}, 'job_ref': new_ref,
                    'previous_job_id': job.name, 'state': 'submitted', 'snapshot': receipt['snapshot'],
                    'request': descriptor, 'resume': params.get('resume')},
                    artifacts={'receipt': str(path), 'job_directory': str(new_job)})


def run(args):
    retrying = args.action == 'retry-submission'
    path = canonical(args.receipt)
    folder = path.parent
    require(path.name == 'handoff.json' and re.fullmatch(r'handoff-[0-9a-f]{32}', folder.name),
            'INVALID_REQUEST', 'Expected the original handoff.json in its generated handoff directory')
    require(re.fullmatch(r'[0-9a-f]{64}', args.expected_sha256), 'INVALID_REQUEST', 'Expected lowercase receipt SHA-256')
    jobs = folder / 'jobs'
    if args.jobs_dir is not None:
        require(absolute(args.jobs_dir) == jobs, 'CONFLICT', '--jobs-dir differs from the handoff-owned jobs directory')

    with PathLocks([path]), ExitStack() as stack:
        for parent in reversed(path.parents):
            if parent != Path(parent.anchor):
                stack.enter_context(FileGuard(parent, directory=True))
        with FileGuard(path) as guard:
            require(guard.sha256() == args.expected_sha256, 'CONFLICT', 'Handoff receipt SHA-256 changed')
            raw = read_json(path)
            require(isinstance(raw, dict) and raw.get('receipt') == str(path) and raw.get('folder') == str(folder)
                    and raw.get('handoff_version') == '1.0' and raw.get('id') == folder.name,
                    'INVALID_REQUEST', 'Invalid handoff receipt ownership')
            mode, state, ref = raw.get('mode'), raw.get('state'), raw.get('job_ref')
            require(isinstance(mode, str) and mode in MODES and isinstance(state, str)
                    and (state in PENDING | TERMINAL | {'submitted'} if ref else state in {'submitting', 'submission_unknown'}),
                    'INVALID_REQUEST', 'Receipt has no accepted job or recoverable submission state')
            snapshot, resources = raw.get('snapshot'), raw.get('resources')
            require(document(snapshot, folder, stack).name == 'snapshot.blend' and isinstance(resources, list),
                    'INVALID_REQUEST', 'Invalid handoff snapshot or resource list')
            request = bound_request(raw.get('request'), snapshot, resources, MODES[mode], folder, stack)
            receipt = handoff.load_receipt(path)

        jobs = canonical(jobs)
        stack.enter_context(FileGuard(jobs, directory=True))
        # Select executable paths from the caller and this installation, never from the receipt.
        trusted = job_bridge.configure(ROOT, canonical(args.blender))
        require(receipt.get('config') == trusted, 'CONFLICT', 'Receipt backend differs from this CLI and selected Blender')
        already_known = bool(ref)
        if retrying:
            require(already_known, 'CONFLICT', 'Recover the existing submission before retrying')
        if not already_known or retrying:
            attempts = receipt.get('attempts', [])
            require(isinstance(attempts, list), 'INVALID_REQUEST', 'Invalid previous attempt list')
            prior = []
            for attempt in attempts:
                require(isinstance(attempt, dict), 'INVALID_REQUEST', 'Invalid previous attempt')
                previous = bound_request(attempt.get('request'), snapshot, resources, MODES[mode], folder, stack)
                job = verify_ref(attempt.get('job_ref'), previous, jobs, trusted, stack)
                prior.append(job.name)
            if not already_known:
                ref = job_bridge.recover(request[0], jobs, trusted, exclude_job_ids=prior)
        job = verify_ref(ref, request, jobs, trusted, stack)
        output, output_error = refresh(receipt, ref, trusted, job, snapshot, stack)

        # Public invocations serialize on the receipt. Detect a change from an external writer before publishing.
        require(digest(path) == args.expected_sha256, 'CONFLICT', 'Handoff receipt changed while recovering the job')
        if retrying:
            return retry(path, receipt, request, job, trusted, jobs, stack)
        atomic_json(path, receipt)
        receipt_file = {'file': str(path), 'sha256': digest(path)}

    data = {'receipt': receipt_file, 'job_ref': ref, 'state': receipt['state'],
            'job_status': receipt['job_status'], 'job_result_error': (receipt.get('result') or {}).get('error'),
            'already_known': already_known, 'workflow_status': receipt.get('workflow_status'),
            'output': output, 'output_error': output_error}
    artifacts = {'receipt': str(path), 'job_directory': str(job)}
    if receipt['state'] in TERMINAL:
        artifacts['job_result'] = str(job / 'result.json')
    if args.full:
        data['receipt_data'] = receipt
    return envelope(COMMAND, job.name, data, artifacts=artifacts)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure('INVALID_REQUEST', message)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    parser = Parser(description=__doc__)
    parser.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    parser.add_argument('--jobs-dir', type=Path, help='Optional assertion of the handoff-owned jobs directory')
    options = parser.add_mutually_exclusive_group()
    for flag in ('--compact', '--full', '--human'):
        options.add_argument(flag, action='store_true')
    actions = parser.add_subparsers(dest='action', required=True, parser_class=Parser)
    action = actions.add_parser(
        'recover-submission', description='Find the existing job for a Material Workflow single-material or batch '
        'handoff and refresh its original handoff.json. This command does not submit or retry jobs.',
        epilog='Use the original project CLI backend and select the same Blender binary used to submit '
        'the handoff (global --blender-version or --blender). Match the current receipt SHA-256.')
    again = actions.add_parser('retry-submission',description='Retry a terminal SINGLE or BATCH job from its verified original snapshot. '
                               'For BATCH, reuse the preceding batch-report.json when available. Refresh the new job with recover-submission.')
    for command in (action, again):
        command.add_argument('--receipt', type=Path, required=True, help='Absolute path to the original handoff.json')
        command.add_argument('--expected-sha256', required=True, help='Current SHA-256 of handoff.json')
    args = None
    name = COMMAND
    try:
        args = parser.parse_args()
        name = RETRY if args.action == 'retry-submission' else COMMAND
        result = run(args)
    except job_bridge.BridgeError as exc:
        error = (exc.envelope or {}).get('error') or {'code': 'CONFLICT', 'message': str(exc)}
        result = envelope(name, error=error)
    except (Failure, WorkflowError, FileNotFoundError, OSError, ValueError, TypeError, KeyError) as exc:
        fallback = ('NOT_FOUND' if isinstance(exc, FileNotFoundError) else 'IO_ERROR' if isinstance(exc, OSError)
                    else 'INVALID_REQUEST')
        result = envelope(name, error={'code': getattr(exc, 'code', fallback), 'message': str(exc)})
    if args and args.human:
        print(result['artifacts']['receipt'] if result['ok'] else result['error']['message'])
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['ok'] else CODES.get(result['error']['code'], 5)


if __name__ == '__main__':
    raise SystemExit(main())
