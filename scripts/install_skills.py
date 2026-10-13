# SPDX-License-Identifier: GPL-3.0-or-later
"""Install the manifest-listed companion skill folders into an explicit destination.

No downloads, dependency installations or existing skill replacement. Works
from the repository or the standalone skill ZIP; --list is read-only.
"""
import argparse
import json
from pathlib import Path
import re
import shutil
import sys

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / 'skills' if (HERE.parent / 'skills/manifest.json').is_file() else HERE / 'skills'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--list', action='store_true')
    args = parser.parse_args()
    manifest = json.loads((SOURCE / 'manifest.json').read_text(encoding='utf-8'))
    names = [entry['name'] for entry in manifest['skills']]
    if len(set(names)) != len(names) or any(not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) for name in names):
        raise RuntimeError('Invalid or duplicate skill directory name')
    if args.list:
        print(json.dumps({'bundle_version': manifest['bundle_version'], 'skills': names, 'mutations': 0}, indent=2))
        return 0
    if not args.destination:
        parser.error('--destination is required; existing skills are never overwritten')
    destination = args.destination.expanduser().resolve()
    collisions = [name for name in names if (destination / name).exists() or (destination / name).is_symlink()]
    if collisions:
        print(json.dumps({'ok': False, 'error': 'Existing skills; no files were copied', 'skills': collisions, 'mutations': 0}, indent=2))
        return 2
    for name in names:
        if not (SOURCE / name / 'SKILL.md').is_file():
            raise RuntimeError('Incomplete source skill: ' + name)
        if any(p.is_symlink() or p.is_junction() for p in [SOURCE / name, *(SOURCE / name).rglob('*')]):
            raise RuntimeError('Skill sources must not contain symlinks/junctions')
    destination.mkdir(parents=True, exist_ok=True)
    installed = []
    try:
        for name in names:
            shutil.copytree(SOURCE / name, destination / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            installed.append(name)
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc), 'installed_before_error': installed,
                          'destination': str(destination), 'existing_files_replaced': False}, indent=2))
        return 1
    print(json.dumps({'ok': True, 'bundle_version': manifest['bundle_version'], 'destination': str(destination),
                      'installed': installed, 'existing_files_replaced': False}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
