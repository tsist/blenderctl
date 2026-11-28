"""Route public calls to the Physics Workflow release for the selected Blender."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from protocol import CODES, DEFAULT_JOBS, Failure, read_json


ROOT = Path(__file__).resolve().parents[2]
PRODUCT_TOOLS = ROOT / 'tools'
JOBS = {'run', 'sweep', 'batch'}
CONTROLS = {'resume', 'status', 'cancel'}


def from_document(path, scratch):
    doc = read_json(path)
    if (not isinstance(doc, dict) or set(doc) != {'schema_version', 'command', 'params'}
            or doc['schema_version'] != '1.0' or not isinstance(doc['params'], dict)):
        raise Failure('INVALID_REQUEST', 'Expected schema_version 1.0, command and object params')
    command = doc['command']
    if not isinstance(command, str) or not command.startswith('physics.'):
        raise Failure('INVALID_REQUEST', 'Expected a physics command')
    action = command.removeprefix('physics.')
    params = doc['params']
    if action in JOBS:
        payload = Path(scratch) / 'request.json'
        payload.write_text(json.dumps(params, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        return [action, '--request', str(payload)]
    if action == 'describe' and not params:
        return [action]
    if action in CONTROLS and set(params) == {'manifest'} and isinstance(params['manifest'], str):
        return [action, '--manifest', params['manifest']]
    raise Failure('INVALID_REQUEST', 'Unsupported physics command or params; controls take a manifest path')


def invoke(args, argv):
    from physics_workflow_cli_v1.cli import main as physics_main

    action = argv[0] if argv else None
    options = {v.split('=', 1)[0] for v in argv[1:]}
    if action in JOBS:
        if args.jobs_dir and '--jobs-dir' in options:
            raise Failure('INVALID_REQUEST', 'Specify --jobs-dir once, globally or on the physics command')
        if '--jobs-dir' not in options:
            argv.extend(['--jobs-dir', str(args.jobs_dir or DEFAULT_JOBS / 'physics')])
    elif args.jobs_dir and action in CONTROLS:
        raise Failure('INVALID_REQUEST', 'Control commands use the job referenced by --manifest')
    if args.timeout is not None:
        if action not in JOBS | {'resume'} or '--timeout-seconds' in options:
            raise Failure('INVALID_REQUEST', 'Use one timeout for run, sweep, batch or resume')
        argv.extend(['--timeout-seconds', str(args.timeout)])
    from physics_native import SCENE_OPERATIONS, describe, route_request, run

    native = route_request(argv)
    if native is not None and native.get('operation') not in SCENE_OPERATIONS:
        return run(args.blender, native, argv[1:])
    if argv == ['describe']:
        from physics_workflow_cli_v1 import runner

        response = runner.describe().to_dict()
        try:
            response['data']['native_fluid'] = describe(args.blender)
        except (Failure, OSError, ValueError) as exc:
            response['data']['native_fluid'] = {'available': False, 'message': str(exc)}
        print(json.dumps(response, indent=2, ensure_ascii=True))
        return 0
    return physics_main(argv)


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--blender', type=Path, required=True)
    parser.add_argument('--host-version', required=True)
    parser.add_argument('--jobs-dir', type=Path)
    parser.add_argument('--timeout', type=int)
    parser.add_argument('domain', choices=('physics', 'request'))
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if tuple(map(int, args.host_version.split('.'))) < (5, 3, 0):
            raise Failure('UNSUPPORTED', 'Physics Workflow requires Windows Blender 5.3 or newer')
        tools = PRODUCT_TOOLS
        if not args.blender.is_file() or not (tools / 'physics_workflow_cli_v1/cli.py').is_file():
            raise Failure('NOT_FOUND', 'Selected Blender executable or Physics Workflow release is missing')
        # Both public solver families load the workspace add-on source.
        os.environ['BLENDER_PATH'] = str(args.blender.resolve())
        os.environ['PHYSICS_WORKFLOW_CLI_CORE_ROOT'] = str(ROOT / 'tools/blenderctl')
        os.environ['PHYSICS_WORKFLOW_ADDON_DIR'] = str(tools / 'physics_nodes_workflow_addon')
        # The public worker checks native operation results before publishing a
        # candidate. Physics Nodes itself comes from the host-specific addon.
        sys.path.insert(0, str(ROOT / 'tools'))
        if args.domain == 'physics':
            return invoke(args, args.arguments)
        if len(args.arguments) != 1:
            raise Failure('INVALID_REQUEST', 'Provide one JSON request file')
        with tempfile.TemporaryDirectory(prefix='blenderctl-physics-') as scratch:
            return invoke(args, from_document(args.arguments[0], scratch))
    except (Failure, ValueError, OSError) as exc:
        code = exc.code if isinstance(exc, Failure) else 'INVALID_REQUEST'
        print(json.dumps({'ok': False, 'error': {'code': code, 'message': str(exc)}}))
        return CODES.get(code, 2)


if __name__ == '__main__':
    sys.exit(main())
