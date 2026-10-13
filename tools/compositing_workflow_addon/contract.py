"""静态合成纯数据合同：workflow 1.0 保持 M1，1.1 显式输入绑定。"""
import copy
import math
import re
from pathlib import Path


class WorkflowError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details


def obj(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 1024}
NAME = {'type': 'string', 'minLength': 1, 'maxLength': 255}
ID = {'type': 'string', 'pattern': r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$'}
SHA = {'type': 'string', 'pattern': r'^[0-9a-f]{64}$'}
BOOL = {'type': 'boolean'}
NUMBER = {'type': 'number'}
FILE = obj({'file': TEXT, 'expected_sha256': SHA}, ('file', 'expected_sha256'))
FIELDS = {'type': 'object', 'minProperties': 1, 'maxProperties': 64}
RESOURCES = {'type': 'array', 'maxItems': 1024, 'items': FILE}
DEVICE_SCHEMA = obj({'backend': {'enum': ['CPU', 'CUDA', 'OPTIX', 'GRAPHICS']}, 'id': TEXT}, ('backend',))
PREVIEW_SCHEMA = obj({
    'width': {'type': 'integer', 'minimum': 16, 'maximum': 4096},
    'height': {'type': 'integer', 'minimum': 16, 'maximum': 4096},
    'frames': {'type': 'array', 'minItems': 1, 'maxItems': 1, 'uniqueItems': True,
               'items': {'type': 'integer', 'minimum': -100000, 'maximum': 100000}},
    'samples': {'type': 'integer', 'minimum': 1, 'maximum': 256},
    'engine': {'enum': ['SOURCE', 'CYCLES', 'BLENDER_EEVEE']},
    'camera': NAME, 'device': DEVICE_SCHEMA,
    'compositor_device': {'enum': ['GPU', 'CPU']}, 'allow_cpu_fallback': BOOL,
    'precision': {'const': 'FULL'}, 'baseline': BOOL,
    'pixel_frame_budget': {'type': 'integer', 'minimum': 1, 'maximum': 2147483648},
})
SOURCE_SCHEMA = obj({'kind': {'enum': ['render_layers', 'existing', 'image']},
    'file': TEXT, 'expected_sha256': SHA, 'colorspace': NAME,
    'alpha_mode': {'enum': ['STRAIGHT', 'PREMUL', 'CHANNEL_PACKED', 'NONE']}}, ('kind',))
M1_NODE_TYPES = (
    'CompositorNodeExposure', 'CompositorNodeBlur', 'CompositorNodeGlare', 'CompositorNodeHueSat',
    'CompositorNodeBrightContrast', 'CompositorNodeInvert', 'CompositorNodeAlphaOver',
    'CompositorNodeSetAlpha', 'CompositorNodePremulKey', 'CompositorNodeRGB',
    'CompositorNodeCombineColor', 'CompositorNodeSeparateColor',
    'ShaderNodeMath', 'ShaderNodeVectorMath', 'ShaderNodeMix', 'ShaderNodeMapRange',
    'ShaderNodeClamp', 'ShaderNodeValToRGB', 'ShaderNodeRGBCurve', 'ShaderNodeFloatCurve',
    'ShaderNodeVectorCurve', 'ShaderNodeSeparateXYZ', 'ShaderNodeCombineXYZ', 'ShaderNodeValue',
)
M2_NODE_TYPES = (
    'CompositorNodeImage', 'CompositorNodeRLayers', 'CompositorNodeDenoise',
    'CompositorNodeDefocus', 'CompositorNodeDilateErode', 'CompositorNodeBoxMask',
    'CompositorNodeEllipseMask', 'CompositorNodeKeying', 'CompositorNodeColorSpill',
    'CompositorNodeTransform', 'CompositorNodeScale', 'CompositorNodeLensdist',
)
NODE_TYPES = M1_NODE_TYPES + M2_NODE_TYPES
M1_NODE_SCHEMA = obj({'id': ID, 'type': {'enum': list(M1_NODE_TYPES)},
    'inputs': FIELDS, 'properties': FIELDS, 'outputs': FIELDS}, ('id', 'type'))
IMAGE_BINDING_SCHEMA = obj({'kind': {'const': 'image'}, 'file': TEXT, 'expected_sha256': SHA,
    'colorspace': NAME, 'alpha_mode': {'enum': ['STRAIGHT', 'PREMUL', 'CHANNEL_PACKED', 'NONE']},
    'layer': NAME}, ('kind', 'file', 'expected_sha256', 'colorspace', 'alpha_mode'))
RLAYERS_BINDING_SCHEMA = obj({'kind': {'const': 'render_layers'}, 'scene': NAME,
    'view_layer': NAME}, ('kind', 'scene', 'view_layer'))
BINDING_SCHEMA = {'oneOf': [IMAGE_BINDING_SCHEMA, RLAYERS_BINDING_SCHEMA]}
NODE_SCHEMA = obj({'id': ID, 'type': {'enum': list(NODE_TYPES)},
    'inputs': FIELDS, 'properties': FIELDS, 'outputs': FIELDS, 'binding': BINDING_SCHEMA}, ('id', 'type'))
NODE_SCHEMA['allOf'] = [
    {'if': {'properties': {'type': {'const': 'CompositorNodeImage'}}},
     'then': {'required': ['binding'], 'properties': {'binding': IMAGE_BINDING_SCHEMA}}},
    {'if': {'properties': {'type': {'const': 'CompositorNodeRLayers'}}},
     'then': {'required': ['binding'], 'properties': {'binding': RLAYERS_BINDING_SCHEMA}}},
    {'if': {'properties': {'type': {'enum': ['CompositorNodeImage', 'CompositorNodeRLayers']}}},
     'else': {'not': {'required': ['binding']}}},
]
LINK_SCHEMA = obj({'from_node': NAME, 'from_socket': NAME, 'to_node': NAME, 'to_socket': NAME},
                  ('from_node', 'from_socket', 'to_node', 'to_socket'))
GRAPH_SCHEMA = obj({'nodes': {'type': 'array', 'minItems': 1, 'maxItems': 128, 'items': NODE_SCHEMA},
    'links': {'type': 'array', 'minItems': 1, 'maxItems': 256, 'items': LINK_SCHEMA}}, ('nodes', 'links'))
M1_GRAPH_SCHEMA = copy.deepcopy(GRAPH_SCHEMA)
M1_GRAPH_SCHEMA['properties']['nodes']['items'] = M1_NODE_SCHEMA
M1_COMPONENT_KINDS = ('exposure', 'blur', 'glare', 'hue_saturation')
M2_COMPONENT_KINDS = ('color_spill', 'transform', 'scale', 'lens_distortion')
COMPONENT_SCHEMA = obj({'id': ID, 'kind': {'enum': list(M1_COMPONENT_KINDS + M2_COMPONENT_KINDS)},
    'params': {'type': 'object', 'maxProperties': 16}}, ('id', 'kind', 'params'))
M1_COMPONENT_SCHEMA = copy.deepcopy(COMPONENT_SCHEMA)
M1_COMPONENT_SCHEMA['properties']['kind']['enum'] = list(M1_COMPONENT_KINDS)
COMPONENT_BASE_SCHEMA = copy.deepcopy(COMPONENT_SCHEMA)

# Actual 5.2.1 socket identifiers and menu values were round-tripped in M2-core/component-menu-probe.json.
# This describes writable RNA choices; it does not certify all menu modes' rendered effects.
def menu(values): return {**NAME, 'enum': list(values)}
INTERPOLATION = menu(('Nearest', 'Bilinear', 'Bicubic', 'Anisotropic'))
EXTENSION = menu(('Clip', 'Extend', 'Repeat'))
RGB_CHANNEL = menu(('R', 'G', 'B'))
def parameter(socket, schema): return {'socket': socket, 'schema': schema}
COMPONENT_DEFINITIONS = {
    'exposure': {'node_type': 'CompositorNodeExposure', 'parameters': {
        'ev': parameter('Exposure', NUMBER)}},
    'blur': {'node_type': 'CompositorNodeBlur', 'parameters': {
        'size': parameter('Size', {'type': 'array', 'minItems': 2, 'maxItems': 2,
            'items': {'type': 'number', 'minimum': 0, 'maximum': 4096}}),
        'type': parameter('Type', menu(('Flat', 'Tent', 'Quadratic', 'Cubic', 'Gaussian', 'Fast Gaussian', 'Catrom', 'Mitch')))}},
    'glare': {'node_type': 'CompositorNodeGlare', 'parameters': {
        'type': parameter('Type', menu(('Bloom', 'Ghosts', 'Streaks', 'Fog Glow', 'Simple Star', 'Sun Beams', 'Kernel'))),
        'quality': parameter('Quality', menu(('High', 'Medium', 'Low'))),
        'threshold': parameter('Highlights Threshold', NUMBER), 'strength': parameter('Strength', NUMBER)}},
    'hue_saturation': {'node_type': 'CompositorNodeHueSat', 'parameters': {
        'hue': parameter('Hue', NUMBER), 'saturation': parameter('Saturation', NUMBER),
        'value': parameter('Value', NUMBER), 'factor': parameter('Fac', NUMBER)}},
    'color_spill': {'node_type': 'CompositorNodeColorSpill', 'parameters': {
        'factor': parameter('Fac', NUMBER), 'spill_channel': parameter('Spill Channel', RGB_CHANNEL),
        'limit_method': parameter('Limit Method', menu(('Single', 'Average'))),
        'limit_channel': parameter('Limit Channel', RGB_CHANNEL), 'limit_strength': parameter('Limit Strength', NUMBER),
        'use_spill_strength': parameter('Use Spill Strength', BOOL),
        'spill_strength': parameter('Spill Strength', {'type': 'array', 'minItems': 4, 'maxItems': 4, 'items': NUMBER})}},
    'transform': {'node_type': 'CompositorNodeTransform', 'parameters': {
        'x': parameter('X', NUMBER), 'y': parameter('Y', NUMBER), 'angle': parameter('Angle', NUMBER),
        'scale': parameter('Scale', NUMBER), 'interpolation': parameter('Interpolation', INTERPOLATION),
        'extension_x': parameter('Extension X', EXTENSION), 'extension_y': parameter('Extension Y', EXTENSION)}},
    'scale': {'node_type': 'CompositorNodeScale', 'parameters': {
        'type': parameter('Type', menu(('Relative', 'Absolute', 'Scene Size', 'Render Size'))),
        'x': parameter('X', NUMBER), 'y': parameter('Y', NUMBER),
        'frame_type': parameter('Frame Type', menu(('Stretch', 'Fit', 'Crop'))),
        'interpolation': parameter('Interpolation', INTERPOLATION),
        'extension_x': parameter('Extension X', EXTENSION), 'extension_y': parameter('Extension Y', EXTENSION)}},
    'lens_distortion': {'node_type': 'CompositorNodeLensdist', 'parameters': {
        'type': parameter('Type', menu(('Radial', 'Horizontal'))), 'distortion': parameter('Distortion', NUMBER),
        'dispersion': parameter('Dispersion', NUMBER), 'jitter': parameter('Jitter', BOOL), 'fit': parameter('Fit', BOOL)}},
}
COMPONENT_PARAM_SCHEMAS = {kind: {**obj({key: {**copy.deepcopy(spec['schema']), 'x-socket-identifier': spec['socket']}
    for key, spec in definition['parameters'].items()}), 'maxProperties': 16, 'x-node-type': definition['node_type'],
    'x-scope': 'Actual RNA setter choices; rendered effect acceptance remains separate'}
    for kind, definition in COMPONENT_DEFINITIONS.items()}
def component_param_schema(kind, workflow_version='1.1'):
    schema = copy.deepcopy(COMPONENT_PARAM_SCHEMAS[kind])
    if workflow_version == '1.0':
        # Derive the exact former NAME validation rather than tightening legacy menu strings.
        for field in schema['properties'].values(): field.pop('enum', None)
    return schema
COMPONENT_SCHEMA['allOf'] = [{'if': {'properties': {'kind': {'const': kind}}},
    'then': {'properties': {'params': schema}}} for kind, schema in COMPONENT_PARAM_SCHEMAS.items()]
PATCH_SCHEMA = obj({'node': ID, 'inputs': FIELDS, 'properties': FIELDS, 'outputs': FIELDS}, ('node',))
MANIFEST_SCHEMA = obj({'workflow_version': {'enum': ['1.0', '1.1']}, 'workflow_id': ID,
    'scene': NAME, 'view_layer': NAME, 'operation': {'enum': ['create', 'update', 'adopt', 'detach']},
    'frame': {'type': 'integer', 'minimum': -100000, 'maximum': 100000}, 'source': SOURCE_SCHEMA,
    'components': {'type': 'array', 'minItems': 1, 'maxItems': 128, 'items': COMPONENT_BASE_SCHEMA},
    'graph': GRAPH_SCHEMA, 'patches': {'type': 'array', 'minItems': 1, 'maxItems': 128, 'items': PATCH_SCHEMA},
    'replace_graph': BOOL}, ('workflow_version', 'workflow_id', 'scene', 'view_layer', 'operation'))
MANIFEST_SCHEMA['allOf'] = [{'if': {'properties': {'workflow_version': {'const': '1.0'}}},
    'then': {'properties': {'graph': M1_GRAPH_SCHEMA, 'components': {'type': 'array',
        'minItems': 1, 'maxItems': 128, 'items': M1_COMPONENT_SCHEMA}}},
    'else': {'properties': {'components': {'type': 'array', 'minItems': 1, 'maxItems': 128, 'items': COMPONENT_SCHEMA}}}}]
RUN_SCHEMA = obj({'file': TEXT, 'expected_sha256': SHA, 'resources': RESOURCES,
    'manifest': MANIFEST_SCHEMA, 'preview': PREVIEW_SCHEMA, 'resume': FILE,
    'implementation_sha256': SHA}, ('file', 'expected_sha256', 'resources', 'manifest'))
INSPECT_SCHEMA = obj({'file': TEXT, 'expected_sha256': SHA, 'resources': RESOURCES,
    'scene': NAME, 'workflow_id': ID, 'implementation_sha256': SHA},
    ('file', 'expected_sha256', 'resources', 'scene', 'workflow_id'))


def validate(value, schema, path='$'):
    """有限 JSON Schema 验证器，只使用本模块实际导出的关键字。"""
    if 'const' in schema and value != schema['const']:
        raise WorkflowError('INVALID_REQUEST', path + ': invalid constant')
    if 'enum' in schema and value not in schema['enum']:
        raise WorkflowError('INVALID_REQUEST', path + ': unsupported enum value', {'value': value, 'allowed': schema['enum']})
    if 'oneOf' in schema:
        matches = 0
        for branch in schema['oneOf']:
            try: validate(value, branch, path)
            except WorkflowError: continue
            matches += 1
        if matches != 1: raise WorkflowError('INVALID_REQUEST', path + ': exactly one binding shape must match')
    for branch in schema.get('allOf', []): validate(value, branch, path)
    if 'not' in schema:
        try: validate(value, schema['not'], path)
        except WorkflowError: pass
        else: raise WorkflowError('INVALID_REQUEST', path + ': forbidden shape')
    if 'if' in schema:
        try: validate(value, schema['if'], path)
        except WorkflowError: selected = schema.get('else')
        else: selected = schema.get('then')
        if selected is not None: validate(value, selected, path)
    kind = schema.get('type')
    predicates = {'object': lambda: isinstance(value, dict), 'array': lambda: isinstance(value, list),
        'string': lambda: isinstance(value, str), 'boolean': lambda: type(value) is bool,
        'integer': lambda: type(value) is int, 'number': lambda: type(value) in (int, float) and math.isfinite(value)}
    if kind and not predicates[kind](): raise WorkflowError('INVALID_REQUEST', path + ': expected ' + kind)
    if kind == 'object' or (kind is None and isinstance(value, dict) and ('properties' in schema or 'required' in schema)):
        missing = set(schema.get('required', [])) - set(value)
        if missing: raise WorkflowError('INVALID_REQUEST', path + ': missing fields', {'fields': sorted(missing)})
        extra = set(value) - set(schema.get('properties', value))
        if schema.get('additionalProperties') is False and extra:
            raise WorkflowError('INVALID_REQUEST', path + ': unknown fields', {'fields': sorted(extra)})
        if len(value) < schema.get('minProperties', 0) or len(value) > schema.get('maxProperties', 100000):
            raise WorkflowError('INVALID_REQUEST', path + ': field count outside bound')
        for key, item in value.items():
            if key in schema.get('properties', {}): validate(item, schema['properties'][key], path + '/' + key)
    if kind == 'array':
        if not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', 100000):
            raise WorkflowError('INVALID_REQUEST', path + ': item count outside bound')
        if schema.get('uniqueItems') and len({repr(x) for x in value}) != len(value):
            raise WorkflowError('INVALID_REQUEST', path + ': duplicate items')
        for i, item in enumerate(value): validate(item, schema.get('items', {}), path + '/' + str(i))
    if kind == 'string':
        if not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 100000):
            raise WorkflowError('INVALID_REQUEST', path + ': string length outside bound')
        if 'pattern' in schema and not re.fullmatch(schema['pattern'], value):
            raise WorkflowError('INVALID_REQUEST', path + ': invalid identifier')
    if kind in ('number', 'integer') and not schema.get('minimum', -math.inf) <= value <= schema.get('maximum', math.inf):
        raise WorkflowError('INVALID_REQUEST', path + ': number outside bound')
    return value


def _json(value):
    """任意字段映射仍须是有限、无可执行对象的 JSON。"""
    if value is None or isinstance(value, (str, bool, int)): return
    if isinstance(value, float) and math.isfinite(value): return
    if isinstance(value, list):
        for item in value: _json(item)
        return
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        for item in value.values(): _json(item)
        return
    raise WorkflowError('INVALID_REQUEST', 'Only finite JSON values are accepted')


def normalize_preview(value):
    value = copy.deepcopy(value)
    validate(value, PREVIEW_SCHEMA)
    if 'frames' in value and value['frames'] != sorted(value['frames']):
        raise WorkflowError('INVALID_REQUEST', 'Preview frames must be sorted and unique')
    if all(k in value for k in ('width', 'height', 'frames')):
        if value['width'] * value['height'] * len(value['frames']) > value.get('pixel_frame_budget', 32000000):
            raise WorkflowError('UNSUPPORTED', 'Preview exceeds its pixel-frame budget')
    value.setdefault('baseline', True)
    return value


def normalize_manifest(value):
    value = copy.deepcopy(value); _json(value); validate(value, MANIFEST_SCHEMA)
    op = value['operation']
    if op == 'create':
        if 'source' not in value or (('components' in value) + ('graph' in value)) != 1:
            raise WorkflowError('INVALID_REQUEST', 'Create requires source and exactly one components/graph')
        if set(value) & {'patches', 'replace_graph'}: raise WorkflowError('INVALID_REQUEST', 'Create cannot patch/replace')
    elif op == 'update':
        if 'source' in value or 'components' in value: raise WorkflowError('INVALID_REQUEST', 'Update cannot silently replace source/components')
        if value.get('replace_graph'):
            if 'graph' not in value or 'patches' in value: raise WorkflowError('INVALID_REQUEST', 'replace_graph requires graph and no patches')
        elif 'patches' not in value or 'graph' in value: raise WorkflowError('INVALID_REQUEST', 'Update requires patches or explicit graph replacement')
    elif set(value) & {'source', 'components', 'graph', 'patches', 'replace_graph'}:
        raise WorkflowError('INVALID_REQUEST', op + ' takes no graph or parameter changes')
    if 'source' in value:
        source = value['source']; fields = set(source) - {'kind'}
        if source['kind'] == 'image':
            if fields != {'file', 'expected_sha256', 'colorspace', 'alpha_mode'}:
                raise WorkflowError('INVALID_REQUEST', 'Image source requires file/SHA/colorspace/alpha_mode')
            if not Path(source['file']).is_absolute(): raise WorkflowError('INVALID_REQUEST', 'Image path must be absolute')
        elif fields: raise WorkflowError('INVALID_REQUEST', 'Non-image source takes only kind')
    if 'components' in value:
        ids = [x['id'] for x in value['components']]
        if len(ids) != len(set(ids)): raise WorkflowError('INVALID_REQUEST', 'Duplicate component ID')
        for component in value['components']:
            if value['workflow_version'] == '1.0' and component['kind'] not in M1_COMPONENT_KINDS:
                raise WorkflowError('INVALID_REQUEST', 'New components require workflow_version 1.1')
            validate(component['params'], component_param_schema(component['kind'], value['workflow_version']),
                     '$/components/' + component['id'] + '/params')
    if 'graph' in value:
        graph = value['graph']; ids = [n['id'] for n in graph['nodes']]
        if len(ids) != len(set(ids)): raise WorkflowError('INVALID_REQUEST', 'Duplicate graph node ID')
        for node in graph['nodes']:
            if value['workflow_version'] == '1.0' and (node['type'] not in M1_NODE_TYPES or 'binding' in node):
                raise WorkflowError('INVALID_REQUEST', 'New node types/bindings require workflow_version 1.1')
            required_binding = {'CompositorNodeImage': 'image', 'CompositorNodeRLayers': 'render_layers'}.get(node['type'])
            if required_binding:
                if node.get('binding', {}).get('kind') != required_binding:
                    raise WorkflowError('INVALID_REQUEST', node['type'] + ' requires an exact matching binding')
                binding = node['binding']
                if required_binding == 'image' and not Path(binding['file']).is_absolute():
                    raise WorkflowError('INVALID_REQUEST', 'Bound image file must be absolute')
                if required_binding == 'render_layers' and (binding['scene'] != value['scene'] or binding['view_layer'] != value['view_layer']):
                    raise WorkflowError('UNSUPPORTED', 'Render Layers binding must match manifest scene/view_layer')
            elif 'binding' in node:
                raise WorkflowError('INVALID_REQUEST', 'Binding is only supported on Image and Render Layers nodes')
        targets = set(ids) | {'$input', '$output'}
        for link in graph['links']:
            if link['from_node'] not in targets - {'$output'} or link['to_node'] not in targets - {'$input'}:
                raise WorkflowError('INVALID_REQUEST', 'Unknown/invalid graph endpoint')
        if not any(l['to_node'] == '$output' and l['to_socket'] == 'Image' for l in graph['links']):
            raise WorkflowError('INVALID_REQUEST', 'Graph must connect $output.Image')
    return value


def _paths(value):
    for item in [{'file': value['file']}, *value['resources'], *([value['resume']] if 'resume' in value else [])]:
        if not Path(item['file']).is_absolute(): raise WorkflowError('INVALID_REQUEST', 'Input paths must be absolute')
    paths = [str(Path(r['file']).resolve()).casefold() for r in value['resources']]
    if len(paths) != len(set(paths)): raise WorkflowError('INVALID_REQUEST', 'Duplicate resource paths')
    return value


def normalize_run_params(value):
    value = copy.deepcopy(value); _json(value); validate(value, RUN_SCHEMA); _paths(value)
    value['manifest'] = normalize_manifest(value['manifest'])
    if 'preview' in value: value['preview'] = normalize_preview(value['preview'])
    manifest = value['manifest']
    images = ([manifest['source']] if manifest.get('source', {}).get('kind') == 'image' else [])
    images += [node['binding'] for node in manifest.get('graph', {}).get('nodes', []) if node.get('binding', {}).get('kind') == 'image']
    for source in images:
        matching = [r for r in value['resources'] if Path(r['file']).resolve() == Path(source['file']).resolve()]
        if len(matching) != 1 or matching[0]['expected_sha256'] != source['expected_sha256']:
            raise WorkflowError('INVALID_REQUEST', 'Every image source/binding requires a matching resource descriptor')
    return value


def normalize_inspect_params(value):
    value = copy.deepcopy(value); _json(value); validate(value, INSPECT_SCHEMA)
    return _paths(value)


normalize_params = normalize_run_params

# M3 is an explicit new branch; the two legacy contracts remain exact snapshots.
LEGACY_MANIFEST_SCHEMA = copy.deepcopy(MANIFEST_SCHEMA)
LEGACY_PREVIEW_SCHEMA = copy.deepcopy(PREVIEW_SCHEMA)
LEGACY_SOURCE_SCHEMA = copy.deepcopy(SOURCE_SCHEMA)
LEGACY_BINDING_SCHEMA = copy.deepcopy(BINDING_SCHEMA)
_legacy_manifest = normalize_manifest
_legacy_preview = normalize_preview
FRAME = {'type': 'integer', 'minimum': -100000, 'maximum': 100000}
FPS_SCHEMA = obj({'numerator': {'type': 'integer', 'minimum': 1, 'maximum': 120000},
    'denominator': {'type': 'integer', 'minimum': 1, 'maximum': 10000}}, ('numerator', 'denominator'))
TIME_MAPPING_SCHEMA = obj({'scene_start': FRAME, 'source_start': FRAME,
    'frame_count': {'type': 'integer', 'minimum': 1, 'maximum': 300},
    'rate': {'const': 'ONE_TO_ONE'}, 'scene_fps': FPS_SCHEMA},
    ('scene_start', 'source_start', 'frame_count', 'rate', 'scene_fps'))
SEQUENCE_FRAME_SCHEMA = obj({'source_frame': FRAME, 'file': TEXT, 'expected_sha256': SHA},
    ('source_frame', 'file', 'expected_sha256'))
SEQUENCE_SCHEMA = obj({'kind': {'const': 'image_sequence'},
    'frames': {'type': 'array', 'minItems': 1, 'maxItems': 300, 'items': SEQUENCE_FRAME_SCHEMA},
    'mapping': TIME_MAPPING_SCHEMA, 'colorspace': NAME,
    'alpha_mode': {'enum': ['STRAIGHT', 'PREMUL', 'CHANNEL_PACKED', 'NONE']}},
    ('kind', 'frames', 'mapping', 'colorspace', 'alpha_mode'))
ANIMATION_TARGET_SCHEMA = obj({'node': ID, 'domain': {'enum': ['inputs', 'outputs', 'properties']},
    'identifier': NAME}, ('node', 'domain', 'identifier'))
ANIMATION_VALUE_SCHEMA = {'oneOf': [NUMBER,
    {'type': 'array', 'minItems': 1, 'maxItems': 4, 'items': NUMBER}]}
ANIMATION_KEY_SCHEMA = obj({'frame': FRAME, 'value': ANIMATION_VALUE_SCHEMA,
    'interpolation': {'enum': ['LINEAR', 'CONSTANT']}}, ('frame', 'value', 'interpolation'))
ANIMATION_TRACK_SCHEMA = obj({'target': ANIMATION_TARGET_SCHEMA,
    'keys': {'type': 'array', 'minItems': 1, 'maxItems': 300, 'items': ANIMATION_KEY_SCHEMA},
    'extrapolation': {'const': 'CONSTANT'}}, ('target', 'keys', 'extrapolation'))
ANIMATION_SCHEMA = obj({'animation_version': {'const': '1.0'},
    'tracks': {'type': 'array', 'minItems': 1, 'maxItems': 64, 'items': ANIMATION_TRACK_SCHEMA}},
    ('animation_version', 'tracks'))
ANIMATION_PATCH_SCHEMA = {'oneOf': [obj({'operation': {'const': 'replace'}, 'track': ANIMATION_TRACK_SCHEMA},
    ('operation', 'track')), obj({'operation': {'const': 'remove'}, 'target': ANIMATION_TARGET_SCHEMA,
    'static_value': ANIMATION_VALUE_SCHEMA}, ('operation', 'target', 'static_value'))]}
TRACKING_IMPORT_SCHEMA = obj({'file': TEXT, 'expected_sha256': SHA,
    'source_binding': NAME, 'targets': obj({'position': ANIMATION_TARGET_SCHEMA,
        'size': ANIMATION_TARGET_SCHEMA, 'rotation': ANIMATION_TARGET_SCHEMA}, ('position', 'size', 'rotation'))},
    ('file', 'expected_sha256', 'source_binding', 'targets'))
TRACKING_SAMPLE_SCHEMA = obj({'source_frame': FRAME,
    'position': {'type': 'array', 'minItems': 2, 'maxItems': 2, 'items': {'type': 'number', 'minimum': 0, 'maximum': 1}},
    'size': {'type': 'array', 'minItems': 2, 'maxItems': 2, 'items': {'type': 'number', 'minimum': 0, 'maximum': 1}},
    'rotation': NUMBER}, ('source_frame', 'position', 'size', 'rotation'))
TRACKING_DOCUMENT_SCHEMA = obj({'tracking_version': {'const': '1.0'}, 'source_binding': NAME,
    'coordinates': {'const': 'normalized_bottom_left'}, 'rotation_unit': {'const': 'radians'},
    'samples': {'type': 'array', 'minItems': 1, 'maxItems': 300, 'items': TRACKING_SAMPLE_SCHEMA}},
    ('tracking_version', 'source_binding', 'coordinates', 'rotation_unit', 'samples'))
SOURCE_SCHEMA = {'oneOf': [LEGACY_SOURCE_SCHEMA, SEQUENCE_SCHEMA]}
BINDING_SCHEMA = {'oneOf': [IMAGE_BINDING_SCHEMA, RLAYERS_BINDING_SCHEMA, SEQUENCE_SCHEMA]}
M3_GRAPH_SCHEMA = copy.deepcopy(GRAPH_SCHEMA)
M3_GRAPH_SCHEMA['properties']['nodes']['items']['properties']['binding'] = BINDING_SCHEMA
M3_GRAPH_SCHEMA['properties']['nodes']['items']['allOf'][0]['then']['properties']['binding'] = {
    'oneOf': [IMAGE_BINDING_SCHEMA, SEQUENCE_SCHEMA]}
M3_MANIFEST_SCHEMA = copy.deepcopy(LEGACY_MANIFEST_SCHEMA)
M3_MANIFEST_SCHEMA['properties'].update({'workflow_version': {'const': '1.2'}, 'source': SOURCE_SCHEMA,
    'graph': M3_GRAPH_SCHEMA, 'reference_frame': FRAME, 'animation': ANIMATION_SCHEMA,
    'animation_patches': {'type': 'array', 'minItems': 1, 'maxItems': 64, 'items': ANIMATION_PATCH_SCHEMA},
    'tracking_imports': {'type': 'array', 'minItems': 1, 'maxItems': 16, 'items': TRACKING_IMPORT_SCHEMA}})
M3_MANIFEST_SCHEMA.pop('allOf')
MANIFEST_SCHEMA = {'oneOf': [LEGACY_MANIFEST_SCHEMA, M3_MANIFEST_SCHEMA]}
PREVIEW_SCHEMA = copy.deepcopy(LEGACY_PREVIEW_SCHEMA)
PREVIEW_SCHEMA['properties']['frames']['maxItems'] = 300
TIMELINE_SCHEMA = obj({'scene_start': FRAME, 'frame_count': {'type': 'integer', 'minimum': 1, 'maximum': 300},
    'scene_fps': FPS_SCHEMA}, ('scene_start', 'frame_count', 'scene_fps'))
VIDEO_SCHEMA = {'oneOf': [obj({'enabled': {'type': 'boolean', 'const': False}}, ('enabled',)),
    obj({'enabled': {'type': 'boolean', 'const': True}, 'kind': {'const': 'SDR_MP4_H264'}, 'alpha_policy': {'const': 'COMPOSITE_BLACK'},
        'runtime': obj({'ffmpeg': FILE, 'ffprobe': FILE}, ('ffmpeg', 'ffprobe'))},
        ('enabled', 'kind', 'alpha_policy', 'runtime'))]}
PREVIEW_SCHEMA['properties'].update({'timeline': TIMELINE_SCHEMA, 'video': VIDEO_SCHEMA})
PREVIEW_SCHEMA['allOf'] = [{'not': {'required': ['frames', 'timeline']}},
    {'if': {'required': ['video'], 'properties': {'video': {'required': ['enabled'],
        'properties': {'enabled': {'const': True}}}}}, 'then': {'required': ['timeline']}}]
RUN_SCHEMA['properties'].update({'manifest': MANIFEST_SCHEMA, 'preview': PREVIEW_SCHEMA})
RUN_SCHEMA.setdefault('allOf', []).append({
    'if': {'properties': {'manifest': {'properties': {'workflow_version': {'enum': ['1.0', '1.1']}},
                                     'required': ['workflow_version']}}, 'required': ['manifest']},
    'then': {'properties': {'preview': copy.deepcopy(LEGACY_PREVIEW_SCHEMA)}},
})

def media_sources(manifest):
    rows = []
    if manifest.get('source', {}).get('kind') in {'image', 'image_sequence', 'movie'}:
        rows.append(('$source', manifest['source']))
    rows.extend((n['id'], n['binding']) for n in manifest.get('graph', {}).get('nodes', [])
                if n.get('binding', {}).get('kind') in {'image', 'image_sequence', 'movie'})
    return rows

def _target_key(target): return (target['node'], target['domain'], target['identifier'])

def _check_track(track):
    validate(track, ANIMATION_TRACK_SCHEMA)
    frames = [k['frame'] for k in track['keys']]
    if frames != sorted(set(frames)): raise WorkflowError('INVALID_REQUEST', 'Track keys must be sorted and unique')
    shapes = {(type(k['value']) is list, len(k['value']) if isinstance(k['value'], list) else 1) for k in track['keys']}
    if len(shapes) != 1: raise WorkflowError('INVALID_REQUEST', 'Track key value dimensions must agree')

def normalize_manifest(value):
    if value.get('workflow_version') == '1.3':
        return _normalize_movie_manifest(value)
    if value.get('workflow_version') != '1.2':
        validate(value, LEGACY_MANIFEST_SCHEMA); return _legacy_manifest(value)
    value = copy.deepcopy(value); _json(value); validate(value, M3_MANIFEST_SCHEMA)
    op = value['operation']
    if op != 'create' and set(value) & {'animation', 'tracking_imports'} and not value.get('replace_graph'):
        raise WorkflowError('INVALID_REQUEST', 'New full animation/tracking requires create or replace_graph')
    if op != 'update' and 'animation_patches' in value:
        raise WorkflowError('INVALID_REQUEST', 'animation_patches requires update')
    if value.get('replace_graph') and 'animation_patches' in value:
        raise WorkflowError('INVALID_REQUEST', 'replace_graph takes full animation declarations, not old-track patches')
    legacy = copy.deepcopy(value); legacy['workflow_version'] = '1.1'
    for key in ('reference_frame', 'animation', 'animation_patches', 'tracking_imports'): legacy.pop(key, None)
    for _, media in media_sources(legacy):
        if media['kind'] == 'image_sequence':
            first = media['frames'][0]; colorspace = media['colorspace']; alpha_mode = media['alpha_mode']
            media.clear(); media.update({'kind': 'image', 'file': first['file'], 'expected_sha256': first['expected_sha256'],
                'colorspace': colorspace, 'alpha_mode': alpha_mode})
    if op == 'update' and 'animation_patches' in value and 'patches' not in legacy and not value.get('replace_graph'):
        legacy['patches'] = [{'node': 'contract-check-only'}]
    _legacy_manifest(legacy)
    temporal = [m for _, m in media_sources(value) if m['kind'] == 'image_sequence']
    for media in temporal:
        mapping = media['mapping']; members = media['frames']; start = mapping['source_start']
        fps = mapping['scene_fps']; divisor = math.gcd(fps['numerator'], fps['denominator'])
        fps['numerator'] //= divisor; fps['denominator'] //= divisor
        if len(members) != mapping['frame_count'] or [m['source_frame'] for m in members] != list(range(start, start + len(members))):
            raise WorkflowError('INVALID_REQUEST', 'Sequence members must exactly cover the explicit continuous source domain')
        paths = [str(Path(m['file']).resolve()).casefold() for m in members]
        if len(paths) != len(set(paths)) or any(not Path(m['file']).is_absolute() for m in members):
            raise WorkflowError('INVALID_REQUEST', 'Sequence paths must be absolute and unique')
        if mapping['scene_start'] + len(members) - 1 > 100000 or start + len(members) - 1 > 100000:
            raise WorkflowError('INVALID_REQUEST', 'Sequence frame domain outside bounds')
    if temporal:
        fps = [m['mapping']['scene_fps'] for m in temporal]
        if any(f['numerator'] * fps[0]['denominator'] != fps[0]['numerator'] * f['denominator'] for f in fps):
            raise WorkflowError('INVALID_REQUEST', 'All temporal bindings require the same rational FPS')
        low = max(m['mapping']['scene_start'] for m in temporal)
        high = min(m['mapping']['scene_start'] + m['mapping']['frame_count'] - 1 for m in temporal)
        if op == 'create': value.setdefault('reference_frame', low)
        if low > high or ('reference_frame' in value and not low <= value['reference_frame'] <= high):
            raise WorkflowError('INVALID_REQUEST', 'reference_frame outside common temporal domain')
    elif op == 'create': value.setdefault('reference_frame', value.get('frame', 1))
    if 'frame' in value and 'reference_frame' in value and value['frame'] != value['reference_frame']:
        raise WorkflowError('INVALID_REQUEST', 'M3 frame must equal reference_frame')
    tracks = value.get('animation', {}).get('tracks', [])
    for track in tracks: _check_track(track)
    targets = [_target_key(t['target']) for t in tracks]
    if len(targets) != len(set(targets)): raise WorkflowError('INVALID_REQUEST', 'Duplicate animation target')
    patch_targets = []
    for patch in value.get('animation_patches', []):
        if patch['operation'] == 'replace': _check_track(patch['track'])
        patch_targets.append(_target_key(patch['track']['target'] if patch['operation'] == 'replace' else patch['target']))
    if len(patch_targets) != len(set(patch_targets)): raise WorkflowError('INVALID_REQUEST', 'Duplicate animation patch target')
    for tracking in value.get('tracking_imports', []):
        if not Path(tracking['file']).is_absolute(): raise WorkflowError('INVALID_REQUEST', 'Tracking file must be absolute')
    return value

def normalize_preview(value, workflow_version='1.2'):
    if workflow_version not in {'1.2', '1.3'}:
        validate(value, LEGACY_PREVIEW_SCHEMA); return _legacy_preview(value)
    value = copy.deepcopy(value); validate(value, PREVIEW_SCHEMA)
    if 'timeline' in value:
        if 'frames' in value: raise WorkflowError('INVALID_REQUEST', 'timeline and frames are mutually exclusive')
        timeline = value['timeline']; fps = timeline['scene_fps']; divisor = math.gcd(fps['numerator'], fps['denominator'])
        fps['numerator'] //= divisor; fps['denominator'] //= divisor
        if timeline['scene_start'] + timeline['frame_count'] - 1 > 100000:
            raise WorkflowError('INVALID_REQUEST', 'Preview timeline frame domain outside bounds')
        if 'width' in value and 'height' in value and value['width'] * value['height'] * timeline['frame_count'] > value.get('pixel_frame_budget', 640000000):
            raise WorkflowError('UNSUPPORTED', 'Preview timeline exceeds its pixel-frame budget')
    if value.get('video', {}).get('enabled'):
        if 'timeline' not in value: raise WorkflowError('INVALID_REQUEST', 'Enabled video requires an explicit complete timeline')
        for runtime in value['video']['runtime'].values():
            if not Path(runtime['file']).is_absolute(): raise WorkflowError('INVALID_REQUEST', 'Video runtime paths must be absolute')
    if 'frames' in value and value['frames'] != sorted(set(value['frames'])):
        raise WorkflowError('INVALID_REQUEST', 'Preview frames must be sorted and unique')
    if all(k in value for k in ('width', 'height', 'frames')) and value['width'] * value['height'] * len(value['frames']) > value.get('pixel_frame_budget', 640000000):
        raise WorkflowError('UNSUPPORTED', 'Preview exceeds its pixel-frame budget')
    value.setdefault('baseline', True); return value

def normalize_run_params(value):
    value = copy.deepcopy(value); _json(value); validate(value, RUN_SCHEMA); _paths(value)
    value['manifest'] = normalize_manifest(value['manifest'])
    if 'preview' in value: value['preview'] = normalize_preview(value['preview'], value['manifest']['workflow_version'])
    if 'timeline' in value.get('preview', {}):
        timeline = value['preview']['timeline']; first = timeline['scene_start']; last = first + timeline['frame_count'] - 1
        for _, media in media_sources(value['manifest']):
            if media['kind'] not in {'image_sequence', 'movie'}: continue
            mapping = media['mapping']; fps = timeline['scene_fps']; actual = mapping['scene_fps']
            if not mapping['scene_start'] <= first <= last < mapping['scene_start'] + mapping['frame_count']:
                raise WorkflowError('INVALID_REQUEST', 'Preview timeline outside a declared temporal binding')
            if fps != actual: raise WorkflowError('INVALID_REQUEST', 'Preview timeline FPS differs from temporal binding')
    documents = []
    for _, media in media_sources(value['manifest']):
        documents.extend(media['frames'] if media['kind'] == 'image_sequence' else [media])
        if media['kind'] == 'movie': documents.extend(media['decoder_runtime'].values())
    documents.extend(value['manifest'].get('tracking_imports', []))
    for source in documents:
        matching = [r for r in value['resources'] if Path(r['file']).resolve() == Path(source['file']).resolve()]
        if len(matching) != 1 or matching[0]['expected_sha256'] != source['expected_sha256']:
            raise WorkflowError('INVALID_REQUEST', 'Every media/tracking document requires a matching resource descriptor')
    return value

normalize_params = normalize_run_params

# M3b keeps each legacy schema and its normalized persistent representation exact.
# Movie is an explicit public 1.3 declaration, never a serialized image_sequence.
MOVIE_INTERPRETATION_SCHEMA = obj({'method': {'const': 'BT709_OETF_INVERSE_V1'},
    'coded_orientation': {'const': 'AS_CODED'}, 'audio': {'const': 'IGNORE'}},
    ('method', 'coded_orientation', 'audio'))
MOVIE_MAPPING_SCHEMA = copy.deepcopy(TIME_MAPPING_SCHEMA)
MOVIE_MAPPING_SCHEMA['properties']['source_start'] = {'type': 'integer', 'const': 1}
MOVIE_SCHEMA = obj({'kind': {'const': 'movie'}, 'file': TEXT, 'expected_sha256': SHA,
    'mapping': MOVIE_MAPPING_SCHEMA, 'interpretation': MOVIE_INTERPRETATION_SCHEMA,
    'decoder_runtime': obj({'ffmpeg': FILE, 'ffprobe': FILE}, ('ffmpeg', 'ffprobe'))},
    ('kind', 'file', 'expected_sha256', 'mapping', 'interpretation', 'decoder_runtime'))
M3B_SOURCE_SCHEMA = {'oneOf': [LEGACY_SOURCE_SCHEMA, SEQUENCE_SCHEMA, MOVIE_SCHEMA]}
M3B_BINDING_SCHEMA = {'oneOf': [IMAGE_BINDING_SCHEMA, RLAYERS_BINDING_SCHEMA, SEQUENCE_SCHEMA, MOVIE_SCHEMA]}
M3B_GRAPH_SCHEMA = copy.deepcopy(M3_GRAPH_SCHEMA)
M3B_GRAPH_SCHEMA['properties']['nodes']['items']['properties']['binding'] = M3B_BINDING_SCHEMA
M3B_GRAPH_SCHEMA['properties']['nodes']['items']['allOf'][0]['then']['properties']['binding'] = {
    'oneOf': [IMAGE_BINDING_SCHEMA, SEQUENCE_SCHEMA, MOVIE_SCHEMA]}
M3B_MANIFEST_SCHEMA = copy.deepcopy(M3_MANIFEST_SCHEMA)
M3B_MANIFEST_SCHEMA['properties'].update({'workflow_version': {'const': '1.3'},
    'source': M3B_SOURCE_SCHEMA, 'graph': M3B_GRAPH_SCHEMA})
MANIFEST_SCHEMA = {'oneOf': [LEGACY_MANIFEST_SCHEMA, M3_MANIFEST_SCHEMA, M3B_MANIFEST_SCHEMA]}
RUN_SCHEMA['properties']['manifest'] = MANIFEST_SCHEMA

def _normalize_movie_manifest(value):
    value = copy.deepcopy(value); _json(value); validate(value, M3B_MANIFEST_SCHEMA)
    projected = copy.deepcopy(value); projected['workflow_version'] = '1.2'
    movies = {}
    for key, media in media_sources(projected):
        if media['kind'] != 'movie': continue
        original = copy.deepcopy(media); movies[key] = original
        for row in [original, *original['decoder_runtime'].values()]:
            if not Path(row['file']).is_absolute():
                raise WorkflowError('INVALID_REQUEST', 'Movie/decoder runtime paths must be absolute')
        # Only pure structural validation consumes this virtual sequence. No file read,
        # source bytes, persisted declaration or receipt ever uses the virtual path.
        mapping = original['mapping']
        media.clear(); media.update({'kind': 'image_sequence', 'mapping': copy.deepcopy(mapping),
            'colorspace': 'Linear Rec.709', 'alpha_mode': 'PREMUL',
            'frames': [{'source_frame': index, 'file': str(Path(original['file']).with_name(
                'movie-contract-%s-%06d.exr' % (key.replace('$', ''), index))),
                'expected_sha256': original['expected_sha256']} for index in range(1, mapping['frame_count'] + 1)]})
    result = normalize_manifest(projected)
    result['workflow_version'] = '1.3'
    for key, media in media_sources(result):
        if key not in movies: continue
        normalized_mapping = copy.deepcopy(media['mapping'])
        media.clear(); media.update(movies[key]); media['mapping'] = normalized_mapping
    return result
