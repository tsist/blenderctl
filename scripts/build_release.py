"""Deterministic source, extension, skill and template archives from PUBLICATION.json."""
import argparse
import io
import json
from pathlib import Path
import sys
import zipfile
sys.dont_write_bytecode = True
from verify_release import ROOT, ADDON, load_publication, release_files, safe_file, sha, verify

def archive(entries):
    if len(entries) != len({name for name, _ in entries}):
        raise ValueError('Duplicate archive member')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_STORED) as output:
        for name, data in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_STORED
            output.writestr(info, data)
    return buffer.getvalue()

def build(out):
    checked = verify()
    p = load_publication()
    source = [(name, safe_file(ROOT, name).read_bytes()) for name in release_files(p)]
    license_data = safe_file(ROOT, 'LICENSE').read_bytes()
    skill = [(n[len('skills/'):], b) for n, b in source if n.startswith('skills/blender-video-editing/')]
    skill.append(('LICENSE', license_data))
    template = [(n[len('templates/'):], b) for n, b in source if n.startswith('templates/video/')]
    template.append(('LICENSE', license_data))
    bundles = {p['repository'] + '-' + p['release'] + '-source.zip': source,
               'video_editing_workflow_addon-' + p['addon'] + '.zip': [(n[len(ADDON):], b) for n, b in source if n.startswith(ADDON)],
               'blender-video-editing-skill-' + p['distribution'] + '.zip': skill,
               'blender-video-templates-' + p['template_library'] + '.zip': template}
    if p['repository'] == 'blenderctl':
        all_skills = [(n, b) for n, b in source if n.startswith('skills/')]
        all_skills.append(('install_skills.py', safe_file(ROOT, 'scripts/install_skills.py').read_bytes()))
        if not any(n == 'LICENSE' for n, _ in all_skills):
            all_skills.append(('LICENSE', license_data))
        bundles['blenderctl-skills-' + p['skill_bundle'] + '.zip'] = all_skills
    if out.is_symlink():
        raise ValueError('Refusing symlink output')
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, entries in bundles.items():
        data = archive(entries)
        if data != archive(entries):
            raise ValueError('Reproducibility failure: ' + name)
        path = out / name
        if path.exists() and path.read_bytes() != data:
            raise ValueError('Refusing to overwrite differing artifact: ' + name)
        if not path.exists():
            path.write_bytes(data)
        with zipfile.ZipFile(path) as z:
            if z.namelist() != sorted(n for n, _ in entries) or any(z.read(n) != b for n, b in entries):
                raise ValueError('Archive member mismatch: ' + name)
        rows.append({'file': name, 'sha256': sha(data), 'bytes': len(data), 'entries': len(entries)})
    sums = ''.join(row['sha256'] + '  ' + row['file'] + '\n' for row in sorted(rows, key=lambda row: row['file'])).encode()
    checksum = out / 'SHA256SUMS'
    if checksum.exists() and checksum.read_bytes() != sums:
        raise ValueError('Refusing to overwrite differing SHA256SUMS')
    if not checksum.exists():
        checksum.write_bytes(sums)
    return {'status': 'passed', 'validation': checked, 'artifacts': rows}

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    parser.add_argument('--verify', action='store_true', help='Accepted for older CI; every build verifies bytes')
    args = parser.parse_args()
    print(json.dumps(build(args.output.resolve()), indent=2))
