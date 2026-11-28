"""Public CLI for protected animation production operations."""

import argparse
import json
from pathlib import Path

import runner
from protocol import CODES, DEFAULT_BLENDER, DEFAULT_JOBS, DEFAULT_TRANSACTIONS, SCHEMA, Failure, envelope, read_json


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure('INVALID_REQUEST', message)


def main(argv=None):
    parser = Parser(description=__doc__, prog='blenderctl animation')
    parser.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    parser.add_argument('--jobs-dir', type=Path, default=DEFAULT_JOBS)
    parser.add_argument('--transactions-dir', type=Path, default=DEFAULT_TRANSACTIONS)
    parser.add_argument('--timeout', type=int, default=120)
    parser.add_argument('--compact', action='store_true')
    parser.add_argument('--async', dest='background', action='store_true')
    actions = parser.add_subparsers(dest='operation', required=True)
    production = actions.add_parser('production', help='apply slot, correction, rotation, graph or pose tools')
    production.add_argument('--file', required=True, type=Path)
    production.add_argument('--expected-sha256', required=True)
    production.add_argument('--manifest', required=True, type=Path, help='animation operation JSON')
    production.add_argument('--release', default='workspace', help='workspace source or a pinned production release')
    production.add_argument('--context', type=Path, help='optional explicit scene and view-layer JSON')
    production.add_argument('--resources', type=Path, help='optional file and SHA-256 descriptor list')

    command = 'production.run'
    args = None
    try:
        args = parser.parse_args(argv)
        params = {
            'file': str(args.file.resolve()),
            'expected_sha256': args.expected_sha256,
            'domain': 'animation',
            'release': args.release,
            'request': read_json(args.manifest),
        }
        if args.context:
            params['context'] = read_json(args.context)
        if args.resources:
            params['resources'] = read_json(args.resources)
        job = runner.prepare({'schema_version': SCHEMA, 'command': command, 'params': params},
                             args.blender, args.jobs_dir, args.timeout, args.transactions_dir)
        if args.background:
            result, code = runner.submit(job), 0
        else:
            result, code = runner.execute(job)
    except (Failure, OSError) as error:
        failure_code = error.code if isinstance(error, Failure) else 'IO_ERROR'
        result = envelope(command, error={'code': failure_code, 'message': str(error)})
        code = CODES[failure_code]
    result.setdefault('schema_version', SCHEMA)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False,
                     separators=(',', ':') if args and args.compact else None))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
