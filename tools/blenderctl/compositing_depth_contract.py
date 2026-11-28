"""Public depth contract bridge; no bpy, numeric runtime, IO or producer launch."""
from pathlib import Path
import hashlib
import json
import types

CONTRACT_PATH = Path(__file__).resolve().parents[1] / 'compositing_workflow_addon/depth_contract.py'
_module = types.ModuleType('compositing_depth_pure_contract')
_module.__file__ = str(CONTRACT_PATH)
_source_bytes = CONTRACT_PATH.read_bytes()
_source_sha256 = hashlib.sha256(_source_bytes).hexdigest()
# Loading exact source avoids executing the addon's bpy-dependent __init__ or stale pyc.
exec(compile(_source_bytes, str(CONTRACT_PATH), 'exec'), _module.__dict__)
DepthError = _module.DepthError
loads_strict = _module.loads_strict
input_descriptors = _module.input_descriptors


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def normalize_params(value, operation):
    _check_loaded_source()
    op = operation.removeprefix('compositing.')
    if op == 'depth-prepare':
        return _module.normalize_prepare(value)
    if op == 'depth-inspect':
        return _module.normalize_inspect(value)
    raise DepthError('INVALID_REQUEST', 'Unknown depth operation')


def params_schema(operation):
    _check_loaded_source()
    import copy
    op = operation.removeprefix('compositing.')
    if op not in ('depth-prepare', 'depth-inspect'):
        raise DepthError('INVALID_REQUEST', 'Unknown depth operation')
    return copy.deepcopy(_module.PREPARE_SCHEMA if op == 'depth-prepare' else _module.INSPECT_SCHEMA)


def implementation_identity():
    _check_loaded_source()
    return {'contract_file': str(CONTRACT_PATH), 'contract_sha256': _source_sha256,
            'schema_signature': signature({'prepare': _module.PREPARE_SCHEMA, 'inspect': _module.INSPECT_SCHEMA})}


def _check_loaded_source():
    if hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest() != _source_sha256:
        raise DepthError('CONFLICT', 'Loaded pure contract source changed; restart with the new identity')
