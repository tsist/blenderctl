# SPDX-License-Identifier: GPL-3.0-or-later
"""Conservative static surface-node compatibility for independent previews.

This is an explicit supported subset, not Blender's complete node catalogue.
Every node, including disconnected/manual nodes, is checked deliberately.
"""
from .contract import WorkflowError

ENGINES = ('CYCLES', 'BLENDER_EEVEE')
COMMON = frozenset(('NodeFrame', 'NodeReroute', 'NodeGroupInput', 'NodeGroupOutput',
    'ShaderNodeGroup', 'ShaderNodeOutputMaterial', 'ShaderNodeBsdfPrincipled',
    'ShaderNodeBsdfDiffuse', 'ShaderNodeBsdfTransparent', 'ShaderNodeEmission',
    'ShaderNodeMixShader', 'ShaderNodeAddShader', 'ShaderNodeMixRGB', 'ShaderNodeMix',
    'ShaderNodeMath', 'ShaderNodeVectorMath', 'ShaderNodeValToRGB', 'ShaderNodeRGB',
    'ShaderNodeValue', 'ShaderNodeClamp', 'ShaderNodeMapRange',
    'ShaderNodeSeparateColor', 'ShaderNodeCombineColor', 'ShaderNodeSeparateXYZ',
    'ShaderNodeCombineXYZ', 'ShaderNodeInvert', 'ShaderNodeGamma', 'ShaderNodeHueSaturation',
    'ShaderNodeUVMap', 'ShaderNodeMapping', 'ShaderNodeTexImage',
    'ShaderNodeNormalMap', 'ShaderNodeBump'))
EEVEE_ONLY = frozenset(('ShaderNodeShaderToRGB',))

def _engine(engine):
    if engine not in ENGINES:
        raise WorkflowError('unsupported_engine', 'Supported preview engines: CYCLES, BLENDER_EEVEE; got '+str(engine))
    return engine

def validate_tree(tree, engine, *, effect=False, _stack=(), _budget=None):
    """Read-only bounded scan; effect groups must also pass content fingerprinting."""
    _engine(engine)
    if tree is None or tree.bl_idname != 'ShaderNodeTree':
        raise WorkflowError('unsupported_node', 'A ShaderNodeTree is required')
    if tree.library or tree.override_library or tree.animation_data:
        raise WorkflowError('unsupported_node', 'Preview requires local static shader trees: '+tree.name)
    if tree in _stack or len(_stack) >= 8:
        raise WorkflowError('unsupported_node', 'Recursive or excessively deep shader tree: '+tree.name)
    budget = _budget if _budget is not None else [0]
    budget[0] += len(tree.nodes)
    if budget[0] > 1024:
        raise WorkflowError('unsupported_node', 'Material exceeds 1024 total shader nodes')
    found = set()
    for node in tree.nodes:
        kind = node.bl_idname
        if kind in EEVEE_ONLY and engine != 'BLENDER_EEVEE':
            raise WorkflowError('unsupported_node', 'Shader to RGB requires Eevee: '+tree.name+'/'+node.name)
        if kind not in COMMON | (EEVEE_ONLY if engine == 'BLENDER_EEVEE' else frozenset()):
            raise WorkflowError('unsupported_node', 'Node is outside the verified static surface subset: '+tree.name+'/'+node.name+' ('+kind+')')
        if kind == 'ShaderNodeTexImage' and effect:
            raise WorkflowError('unsupported_group', 'Effect texture resources must be managed layer channels')
        if kind == 'ShaderNodeOutputMaterial':
            if node.target not in ('ALL', 'EEVEE' if engine == 'BLENDER_EEVEE' else 'CYCLES'):
                raise WorkflowError('unsupported_node', 'Material output targets another engine: '+node.name)
            for socket in ('Volume', 'Displacement'):
                if node.inputs.get(socket) and node.inputs[socket].is_linked:
                    raise WorkflowError('unsupported_node', 'Preview supports surface output only: '+socket)
        if kind == 'ShaderNodeGroup':
            if node.node_tree is None:
                raise WorkflowError('unsupported_group', 'Missing nested shader group: '+node.name)
            from .core_v2 import group_fingerprint
            group_fingerprint(node.node_tree)
            found.update(validate_tree(node.node_tree, engine, effect=True,
                _stack=_stack+(tree,), _budget=budget)['node_types'])
        found.add(kind)
    return {'engine': engine, 'node_types': sorted(found), 'scope': 'static_surface_subset'}

def validate_group(group, engine):
    from .core_v2 import group_fingerprint
    group_fingerprint(group)
    return validate_tree(group, engine, effect=True)

def validate_material(material, engine):
    _engine(engine)
    if material is None: return {'engine':engine,'node_types':[],'scope':'empty_slot'}
    if material.library or material.override_library or material.animation_data:
        raise WorkflowError('unsupported_node', 'Preview requires local static material: '+material.name)
    if not material.use_nodes: return {'engine':engine,'node_types':[],'scope':'diffuse_material'}
    return validate_tree(material.node_tree, engine)

def validate_manifest(manifest):
    import bpy
    engine = _engine(manifest.get('preview', {}).get('engine', 'CYCLES'))
    reports = []
    for layer in manifest['layers']:
        spec = layer.get('effect')
        if spec:
            group = bpy.data.node_groups.get(spec['group'])
            if group is None: raise WorkflowError('missing_group', 'Effect group missing: '+spec['group'])
            reports.append(validate_group(group, engine))
    return {'engine':engine,'effects':reports,'scope':'static_surface_subset'}
