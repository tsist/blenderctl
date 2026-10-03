# SPDX-License-Identifier: GPL-3.0-or-later
"""Build deterministic source/Blender extension ZIPs and SHA-256 release metadata.

Run from a clean, reviewed checkout. --verify compares rebuilt bytes to existing
artifacts without replacing them. Source releases can also rebuild without Git.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import tomllib
import zipfile
from check_release import ROOT, public_files, audit


def archive(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, content in sorted(entries):
            item = zipfile.ZipInfo(name, date_time=(2026, 10, 4, 0, 0, 0))
            item.create_system = 3
            item.external_attr = 0o100644 << 16
            item.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(item, content, compresslevel=9)
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('dist'))
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--skills-only', action='store_true', help='build the standalone skills companion distribution')
    args = parser.parse_args()
    files = public_files()
    errors = audit(files)
    if errors:
        raise SystemExit('\n'.join(errors))
    cli = re.search(r'^VERSION = "([0-9.]+)"', (ROOT / 'tools/blenderctl/protocol.py').read_text(), re.M).group(1)
    addon = ROOT / 'tools/material_workflow_addon'
    manifest = tomllib.loads((addon / 'blender_manifest.toml').read_text())
    version = manifest['version']
    init = (addon / '__init__.py').read_text()
    if tuple(int(n) for n in version.split('.')) != tuple(int(n) for n in re.search(r'"version": \(([^)]+)\)', init).group(1).split(',')):
        raise SystemExit('Extension manifest and bl_info versions differ')
    extension_files = [p for p in files if p.parent == addon and
                       (p.suffix == '.py' or p.name in {'blender_manifest.toml', 'LICENSE', 'README.md'}) and
                       not p.name.startswith('test')]
    if not {addon / name for name in ('__init__.py', 'blender_manifest.toml', 'LICENSE')} <= set(extension_files):
        raise SystemExit('Incomplete extension source')
    payloads = {
        f'blenderctl-{cli}-source.zip': archive([(f'blenderctl-{cli}/' + p.relative_to(ROOT).as_posix(), p.read_bytes()) for p in files]),
        f'material-workflow-{version}.zip': archive([(p.name, p.read_bytes()) for p in extension_files]),
    }
    skill_manifest = ROOT / 'skills/manifest.json'
    skill_version = None
    skill_file_count = 0
    if skill_manifest.is_file():
        from check_skills import check
        skill_check = check()
        if not skill_check['ok']:
            raise SystemExit('\n'.join(skill_check['errors']))
        skill_version = json.loads(skill_manifest.read_text(encoding='utf-8'))['bundle_version']
        skill_entries = [('blender-skills-' + skill_version + '/' + p.relative_to(ROOT).as_posix(), p.read_bytes())
                         for p in files if p.is_relative_to(ROOT / 'skills')]
        skill_entries.append(('blender-skills-' + skill_version + '/install_skills.py',
                              (ROOT / 'scripts/install_skills.py').read_bytes()))
        skill_file_count = len(skill_entries)
        if args.skills_only:
            payloads = {}
        payloads[f'blender-skills-{skill_version}.zip'] = archive(skill_entries)
    elif args.skills_only:
        raise SystemExit('No skill bundle manifest is present')
    rows = [{'file': name, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)} for name, data in sorted(payloads.items())]
    payloads['release-manifest.json'] = (json.dumps({'cli_version': cli, 'extension_version': version,
        'license': 'GPL-3.0-or-later', 'skills_bundle_version': skill_version,
        'distribution': 'skills-only' if args.skills_only else 'source-extension-skills',
        'audited_source_files': len(files), 'skills_files': skill_file_count,
        'source_files': None if args.skills_only else len(files),
        'extension_files': None if args.skills_only else len(extension_files),
        'reproducible_zip_timestamp': '2026-10-04T00:00:00', 'artifacts': rows}, indent=2) + '\n').encode()
    payloads['SHA256SUMS.txt'] = ''.join(hashlib.sha256(data).hexdigest() + '  ' + name + '\n' for name, data in sorted(payloads.items())).encode()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name, data in payloads.items():
        path = output / name
        if args.verify:
            if not path.is_file() or path.read_bytes() != data:
                raise SystemExit('Reproducibility check failed: ' + name)
        else:
            with path.open('xb') as stream:
                stream.write(data)
    print(json.dumps({'ok': True, 'mode': 'verify' if args.verify else 'build', 'output': str(output), 'artifacts': rows}, indent=2))


if __name__ == '__main__':
    main()
