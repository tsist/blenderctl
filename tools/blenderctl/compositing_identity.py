"""Pinned local compositing implementation, shared by worker and fresh reader."""
import hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]

def runtime_documents():
    paths=set((ROOT/'tools/blenderctl').glob('*.py'))
    package=ROOT/'tools/compositing_workflow_addon'
    paths.update(package.glob('*.py'))
    if (package/'blender_manifest.toml').is_file():paths.add(package/'blender_manifest.toml')
    return [{'file':str(p.resolve()),'expected_sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(paths)]

def implementation_sha256():
    rows=[{**r,'file':str(Path(r['file']).relative_to(ROOT)).replace('\\','/')} for r in runtime_documents()]
    return hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()
