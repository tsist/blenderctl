"""Exact local implementation inputs shared by modeling jobs and readers."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def runtime_documents():
    paths = list((ROOT / 'tools/blenderctl').glob('*.py'))
    paths += list((ROOT / 'tools/modeling_workflow_addon').rglob('*.py'))
    return [{'file': str(p.resolve()), 'expected_sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in sorted(set(paths))]


def implementation_sha256():
    rows = [{**row, 'file': str(Path(row['file']).relative_to(ROOT)).replace('\\', '/')}
            for row in runtime_documents()]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
