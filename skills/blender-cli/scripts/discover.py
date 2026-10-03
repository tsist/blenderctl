# SPDX-License-Identifier: GPL-3.0-or-later
"""Read public CLI registrations and schemas, without Blender or jobs."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.dont_write_bytecode = True

def valid_root(path):
    return (path / 'tools/blenderctl/protocol.py').is_file() and (path / 'docs/cli/schemas/request.schema.json').is_file()

def find_root(explicit=None):
    selected = explicit or os.environ.get('BLENDERCTL_ROOT')
    if selected:
        root = Path(selected).expanduser().resolve()
        if not valid_root(root): raise ValueError('Selected root lacks public CLI sources and schemas: ' + str(root))
        return root
    for start in (Path(__file__).resolve().parent, Path.cwd().resolve()):
        for candidate in (start, *start.parents):
            if valid_root(candidate): return candidate
    raise ValueError('Cannot locate public checkout; specify --root or BLENDERCTL_ROOT')

def read(path): return json.loads(path.read_text(encoding='utf-8-sig'))

def describe(root, command=None, query=None, limit=8, include_schema=False):
    sys.path.insert(0, str(root / 'tools/blenderctl'))
    from protocol import COMMANDS, VERSION
    from execution_contract import capabilities
    from material_interface import OPERATIONS
    disposition = capabilities()
    allowed = set(disposition['pipeline_commands'])
    exclusions = {row['command']: row for row in disposition['pipeline_exclusions']}
    if command and command not in COMMANDS: raise ValueError('Unknown dotted command: ' + command)
    if include_schema and not command: raise ValueError('--schema requires --command')
    public = read(root / 'docs/cli/schemas/request.schema.json')
    rows = []
    for name in COMMANDS:
        if command and name != command: continue
        row = {'command': name, 'classification': 'pipeline' if name in allowed else exclusions[name]['classification'],
               'pipeline_allowed': name in allowed,
               'reason': 'Registered replayable DAG entry.' if name in allowed else exclusions[name]['reason']}
        material = OPERATIONS.get(name.removeprefix('material.')) if name.startswith('material.') else None
        if material:
            row['purpose'] = material[3]
            row['schemas'] = {kind: str(root / 'docs/cli/schemas' / filename)
                              for kind, filename in zip(('request', 'manifest', 'report'), material[:3]) if filename}
        else: row['schemas'] = {'request': str(root / 'docs/cli/schemas/request.schema.json')}
        if query and not all(word in json.dumps(row, ensure_ascii=False).casefold() for word in query.casefold().split()): continue
        rows.append(row)
    result = {'ok': True, 'root': str(root), 'cli_version': VERSION,
              'counts': {'request': len(COMMANDS), 'pipeline': len(allowed), 'standalone': len(COMMANDS)-len(allowed)},
              'matched': len(rows), 'truncated': len(rows) > limit, 'items': rows[:limit],
              'meaning': 'Source registration and routing only; not functional, platform or visual acceptance.'}
    if include_schema:
        material = OPERATIONS.get(command.removeprefix('material.')) if command.startswith('material.') else None
        if material and material[0]: result['request_schema'] = read(root / 'docs/cli/schemas' / material[0])
        else:
            result['request_branches'] = [row for row in public.get('allOf', [])
                if row.get('if', {}).get('properties', {}).get('command', {}).get('const') == command
                or command in row.get('if', {}).get('properties', {}).get('command', {}).get('enum', [])]
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--command')
    parser.add_argument('--query')
    parser.add_argument('--limit', type=int, default=8)
    parser.add_argument('--schema', action='store_true')
    args = parser.parse_args()
    try:
        if not 1 <= args.limit <= 128: raise ValueError('--limit must be 1..128')
        result = describe(find_root(args.root), args.command, args.query, args.limit, args.schema)
    except (OSError, ValueError, KeyError, ImportError, RuntimeError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=True)); return 2
    print(json.dumps(result, ensure_ascii=True, indent=2)); return 0

if __name__ == '__main__': raise SystemExit(main())
