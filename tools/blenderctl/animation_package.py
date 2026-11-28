"""Build the reproducible Animation Curves Workflow production add-on ZIP."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from protocol import CODES, SCHEMA, Failure, ROOT, envelope


ADDON = 'animation_curves_workflow_addon'
VERSION = '0.5.0'
MAX_BYTES = 2_000_000


def _zip_entry(archive, name, data):
    info = ZipInfo(f'{ADDON}/{name}', date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data, compresslevel=9)


def build(output):
    source = ROOT / 'tools' / ADDON
    ui_kit = source / '_ui_kit'
    files = [*source.glob('*.py'), *(source / 'review_core').rglob('*.py'),
             *(source / 'review_core').rglob('*.json'), *source.glob('README-*.md'),
             source / 'support_preset.json', *ui_kit.glob('*.py'),
             *(ui_kit / 'icons').glob('*.png')]
    content = {file.relative_to(source).as_posix(): file.read_bytes() for file in sorted(files)}
    if '__init__.py' not in content or 'production_operations.py' not in content:
        raise Failure('NOT_FOUND', 'The canonical production add-on source is incomplete')
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in content.items()}
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8') + b'\n'
    destination = output.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.animation-package-') as scratch:
        staged = Path(scratch) / 'package.zip'
        with ZipFile(staged, 'x') as archive:
            for name, data in sorted({**content, 'PACKAGE-MANIFEST.json': payload}.items()):
                _zip_entry(archive, name, data)
        size = staged.stat().st_size
        if size >= MAX_BYTES:
            raise Failure('VALIDATION_FAILED', 'Animation add-on ZIP exceeds the 2 MB production limit')
        sha256 = hashlib.sha256(staged.read_bytes()).hexdigest()
        try:
            os.link(staged, destination)
        except FileExistsError as error:
            raise Failure('CONFLICT', f'Choose an unused animation package path: {destination}') from error
    return {'file': str(destination), 'sha256': sha256,
            'bytes': size, 'addon_version': VERSION, 'files': len(content) + 1}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args(argv)
    command = 'animation.package'
    try:
        data = build(args.output)
        result, code = envelope(command, data=data, artifacts={'archive': data['file']}), 0
    except (Failure, OSError) as error:
        reason = error.code if isinstance(error, Failure) else 'IO_ERROR'
        result = envelope(command, error={'code': reason, 'message': str(error)})
        code = CODES[reason]
    result.setdefault('schema_version', SCHEMA)
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == '__main__':
    sys.exit(main())
