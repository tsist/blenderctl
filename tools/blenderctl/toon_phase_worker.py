"""Single owned Toon phase; process sequencing belongs to the coordinator."""
from pathlib import Path
import sys
import traceback

PROJECT = next(p for p in Path(__file__).resolve().parents if (p / 'blenderctl.ps1').is_file())
sys.path.insert(0, str(PROJECT / 'tools'))
sys.path.insert(0, str(PROJECT / 'tools/blenderctl'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from protocol import CODES, Failure, atomic_json, digest, read_json
from toon_identity import implementation_sha256
from toon_workflow_addon.contract import WorkflowError
import toon_workflow


def run(request):
    phase = request['phase']
    if phase not in ('produce', 'render'):
        raise RuntimeError('Unknown Toon worker phase')
    pin = request['implementation_sha256']
    if implementation_sha256() != pin or request['params'].get('implementation_sha256') != pin:
        raise RuntimeError('Phase worker implementation changed')
    expected = request['execution_context']
    if (type(expected) is not dict or set(expected) != {'cpu_threads'} or
            type(expected['cpu_threads']) is not int or expected['cpu_threads'] <= 0):
        raise RuntimeError('Phase worker requires the original explicit CPU context')
    actual = toon_workflow.execution_context()
    if actual != expected:
        raise RuntimeError('Phase worker CPU context differs')
    if phase == 'produce':
        result = toon_workflow.produce(request['params'], request['job'])
    else:
        result = toon_workflow.render_candidate(request['params'], request['job'], request['report'])
    if type(result) is not dict:
        raise RuntimeError('Phase worker result must be an object')
    return {'status': 'passed', 'phase': phase, 'implementation_sha256': pin,
            'execution_context': actual, 'result': result}


def _failure_receipt(request, exc):
    known = isinstance(exc, (Failure, WorkflowError))
    code = getattr(exc, 'code', None) if known else None
    public = code if type(code) is str and code in CODES else 'WORKER_FAILED'
    domain = getattr(exc, 'domain_code', code) if known else 'WORKER_FAILED'
    path = getattr(exc, 'domain_path', getattr(exc, 'path', '$')) if known else '$'
    detail = {'public_code': public,
              'domain_code': domain if type(domain) is str and domain else 'WORKER_FAILED',
              'path': path if type(path) is str and path else '$',
              'message': str(exc) or type(exc).__name__, 'type': type(exc).__name__}
    return {'schema': 'toon-phase-failure', 'version': '1.0',
            'status': 'failed', 'phase': request.get('phase'),
            'implementation_sha256': request.get('implementation_sha256'),
            'execution_context': request.get('execution_context'),
            'error_detail': detail, 'error': str(exc), 'traceback': traceback.format_exc()}


if __name__ == '__main__':
    arguments = sys.argv[sys.argv.index('--') + 1:]
    if len(arguments) != 2:
        raise Failure('CONFLICT', 'Phase worker requires request path and independently pinned SHA')
    request_path, expected_sha256 = arguments
    if digest(request_path) != expected_sha256:
        raise Failure('CONFLICT', 'Phase request SHA differs before JSON consumption')
    request = read_json(request_path)
    try:
        atomic_json(request['output'], run(request))
    except Exception as exc:
        atomic_json(request['output'], _failure_receipt(request, exc))
        raise
