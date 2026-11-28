"""Public estimate contract bridge, exact source loading without bpy or third parties."""
from pathlib import Path
import hashlib
import json
import types

SOURCE = Path(__file__).resolve().parents[1] / 'compositing_workflow_addon/depth_estimate_contract.py'
_bytes = SOURCE.read_bytes()
_sha = hashlib.sha256(_bytes).hexdigest()
_module = types.ModuleType('public_depth_estimate_pure')
_module.__file__ = str(SOURCE)
exec(compile(_bytes, str(SOURCE), 'exec'), _module.__dict__)
_base_sha = hashlib.sha256(_module._base_source).hexdigest()
DepthError = _module.DepthError
loads_strict = _module.loads_strict
input_descriptors = _module.input_descriptors
expected_tensor_shape = _module.expected_tensor_shape


def signature(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode('utf-8')).hexdigest()


def _check():
    if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != _sha or hashlib.sha256(SOURCE.with_name('depth_contract.py').read_bytes()).hexdigest() != _base_sha:
        raise DepthError('CONFLICT', 'Loaded estimate source changed')


def normalize_params(value):
    _check()
    return _module.normalize_estimate(value)


def params_schema():
    import copy
    _check()
    return copy.deepcopy(_module.PARAMS_SCHEMA)
