"""Run the qualified Windows Camera Workflow on a saved Blender project."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import sys

import runner
from material_draft_handoff import Parser, canonical, guard_file, read_document, require
from protocol import CODES, DEFAULT_BLENDER, DEFAULT_JOBS, DEFAULT_TRANSACTIONS, Failure, SCHEMA, digest, envelope, validate


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / 'runtime/camera/0.8.1-cli0.91.0-b93'
QUALIFIED_BLENDER = ROOT / 'runtime/blender/releases/5.2.1/blender-5.2.1-windows-x64/blender.exe'
QUALIFICATION_SHA = 'd9e9bbc5d3bde8400e84016138d4d65b04be39afe65e8d4c423b108d4111d535'
OPERATIONS = {
    'check': {'request', 'job'},
    'apply': {'job', 'check_sha'},
    'cancel': {'job', 'check_sha'},
    'result': {'job', 'terminal_sha'},
    'diagnose': {'request', 'job'},
}
COMMAND = 'camera.copy'


def parser():
    cli = Parser(description=__doc__, allow_abbrev=False)
    cli.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    cli.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS)
    cli.add_argument('--transactions-dir', type=Path, default=DEFAULT_TRANSACTIONS, help=argparse.SUPPRESS)
    cli.add_argument('--timeout', type=float, default=120)
    display = cli.add_mutually_exclusive_group()
    display.add_argument('--human', action='store_true')
    for mode in ('--compact', '--full'):
        display.add_argument(mode, action='store_true', help=argparse.SUPPRESS)
    actions = cli.add_subparsers(required=True, parser_class=Parser)
    copy = actions.add_parser('copy', allow_abbrev=False, description=(
        'Run Camera Workflow 0.8.1 on Windows Blender 5.2.1 using the fixed, qualified bridge.'),
        epilog=('check/diagnose: {job, request}. check request version 1 covers static lens/local_offset, '
                '2 adjacent cut, 3 bone focus, 4 existing motion easing, 5 focus timing, 6 camera-group retime. '
                'apply/cancel: {job, check_sha} from CHECK. result: {job, terminal_sha} from committed APPLY; '
                'terminal_sha may be null. Source, job and external resource paths inside params must be absolute. '
                'The returned envelope is the real extension.run job and can be read by job result.'))
    copy.add_argument('--operation', required=True, choices=tuple(OPERATIONS))
    copy.add_argument('--params', type=Path, required=True, help='UTF-8 JSON with exact native Camera Workflow parameters')
    return cli


def require_sha(value):
    require(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None,
            'INVALID_REQUEST', 'Expected a lowercase SHA-256')
    return value


def descriptor(path, checksum):
    return {'file': str(path), 'expected_sha256': require_sha(checksum)}


def native_input(value):
    require(isinstance(value, dict) and set(value) == {'file', 'sha256'} and isinstance(value['file'], str),
            'INVALID_REQUEST', 'Expected a Camera Workflow {file, sha256} descriptor')
    path = Path(value['file'])
    require(path.is_absolute() and '..' not in path.parts, 'INVALID_REQUEST', 'Expected an absolute input path')
    return descriptor(path, value['sha256'])


def project_inputs(request):
    require(isinstance(request, dict) and 'source' in request, 'INVALID_REQUEST', 'Expected a native Camera request')
    source = native_input(request['source'])
    require(Path(source['file']).suffix.lower() == '.blend', 'INVALID_REQUEST', 'Expected a saved .blend source')
    resources = request.get('resources', [])
    require(isinstance(resources, list), 'INVALID_REQUEST', 'Expected a native Camera resource list')
    return [source, *(native_input(row) for row in resources)]


def require_qualified_host(blender):
    require(os.name == 'nt' and blender == QUALIFIED_BLENDER.resolve(), 'UNSUPPORTED',
            'The historical Camera 0.8.1 qualification is tied to its original host. '
            'Use camera copy-083 on Windows Blender 5.3.0.')


def qualification(blender, guards):
    require_qualified_host(blender)
    path = guard_file(PACKAGE / 'CLI-QUALIFICATION.json', guards, QUALIFICATION_SHA)
    bindings = read_document(path)
    core = PACKAGE / 'camera_windows_static'
    code_manifest = guard_file(core / 'CODE-MANIFEST.json', guards, bindings['code_manifest_sha256'])
    code = read_document(code_manifest)
    bridge = guard_file(PACKAGE / 'cli_bridge.py', guards, bindings['bridge_sha256'])
    # The exact qualification binds both package manifests. Hold their entire
    # frozen inventory while the public job runs. The worker double-checks its
    # original inventory and owns the native camera transaction.
    for name, checksum in code['files'].items():
        guard_file(core / name, guards, checksum)
    for name, checksum in code['host_files'].items():
        guard_file(name, guards, checksum)
    guard_file(blender, guards, code['runtime']['blender'])
    guard_file(blender.parent / '5.2/python/bin/python.exe', guards, code['runtime']['python'])
    resources = [descriptor(path, QUALIFICATION_SHA), descriptor(code_manifest, bindings['code_manifest_sha256'])]
    resources.extend(descriptor(ROOT / name, sha) for name, sha in bindings['supplemental_host_files'].items())
    return descriptor(bridge, bindings['bridge_sha256']), bindings['code_manifest_sha256'], resources


def operation_inputs(operation, params, package_sha, guards):
    require(isinstance(params, dict) and set(params) == OPERATIONS[operation] and isinstance(params['job'], str),
            'INVALID_REQUEST', 'Supply the exact operation parameters shown in camera copy --help')
    job = Path(params['job'])
    require(job.is_absolute() and '..' not in job.parts, 'INVALID_REQUEST', 'Expected an absolute camera job path')
    if operation in ('check', 'diagnose'):
        require(not os.path.lexists(job), 'CONFLICT', 'Camera check or diagnose requires a new job directory')
        params['job'] = str(canonical(job.parent) / job.name)
        return project_inputs(params['request'])
    job = canonical(job)
    params['job'] = str(job)
    receipts = []
    if operation == 'result':
        terminal_file = guard_file(job / 'terminal.json', guards, params['terminal_sha'])
        terminal_sha = digest(terminal_file)
        terminal = read_document(terminal_file)
        require(isinstance(terminal, dict) and isinstance(terminal.get('check_sha'), str),
                'INVALID_REQUEST', 'Camera terminal receipt is invalid')
        receipts.append(descriptor(terminal_file, terminal_sha))
        check_sha = require_sha(terminal['check_sha'])
    else:
        check_sha = require_sha(params['check_sha'])
    check_path = guard_file(job / 'check.json', guards, check_sha)
    check = read_document(check_path)
    require(isinstance(check, dict) and check.get('job') == str(job),
            'CONFLICT', 'CHECK receipt does not belong to this camera job')
    require(check.get('manifest_sha') == package_sha, 'CONFLICT',
            'CHECK uses a different Camera qualification. Run a new CHECK before applying, '
            'or read a completed public job using job result with its original jobs directory and ID.')
    receipts.append(descriptor(check_path, check_sha))
    if operation == 'cancel':
        # Cancel must release a real CHECK even if its original scene has since changed.
        return receipts
    request_ref = native_input(check.get('request'))
    require(Path(request_ref['file']) == job / 'request.json', 'CONFLICT', 'Camera CHECK request path differs')
    request_path = guard_file(request_ref['file'], guards, request_ref['expected_sha256'])
    request = read_document(request_path)
    require(isinstance(request, dict) and request.get('source') == check.get('source'),
            'CONFLICT', 'Camera CHECK source identity differs from its request')
    return [*receipts, request_ref, *project_inputs(request)]


def run(args):
    with ExitStack() as guards:
        params_path = guard_file(args.params.absolute(), guards)
        params = read_document(params_path)
        json.dumps(params, allow_nan=False)
        blender = canonical(args.blender)
        bridge, package_sha, resources = qualification(blender, guards)
        resources.extend(operation_inputs(args.operation, params, package_sha, guards))
        unique = {}
        for item in resources:
            key = os.path.normcase(item['file'])
            require(key not in unique or unique[key] == item, 'CONFLICT', 'Distinct hashes for the same Camera resource')
            unique[key] = item
        request = {
            'schema_version': SCHEMA,
            'command': 'extension.run',
            'params': {
                'manifest': {
                    'script': bridge,
                    'trust': 'execute_local_python_without_sandbox',
                    'params': {'action': args.operation, 'params': params, 'adapter_manifest_sha': package_sha},
                    'outputs': ['output-camera-static.json'],
                },
                'resources': list(unique.values()),
            },
        }
        expected = validate(request)
        job = runner.prepare(expected, blender, args.jobs_dir, args.timeout, args.transactions_dir)
        # Use the normal public runner and its immutable request path, job
        # identity, exact script authorization, worker guard and durable result.
        persisted = guard_file(job / 'request.json', guards)
        require(read_document(persisted) == expected, 'CONFLICT', 'Prepared public camera request changed')
        return runner.execute(job)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    args = None
    try:
        args = parser().parse_args()
        result, code = run(args)
    except Failure as error:
        result = envelope(COMMAND, error={'code': error.code, 'message': str(error)})
        code = CODES.get(error.code, 8)
    except OSError as error:
        result = envelope(COMMAND, error={'code': 'IO_ERROR', 'message': str(error)})
        code = CODES['IO_ERROR']
    except ValueError as error:
        result = envelope(COMMAND, error={'code': 'INVALID_REQUEST', 'message': str(error)})
        code = CODES['INVALID_REQUEST']
    if args and args.human:
        label = 'OK' if result['ok'] else 'ERROR'
        print(f"{label} {result['command']} job={result['job_id'] or '-'}")
        print(json.dumps(result['data'] if result['ok'] else result['error'], ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
