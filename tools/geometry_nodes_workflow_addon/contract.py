"""Versioned JSON-only geometry workflow contract (no bpy import)."""
import copy
import json
import math
import re
from pathlib import Path


class WorkflowError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def obj(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


TEXT = {'type': 'string', 'minLength': 1}
ID = {'type': 'string', 'pattern': r'^[A-Za-z0-9_.:/-]+$', 'minLength': 1}
SHA = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
NUMBER = {'type': 'number'}
VALUE = {}  # JSON/finiteness checked separately; actual socket RNA validates values.
FILE = obj({'file': TEXT, 'expected_sha256': SHA}, ('file', 'expected_sha256'))
CONTEXT = obj({'scene': TEXT, 'view_layer': TEXT, 'frame': {'type': 'integer'}}, ('scene', 'view_layer', 'frame'))
SOCKET = obj({'key': ID, 'name': TEXT, 'direction': {'enum': ['INPUT', 'OUTPUT']},
              'type': TEXT, 'default': VALUE, 'min': NUMBER, 'max': NUMBER,
              'unit': {'type': 'string'}, 'semantic': {'type': 'string'}}, ('key', 'name', 'direction', 'type'))
RESOURCE = obj({'kind': {'enum': ['OBJECT', 'COLLECTION', 'MATERIAL', 'IMAGE', 'GROUP']}, 'name': TEXT}, ('kind', 'name'))
NODE = obj({'id': ID, 'type': TEXT, 'properties': {'type': 'object', 'additionalProperties': VALUE},
            'inputs': {'type': 'object', 'additionalProperties': VALUE},
            'resources': {'type': 'object', 'additionalProperties': RESOURCE},
            'location': {'type': 'array', 'items': NUMBER, 'minItems': 2, 'maxItems': 2},
            'label': {'type': 'string'}}, ('id', 'type'))
END = {'type': 'array', 'items': TEXT, 'minItems': 2, 'maxItems': 2}
LINK = obj({'from': END, 'to': END}, ('from', 'to'))
ZONE = obj({'input': ID, 'output': ID, 'items': {'type': 'array', 'items': obj({'name': TEXT, 'type': TEXT}, ('name', 'type'))}}, ('input', 'output', 'items'))
STATE_ITEM = obj({'name': TEXT, 'type': TEXT, 'attribute_domain': TEXT}, ('name', 'type'))
SIMULATION_ZONE = obj({'input': ID, 'output': ID, 'items': {'type': 'array', 'items': STATE_ITEM}}, ('input', 'output', 'items'))
BAKE_NODE = obj({'node': ID, 'items': {'type': 'array', 'items': obj({'name': TEXT, 'type': TEXT,
                'attribute_domain': TEXT, 'is_attribute': {'type': 'boolean'}}, ('name', 'type'))}}, ('node', 'items'))
GRAPH_SCHEMA = obj({'id': ID, 'interface': {'type': 'array', 'items': SOCKET},
                    'nodes': {'type': 'array', 'items': NODE}, 'links': {'type': 'array', 'items': LINK},
                    'repeat_zones': {'type': 'array', 'items': ZONE},
                    'simulation_zones': {'type': 'array', 'items': SIMULATION_ZONE},
                    'bake_nodes': {'type': 'array', 'items': BAKE_NODE},
                    'groups': {'type': 'array', 'items': {'$ref': '#/$defs/blueprint'}}}, ('id', 'interface', 'nodes', 'links'))
TARGET_SCHEMA = obj({'id': ID, 'object': TEXT, 'modifier': TEXT,
                     'action': {'enum': ['build', 'update', 'parameters', 'adopt']}, 'create': {'type': 'boolean'},
                     'recipe': {'enum': ['spiral_stair', 'curve_fence', 'surface_scatter']}, 'graph': GRAPH_SCHEMA,
                     'parameters': {'type': 'object', 'additionalProperties': VALUE}, 'source_group': TEXT,
                     'overwrite_core': {'type': 'boolean'}, 'expected_core_sha256': SHA, 'share_with': ID}, ('id', 'object', 'modifier', 'action'))
OBSERVATION_SCHEMA = obj({'id': ID, 'node': TEXT, 'socket': TEXT, 'component': TEXT, 'attribute': TEXT,
                          'domain': {'enum': ['POINT', 'EDGE', 'FACE', 'CORNER', 'CURVE', 'INSTANCE']},
                          'type': TEXT, 'geometry_socket': TEXT, 'geometry_node': TEXT,
                          'limit': {'type': 'integer', 'minimum': 1, 'maximum': 64}, 'object': TEXT,
                          'modifier': TEXT, 'group_path': {'type': 'array', 'items': TEXT},
                          'repeat_zone': TEXT, 'iteration': {'type': 'integer', 'minimum': 0}}, ())
PREVIEW = obj({'enabled': {'type': 'boolean'}, 'engine': {'enum': ['CYCLES', 'BLENDER_EEVEE']},
               'width': {'type': 'integer', 'minimum': 1, 'maximum': 2048},
               'height': {'type': 'integer', 'minimum': 1, 'maximum': 2048},
               'samples': {'type': 'integer', 'minimum': 1}}, ('enabled',))
TEMPORAL_SCHEMA = obj({'frame_start': {'type': 'integer'}, 'frame_end': {'type': 'integer'},
                       'fps': {'type': 'number', 'exclusiveMinimum': 0},
                       'observe_frames': {'type': 'array', 'items': {'type': 'integer'}, 'maxItems': 64},
                       'bakes': {'type': 'array', 'minItems': 1, 'maxItems': 64, 'items': obj({
                           'target': ID, 'node': ID, 'kind': {'enum': ['SIMULATION', 'BAKE']},
                           'group_path': {'type': 'array', 'items': TEXT}}, ('target', 'node', 'kind'))},
                       'storage': {'enum': ['DISK']},
                       'max_frames': {'type': 'integer', 'minimum': 1, 'maximum': 4096}}, ('frame_start', 'frame_end', 'bakes'))
MANIFEST_SCHEMA = obj({'schema_version': {'enum': ['1.0', '1.1']}, 'initial_scene': TEXT, 'context': CONTEXT,
                       'targets': {'type': 'array', 'items': TARGET_SCHEMA, 'minItems': 1},
                       'observations': {'type': 'array', 'items': OBSERVATION_SCHEMA}, 'preview': PREVIEW,
                       'temporal': TEMPORAL_SCHEMA}, ('schema_version', 'context', 'targets'))
MANIFEST_SCHEMA['$defs'] = {'blueprint': GRAPH_SCHEMA}
RUN_PARAMS_SCHEMA = obj({'file': TEXT, 'expected_sha256': SHA, 'resources': {'type': 'array', 'items': FILE},
                         'manifest': MANIFEST_SCHEMA, 'resume': FILE, 'cache_manifest': FILE}, ('resources', 'manifest'))
RUN_PARAMS_SCHEMA['$defs'] = {'blueprint': GRAPH_SCHEMA}
INSPECT_PARAMS_SCHEMA = obj({'file': TEXT, 'expected_sha256': SHA, 'resources': {'type': 'array', 'items': FILE},
                             'context': CONTEXT, 'targets': {'type': 'array', 'items': obj({'object': TEXT, 'modifier': TEXT}, ('object',))},
                             'observations': {'type': 'array', 'items': OBSERVATION_SCHEMA}, 'discover': {'type': 'boolean'},
                             'cache_manifest': FILE}, ('file', 'expected_sha256', 'resources'))
REPORT_SCHEMA = {'type': 'object', 'required': ['report_version', 'status', 'targets', 'checks'],
                 'properties': {'report_version': {'enum': ['1.0', '1.1']}, 'geometry_report_version': {'enum': ['1.0', '1.1']}, 'status': {'enum': ['passed', 'partial', 'failed']},
                                'targets': {'type': 'array', 'items': {'type': 'object'}}, 'checks': {'type': 'object'}},
                 'additionalProperties': True}


def _temporal_report_definitions():
    """Explicit cached geometry envelopes; inspection component bodies vary."""
    string = {'type': 'string'}
    integer = {'type': 'integer'}
    boolean = {'type': 'boolean'}
    array = lambda item: {'type': 'array', 'items': item}
    nullable = lambda value: {'anyOf': [value, {'type': 'null'}]}
    runtime = obj({'version': string, 'version_tuple': array(integer), 'build_hash': string},
                  ('version', 'version_tuple', 'build_hash'))
    member = obj({'path': TEXT, 'size': {'type': 'integer', 'minimum': 0}, 'sha256': SHA},
                 ('path', 'size', 'sha256'))
    candidate = obj({'file': TEXT, 'sha256': SHA}, ('file', 'sha256'))
    evaluation = {'type': 'object', 'properties': {'base': nullable({'type': 'object'}),
        'evaluated': {'type': 'object'}, 'instances': {'type': 'object'},
        'observations': array({'type': 'object'})}, 'additionalProperties': True}
    sample_target = obj({'target': ID, 'evaluation': evaluation,
        'status': {'enum': ['passed', 'failed']}, 'mismatch': nullable(string)}, ('target', 'evaluation'))
    sample = obj({'frame': integer, 'targets': array(sample_target),
        'status': {'enum': ['passed', 'failed']}, 'mismatch': nullable(string)}, ('frame', 'targets'))
    binding = obj({'wrapper_sha256': nullable(SHA), 'core_sha256': nullable(SHA),
        'parameter_state': {'type': 'object'}, 'parameters': {'type': 'object'}},
        ('wrapper_sha256', 'core_sha256', 'parameter_state', 'parameters'))
    target = obj({'id': ID, 'object': TEXT, 'modifier': TEXT}, ('id', 'object', 'modifier'))
    cache_target = copy.deepcopy(target)
    cache_target['properties']['binding'] = binding
    cache_target['required'].append('binding')
    item_fields = {'bake_target': {'enum': ['INHERIT', 'DISK', 'PACKED']},
        'bake_mode': {'enum': ['ANIMATION', 'STILL']}, 'frame_start': integer, 'frame_end': integer,
        'use_custom_simulation_frame_range': boolean, 'use_custom_path': boolean, 'directory': string}
    metadata = obj({'global': obj({'bake_target': {'enum': ['DISK', 'PACKED']},
        'bake_directory': string}, ('bake_target', 'bake_directory')),
        'item': obj(item_fields, tuple(item_fields))}, ('global', 'item'))
    bake_fields = {'target': ID, 'object': TEXT, 'modifier': TEXT, 'node_id': ID,
        'group_path': array(TEXT), 'kind': {'enum': ['SIMULATION', 'BAKE']},
        'node_type': {'enum': ['GeometryNodeSimulationOutput', 'GeometryNodeBake']},
        'bake_id': {'type': 'integer', 'minimum': 0}, 'directory': TEXT, 'metadata': metadata,
        'operator_result': array(string), 'files': array(member), 'frame_coverage': array(integer)}
    bake_required = ('target', 'object', 'modifier', 'node_id', 'group_path', 'kind',
                     'node_type', 'bake_id', 'directory', 'metadata')
    bake = obj(bake_fields, bake_required)
    cache_bake = obj(bake_fields, (*bake_required, 'files', 'frame_coverage'))
    manifest_fields = {'cache_manifest_version': {'const': '1.0'}, 'root': TEXT,
        'implementation_sha256': SHA, 'blender': runtime,
        'fps': {'type': 'number', 'exclusiveMinimum': 0}, 'frame_start': integer, 'frame_end': integer,
        'scene_frame_range': {'type': 'array', 'items': integer, 'minItems': 2, 'maxItems': 2},
        'observe_frames': array(integer), 'targets': array(cache_target), 'bakes': array(cache_bake),
        'observations': array(OBSERVATION_SCHEMA), 'samples': array(sample),
        'readback_order': array(integer), 'scope': string, 'candidate': candidate,
        'created_unix_seconds': NUMBER, 'files': array(member)}
    # Old v1.0 receipts predate scene_frame_range; readers retain their explicit fallback.
    manifest = obj(manifest_fields, tuple(k for k in manifest_fields if k != 'scene_frame_range'))
    draft = copy.deepcopy(manifest)
    for key in ('candidate', 'created_unix_seconds', 'files'):
        draft['required'].remove(key)
        del draft['properties'][key]
    report = obj({'temporal_report_version': {'const': '1.0'},
        'status': {'enum': ['passed', 'failed']}, 'stage': string,
        'metrics': {'type': 'object', 'properties': {'elapsed_seconds': NUMBER}, 'additionalProperties': True},
        'bakes': array(bake), 'sequential': array(sample), 'readback': array(sample), 'blender': runtime,
        'implementation_sha256': SHA, 'fps': NUMBER, 'frame_start': integer, 'frame_end': integer,
        'observe_frames': array(integer), 'targets': array(target), 'manifest_draft': draft,
        'manifest': draft, 'manifest_output_root': TEXT, 'cache_manifest': FILE, 'traceback': string},
        ('temporal_report_version', 'status', 'stage', 'metrics', 'bakes', 'sequential', 'readback'))
    replay_target = obj({'target': ID, 'status': {'enum': ['passed', 'failed']},
                         'mismatch': nullable(string)}, ('target', 'status', 'mismatch'))
    replay_sample = obj({'frame': integer, 'status': {'enum': ['passed', 'failed']},
                        'targets': array(replay_target)}, ('frame', 'status', 'targets'))
    replay = obj({'status': {'enum': ['passed', 'failed']}, 'readback_order': array(integer),
                  'samples': array(replay_sample)}, ('status', 'readback_order', 'samples'))
    return {'geometry_temporal_report': report, 'geometry_cache_manifest': manifest,
            'geometry_cache_sample': sample, 'geometry_cache_replay': replay}


REPORT_SCHEMA['$defs'] = _temporal_report_definitions()
REPORT_SCHEMA['properties'].update({
    'temporal': {'anyOf': [{'$ref': '#/$defs/geometry_temporal_report'}, {'type': 'null'}]},
    'cache_validation': {'anyOf': [obj({'status': {'enum': ['passed', 'failed']},
                                      'manifest': FILE}, ('status', 'manifest')), {'type': 'null'}]},
    'cache_replay': {'anyOf': [{'$ref': '#/$defs/geometry_cache_replay'}, {'type': 'null'}]},
    'cache_read_snapshot': {'anyOf': [FILE, {'type': 'null'}]},
    'temporal_diagnostic': FILE})


def _validate(value, schema, path='request', _root=None, _depth=0):
    # This is the deliberately small contract validator, not a Draft validator.
    # Preserve the graph's historical standalone blueprint reference while
    # resolving every other local definition against the original root.
    if schema is True: return value
    if schema is False: raise WorkflowError('INVALID_REQUEST', path + ': false schema rejects value')
    if not isinstance(schema, dict): raise WorkflowError('INVALID_REQUEST', path + ': object or boolean schema required')
    if _root is None: _root = schema
    if _depth > 256:
        raise WorkflowError('INVALID_REQUEST', path + ': schema/value nesting exceeds validation limit')
    if '$ref' in schema:
        reference = schema['$ref']
        if not isinstance(reference, str) or not reference.startswith('#/$defs/'):
            raise WorkflowError('INVALID_REQUEST', path + ': unsupported schema reference ' + str(reference))
        name = reference[len('#/$defs/'):]
        if not name or '/' in name:
            raise WorkflowError('INVALID_REQUEST', path + ': unresolved schema reference ' + reference)
        name = name.replace('~1', '/').replace('~0', '~')
        resolved = _root.get('$defs', {}).get(name)
        if resolved is None and reference == '#/$defs/blueprint': resolved = GRAPH_SCHEMA
        if not isinstance(resolved, dict) and type(resolved) is not bool:
            raise WorkflowError('INVALID_REQUEST', path + ': unresolved schema reference ' + reference)
        _validate(value, resolved, path, _root, _depth + 1)
    if 'anyOf' in schema:
        alternatives = schema['anyOf']
        if not isinstance(alternatives, list) or not alternatives:
            raise WorkflowError('INVALID_REQUEST', path + ': nonempty anyOf branches required')
        failures = []
        for branch in alternatives:
            try:
                _validate(value, branch, path, _root, _depth + 1)
                break
            except WorkflowError as exc: failures.append(str(exc))
        else:
            raise WorkflowError('INVALID_REQUEST', path + ': no anyOf branch matched: ' + ' | '.join(failures))
    if 'const' in schema and value != schema['const']:
        raise WorkflowError('INVALID_REQUEST', path + ': wrong constant')
    if 'enum' in schema and value not in schema['enum']:
        raise WorkflowError('INVALID_REQUEST', path + ': invalid enum')
    typ = schema.get('type')
    valid = {'object': isinstance(value, dict), 'array': isinstance(value, list),
             'string': isinstance(value, str), 'boolean': type(value) is bool,
             'integer': type(value) is int, 'number': type(value) in (int, float),
             'null': value is None}
    if typ and not valid.get(typ, False):
        raise WorkflowError('INVALID_REQUEST', path + ': expected ' + typ)
    if typ == 'object':
        missing = set(schema.get('required', ())) - set(value)
        if missing: raise WorkflowError('INVALID_REQUEST', path + ': missing ' + str(sorted(missing)))
        props = schema.get('properties', {})
        for key, child in value.items():
            if key not in props and schema.get('additionalProperties', True) is False:
                raise WorkflowError('INVALID_REQUEST', path + ': unknown field ' + key)
            _validate(child, props.get(key, schema.get('additionalProperties') or {}), path + '.' + key, _root, _depth + 1)
    if typ == 'array':
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', math.inf):
            raise WorkflowError('INVALID_REQUEST', path + ': array length')
        for index, child in enumerate(value):
            _validate(child, schema.get('items', {}), path + '[' + str(index) + ']', _root, _depth + 1)
    if typ == 'string':
        if len(value) < schema.get('minLength', 0) or ('pattern' in schema and not re.fullmatch(schema['pattern'], value)):
            raise WorkflowError('INVALID_REQUEST', path + ': invalid string')
    if typ in ('number', 'integer'):
        if not math.isfinite(value) or not schema.get('minimum', -math.inf) <= value <= schema.get('maximum', math.inf):
            raise WorkflowError('INVALID_REQUEST', path + ': number outside range')
        if 'exclusiveMinimum' in schema and value <= schema['exclusiveMinimum']:
            raise WorkflowError('INVALID_REQUEST', path + ': number outside exclusive range')
    return value


def _json(value):
    try: return json.loads(json.dumps(value, allow_nan=False))
    except (ValueError, TypeError, OverflowError) as exc: raise WorkflowError('INVALID_REQUEST', 'Finite JSON required') from exc


def normalize_blueprint(value):
    result = _validate(_json(value), GRAPH_SCHEMA, 'graph')
    keys = [s['key'] for s in result['interface']]
    if len(keys) != len(set(keys)): raise WorkflowError('INVALID_REQUEST', 'Duplicate interface key')
    ids = [n['id'] for n in result['nodes']]
    if len(ids) != len(set(ids)): raise WorkflowError('INVALID_REQUEST', 'Duplicate node id')
    for direction in ('INPUT', 'OUTPUT'):
        if len([s for s in result['interface'] if s['direction'] == direction and s['type'] == 'NodeSocketGeometry']) != 1:
            raise WorkflowError('INVALID_REQUEST', 'Each graph needs exactly one Geometry ' + direction)
    for link in result['links']:
        if link['from'][0] not in ids or link['to'][0] not in ids:
            raise WorkflowError('INVALID_REQUEST', 'Link references missing node')
    for zone in result.get('repeat_zones', []):
        if zone['input'] not in ids or zone['output'] not in ids:
            raise WorkflowError('INVALID_REQUEST', 'Repeat zone references missing node')
    types = {node['id']: node['type'] for node in result['nodes']}
    declared = set()
    for zone in result.get('simulation_zones', []):
        for key, kind in (('input', 'GeometryNodeSimulationInput'), ('output', 'GeometryNodeSimulationOutput')):
            node_id = zone[key]
            if types.get(node_id) != kind:
                raise WorkflowError('INVALID_REQUEST', 'Simulation declaration requires ' + kind)
            if node_id in declared: raise WorkflowError('INVALID_REQUEST', 'Duplicate Simulation pairing')
            declared.add(node_id)
        names = [item['name'] for item in zone['items']]
        if len(names) != len(set(names)): raise WorkflowError('INVALID_REQUEST', 'Duplicate Simulation item name')
    for bake in result.get('bake_nodes', []):
        if types.get(bake['node']) != 'GeometryNodeBake':
            raise WorkflowError('INVALID_REQUEST', 'Bake declaration requires GeometryNodeBake')
        if bake['node'] in declared: raise WorkflowError('INVALID_REQUEST', 'Duplicate Bake declaration')
        declared.add(bake['node'])
        names = [item['name'] for item in bake['items']]
        if len(names) != len(set(names)): raise WorkflowError('INVALID_REQUEST', 'Duplicate Bake item name')
    for node_id, kind in types.items():
        if kind in ('GeometryNodeSimulationInput', 'GeometryNodeSimulationOutput', 'GeometryNodeBake') and node_id not in declared:
            raise WorkflowError('INVALID_REQUEST', 'Dynamic node requires explicit declaration: ' + node_id)
    for group in result.get('groups', []): normalize_blueprint(group)
    result.setdefault('groups', []); result.setdefault('repeat_zones', [])
    result.setdefault('simulation_zones', []); result.setdefault('bake_nodes', [])
    return result


def blueprint_has_temporal(graph):
    return bool(graph.get('simulation_zones') or graph.get('bake_nodes') or
                any(blueprint_has_temporal(group) for group in graph.get('groups', [])))


def normalize_manifest(value):
    result = _validate(_json(value), MANIFEST_SCHEMA, 'manifest')
    seen = set(); bindings = set()
    for target in result['targets']:
        if target['id'] in seen: raise WorkflowError('INVALID_REQUEST', 'Duplicate target id')
        binding = (target['object'], target['modifier'])
        if binding in bindings: raise WorkflowError('INVALID_REQUEST', 'Duplicate physical target binding')
        bindings.add(binding)
        if target.get('share_with') and target['share_with'] not in seen:
            raise WorkflowError('INVALID_REQUEST', 'share_with must reference an earlier target')
        seen.add(target['id'])
        action = target['action']
        if action in ('build', 'update') and ('recipe' in target) == ('graph' in target):
            raise WorkflowError('INVALID_REQUEST', 'Build/update requires exactly one recipe or graph')
        if action == 'adopt' and not target.get('source_group'):
            raise WorkflowError('INVALID_REQUEST', 'adopt requires source_group')
        if action == 'parameters' and ('graph' in target or 'recipe' in target or 'source_group' in target):
            raise WorkflowError('INVALID_REQUEST', 'parameters cannot change graph/source')
        if target.get('overwrite_core') and not target.get('expected_core_sha256'):
            raise WorkflowError('INVALID_REQUEST', 'Core overwrite requires expected_core_sha256')
        if 'graph' in target: target['graph'] = normalize_blueprint(target['graph'])
        target.setdefault('parameters', {}); target.setdefault('overwrite_core', False)
    result.setdefault('observations', [])
    result.setdefault('preview', {'enabled': False})
    if 'temporal' in result or any(blueprint_has_temporal(t['graph']) for t in result['targets'] if 'graph' in t):
        if result['schema_version'] != '1.1':
            raise WorkflowError('INVALID_REQUEST', 'Temporal workflow requires schema_version 1.1')
    if 'temporal' in result:
        temporal = result['temporal']
        temporal.setdefault('storage', 'DISK'); temporal.setdefault('max_frames', 512)
        temporal.setdefault('observe_frames', [])
        start, end = temporal['frame_start'], temporal['frame_end']
        if end < start or end - start + 1 > temporal['max_frames']:
            raise WorkflowError('INVALID_REQUEST', 'Temporal frame range exceeds budget or is reversed')
        if any(frame < start or frame > end for frame in temporal['observe_frames']):
            raise WorkflowError('INVALID_REQUEST', 'Observation frame outside temporal range')
        if any(bake['target'] not in seen for bake in temporal['bakes']):
            raise WorkflowError('INVALID_REQUEST', 'Bake references missing target')
    return result


def _absolute(path):
    value = Path(path).expanduser()
    if not value.is_absolute(): raise WorkflowError('INVALID_REQUEST', 'Absolute file path required: ' + path)
    return str(value.resolve())


def _resources(result):
    seen = set()
    for resource in result['resources']:
        resource['file'] = _absolute(resource['file'])
        if resource['file'].casefold() in seen: raise WorkflowError('INVALID_REQUEST', 'Duplicate resource path')
        seen.add(resource['file'].casefold())
    if 'resume' in result: result['resume']['file'] = _absolute(result['resume']['file'])
    if 'cache_manifest' in result: result['cache_manifest']['file'] = _absolute(result['cache_manifest']['file'])
    return result


def normalize_params(value):
    result = _validate(_json(value), RUN_PARAMS_SCHEMA)
    if ('file' in result) != ('expected_sha256' in result): raise WorkflowError('INVALID_REQUEST', 'file/SHA must be paired')
    result['manifest'] = normalize_manifest(result['manifest'])
    if 'file' not in result and not result['manifest'].get('initial_scene'):
        raise WorkflowError('INVALID_REQUEST', 'No source requires explicit initial_scene')
    if 'file' in result:
        result['file'] = _absolute(result['file'])
        if Path(result['file']).suffix.lower() != '.blend': raise WorkflowError('INVALID_REQUEST', 'Expected .blend source')
    return _resources(result)


def normalize_inspect_params(value):
    result = _validate(_json(value), INSPECT_PARAMS_SCHEMA)
    result['file'] = _absolute(result['file'])
    if Path(result['file']).suffix.lower() != '.blend': raise WorkflowError('INVALID_REQUEST', 'Expected .blend source')
    result.setdefault('discover', False); result.setdefault('targets', []); result.setdefault('observations', [])
    return _resources(result)
