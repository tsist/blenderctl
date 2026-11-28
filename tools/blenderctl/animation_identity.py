"""Fixed animation runtime identity shared by host, worker and verifier.

The add-on is addressed only through the fixed ``tools`` sibling location.
No request field is allowed to select a Python file, add-on directory or
import path.
"""
import hashlib
import json
from pathlib import Path

from protocol import Failure


MODULE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = MODULE_DIR.parent
ROOT = TOOLS_DIR.parent
ADDON_DIR = TOOLS_DIR / 'animation_curves_workflow_addon'

# The public CLI surface plus the animation adapter and independent verifier.
# Hash only the animation-relevant files, not the complete tools tree.
CLI_RUNTIME_FILES = (
    'cli.py',
    'protocol.py',
    'runner.py',
    'worker.py',
    'animation_identity.py',
    'animation_workflow_contract.py',
    'animation_workflow.py',
    'animation_verifier.py',
    'animation_reuse_contract.py',
    'animation_reuse.py',
    'animation_reuse_verifier.py',
)


def _addon_runtime_files():
    if not ADDON_DIR.is_dir():
        raise Failure('NOT_FOUND',
                      'Fixed animation add-on directory is missing: ' + str(ADDON_DIR))
    paths = []
    for path in sorted(ADDON_DIR.rglob('*.py')):
        relative = path.relative_to(ADDON_DIR)
        if any(part in ('__pycache__', '.git', 'tests') for part in relative.parts):
            continue
        paths.append(path)
    if not paths:
        raise Failure('NOT_FOUND',
                      'Fixed animation add-on contains no production Python files: '
                      + str(ADDON_DIR))
    return paths


def runtime_files():
    paths = [MODULE_DIR / name for name in CLI_RUNTIME_FILES] + _addon_runtime_files()
    for path in paths:
        if not path.is_file():
            raise Failure('NOT_FOUND', 'Animation runtime document missing: ' + str(path))
    return paths


def relative_runtime_path(path):
    path = Path(path).resolve()
    try:
        return str(path.relative_to(ROOT)).replace('\\', '/')
    except ValueError:
        return str(path).replace('\\', '/')


def runtime_documents():
    return [{'file': str(path.resolve()),
             'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in runtime_files()]


def implementation_sha256():
    rows = [{**row, 'file': relative_runtime_path(row['file'])}
            for row in runtime_documents()]
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(',', ':')).encode('utf-8')
    ).hexdigest()
