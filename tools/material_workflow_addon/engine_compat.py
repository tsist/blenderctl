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
EEVEE_MATERIAL_LIGHTING = frozenset(('ShaderNodeLightInfo', 'ShaderNodeLightEvaluation',
    'ShaderNodeLightAccumulation', 'ShaderNodeShadowRaycast'))
PROCEDURAL = frozenset(('ShaderNodeTexNoise', 'ShaderNodeTexWave', 'ShaderNodeTexVoronoi',
    'ShaderNodeTexCoord', 'ShaderNodeNewGeometry', 'ShaderNodeVectorTransform'))

def _engine(engine):
    if engine not in ENGINES:
        raise WorkflowError('unsupported_engine', 'Supported preview engines: CYCLES, BLENDER_EEVEE; got '+str(engine))
    return engine

def validate_tree(tree, engine, *, effect=False, _stack=(), _budget=None, _procedural=False):
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
    allowed = COMMON | (PROCEDURAL if _procedural else frozenset())
    for node in tree.nodes:
        kind = node.bl_idname
        light_space = (kind == 'ShaderNodeVectorTransform'
                       and 'LIGHT' in (node.convert_from, node.convert_to))
        light_attribute = kind == 'ShaderNodeAttribute' and node.attribute_type == 'LIGHT'
        material_lighting = kind in EEVEE_MATERIAL_LIGHTING or light_space or light_attribute
        eevee_only = kind in EEVEE_ONLY or material_lighting
        if eevee_only and engine != 'BLENDER_EEVEE':
            requirement = 'Material lighting requires Eevee: ' if material_lighting else 'Shader to RGB requires Eevee: '
            raise WorkflowError('unsupported_node', requirement+tree.name+'/'+node.name)
        if kind not in allowed and not eevee_only:
            raise WorkflowError('unsupported_node', 'Node is outside the verified static surface subset: '+tree.name+'/'+node.name+' ('+kind+')')
        if kind == 'ShaderNodeTexImage' and effect and not _procedural:
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
            if not _procedural:
                from .core_v2 import group_fingerprint
                group_fingerprint(node.node_tree)
            found.update(validate_tree(node.node_tree, engine, effect=True,
                _stack=_stack+(tree,), _budget=budget, _procedural=_procedural)['node_types'])
        found.add(kind)
    return {'engine': engine, 'node_types': sorted(found),
            'scope': 'managed_procedural_surface' if _procedural else 'static_surface_subset'}

def validate_group(group, engine):
    from .core_v2 import group_fingerprint
    group_fingerprint(group)
    return validate_tree(group, engine, effect=True)

def validate_channel_material(material, engine, mesh, uv_layer):
    """Check a declared terminal graph before baking or moving it into a studio scene."""
    from . import procedural_bake, procedural_core

    if material is None or material.override_library:
        raise WorkflowError('bake_source', 'Channel export needs a local material')
    # A custom preview never substitutes for a managed graph's integrity contract.
    if any(key in material for key in (procedural_core.KEY, procedural_core.STATE,
                                       procedural_core.MANIFEST, 'mwp_group_name')):
        try:
            manifest = procedural_core.get_stored_manifest(material)
        except (ValueError, TypeError, KeyError) as error:
            raise WorkflowError('managed_conflict', 'Managed channel author metadata is invalid') from error
        if material.get(procedural_core.KEY) != manifest['material']['id']:
            raise WorkflowError('managed_conflict', 'Managed channel author identity differs from its recipe')
    procedural_bake._material_contract(material)
    result = validate_tree(material.node_tree, engine, _procedural=True)
    active_uv = next((layer.name for layer in mesh.uv_layers if layer.active_render), None)
    trees = [material.node_tree]
    visited = set()
    while trees:
        tree = trees.pop()
        if tree in visited:
            continue
        visited.add(tree)
        for node in tree.nodes:
            kind = node.bl_idname
            if kind == 'ShaderNodeGroup':
                trees.append(node.node_tree)
            if kind == 'ShaderNodeTexCoord' and node.object and node.outputs['Object'].is_linked:
                raise WorkflowError('bake_context', 'External object coordinates cannot move with an isolated preview')
            if kind == 'ShaderNodeUVMap' and node.from_instancer:
                raise WorkflowError('bake_context', 'Instancer UVs require the original instance context')
            if kind in ('ShaderNodeUVMap', 'ShaderNodeNormalMap') and node.uv_map and not mesh.uv_layers.get(node.uv_map):
                raise WorkflowError('bake_context', 'Referenced UV map is missing: ' + node.uv_map)
            implicit_uv = ((kind == 'ShaderNodeTexCoord' and node.outputs['UV'].is_linked)
                           or (kind == 'ShaderNodeUVMap' and not node.uv_map)
                           or (kind == 'ShaderNodeTexImage' and not node.inputs['Vector'].is_linked)
                           or (kind == 'ShaderNodeNormalMap' and node.space == 'TANGENT' and not node.uv_map))
            if implicit_uv and active_uv != uv_layer:
                raise WorkflowError('bake_context', 'Implicit UV input would change when activating the requested bake layer')
            if kind == 'ShaderNodeNewGeometry' and any(s.is_linked for s in node.outputs):
                raise WorkflowError('bake_context', 'Geometry shader inputs need their original shading context')
            if kind == 'ShaderNodeVectorTransform' and (node.convert_from != 'OBJECT' or node.convert_to != 'OBJECT'):
                raise WorkflowError('bake_context', 'World, light or camera transforms cannot move with an isolated preview')
            if kind in EEVEE_MATERIAL_LIGHTING or kind in ('ShaderNodeShaderToRGB', 'ShaderNodeAttribute'):
                raise WorkflowError('bake_context', 'Lighting signals cannot be exported as static PBR channels')
            if node.mute and kind not in ('NodeFrame', 'NodeReroute'):
                raise WorkflowError('bake_source', 'Muted nodes require their effective signals to be made explicit')
    result['scope'] = 'validated_channel_terminals'
    return result

def validate_material(material, engine):
    _engine(engine)
    if material is None: return {'engine':engine,'node_types':[],'scope':'empty_slot'}
    if material.library or material.override_library or material.animation_data:
        raise WorkflowError('unsupported_node', 'Preview requires local static material: '+material.name)
    if not material.use_nodes: return {'engine':engine,'node_types':[],'scope':'diffuse_material'}
    if 'mwp_state' in material or 'mwp_manifest' in material:
        from .procedural_core import get_stored_manifest
        # Validate the real graph, images and exposed parameter types. A tag alone
        # must never enable the additional procedural preview node subset.
        get_stored_manifest(material)
        return validate_tree(material.node_tree, engine, _procedural=True)
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
