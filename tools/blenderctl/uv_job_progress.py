"""Read the UV panel's supervised job state without waiting or changing the job."""
import argparse
import json
import math
from pathlib import Path
import re
import sys
import time

sys.dont_write_bytecode = True
from protocol import CODES, DEFAULT_JOBS, Failure, envelope, read_json
from runner import job_path


COMMAND = 'uv.job-progress'
TERMINAL = {'succeeded', 'failed', 'cancelled', 'timed_out'}
STATES = TERMINAL | {'queued', 'running', 'cancelling'}
MAX_RECORD_BYTES = 2 * 1024 * 1024


def require(condition, code, message):
    if not condition:
        raise Failure(code, message)


def read_record(path):
    require(path.resolve(strict=True) == path and path.is_file(), 'CONFLICT', f'Redirected job record: {path.name}')
    require(path.stat().st_size <= MAX_RECORD_BYTES, 'RESOURCE_LIMIT', f'{path.name} exceeds 2 MiB')
    value = read_json(path)
    require(isinstance(value, dict), 'INVALID_REQUEST', f'{path.name} must be a JSON object')
    return value


def nonnegative_seconds(value):
    try:
        return type(value) in (int, float) and 0 <= value and math.isfinite(value)
    except OverflowError:
        return False


def progress_record(path):
    try:
        value = read_record(path)
    except FileNotFoundError:
        require(not path.is_symlink(), 'CONFLICT', 'Redirected uv-progress.json')
        return None
    require(value.get('uv_progress_version') == '1.0'
            and isinstance(value.get('stage'), str) and 0 < len(value['stage']) <= 128
            and isinstance(value.get('status'), str) and 0 < len(value['status']) <= 128
            and nonnegative_seconds(value.get('elapsed_seconds')),
            'INVALID_REQUEST', 'Invalid UV progress version, stage, status or elapsed seconds')
    if 'job_id' in value:
        require(value['job_id'] == path.parent.name, 'CONFLICT', 'UV progress belongs to another job')
    names = ('uv_progress_version', 'stage', 'status', 'elapsed_seconds', 'operation', 'method',
             'action', 'object', 'frame', 'mode', 'triangles', 'quality_acceptance', 'error')
    return {key: value[key] for key in names if key in value}


def observe(jobs_dir, ident):
    job = job_path(jobs_dir, ident)
    require(job.name == ident, 'CONFLICT', 'Job directory does not match its ID')
    request_file = job / 'request.json'
    request = read_record(request_file)
    command = request.get('command')
    require(isinstance(command, str) and command in ('uv.run', 'uv.inspect'),
            'UNSUPPORTED', 'Expected a uv.run or uv.inspect job. Use job wait for other jobs.')
    params = request.get('params')
    require(request.get('schema_version') == '1.0' and isinstance(params, dict)
            and isinstance(params.get('file'), str) and '\x00' not in params['file']
            and Path(params['file']).is_absolute() and Path(params['file']).suffix.lower() == '.blend'
            and isinstance(params.get('expected_sha256'), str)
            and re.fullmatch(r'[0-9a-f]{64}', params['expected_sha256']),
            'INVALID_REQUEST', 'Expected a bound file and SHA-256 in the saved UV request')

    status_file = job / 'status.json'
    status = read_record(status_file)
    state, heartbeat_at = status.get('state'), status.get('heartbeat_at')
    require(status.get('job_id') == ident, 'CONFLICT', 'Status belongs to another job')
    require(isinstance(state, str) and state in STATES and nonnegative_seconds(heartbeat_at),
            'INVALID_REQUEST', 'Invalid job state or heartbeat timestamp')
    progress_file = job / 'uv-progress.json'
    progress = progress_record(progress_file)
    observed_at = time.time()
    age = max(0.0, observed_at - heartbeat_at)
    health = 'stale_supervisor_review_required' if state in ('queued', 'running') and age > 15 else 'current'
    data = {'job_command': command, 'state': state, 'terminal': state in TERMINAL,
            'source': {'file': params['file'], 'sha256': params['expected_sha256']},
            'heartbeat': {'at': heartbeat_at, 'age_seconds': round(age, 3), 'health': health},
            'progress': progress, 'observed_at': observed_at, 'quality': 'not_assessed'}
    artifacts = {'job_directory': str(job), 'request': str(request_file), 'status': str(status_file)}
    if progress is not None:
        artifacts['progress'] = str(progress_file)
    return envelope(COMMAND, ident, data, artifacts=artifacts)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure('INVALID_REQUEST', message)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = Parser(description=__doc__)
    parser.add_argument('--blender', help=argparse.SUPPRESS)
    parser.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS, help='Original public jobs root')
    output = parser.add_mutually_exclusive_group()
    output.add_argument('--compact', action='store_true')
    output.add_argument('--human', action='store_true')
    action = parser.add_subparsers(required=True, parser_class=Parser).add_parser(
        'job-progress', description='Read one snapshot of an existing uv.run or uv.inspect job. '
        'Select its original root with global --jobs-dir. Follow the same ID with job wait. '
        'A terminal state is independent of UV quality.')
    action.add_argument('job_id', help='Existing 32-character lowercase hexadecimal job ID')
    args = None
    try:
        args = parser.parse_args()
        result = observe(args.jobs_dir, args.job_id)
        rendered = json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (Failure, OSError, ValueError) as exc:
        fallback = ('NOT_FOUND' if isinstance(exc, FileNotFoundError) else 'IO_ERROR' if isinstance(exc, OSError)
                    else 'INVALID_REQUEST')
        result = envelope(COMMAND, error={'code': getattr(exc, 'code', fallback), 'message': str(exc)})
        rendered = json.dumps(result, ensure_ascii=False, allow_nan=False)
    if args and args.human:
        if result['ok']:
            data = result['data']
            phase = (data['progress'] or {}).get('stage')
            print(' '.join(filter(None, (result['job_id'], data['job_command'], data['state'], phase))))
        else:
            print(result['error']['message'])
    else:
        print(rendered)
    return 0 if result['ok'] else CODES.get(result['error']['code'], 5)


if __name__ == '__main__':
    raise SystemExit(main())
