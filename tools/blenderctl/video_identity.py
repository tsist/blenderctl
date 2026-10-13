"""Pinned development implementation shared by Host and fresh VSE reader."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def runtime_documents():
    paths = set((ROOT / 'tools/blenderctl').glob('*.py'))
    package = ROOT / 'tools/video_editing_workflow_addon'
    paths.update(package.glob('*.py'))
    if (package / 'blender_manifest.toml').is_file():
        paths.add(package / 'blender_manifest.toml')
    return [{'file': str(path.resolve()), 'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(paths)]


def implementation_sha256():
    rows = [{**row, 'file': Path(row['file']).relative_to(ROOT).as_posix()}
            for row in runtime_documents()]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
