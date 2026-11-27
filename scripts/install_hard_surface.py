# SPDX-License-Identifier: GPL-3.0-or-later
"""Install the verified Hard Surface Workflow build into this CLI checkout."""
import argparse
import hashlib
import json
from pathlib import Path
import uuid
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs" / "hard-surface-package.json"
RUNTIME = ROOT / "runtime" / "blender53-production"
MAX_UNPACKED = 8 * 1024 * 1024


def install(archive_path):
    manifest_bytes = MANIFEST.read_bytes()
    manifest = json.loads(manifest_bytes)
    expected = manifest["products"]["hard-surface"]["files"]
    with ZipFile(archive_path) as archive:
        members = [member for member in archive.infolist() if not member.is_dir()]
        names = [member.filename for member in members]
        if (len(names) != len(expected) or set(names) != set(expected)
                or sum(member.file_size for member in members) > MAX_UNPACKED):
            raise ValueError("The ZIP member inventory does not match the qualified package")
        files = {name: archive.read(name) for name in names}
    if any(hashlib.sha256(data).hexdigest() != expected[name] for name, data in files.items()):
        raise ValueError("A ZIP member differs from the qualified package")
    release = RUNTIME / manifest["release"]
    if not release.resolve().is_relative_to(ROOT):
        raise ValueError("The production runtime path leaves this checkout")
    if release.exists():
        package = release / "packages" / "hard_surface_workflow_addon"
        same_manifest = (release / "manifest.json").is_file() and (release / "manifest.json").read_bytes() == manifest_bytes
        same_files = all((package / name).is_file() and (package / name).read_bytes() == data
                         for name, data in files.items())
        actual = {file.relative_to(package).as_posix() for file in package.rglob("*") if file.is_file()
                  and "__pycache__" not in file.parts}
        if same_manifest and same_files and actual == set(expected):
            return release
        raise FileExistsError(f"Runtime already exists with different contents: {release}")
    stage = RUNTIME / (".hard-surface-" + uuid.uuid4().hex)
    package = stage / "packages" / "hard_surface_workflow_addon"
    package.mkdir(parents=True)
    for name, data in files.items():
        destination = package / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    (stage / "manifest.json").write_bytes(manifest_bytes)
    stage.rename(release)
    return release


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("extension_zip", type=Path, help="Qualified locally built 0.3.7 extension archive")
    args = parser.parse_args()
    print(install(args.extension_zip))
