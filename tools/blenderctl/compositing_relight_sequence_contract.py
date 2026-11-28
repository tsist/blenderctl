"""Finite shot contract. Pure validation; no numerical, model or scene imports.

Consumes declared real CG Passes or the fixed qualified DAV2 RGB profile. It
does not relabel the single-image model registration or bypass its gates.
"""
import copy
import ntpath

from compositing_depth_contract import DepthError, loads_strict, signature, _module as base
from compositing_relight_contract import CONFIG, validate, input_descriptors, pathkey, need
from compositing_relight_kernel import normalize as normalize_config

obj, const, FILE = base._obj, base._const, base.FILE
INT = {'type': 'integer', 'minimum': -2147483648, 'maximum': 2147483647}
NUMBER = {'type': 'number'}
MATRIX = {'type': 'array', 'minItems': 4, 'maxItems': 4,
          'items': {'type': 'array', 'minItems': 4, 'maxItems': 4, 'items': NUMBER}}
K = obj({'fx': base.POSITIVE, 'fy': base.POSITIVE, 'cx': NUMBER, 'cy': NUMBER})
MASK = {'anyOf': [{'type': 'null'}, FILE]}
OPTIONAL_FILE = {'anyOf': [{'type': 'null'}, FILE]}
FRAME = obj({'source_frame': INT, 'scene_frame': INT,
             'stage_frame': {'type': 'integer', 'minimum': 0, 'maximum': 119},
             'time': obj({'numerator': INT, 'denominator': const(24)}),
             'rgb': FILE, 'depth': OPTIONAL_FILE, 'normal': OPTIONAL_FILE,
             'validity': OPTIONAL_FILE, 'normal_validity': OPTIONAL_FILE, 'camera_to_world': MATRIX,
             'K': K, 'masks': obj({'light': MASK, 'highlight': MASK, 'line': MASK})})
DOMAIN = copy.deepcopy(base.PREPARE_SCHEMA['properties']['domain'])
CG_SOURCE = obj({'kind': const('declared_CG_geometry_pass'),
        'producer_evidence': FILE, 'statement': base.STATEMENT,
        'rgb_encoding': const('scene_linear_Rec709_straight_RGBA_float32_NPY'),
        'depth_semantics': const('axial_z_scene_unit_smaller_nearer'),
        'normal_space': const('camera_Xright_Yup_Zbackward'),
        'normal_surface': {'enum': ['geometry_surface', 'shader_surface']},
        'motion_blur': const(False), 'depth_of_field': const(False)})
RGB_SOURCE = obj({'kind': const('RGB_DAV2_LARGE'), 'producer_evidence': FILE,
        'statement': base.STATEMENT, 'rgb_encoding': const('display_sRGB_PNG8'),
        'validity': const('all_frame_artistic_declared'), 'near_order': const('larger_nearer'),
        'model': const('DAV2_Large_vitl_518_CUDA0_FP32'), 'runtime_profile': FILE,
        'max_tensor_pixels': {'type': 'integer', 'minimum': 1, 'maximum': 4194304}})
CREATE = obj({'depth_sequence_version': const('1.0'), 'mode': const('create'),
    'shot_id': {'type': 'string', 'minLength': 1, 'maxLength': 128},
    'fps': obj({'numerator': const(24), 'denominator': const(1)}),
    'domain': DOMAIN,
    'source': {'oneOf': [CG_SOURCE, RGB_SOURCE]},
    'frames': {'type': 'array', 'items': FRAME, 'minItems': 2, 'maxItems': 120},
    'normal_source': {'enum': ['import_geometry', 'derived_axial_z', 'derived_relative_heightfield']},
    'max_relative_jump': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1},
    'statistics': {'oneOf': [obj({'method': const('all_valid_minmax'), 'mapping': const('axial_identity')}),
        obj({'method': const('all_valid_exact_percentile'), 'mapping': const('relative_near_one'),
             'low': {'type': 'number', 'minimum': 0, 'maximum': 100},
             'high': {'type': 'number', 'minimum': 0, 'maximum': 100}})]},
    'config': CONFIG, 'compute': {'oneOf': [obj({'device': const('CPU_NUMPY')}),
            obj({'device': const('CUDA0_FP32'), 'runtime_profile': FILE})]},
    'resume': {'anyOf': [{'type': 'null'}, FILE]},
    'reuse_raw': OPTIONAL_FILE,
    'budget': obj({'max_pixels': {'type': 'integer', 'minimum': 1, 'maximum': 4194304},
                   'max_total_pixels': {'type': 'integer', 'minimum': 1, 'maximum': 503316480},
                   'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 7200}}),
    'resources': base.RESOURCES})
# Additive input: old create callers explicitly normalize to no raw reuse.
CREATE['required'].remove('reuse_raw')
INSPECT = obj({'depth_sequence_version': const('1.0'), 'mode': const('inspect'),
    'bundle': FILE,
    'frame_order': {'type': 'array', 'items': {'type': 'integer', 'minimum': 0, 'maximum': 119},
                    'minItems': 1, 'maxItems': 240}, 'resources': base.RESOURCES})
SCHEMA = {'$schema': 'http://json-schema.org/draft-07/schema#',
          'title': 'compositing.depth-sequence params v1.0', 'oneOf': [CREATE, INSPECT],
          '$comment': 'Finite CG CPU/CUDA or fixed DAV2 Large CUDA RGB profile; no fallback. Complete shot requires successful public parent. Raw sources retain original bytes. Inspect may repeat visits after complete inventory verification. Resume is terminal same-shot recovery, never partial consumer authority.'}


def normalize_params(value):
    base._finite_tree(value)
    validate(value, SCHEMA)
    p = copy.deepcopy(value)
    if p['mode'] == 'create':
        p.setdefault('reuse_raw', None)
    descriptors = input_descriptors(p)
    expected = {}
    for d in descriptors + p['resources']:
        k = pathkey(d['file'])
        need(ntpath.isabs(d['file']) and not d['file'].startswith('\\\\'), 'Absolute local path required')
        need(k not in expected or expected[k] == d['expected_sha256'], 'Conflicting input SHA')
        expected[k] = d['expected_sha256']
    resources = {pathkey(d['file']): d['expected_sha256'] for d in p['resources']}
    need(len(resources) == len(p['resources']), 'Duplicate resource')
    for d in descriptors:
        need(resources.get(pathkey(d['file'])) == d['expected_sha256'], 'Input lacks resource coverage')
    if p['mode'] == 'inspect':
        return p
    p['config'] = normalize_config(p['config'])
    cg = p['source']['kind'] == 'declared_CG_geometry_pass'
    need(p['reuse_raw'] is None or (not cg and p['compute']['device'] == 'CUDA0_FP32'),
         'Raw reuse is only qualified for RGB_DAV2_LARGE CUDA', 'UNSUPPORTED')
    need(p['reuse_raw'] is None or p['resume'] is None,
         'Completed raw reuse and partial same-recipe resume are mutually exclusive')
    if cg:
        need(p['config']['frame']['kind'] == 'metric_perspective' and p['normal_source'] in ('import_geometry', 'derived_axial_z'),
             'CG shot needs metric perspective Normal/frame')
        need(p['statistics'] == {'method': 'all_valid_minmax', 'mapping': 'axial_identity'}, 'Axial shot must preserve metric scalar')
    else:
        need(p['domain']['width'] >= 64 and p['domain']['height'] >= 64, 'DAV2 original domain minimum64 required')
        need(p['compute']['device'] == 'CUDA0_FP32' and
             p['compute']['runtime_profile'] == p['source']['runtime_profile'], 'RGB model requires same explicit CUDA profile')
        need(p['normal_source'] == 'derived_relative_heightfield' and p['config']['frame']['kind'] == 'screen_heightfield',
             'Relative DAV2 requires artistic heightfield')
        need(p['statistics']['method'] == 'all_valid_exact_percentile' and p['statistics']['mapping'] == 'relative_near_one' and
             p['statistics']['low'] < p['statistics']['high'], 'RGB shot requires explicit frozen whole-shot percentiles')
    need(not p['config'].get('projection', {}).get('enabled'), 'Sequence projection receiver/caster maps not qualified', 'UNSUPPORTED')
    pixels = p['domain']['width'] * p['domain']['height']
    need(pixels <= p['budget']['max_pixels'] and pixels * len(p['frames']) <= p['budget']['max_total_pixels'],
         'Shot pixel budget exceeded', 'RESOURCE_LIMIT')
    first = p['frames'][0]
    for i, f in enumerate(p['frames']):
        need(f['stage_frame'] == i and f['source_frame'] == first['source_frame'] + i and
             f['scene_frame'] == first['scene_frame'] + i and f['time']['numerator'] == i,
             'Ordered source/scene/stage mapping must be contiguous, unique and rational i/24')
        if cg:
            need(all(f[k] is not None for k in ('depth', 'normal', 'validity', 'normal_validity')), 'CG Pass member missing')
            need(p['config']['frame'] == {'kind': 'metric_perspective', **f['K']},
                 'Per-frame K differs from fixed recipe frame')
        else:
            need(all(f[k] is None for k in ('depth', 'normal', 'validity', 'normal_validity')), 'RGB model cannot silently consume supplied Passes')
        need(f['camera_to_world'][3] == [0, 0, 0, 1], 'Affine camera matrix required')
    samples = sum(l['samples'] for l in p['config']['lights'])
    work = pixels * max(1, samples) * max(1, p['config']['quality']['shadow_steps'])
    need(work <= p['config']['quality']['max_work'], 'Per-frame work budget exceeded', 'RESOURCE_LIMIT')
    return p


def params_schema():
    return copy.deepcopy(SCHEMA)


def shot_key(params, implementation):
    """Whole-shot statistics bind every raw/validity member, then all stages."""
    canonical = copy.deepcopy(params)
    canonical['resume'] = None
    canonical['resources'] = [d for d in canonical['resources'] if d != params.get('resume')]
    canonical['budget'].pop('timeout_seconds', None)
    return signature({'request': canonical, 'implementation': implementation})
