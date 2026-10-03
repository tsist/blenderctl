# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded node authoring with explicit resource ownership and candidate verification."""
import json,math,hashlib
from contextlib import ExitStack
from pathlib import Path
import bpy
from protocol import Failure,atomic_json,digest
from filesystem import FileGuard
from inspection import all_ids,identity,value,path_value
import scenes,modeling
from node_contract import normalize,NODE_TYPES,MODEL_OPS,SCENE_OPS

RESOURCE_KEY='blenderctl_image_manifest'
PROP_ALLOW={
 'ShaderNodeMath':{'operation','use_clamp'},'ShaderNodeVectorMath':{'operation'},
 'ShaderNodeMixRGB':{'blend_type','use_clamp'},'ShaderNodeMix':{'data_type','blend_type','clamp_factor','clamp_result','factor_mode'},
 'ShaderNodeTexImage':{'interpolation','projection','extension'},'ShaderNodeTexEnvironment':{'interpolation','projection'},
 'ShaderNodeSeparateColor':{'mode'},'ShaderNodeCombineColor':{'mode'},'ShaderNodeTexNoise':{'noise_dimensions','normalize'},
 'ShaderNodeUVMap':{'uv_map'},'ShaderNodeNormalMap':{'space','uv_map'},'ShaderNodeMapping':{'vector_type'},
 'ShaderNodeBump':{'invert'},'CompositorNodeRLayers':{'layer'},
}

def checked(item,attr,v):
    p=item.bl_rna.properties.get(attr)
    if p is None or p.is_readonly:raise Failure('UNSUPPORTED','Read-only or unknown RNA setting: '+attr)
    if p.type=='ENUM':
        if not isinstance(v,str) or v not in {i.identifier for i in p.enum_items}:raise Failure('INVALID_REQUEST','Invalid enum for '+attr)
    elif p.type=='BOOLEAN':
        if type(v) is not bool:raise Failure('INVALID_REQUEST','Boolean required for '+attr)
    elif p.type in ('FLOAT','INT'):
        vals=v if p.is_array else [v]
        if not isinstance(vals,list) or p.is_array and len(vals)!=p.array_length:raise Failure('INVALID_REQUEST','Wrong vector length for '+attr)
        if any(type(x) not in (int,float) or not p.hard_min<=x<=p.hard_max or not math.isfinite(x) or p.type=='INT' and type(x) is not int for x in vals):raise Failure('INVALID_REQUEST','Numeric setting outside RNA range: '+attr)
    elif p.type=='STRING':
        if not isinstance(v,str) or '\x00' in v or len(v)>1024:raise Failure('INVALID_REQUEST','Invalid string setting')
    elif p.type=='POINTER':
        if not isinstance(v,bpy.types.ID):raise Failure('INVALID_REQUEST','ID reference required')
    else:raise Failure('UNSUPPORTED','Unsupported setting type: '+p.type)
    try:setattr(item,attr,v)
    except (TypeError,ValueError) as exc:raise Failure('INVALID_REQUEST',str(exc)) from exc
    if scenes.compare(value(getattr(item,attr)),value(v)):raise Failure('INVALID_REQUEST','Blender changed requested setting: '+attr)

def id_value(v):
    if isinstance(v,dict):return scenes.find({'Material':bpy.data.materials,'Object':bpy.data.objects,'Collection':bpy.data.collections,'Image':bpy.data.images}[v['id_type']],v['name'])
    return v

def mutable(item,scope='shared'):
    if item.library or item.override_library or getattr(item,'animation_data',None):raise Failure('UNSUPPORTED','Node editing requires local unanimated owners')
    if scope=='reject_shared' and item.users>1:raise Failure('CONFLICT','Shared owner requires explicit shared scope or an independent copy')

def tree_for(ref):
    owner=scenes.find({'MATERIAL':bpy.data.materials,'WORLD':bpy.data.worlds,'GROUP':bpy.data.node_groups}[ref['kind']],ref['name']);mutable(owner,ref['scope'])
    tree=owner if ref['kind']=='GROUP' else owner.node_tree
    if tree is None:raise Failure('UNSUPPORTED','Owner has no enabled node tree')
    mutable(tree)
    if len(tree.nodes)>500:raise Failure('UNSUPPORTED','Node tree exceeds 500 nodes')
    return tree

def socket_find(sockets,ref):
    field='identifier' if 'identifier' in ref else 'name';matches=[s for s in sockets if getattr(s,field)==ref[field]]
    if len(matches)!=1:raise Failure('CONFLICT' if matches else 'NOT_FOUND','Socket must resolve uniquely: '+str(ref))
    s=matches[0]
    if getattr(s,'is_unavailable',False) or hasattr(s,'enabled') and not s.enabled:raise Failure('UNSUPPORTED','Socket is unavailable in current node configuration')
    return s

def acyclic(graph):
    done=set()
    def visit(node,trail):
        if node in trail:raise Failure('CONFLICT','Node/group cycle')
        if len(trail)>100:raise Failure('UNSUPPORTED','Node hierarchy exceeds 100 levels')
        if node in done:return
        for n in graph.get(node,[]):visit(n,trail|{node})
        done.add(node)
    for node in graph:visit(node,set())

def group_cycle(tree,group):
    groups=list(bpy.data.node_groups)+[tree]
    graph={g.as_pointer():[n.node_tree.as_pointer() for n in g.nodes if getattr(n,'node_tree',None)] for g in groups}
    graph.setdefault(tree.as_pointer(),[]).append(group.as_pointer());acyclic(graph)

def image_members(image):
    raw=image.get(RESOURCE_KEY)
    if raw:
        try:return json.loads(raw)
        except (ValueError,TypeError) as exc:raise Failure('INVALID_REQUEST','Invalid stored image manifest') from exc
    return []

def resource_guards(params,stack):
    declared={str(Path(f['file']).resolve()):f['expected_sha256'] for f in params.get('resources',[])};checked_files={}
    for image in bpy.data.images:
        if image.type in ('RENDER_RESULT','COMPOSITING'):continue
        if image.source not in ('FILE','TILED','SEQUENCE','GENERATED'):raise Failure('UNSUPPORTED','Image source requires another media workflow: '+image.source)
        if image.source=='FILE' and not image.filepath and not image.packed_file:raise Failure('INVALID_REQUEST','Image has no file or packed contents')
        members=image_members(image)
        if image.source=='FILE' and not members and image.filepath:
            p=path_value(image.filepath,image.library);expected=declared.get(str(Path(p).resolve()))
            if not image.packed_file and expected is None:raise Failure('INVALID_REQUEST','Existing external image requires resources file/hash: '+p)
            members=[{'file':p,'expected_sha256':expected,'number':0}]
        packed={p.tile_number for p in image.packed_files if p.packed_file}
        if image.source=='TILED' and not members:raise Failure('INVALID_REQUEST','Existing tiled image requires an authored explicit member manifest')
        if image.source=='SEQUENCE' and not members:raise Failure('INVALID_REQUEST','Existing sequence requires an authored explicit member manifest')
        if members and image.source=='FILE' and not image.packed_file:
            if len(members)!=1 or members[0]['number']!=0 or Path(path_value(image.filepath,image.library)).resolve()!=Path(members[0]['file']).resolve():raise Failure('CONFLICT','Image path differs from stored member manifest')
        if image.source=='TILED':
            if {t.number for t in image.tiles}!={m['number'] for m in members}:raise Failure('CONFLICT','UDIM tiles differ from declared members')
            external=[m for m in members if m['number'] not in packed]
            if external:
                template=path_value(image.filepath,image.library);token='<udim>' if '<udim>' in template else '<UDIM>'
                if template.count(token)!=1 or any(Path(template.replace(token,str(m['number']))).resolve()!=Path(m['file']).resolve() for m in external):raise Failure('CONFLICT','UDIM path differs from declared member')
        for member in members:
            if image.source=='FILE' and image.packed_file or image.source=='TILED' and member['number'] in packed:continue
            p=str(Path(member['file']).resolve());expected=declared.get(p,member.get('expected_sha256'))
            if p in checked_files:
                if checked_files[p]!=expected:raise Failure('CONFLICT','Conflicting image fingerprints')
                continue
            guard=stack.enter_context(FileGuard(p));actual=guard.sha256()
            if expected is None or actual!=expected:raise Failure('CONFLICT','Texture fingerprint differs: '+p)
            checked_files[p]=actual
    return checked_files

def image_load(op):
    scenes.fresh(bpy.data.images,op['name'])
    dimensions=[]
    for f in op['files']:
        # Also validate decoding, not only byte existence or hash.
        test=bpy.data.images.load(f['file'],check_existing=False)
        try:
            size=list(test.size)
            if min(size)<1 or size[0]*size[1]>16777216 or not len(test.pixels):raise Failure('UNSUPPORTED','Invalid or over-16MP image')
            dimensions.append(size)
        finally:bpy.data.images.remove(test)
    if any(d!=dimensions[0] for d in dimensions):raise Failure('UNSUPPORTED','Image members must use identical dimensions')
    image=scenes.named(bpy.data.images.load(op['files'][0]['file'],check_existing=False),op['name'])
    if op['source']=='TILED':
        image.source='TILED';image.filepath=op['template']
        # Blender may auto-discover neighboring tiles; the explicit list is authoritative.
        wanted={f['number'] for f in op['files']}
        for tile in list(image.tiles):
            if tile.number not in wanted:image.tiles.remove(tile)
        existing={t.number for t in image.tiles}
        for n in sorted(wanted-existing):image.tiles.new(n)
    elif op['source']=='SEQUENCE':image.source='SEQUENCE'
    checked(image.colorspace_settings,'name',op['colorspace']);checked(image,'alpha_mode',op['alpha_mode'])
    image[RESOURCE_KEY]=json.dumps(op['files'],sort_keys=True)
    if op['pack']:
        image.pack()
        if op['source']=='FILE' and not image.packed_file:raise Failure('VALIDATION_FAILED','Image did not pack')
        if op['source']=='TILED' and {p.tile_number for p in image.packed_files if p.packed_file}!={f['number'] for f in op['files']}:raise Failure('VALIDATION_FAILED','Not every declared UDIM tile packed')
        expected={f['number']:f['expected_sha256'] for f in op['files']}
        actual={0:hashlib.sha256(image.packed_file.data).hexdigest()} if op['source']=='FILE' else {p.tile_number:hashlib.sha256(p.packed_file.data).hexdigest() for p in image.packed_files}
        if actual!=expected:raise Failure('VALIDATION_FAILED','Packed image bytes differ from declared inputs')

def sequence_bindings(frame):
    from dependencies import sequence_frame
    seen=set();evidence=[]
    for item in all_ids():
        tree=item if isinstance(item,bpy.types.NodeTree) else getattr(item,'node_tree',None)
        if not tree or tree.as_pointer() in seen:continue
        seen.add(tree.as_pointer())
        for node in tree.nodes:
            image=getattr(node,'image',None);user=getattr(node,'image_user',None)
            if not image or image.source!='SEQUENCE' or user is None:continue
            if tree.animation_data:raise Failure('UNSUPPORTED','Animated sequence bindings require an animation workflow')
            members={m['number']:m for m in image_members(image)};number=sequence_frame(user,frame)
            if number not in members:raise Failure('CONFLICT','Sequence frame resolves outside declared members')
            old=user.frame_current
            try:
                user.frame_current=number;resolved=path_value(image.filepath_from_user(image_user=user),image.library)
                if str(Path(resolved).resolve())!=str(Path(members[number]['file']).resolve()):raise Failure('CONFLICT','Sequence path differs from declared member')
            finally:user.frame_current=old
            evidence.append({'tree':tree.name,'node':node.name,'scene_frame':frame,'image_frame':number,'file':resolved})
    return evidence

def execute(op,context,deformation_guard=False):
    kind=op['op']
    if kind in MODEL_OPS or kind in SCENE_OPS:modeling.execute(op,context);return
    if kind=='material.create':
        scenes.fresh(bpy.data.materials,op['name']);material=scenes.named(bpy.data.materials.new(op['name']),op['name']);material.use_nodes=True
        if op['preset']=='EMPTY':material.node_tree.nodes.clear()
    elif kind=='material.copy':
        source=scenes.find(bpy.data.materials,op['material']);mutable(source)
        if source.asset_data or source.get('asset_id'):raise Failure('UNSUPPORTED','Cannot duplicate material asset identity')
        scenes.fresh(bpy.data.materials,op['name']);scenes.named(source.copy(),op['name'])
    elif kind in ('material.assign','material.faces'):
        if deformation_guard:
            from material_deformation import editable
            item=editable(op)
        else:item=modeling.editable(op)
        slots=item.data.materials
        if kind=='material.assign':
            mat=scenes.find(bpy.data.materials,op['material'])
            if op['mode']=='append':
                if op['slot']!=len(slots):raise Failure('CONFLICT','Append slot must equal current slot count')
                slots.append(mat)
            else:
                if op['slot']>=len(slots):raise Failure('NOT_FOUND','Material slot missing')
                if item.material_slots[op['slot']].link!='DATA':raise Failure('UNSUPPORTED','Object-linked material slot requires a separate policy')
                slots[op['slot']]=mat
        else:
            if op['slot']>=len(slots) or slots[op['slot']] is None:raise Failure('NOT_FOUND','Material slot missing')
            for face in modeling.select(item.data,item.data.polygons,op['selection']):face.material_index=op['slot']
        item.data.update()
    elif kind=='world.assign':scenes.find(bpy.data.scenes,op['scene'],True).world=scenes.find(bpy.data.worlds,op['world'])
    elif kind=='tree.create':
        scenes.fresh(bpy.data.node_groups,op['name']);scenes.named(bpy.data.node_groups.new(op['name'],op['type']),op['name'])
    elif kind.startswith('zone.'):
        import node_zones
        node_zones.execute(op)
    elif kind=='tree.interface':
        tree=tree_for(op['tree'])
        if op['tree']['kind']!='GROUP':raise Failure('UNSUPPORTED','Interface belongs to standalone node groups')
        if len(tree.interface.items_tree)>=64:raise Failure('UNSUPPORTED','Group interface limit 64')
        if any(s.item_type=='SOCKET' and s.in_out==op['direction'] and s.name==op['name'] for s in tree.interface.items_tree):raise Failure('CONFLICT','Duplicate interface socket name')
        s=tree.interface.new_socket(name=op['name'],in_out=op['direction'],socket_type=op['socket_type'])
        if s.socket_type!=op['socket_type']:raise Failure('UNSUPPORTED','Blender changed interface socket type')
        if 'default' in op:checked(s,'default_value',id_value(op['default']))
    elif kind.startswith('node.') or kind=='image.sequence':
        tree=tree_for(op['tree'])
        if kind=='node.add':
            if op['type'] not in NODE_TYPES[tree.bl_idname]:raise Failure('INVALID_REQUEST','Node type incompatible with tree')
            scenes.fresh(tree.nodes,op['name']);scenes.named(tree.nodes.new(op['type']),op['name'])
        elif kind=='node.link':
            a=scenes.find(tree.nodes,op['from_node']);b=scenes.find(tree.nodes,op['to_node']);output=socket_find(a.outputs,op['from_socket']);target=socket_find(b.inputs,op['to_socket'])
            if target.is_multi_input:raise Failure('UNSUPPORTED','Multi-input link ordering not supported')
            if b.bl_idname=='GeometryNodeRepeatInput' and target.identifier=='Iterations':raise Failure('UNSUPPORTED','Repeat iterations must be an explicit bounded constant')
            if target.is_linked and not op['replace']:raise Failure('CONFLICT','Input already linked; explicit replace required')
            if output.type!=target.type:raise Failure('UNSUPPORTED','Implicit socket type conversion is not supported')
            graph={n.name:[] for n in tree.nodes}
            for link in tree.links:
                if link.to_socket!=target:graph[link.from_node.name].append(link.to_node.name)
            graph[a.name].append(b.name);acyclic(graph)
            link=tree.links.new(output,target)
            if not link.is_valid:raise Failure('VALIDATION_FAILED','Blender rejected node link')
        else:
            node=scenes.find(tree.nodes,op['node'])
            if kind=='node.remove':
                from node_zones import TYPES
                if node.bl_idname in TYPES:raise Failure('UNSUPPORTED','Use paired zone.remove for zone endpoints')
                tree.nodes.remove(node)
            elif kind in ('node.input','node.output'):
                if kind=='node.output' and node.bl_idname not in ('ShaderNodeValue','ShaderNodeRGB','CompositorNodeRGB'):raise Failure('UNSUPPORTED','Output defaults only supported for Value/RGB constant nodes')
                s=socket_find(node.inputs if kind=='node.input' else node.outputs,op['socket'])
                if kind=='node.input' and s.is_linked:raise Failure('CONFLICT','Unlink input before changing its default')
                if node.bl_idname=='GeometryNodeRepeatInput' and s.identifier=='Iterations' and (type(op['value']) is not int or not 1<=op['value']<=1000):raise Failure('UNSUPPORTED','Repeat iterations must be an integer 1..1000')
                checked(s,'default_value',id_value(op['value']))
            elif kind=='node.unlink':
                s=socket_find(node.inputs,op['socket'])
                for link in list(s.links):tree.links.remove(link)
            elif kind=='node.properties':
                allowed=PROP_ALLOW.get(node.bl_idname,set())
                if set(op['properties'])-allowed:raise Failure('INVALID_REQUEST','Setting not allowed for this node type')
                for k,v in op['properties'].items():checked(node,k,v)
            elif kind=='node.resource':
                attr=op['property']
                if attr=='node_tree':
                    if node.bl_idname not in ('ShaderNodeGroup','GeometryNodeGroup','CompositorNodeGroup'):raise Failure('INVALID_REQUEST','Group node required')
                    group=scenes.find(bpy.data.node_groups,op['name'])
                    if group.bl_idname!=tree.bl_idname:raise Failure('INVALID_REQUEST','Group tree types differ')
                    group_cycle(tree,group);node.node_tree=group
                elif attr=='image':
                    if node.bl_idname not in ('ShaderNodeTexImage','ShaderNodeTexEnvironment','CompositorNodeImage'):raise Failure('INVALID_REQUEST','Image node required')
                    node.image=scenes.find(bpy.data.images,op['name'])
                    if node.image.source=='SEQUENCE':
                        node.image_user.frame_start=1;node.image_user.frame_offset=0;node.image_user.frame_duration=len(image_members(node.image));node.image_user.use_cyclic=False;node.image_user.use_auto_refresh=True
                else:
                    if node.bl_idname!='CompositorNodeRLayers':raise Failure('INVALID_REQUEST','Render layers node required')
                    node.scene=scenes.find(bpy.data.scenes,op['name'])
            elif kind=='node.ramp':
                if node.bl_idname!='ShaderNodeValToRGB':raise Failure('INVALID_REQUEST','Color ramp node required')
                ramp=node.color_ramp
                while len(ramp.elements)>2:ramp.elements.remove(ramp.elements[-1])
                for i,element in enumerate(op['elements']):
                    e=ramp.elements[i] if i<2 else ramp.elements.new(element['position']);e.position=element['position'];e.color=element['color']
                ramp.interpolation=op['interpolation']
            elif kind=='image.sequence':
                image=getattr(node,'image',None)
                if not image or image.source!='SEQUENCE':raise Failure('INVALID_REQUEST','Sequence image node required')
                if op['frame_duration']!=len(image_members(image)):raise Failure('INVALID_REQUEST','Duration must match declared sequence members')
                user=node.image_user
                for prop,key in [('frame_start','frame_start'),('frame_offset','frame_offset'),('frame_duration','frame_duration'),('use_cyclic','cyclic')]:checked(user,prop,op[key])
                user.use_auto_refresh=True
        tree.update_tag()
    elif kind=='geometry.bind':
        item=scenes.find(bpy.data.objects,op['object'],True);mutable(item);group=scenes.find(bpy.data.node_groups,op['group'])
        if item.type!='MESH' or group.bl_idname!='GeometryNodeTree':raise Failure('INVALID_REQUEST','Geometry modifier requires mesh and geometry group')
        scenes.fresh(item.modifiers,op['name']);mod=scenes.named(item.modifiers.new(op['name'],'NODES'),op['name']);mod.node_group=group
    elif kind=='geometry.input':
        item=scenes.find(bpy.data.objects,op['object'],True);mutable(item);mod=scenes.find(item.modifiers,op['modifier'])
        if mod.type!='NODES' or not mod.node_group:raise Failure('INVALID_REQUEST','Geometry node modifier required')
        sockets=[s for s in mod.node_group.interface.items_tree if s.item_type=='SOCKET' and s.in_out=='INPUT'];s=socket_find(sockets,op['socket'])
        if s.socket_type=='NodeSocketGeometry':raise Failure('UNSUPPORTED','Cannot set geometry interface default')
        # Blender 5.2 stores modifier socket values in typed RNA properties.
        prop=getattr(mod.properties.inputs,s.identifier)
        if hasattr(prop,'type') and prop.type!='VALUE':raise Failure('UNSUPPORTED','Attribute-bound modifier inputs require an explicit field workflow')
        checked(prop,'value',id_value(op['value']));item.update_tag()
    elif kind=='compositor.bind':
        scene=scenes.find(bpy.data.scenes,op['scene'],True);group=scenes.find(bpy.data.node_groups,op['group'])
        if group.bl_idname!='CompositorNodeTree':raise Failure('INVALID_REQUEST','Compositor tree required')
        scene.compositing_node_group=group;scene.render.use_compositing=op['enabled']
    elif kind=='image.load':image_load(op)
    elif kind=='image.configure':
        image=scenes.find(bpy.data.images,op['image']);mutable(image,op['scope']);checked(image.colorspace_settings,'name',op['colorspace']);checked(image,'alpha_mode',op['alpha_mode'])
    else:raise Failure('INVALID_REQUEST','Unknown node operation')
    import node_zones
    node_zones.validate_all(False)
    scenes.update()

def state():
    import node_zones
    node_zones.validate_all(False)
    return {'node_report_version':'1.0','model':modeling.state(),'zones':node_zones.snapshot(),
        'material_bindings':[{'object':o.name,'slots':[{'material':s.material.name if s.material else None,'link':s.link} for s in o.material_slots],'face_material_indices':[p.material_index for p in o.data.polygons]} for o in sorted(bpy.data.objects,key=lambda o:o.name) if o.type=='MESH'],
        'images':[{'name':i.name,'source':i.source,'colorspace':i.colorspace_settings.name,'alpha_mode':i.alpha_mode,'members':image_members(i),'tiles':sorted(t.number for t in i.tiles) if i.source=='TILED' else []} for i in sorted(bpy.data.images,key=lambda i:i.name) if i.type not in ('RENDER_RESULT','COMPOSITING')],
        'compositors':[{'scene':s.name,'group':s.compositing_node_group.name if s.compositing_node_group else None,'enabled':s.render.use_compositing} for s in sorted(bpy.data.scenes,key=lambda s:s.name)],
        'geometry_inputs':[{'object':o.name,'modifier':m.name,'group':m.node_group.name,'inputs':[{'identifier':s.identifier,'name':s.name,'value':value(getattr(m.properties.inputs,s.identifier).value),'mode':getattr(getattr(m.properties.inputs,s.identifier),'type',None)} for s in m.node_group.interface.items_tree if s.item_type=='SOCKET' and s.in_out=='INPUT' and hasattr(getattr(m.properties.inputs,s.identifier,None),'value')]} for o in sorted(bpy.data.objects,key=lambda o:o.name) for m in o.modifiers if m.type=='NODES' and m.node_group],
        'coverage':'declared_node_fields_and_interfaces_material_slots_images_and_active_view_layer_geometry; rendering/baking_separate'}

def inspect(params,job):
    bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    if 'context' in params:scenes.activate(params['context'])
    report=state();atomic_json(job/'node-report.json',report);return report

def prepare(params,job):
    manifest=normalize(params['manifest'])
    deformation=manifest.get('deformation_guard')
    uv_tolerance=deformation.get('evaluated_uv_tolerance',0) if deformation else 0
    if deformation and not params.get('file'):raise Failure('INVALID_REQUEST','deformation_guard requires an input file')
    if params.get('file'):
        bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
        if tuple(bpy.data.version[:2])!=tuple(bpy.app.version[:2]):raise Failure('UNSUPPORTED','Node edits require matching Blender major/minor')
    else:bpy.ops.wm.read_factory_settings(use_empty=True);scenes.named(bpy.context.scene,manifest.get('initial_scene','Scene'))
    if bpy.context.mode!='OBJECT':raise Failure('UNSUPPORTED','Object mode required')
    with ExitStack() as guards:
        source_resources=resource_guards(params,guards)
        import node_zones
        node_zones.validate_all()
        node_zones.detach_caches(job)
        if deformation:
            import material_deformation
            scenes.activate(manifest['context'])
            deformation_before=material_deformation.snapshot(deformation['frames'],uv_tolerance)
            atomic_json(job/'deformation-before.json',deformation_before)
        atomic_json(job/'node-before.json',state());actions=[]
        for index,op in enumerate(manifest['operations']):
            scenes.prepare_context(manifest['context'])
            try:execute(op,manifest['context'],bool(deformation))
            except Failure as exc:raise Failure(exc.code,f'Operation {index} ({op["op"]}): {exc}') from exc
            actions.append({'index':index,'op':op['op'],'status':'applied_in_candidate_memory'});atomic_json(job/'node-actions.json',actions)
        node_zones.validate_all()
        scenes.activate(manifest['context']);sequence_evidence=sequence_bindings(manifest['context']['frame']);retained=[]
        if deformation:
            in_memory=material_deformation.snapshot(deformation['frames'],uv_tolerance)
            atomic_json(job/'deformation-in-memory.json',in_memory)
            in_memory_uv_error=material_deformation.verify(deformation_before,in_memory,uv_tolerance)
        for item in all_ids():
            if not item.library and not item.is_embedded_data and item.users==0 and not item.use_fake_user:item.use_fake_user=True;retained.append(identity(item))
        expected=state();atomic_json(job/'node-expected.json',expected);candidate=job/'node-candidate.blend'
        if candidate.exists():raise Failure('CONFLICT','Candidate already exists')
        bpy.context.preferences.filepaths.save_version=0;bpy.ops.wm.save_as_mainfile(filepath=str(candidate),copy=True,relative_remap=True,check_existing=False)
        bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False);scenes.activate(manifest['context']);observed=state();atomic_json(job/'node-report.json',observed)
        mismatch=scenes.compare(expected,observed)
        if mismatch:raise Failure('VALIDATION_FAILED','Node candidate reopen mismatch: '+mismatch)
        if deformation:
            deformation_after=material_deformation.snapshot(deformation['frames'],uv_tolerance)
            atomic_json(job/'deformation-after.json',deformation_after)
            reopen_uv_error=material_deformation.verify(deformation_before,deformation_after,uv_tolerance)
            atomic_json(job/'deformation-preservation.json',{'ok':True,'frames':deformation['frames'],'evaluated_uv_tolerance':uv_tolerance,'max_evaluated_uv_error':max(in_memory_uv_error,reopen_uv_error),'base_uv':'exact_hash','scope':'all mesh base coordinates, topology, UV, weights, shape keys, object/rig/action records and active-view-layer evaluated samples; sampled frames only','reopen':'pass'})
        atomic_json(job/'node-change.json',{'operations':manifest['operations'],'context':manifest['context'],'retained_orphans':retained,'source_resources':source_resources,'sequence_bindings':sequence_evidence,'reopen':'pass'})
        return {'candidate':str(candidate),'candidate_sha256':digest(candidate),'operations':len(actions),'report':str(job/'node-report.json'),'context':manifest['context'],'reopen':'pass','publication':'working_candidate_only','next_step':'project plan-copy/plan-files; transaction apply; node inspect and material preview/texture bake'}
