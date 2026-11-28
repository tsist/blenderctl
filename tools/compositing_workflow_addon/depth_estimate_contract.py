"""Pure fixed-profile DAV2 RGB estimate contract. No data/model/scene IO.

Only code bootstrap reads sibling depth_contract.py, compiled from exact source
bytes rather than importing the bpy-dependent addon package or stale bytecode.
Host/child must guard that code identity; normalizers never read input files.
"""
import copy
import math
from pathlib import Path
import types

_base_path = Path(__file__).with_name('depth_contract.py')
_base = types.ModuleType('owned_depth_estimate_base_contract')
_base.__file__ = str(_base_path)
_base_source = _base_path.read_bytes()
exec(compile(_base_source, str(_base_path), 'exec'), _base.__dict__)
DepthError = _base.DepthError
loads_strict = _base.loads_strict
FILE = copy.deepcopy(_base.FILE)

# Verified official local source, not an inferred preprocessing approximation:
# depth_anything_v2/util/transform.py:51-105 (Resize.get_size and constrain),
# SHA a97df1dbb687457a40c387fdc3482edee7ddc5c142c7d2735ca6235a8ae2885f;
# depth_anything_v2/dpt.py:196-218 (image2tensor fixed Resize configuration),
# SHA 7cc9a29b41d31e1628e5c8b8705e25ae95bc6490f779dbb695297ce2341566f5.
# Python round(float) and NumPy round(float, decimals=0) both use ties-to-even.
# Keep the same operation order as get_size; do not use an algebraic ratio
# simplification which can change floating half-way rounding.
PREPROCESSING_SOURCE = {
    'profile': 'official_dav2_input518_lower_bound_multiple14',
    'transform_sha256': 'a97df1dbb687457a40c387fdc3482edee7ddc5c142c7d2735ca6235a8ae2885f',
    'dpt_sha256': '7cc9a29b41d31e1628e5c8b8705e25ae95bc6490f779dbb695297ce2341566f5',
    'input_size': 518, 'ensure_multiple_of': 14,
    'resize_method': 'lower_bound', 'keep_aspect_ratio': True,
    'shape_only': True,
}

DOMAIN = copy.deepcopy(_base.PREPARE_SCHEMA['properties']['domain'])
for _dimension in ('width', 'height'):
    DOMAIN['properties'][_dimension]['minimum'] = 64

DERIVED = _base._obj({
    'validity': copy.deepcopy(_base.PREPARE_SCHEMA['properties']['validity']),
    'normalization': copy.deepcopy(_base.PERCENTILE),
    'edits': copy.deepcopy(_base.PREPARE_SCHEMA['properties']['edits']),
    'normal': {'oneOf': [copy.deepcopy(_base.NONE), copy.deepcopy(_base.HEIGHTFIELD)]},
    'budget': copy.deepcopy(_base.PREPARE_SCHEMA['properties']['budget']),
})
DERIVED['allOf'] = [{
    'if': {'properties': {'validity': {'properties': {'kind': _base._const('all_frame_artistic_declared')}}}},
    'then': {'properties': {'normal': copy.deepcopy(_base.HEIGHTFIELD)}},
}]
PARAMS_SCHEMA = _base._obj({
    'depth_estimate_version': _base._const('1.0'),
    'rgb': FILE, 'rgb_encoding': _base._const('display_sRGB_PNG8'),
    'alpha_policy': _base._const('require_opaque'),
    'domain': DOMAIN,
    'input_provenance': _base._obj({'kind': _base._const('declared_input'), 'statement': copy.deepcopy(_base.STATEMENT)}),
    'runtime_profile': copy.deepcopy(FILE),
    'derived': DERIVED,
    'budget': _base._obj({
        'max_pixels': {'type': 'integer', 'minimum': 1, 'maximum': 4194304},
        'max_tensor_pixels': {'type': 'integer', 'minimum': 1, 'maximum': 1048576},
        'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 1200},
    }),
    'resources': copy.deepcopy(_base.RESOURCES),
})
PARAMS_SCHEMA['$schema'] = 'http://json-schema.org/draft-07/schema#'
PARAMS_SCHEMA['title'] = 'compositing.depth-estimate params v1.0'
PARAMS_SCHEMA['$comment'] = (
    'Pure fixed-profile contract; normalizer also checks direct resource coverage, '
    'derived relative-prepare combinations and actual expected input/tensor budgets. '
    'Host validates PNG/RGBA opacity and the approved profile authority; no model '
    'completion, payload/profile expansion, geometric validity or cache hit is '
    'certified by this schema. max_tensor_pixels counts H*W, not batch/channel count.')


def expected_tensor_shape(domain):
    """Official Resize shape only, [1,3,H,W]; never resize/crop/decode an image."""
    _base._finite_tree(domain)
    _base._validate(domain, DOMAIN)
    width, height = domain['width'], domain['height']
    scale_height = 518 / height
    scale_width = 518 / width
    if scale_width > scale_height:
        scale_height = scale_width
    else:
        scale_width = scale_height
    def constrain(value):
        rounded = round(value / 14) * 14
        if rounded < 518:
            rounded = math.ceil(value / 14) * 14
        return rounded
    return [1, 3, constrain(scale_height * height), constrain(scale_width * width)]


def _validate_derived_prepare(value):
    """Build a structural prepare probe; no recipe or fictitious receipt returned.

    Generated raw does not exist yet. A collision-free hypothetical FILE checks
    the existing prepare combinator without inventing input resources or opening
    paths. Real estimate input resources are checked separately below.
    """
    direct = _base._merge(_base._collect(value))
    suffix = 0
    while True:
        name = 'F:/__depth_estimate_contract_probe__/pending-raw-' + str(suffix) + '.npy'
        if _base._path_key(name) not in direct:
            break
        suffix += 1
    probe = {
        'depth_prepare_version': '1.0', 'rgb': copy.deepcopy(value['rgb']),
        'rgb_encoding': value['rgb_encoding'],
        'depth': {'file': name, 'expected_sha256': '0' * 64, 'format': 'NPY', 'channel': None},
        'domain': copy.deepcopy(value['domain']),
        'semantics': {'measure': 'relative_score', 'near_order': 'larger_nearer', 'units': 'unitless'},
        'provenance': {'kind': 'declared_external', 'statement': 'Pure contract combination probe only; no producer exists'},
        **copy.deepcopy(value['derived']),
    }
    probe['resources'] = list(_base._merge(_base._collect(probe)).values())
    _base.normalize_prepare(probe)


def normalize_estimate(value):
    """Return exact caller fields copied; only pure structural/semantic validation."""
    _base._finite_tree(value)
    _base._validate(value, PARAMS_SCHEMA)
    _base._resource_coverage(value)
    _validate_derived_prepare(value)
    pixels = value['domain']['width'] * value['domain']['height']
    _base._need(pixels <= value['budget']['max_pixels'], 'Estimate original-image pixel budget exceeded')
    _base._need(pixels <= value['derived']['budget']['max_pixels'], 'Derived original-image pixel budget exceeded')
    shape = expected_tensor_shape(value['domain'])
    _base._need(shape[2] * shape[3] <= value['budget']['max_tensor_pixels'], 'Official aspect-preserving tensor pixel budget exceeded')
    return copy.deepcopy(value)


def input_descriptors(params, operation='depth-estimate', include_resources=False):
    """Direct FILEs only, with SCALAR projected to FILE; no profile expansion.

    Additional resources remain explicit declarations for Host authority checks.
    include_resources=True is a protection inventory, not actual-use proof.
    """
    _base._need(type(operation) is str and operation.removeprefix('compositing.') == 'depth-estimate', 'Unknown estimate operation')
    _base._need(type(include_resources) is bool, 'include_resources must be bool')
    normalized = normalize_estimate(params)
    rows = _base._collect(normalized)
    if include_resources:
        rows += normalized['resources']
    return list(_base._merge(rows).values())
