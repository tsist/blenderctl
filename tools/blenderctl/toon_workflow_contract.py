"""Strict Toon host adapter. Explicit pins never migrate to current bytes."""
from pathlib import Path
import sys

from toon_identity import ROOT, implementation_sha256, runtime_documents

sys.path.insert(0, str(ROOT / 'tools/blenderctl'))
sys.path.insert(0, str(ROOT / 'tools'))
from protocol import CODES, Failure
from toon_workflow_addon import contract


def _domain_error(exc):
    public = exc.code if exc.code in CODES else 'INVALID_REQUEST'
    error = Failure(public, exc.code + ' at ' + exc.path + ': ' + str(exc))
    error.domain_code = exc.code
    error.domain_path = exc.path
    raise error from exc


def _identity(params):
    if params['implementation_sha256'] != implementation_sha256():
        raise Failure('CONFLICT', 'Toon implementation changed; preserve old pin and create a new reviewed request')


def normalize_params(value):
    try:
        result = contract.normalize_params(value)
    except contract.WorkflowError as exc:
        _domain_error(exc)
    _identity(result)
    return result


def normalize_inspect_params(value):
    try:
        result = contract.normalize_inspect_params(value)
    except contract.WorkflowError as exc:
        _domain_error(exc)
    _identity(result)
    return result


def normalize_request(value):
    try:
        result = contract.normalize_request(value)
    except contract.WorkflowError as exc:
        _domain_error(exc)
    _identity(result['params'])
    return result


def anchor_documents(params):
    """Runner must guard these bytes before any state/member interpretation."""
    return [dict(params['baseline'])] if 'baseline' in params else []


def input_documents(params):
    """Only explicit inputs; state parsing and content closure remain runtime work.

    Baseline contents are deliberately not opened here before the supervisor
    owns its FileGuard. The worker validates state against the protected source.
    """
    _identity(params)
    documents = [{'file': params['file'], 'expected_sha256': params['expected_sha256']}]
    documents += [dict(row) for row in params['resources']]
    documents += anchor_documents(params) + runtime_documents()
    by_path = {}
    for row in documents:
        key = str(Path(row['file']).resolve()).casefold()
        if key in by_path and row['expected_sha256'] != by_path[key]['expected_sha256']:
            raise Failure('CONFLICT', 'Toon input has conflicting hashes: ' + row['file'])
        by_path[key] = dict(row)
    return list(by_path.values())
