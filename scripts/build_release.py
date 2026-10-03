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
    rows = [{'file': name, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)} for name, data in sorted(payloads.items())]
    payloads['release-manifest.json'] = (json.dumps({'cli_version': cli, 'extension_version': version,
        'license': 'GPL-3.0-or-later', 'source_files': len(files), 'extension_files': len(extension_files),
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
