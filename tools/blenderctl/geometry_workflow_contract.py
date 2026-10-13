"""Host adapter for the geometry add-on's data-only domain contract."""
import hashlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from protocol import Failure
from protocol import read_json, digest
from geometry_nodes_workflow_addon import contract
from geometry_identity import implementation_sha256, runtime_documents


def normalize_params(value):
    supplied = dict(value) if isinstance(value, dict) else value
    identity = supplied.pop('implementation_sha256', None) if isinstance(supplied, dict) else None
    try:
        result = contract.normalize_params(supplied)
    except contract.WorkflowError as exc:
        raise Failure(exc.code, str(exc)) from exc
    current = implementation_sha256()
    if identity is not None and identity != current:
        raise Failure('CONFLICT', 'Geometry implementation changed since request normalization')
    result['implementation_sha256'] = current
    return result


def normalize_inspect_params(value):
    supplied = dict(value) if isinstance(value, dict) else value
    identity = supplied.pop('implementation_sha256', None) if isinstance(supplied, dict) else None
    try:
        result = contract.normalize_inspect_params(supplied)
    except contract.WorkflowError as exc:
        raise Failure(exc.code, str(exc)) from exc
    current = implementation_sha256()
    if identity is not None and identity != current:
        raise Failure('CONFLICT', 'Geometry implementation changed')
    result['implementation_sha256'] = current
    return result


def input_documents(params):
    documents = list(params.get('resources', [])) + runtime_documents()
    if params.get('cache_manifest'):
        # Cache members are first-class inputs, rather than implicit Blender
        # directories. Resolve only after validating the immutable descriptor.
        from geometry_temporal import validate_cache
        cache = validate_cache(params['cache_manifest'],
            candidate={'file': params['file'], 'sha256': params['expected_sha256']},
            identity=params.get('implementation_sha256'))
        documents.append(params['cache_manifest'])
        root = Path(cache['root'])
        documents.extend({'file': str(root / row['path']), 'expected_sha256': row['sha256']}
                         for row in cache['files'])
    if params.get('resume'):
        documents.append(params['resume'])
        if digest(params['resume']['file']) != params['resume']['expected_sha256']:
            raise Failure('CONFLICT','Geometry resume document SHA mismatch')
        resume = read_json(params['resume']['file'])
        checkpoint = resume.get('checkpoint')
        if checkpoint:
            if not isinstance(checkpoint,dict) or set(checkpoint)!={'file','expected_sha256'}:
                raise Failure('INVALID_REQUEST','Invalid geometry resume checkpoint descriptor')
            if not Path(checkpoint['file']).is_absolute():raise Failure('INVALID_REQUEST','Absolute checkpoint path required')
            documents.append(checkpoint)
    return documents
