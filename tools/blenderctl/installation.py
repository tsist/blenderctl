# SPDX-License-Identifier: GPL-3.0-or-later
"""Content identities for the managed runtime, excluding mutable user configuration."""
from pathlib import Path
from protocol import Failure
from filesystem import FileGuard,native_path

def runtime_files(blender):
    root=Path(native_path(Path(blender).resolve().parent))
    files=[p for p in root.iterdir() if p.is_file()]
    for folder in root.iterdir():
        if folder.is_dir() and (folder.name[0:1].isdigit() or folder.name in ('lib','blender.shared','blender.crt')):
            files.extend(p for p in folder.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc')
    for p in files:
        if any(x.is_symlink() or x.is_junction() for x in [p,*p.parents]):raise Failure('UNSUPPORTED','Runtime inventory cannot follow links')
    return sorted(set(files))

def content_inventory(blender,checkpoint):
    root=Path(native_path(Path(blender).resolve().parent));rows={}
    for p in runtime_files(blender):
        checkpoint()
        with FileGuard(p) as g:rows[str(p.relative_to(root))]={'sha256':g.sha256(checkpoint),'bytes':p.stat().st_size}
    return rows
