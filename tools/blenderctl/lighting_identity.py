"""Immutable implementation inputs for lighting workers and independent readers."""
import hashlib, json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
def runtime_documents():
    paths=sorted(set((ROOT/'tools/blenderctl').glob('*.py'))|set((ROOT/'tools/lighting_workflow_addon').glob('*.py')))
    return [{'file':str(p.resolve()),'expected_sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]
def implementation_sha256():
    rows=[{**r,'file':str(Path(r['file']).relative_to(ROOT)).replace('\\','/')} for r in runtime_documents()]
    return hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()
