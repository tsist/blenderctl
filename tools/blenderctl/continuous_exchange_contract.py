"""JSON Schema fragments for bounded shared ordinary-bone exchange evidence.

This module describes reports only. It does not validate Blender evaluation,
cross-field numeric equality, source Key identity, or arbitrary continuous time.
Dynamic diagnostic objects remain extensible; stable evidence is required.
"""
from copy import deepcopy

ADAPTER = 'BBONE_SHARED_CONTINUOUS_V1'
S = {'type': 'string'}
NAME = {'type': 'string', 'minLength': 1}
N = {'type': 'number'}
ERROR = {'type': 'number', 'minimum': 0}
COUNT = {'type': 'integer', 'minimum': 0}
POSITIVE = {'type': 'integer', 'minimum': 1}
SHA = {'type': 'string', 'pattern': '^[a-fA-F0-9]{64}$'}
BOOL = {'type': 'boolean'}


def obj(properties, required=None):
    return {'type': 'object', 'properties': properties,
            'required': list(properties) if required is None else list(required),
            'additionalProperties': True}


def arr(item, minimum=0):
    return {'type': 'array', 'items': item, 'minItems': minimum}


TIMES = arr(N, 1)
KEY_NAMES = {**arr(NAME), 'uniqueItems': True}
NATIVE_STRUCTURE = obj({
    'mesh': NAME, 'vertices': POSITIVE, 'shape_keys': KEY_NAMES, 'rig': NAME,
    'bones': POSITIVE, 'all_vertices_weighted': {'const': True}})
STRUCTURES = arr(NATIVE_STRUCTURE, 1)
SUMMARY = obj({
    'layer': NAME, 'mesh': {'type': ['string', 'null']}, 'checks': POSITIVE,
    'failed': COUNT, 'max_bidirectional_vertex_distance': ERROR,
    'max_indexed_error_meters': ERROR, 'max_area_delta': ERROR})
SUMMARIES = arr(SUMMARY, 1)
BUDGET = obj({
    'max_evaluations': POSITIVE, 'required_evaluations': POSITIVE,
    'max_vertex_samples': POSITIVE, 'required_vertex_samples': POSITIVE,
    'max_affine_joints': POSITIVE, 'required_affine_joints': POSITIVE,
    'max_output_bytes': POSITIVE, 'estimated_output_bytes_diagnostic_only': COUNT,
    'witness_times': POSITIVE, 'seed_substeps': {'enum': [1, 2, 4, 8]},
    'max_depth': COUNT, 'max_position_error': ERROR, 'total_vertices': POSITIVE,
    'ok': BOOL})
KEY = obj({
    'name': NAME, 'relative_key': {'type': ['string', 'null']}, 'vertex_group': S,
    'mask_alias': {'type': ['string', 'null']}, 'slider_min': N, 'slider_max': N,
    'mute': BOOL, 'value_min': N, 'value_max': N, 'non_zero': BOOL})
KEY_SEMANTICS = {'type': 'object', 'minProperties': 1, 'additionalProperties': {
    'oneOf': [
        obj({'mode': {'const': 'no_shape_keys'}, 'keys': {**arr(KEY), 'maxItems': 0}}),
        obj({'mode': {'const': 'sampled_native_driver_values'}, 'keys': arr(KEY, 1),
             'key_animation': NAME})]}}
FILE_COSTS = {'type': 'object', 'minProperties': 1, 'additionalProperties': COUNT}
KEY_CHECK = obj({'frame': N, 'mesh': NAME, 'max_key_value_error': ERROR, 'ok': BOOL})
BREAKPOINT_EVIDENCE = obj({
    'owner': NAME, 'action': NAME, 'curves': COUNT, 'key_times_in_range': arr(N),
    'interpolation': arr(S), 'scope': NAME})

RIG_TRANSFER = obj({
    'adapter': {'const': ADAPTER}, 'frames': arr(N, 2), 'key_frames': arr(N, 2),
    'meshes': STRUCTURES, 'shared_joint_count': POSITIVE,
    'ordinary_bones': {**POSITIVE, 'multipleOf': 2}, 'pivot': NAME,
    'time_branch': NAME, 'keys': KEY_SEMANTICS, 'budget': BUDGET,
    'native_evaluations': POSITIVE, 'native_checks': SUMMARIES,
    'format_checks': SUMMARIES, 'format': {'enum': ['GLB', 'FBX']},
    'format_file': NAME, 'format_sha256': SHA, 'cold_times': TIMES,
    'cold_manifest': NAME, 'file_costs': FILE_COSTS})

CONTINUOUS = obj({
    'status': {'const': 'pass-bounded'}, 'scope': NAME,
    'shared_skeleton': obj({
        'armature': NAME, 'affine_joints': POSITIVE, 'bones': POSITIVE,
        'meshes': STRUCTURES, 'shared_joint_names_sample': arr(NAME, 1),
        'support_pivot': NAME}),
    'breakpoints': arr(N, 2), 'breakpoint_evidence': arr(BREAKPOINT_EVIDENCE),
    'key_times': arr(N, 2), 'witness_times': arr(N, 2),
    'native_evaluations': POSITIVE, 'budget': BUDGET,
    'refinement': {'type': 'object', 'additionalProperties': True},
    'fixed_mapping_max_error': ERROR,
    'native_checks': obj({'summary': SUMMARIES, 'max_matrix_error': ERROR,
                          'key_value_max_error': ERROR, 'all_pass': BOOL}),
    'format_checks': obj({
        'summary': SUMMARIES, 'all_pass': BOOL, 'key_checks': arr(KEY_CHECK),
        'structure': STRUCTURES, 'excluded_custom_shapes': arr(S),
        'saved_blend_subset': obj({'times': TIMES, 'summary': SUMMARIES,
                                   'all_pass': BOOL})}),
    'key_semantics': KEY_SEMANTICS,
    'format_evidence': {'type': 'object', 'additionalProperties': True},
    'cold_manifest': NAME, 'cold_times': TIMES,
    'file_costs': obj({'format_outputs': FILE_COSTS, 'all_outputs': FILE_COSTS,
                       'max_output_bytes': POSITIVE}),
    'source_protection': obj({
        'source_file': NAME, 'source_sha256': SHA, 'source_sha256_after': SHA,
        'source_unchanged': {'const': True}, 'source_action_inventory_sha256': SHA,
        'source_actions': arr(S),
        'source_shape_keys': {'type': 'object', 'additionalProperties': KEY_NAMES},
        'candidate_action_copy_policy': NAME})})
# A producer advertising named native Keys must carry actual Key-value checks;
# the legitimate no-ShapeKey branch can keep the check list empty.
CONTINUOUS['allOf'] = [{'if': {'properties': {'key_semantics': {
    'not': {'additionalProperties': {'properties': {'mode': {'const': 'no_shape_keys'}}}}}}},
    'then': {'properties': {'format_checks': {'properties': {
        'key_checks': {'minItems': 1}}}}}}]


def extend_geometry_check(check):
    """Keep legacy required compare_geometry fields, add optional witness data."""
    check['properties'].update(deepcopy({
        'frame': N, 'mesh': NAME, 'layer': NAME,
        'indexed_error_meters': ERROR, 'is_key': BOOL, 'key_guard': BOOL,
        'area_reference': {'enum': ['triangulation_aligned', 'raw_native']},
        'native_area': ERROR, 'observed_area': ERROR,
        'raw_native_area': ERROR, 'raw_area_delta': ERROR,
        'import_frame': POSITIVE, 'import_time_map': {'const': 'index_domain'},
        'declared_position_pass': BOOL, 'declared_bidirectional_pass': BOOL,
        'declared_indexed_pass': BOOL}))


def extend_report(report):
    """Append the new branch without changing either legacy rig branch."""
    report['properties']['rig_transfer']['oneOf'].append(deepcopy(RIG_TRANSFER))
    report['properties'].update(deepcopy({
        'adapter': {'const': ADAPTER}, 'format_import_blend': NAME,
        'format_import_blend_sha256': SHA, 'continuous': CONTINUOUS}))
    # Trigger on settings OR either advertised evidence discriminator. A shared
    # producer cannot pass by deleting the rig_transfer/continuous evidence.
    selected = {'anyOf': [
        {'required': ['settings'], 'properties': {'settings': {
            'required': ['rig_adapter'], 'properties': {'rig_adapter': {'const': ADAPTER}}}}},
        {'required': ['adapter'], 'properties': {'adapter': {'const': ADAPTER}}},
        {'required': ['continuous']},
        {'required': ['rig_transfer'], 'properties': {'rig_transfer': {
            'required': ['adapter'], 'properties': {'adapter': {'const': ADAPTER}}}}}]}
    required = ['adapter', 'rig_transfer', 'continuous', 'format_import_blend',
                'format_import_blend_sha256']
    report.setdefault('allOf', []).append({'if': selected, 'then': {
        'required': required, 'properties': {
            'operation': {'const': 'export'}, 'mode': {'const': 'ANIMATION'},
            'format': {'enum': ['GLB', 'FBX']},
            'settings': {'required': ['rig_adapter'], 'properties': {
                'rig_adapter': {'const': ADAPTER}}},
            'rig_transfer': deepcopy(RIG_TRANSFER),
            'checks': {'minItems': 1}}}})
