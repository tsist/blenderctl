"""Local runtime identity shared by the geometry worker and live-session bridge."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def runtime_documents():
    paths = sorted(set((ROOT / 'tools/blenderctl').glob('*.py')) |
                   set((ROOT / 'tools/geometry_nodes_workflow_addon').glob('*.py')))
    return [{'file': str(path.resolve()),
             'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in paths if path.is_file()]


def implementation_sha256():
    rows = [{**row, 'file': str(Path(row['file']).relative_to(ROOT)).replace('\\', '/')}
            for row in runtime_documents()]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
