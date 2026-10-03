# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned disk bakes, complete cache membership and reverse-frame verification."""
import hashlib,json,math,shutil,time
from pathlib import Path
from contextlib import ExitStack
import bpy
from protocol import Failure,atomic_json,read_json,digest
from inspection import node_content,value
from filesystem import FileGuard
import nodes,node_zones,scenes,modeling
from zone_cache_contract import normalize,REQUEST
def fingerprint(v):return hashlib.sha256(json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def implementation():return fingerprint({p.name:digest(p) for p in sorted(Path(__file__).parent.glob('*.py'))})
def signature(spec):return {k:spec[k] for k in REQUEST['required']}
def tree_closure(tree):
    found={}
    def visit(t,trail):
        if t.as_pointer() in trail:raise Failure('UNSUPPORTED','Recursive geometry group')
        if len(trail)>16:raise Failure('UNSUPPORTED','Geometry group depth exceeds 16')
        if t.library or t.override_library or t.animation_data:raise Failure('UNSUPPORTED','Cache groups must be local and unanimated')
        found[t.name]=node_content(t)
        from node_contract import NODE_TYPES
        for n in t.nodes:
            if n.bl_idname not in set(NODE_TYPES['GeometryNodeTree'])|node_zones.TYPES:raise Failure('UNSUPPORTED','Unmanaged geometry node in cache: '+n.bl_idname)
            if n.bl_idname=='GeometryNodeMeshCube':
                dims=[n.inputs[k] for k in ('Vertices X','Vertices Y','Vertices Z')]
                if any(s.is_linked or not 2<=s.default_value<=64 for s in dims):raise Failure('UNSUPPORTED','Cache mesh-cube subdivisions must be constant 2..64')
                a,b,c=[s.default_value for s in dims]
                if 2*(a*b+a*c+b*c)>20000:raise Failure('UNSUPPORTED','Procedural cube exceeds cache geometry budget')
            if n.bl_idname=='GeometryNodeStoreNamedAttribute' and (n.domain!='POINT' or n.data_type not in ('FLOAT','FLOAT_VECTOR')):raise Failure('UNSUPPORTED','Cached named attributes are POINT FLOAT/FLOAT_VECTOR')
            child=getattr(n,'node_tree',None)
            if child:
                if any(x.bl_idname in node_zones.TYPES for x in child.nodes):raise Failure('UNSUPPORTED','Nested group zones need a separate work and bake-ID adapter')
                visit(child,trail|{t.as_pointer()})
    visit(tree,set());return found
def preflight(params,spec):
    from rigging import animation_safe
    animation_safe()
    if bpy.data.libraries or len(bpy.data.scenes)!=1:raise Failure('UNSUPPORTED','Geometry zone cache requires one local scene')
    node_zones.validate_all()
    s=scenes.find(bpy.data.scenes,spec['scene']);layer=scenes.find(s.view_layers,spec['view_layer'])
    if s.rigidbody_world:raise Failure('UNSUPPORTED','Mixed physics requires a dedicated adapter')
    total=0;expected={(t['object'],t['modifier']) for t in spec['targets']};actual=set();resolved=[];covered=set();bakes=0
    for o in bpy.data.objects:
        if o.library or o.override_library or o.instance_type!='NONE' or o.particle_systems:raise Failure('UNSUPPORTED','Instanced, linked, override and particle geometry not supported')
        if o.type=='MESH':total+=len(o.data.vertices)
        elif o.type not in ('EMPTY','CAMERA','LIGHT'):raise Failure('UNSUPPORTED','Cache scene requires mesh surfaces and helpers')
        if o.name not in layer.objects or o.hide_viewport or o.hide_get(view_layer=layer):raise Failure('UNSUPPORTED','Cache scene objects must be visible in the view layer')
        if len(o.modifiers)>1:raise Failure('UNSUPPORTED','Cache requires at most one geometry modifier per object')
        for m in o.modifiers:
            if m.type!='NODES' or not m.node_group or not m.show_viewport or not m.show_render:raise Failure('UNSUPPORTED','Only enabled geometry modifiers are supported')
            tree_closure(m.node_group)
            simnodes=[n for n in m.node_group.nodes if n.bl_idname=='GeometryNodeSimulationOutput']
            if not simnodes:continue
            if len(simnodes)!=1:raise Failure('UNSUPPORTED','One root simulation zone per modifier; use separate objects for independent zones')
            outputs=[n for n in m.node_group.nodes if n.bl_idname=='NodeGroupOutput' and n.is_active_output]
            graph={n.name:set() for n in m.node_group.nodes}
            for l in m.node_group.links:graph[l.from_node.name].add(l.to_node.name)
            if len(outputs)!=1 or outputs[0].name not in node_zones.reachable(simnodes[0].name,graph):raise Failure('UNSUPPORTED','Simulation output must reach the active group output')
            actual.add((o.name,m.name));resolved.append((o,m));covered|={(m.node_group.as_pointer(),n.name) for n in simnodes};bakes+=len(simnodes)
            if len(m.bakes)!=len(simnodes) or {b.node.name for b in m.bakes if b.node}!=set(n.name for n in simnodes):raise Failure('UNSUPPORTED','Native bake IDs must map exactly to root simulation outputs')
    all_sim={(t.as_pointer(),n.name) for t in bpy.data.node_groups for n in t.nodes if n.bl_idname=='GeometryNodeSimulationOutput'}
    if actual!=expected or covered!=all_sim:raise Failure('CONFLICT','Targets must cover every scene simulation modifier/root zone')
    if not 1<=bakes<=8 or not 1<=total<=20000:raise Failure('UNSUPPORTED','Cache limit is eight bakes and 20000 input vertices')
    return s,layer,sorted(resolved,key=lambda x:x[0].name),total
def roots(resolved):return [Path(bpy.path.abspath(m.bake_directory)).resolve() for o,m in resolved]
def configure(resolved,spec,job):
    for index,(o,m) in enumerate(resolved):
        m=node_zones.fresh_modifier(o,m);resolved[index]=(o,m)
        o.use_simulation_cache=True
        m.bake_target='DISK';m.bake_directory=str(job/'node-cache'/f'{index:02d}')
        for b in m.bakes:
            b.use_custom_path=False;b.directory='';b.bake_target='INHERIT';b.bake_mode='ANIMATION';b.use_custom_simulation_frame_range=True;b.frame_start=spec['frame_start'];b.frame_end=spec['frame_end']
        o.update_tag()
def bindings(resolved):
    return [{'object':o.name,'modifier':m.name,'group':m.node_group.name,'trees_sha256':fingerprint(tree_closure(m.node_group)),'modifier_inputs':[{'identifier':s.identifier,'name':s.name,'value':value(getattr(m.properties.inputs,s.identifier).value),'mode':getattr(getattr(m.properties.inputs,s.identifier),'type',None)} for s in m.node_group.interface.items_tree if s.item_type=='SOCKET' and s.in_out=='INPUT' and hasattr(getattr(m.properties.inputs,s.identifier,None),'value')],'directory':str(Path(bpy.path.abspath(m.bake_directory)).resolve()),'target':m.bake_target,'enabled':o.use_simulation_cache,'bakes':[{'id':b.bake_id,'node':b.node.name if b.node else None,'items':node_zones.item_records(b.node),'directory':b.directory,'custom_path':b.use_custom_path,'target':b.bake_target,'mode':b.bake_mode,'custom_range':b.use_custom_simulation_frame_range,'start':b.frame_start,'end':b.frame_end} for b in m.bakes]} for o,m in resolved]
def members(resolved):
    files=[];total=0;seen=set()
    for root in roots(resolved):
        if not root.is_dir():raise Failure('CONFLICT','Cache root missing')
        for p in sorted(root.rglob('*')):
            if p.is_symlink() or p.is_junction():raise Failure('UNSUPPORTED','Cache links are unsupported')
            if not p.is_file():continue
            name=str(p.resolve())
            if name in seen:raise Failure('CONFLICT','Overlapping cache roots')
            seen.add(name);size=p.stat().st_size;total+=size
            if size<1 or total>134217728 or len(files)>=1000:raise Failure('RESOURCE_LIMIT','Cache is empty/over128MiB/over1000files')
            files.append({'file':name,'expected_sha256':digest(p),'bytes':size})
    return sorted(files,key=lambda x:x['file'])
def cache_states(resolved,frames):
    states=[]
    for o,m in resolved:
        root=Path(bpy.path.abspath(m.bake_directory)).resolve();ids={str(b.bake_id) for b in m.bakes}
        if {p.name for p in root.iterdir()}!=ids:raise Failure('CONFLICT','Cache directory contains missing/extra bake IDs')
        for b in m.bakes:
            folder=root/str(b.bake_id);wanted={f'{f:05d}_00000.json' for f in frames};meta=folder/'meta';blob=folder/'blobs'
            if not meta.is_dir() or {p.name for p in meta.iterdir()}!=wanted:raise Failure('CONFLICT','Cache metadata does not cover exact integer frame range')
            state_ids={x['identifier'].removeprefix('Item_'):x for x in node_zones.item_records(b.node)};used_blobs=set()
            for frame in frames:
                data=read_json(meta/f'{frame:05d}_00000.json')
                if data.get('version')!=3 or set(data.get('items',{}))!=set(state_ids):raise Failure('CONFLICT','Unsupported cache format or state identifiers')
                scalar=[]
                def refs(x):
                    if isinstance(x,dict):
                        if {'name','start','size'}<=set(x):
                            name=x['name']
                            if not isinstance(name,str) or Path(name).name!=name or '/' in name or '\\' in name:raise Failure('CONFLICT','Cache blob must be a local member')
                            p=blob/name
                            if not p.is_file() or type(x['start']) is not int or type(x['size']) is not int or x['start']<0 or x['size']<0 or x['start']+x['size']>p.stat().st_size:raise Failure('CONFLICT','Missing or invalid cache blob range')
                            used_blobs.add(name)
                        for v in x.values():refs(v)
                    elif isinstance(x,list):
                        for v in x:refs(v)
                for ident,item in data['items'].items():
                    expected=state_ids[ident];typ=expected['type'];allowed={'GEOMETRY':{'GEOMETRY'},'FLOAT':{'FLOAT'},'VECTOR':{'VECTOR','FLOAT_VECTOR'}}[typ]
                    if item.get('value_name')!=expected['name'] or item.get('type') not in allowed:raise Failure('CONFLICT','Cached state type/name differs from zone')
                    refs(item)
                    if typ!='GEOMETRY':
                        val=item.get('data');values=[val] if typ=='FLOAT' else val
                        if not isinstance(values,list) or len(values)!=(1 if typ=='FLOAT' else 3) or any(type(v) not in (int,float) or not math.isfinite(v) for v in values):raise Failure('VALIDATION_FAILED','Nonfinite or field-valued state needs a separate adapter')
                        scalar.append({'identifier':'Item_'+ident,'name':expected['name'],'type':typ,'value':val})
                states.append({'object':o.name,'modifier':m.name,'node':b.node.name,'bake_id':b.bake_id,'frame':frame,'items':scalar})
            if not blob.is_dir() or {p.name for p in blob.iterdir()}!=used_blobs or {p.name for p in folder.iterdir()}!={'meta','blobs'}:raise Failure('CONFLICT','Cache blobs contain missing/extra members')
    return states
def sample(resolved,spec,frames):
    rows=[];observed={x['object']:x.get('attributes',[]) for x in spec['targets']}
    for f in frames:
        bpy.context.scene.frame_set(f);bpy.context.view_layer.update();graph=bpy.context.evaluated_depsgraph_get();row={'frame':f,'objects':[]}
        ids={o.as_pointer() for o,m in resolved}
        if any(i.is_instance and i.parent and i.parent.original.as_pointer() in ids for i in graph.object_instances):raise Failure('UNSUPPORTED','Realize instances before caching')
        for o,m in resolved:
            e=o.evaluated_get(graph);mesh=e.to_mesh()
            try:
                if not mesh.polygons or not 1<=len(mesh.vertices)<=20000:raise Failure('UNSUPPORTED','Cache sample requires 1..20000 evaluated surface vertices')
                vertices=[list(e.matrix_world@v.co) for v in mesh.vertices]
                if any(not math.isfinite(v) for p in vertices for v in p):raise Failure('VALIDATION_FAILED','Nonfinite zone geometry')
                attrs=[]
                for expected in observed[o.name]:
                    attr=mesh.attributes.get(expected['name'])
                    if not attr or attr.domain!='POINT' or attr.data_type!=expected['type']:raise Failure('VALIDATION_FAILED','Observed state attribute missing/type mismatch')
                    data=[v.value if expected['type']=='FLOAT' else list(v.vector) for v in attr.data]
                    if any(not math.isfinite(v) for x in data for v in (x if isinstance(x,list) else [x])):raise Failure('VALIDATION_FAILED','Nonfinite observed state attribute')
                    attrs.append({**expected,'domain':'POINT','data':data})
                row['objects'].append({'name':o.name,'vertices':vertices,'topology_sha256':modeling.topology(mesh),'attributes':attrs})
            finally:e.to_mesh_clear()
        rows.append(row)
    return rows
def verify_receipt(params,spec,receipt,resolved,resources):
    if receipt.get('adapter')!='GEOMETRY_ZONE_V1' or receipt['candidate_sha256']!=digest(params['file']) or receipt['request']!=signature(spec) or receipt['blender_build']!=bpy.app.build_hash.decode() or receipt['implementation_sha256']!=implementation():raise Failure('CONFLICT','Zone receipt invalidated by candidate, request or implementation')
    if receipt['resources']!=resources or receipt['bindings']!=bindings(resolved):raise Failure('CONFLICT','Zone settings, paths, state/node/bake identities or resources changed')
    if receipt['files']!=members(resolved):raise Failure('CONFLICT','Zone cache member set or contents changed')
    frames=list(range(spec['frame_start'],spec['frame_end']+1));states=cache_states(resolved,frames)
    if receipt['states']!=states:raise Failure('CONFLICT','Cached states changed')
    return frames

def readable_copy(resolved,job,folder):
    mapping=[]
    for index,(o,m) in enumerate(resolved):
        old=Path(bpy.path.abspath(m.bake_directory)).resolve();new=job/folder/f'{index:02d}'
        new.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(old,new)
        mapping.extend({'from':str(p),'to':str(new/p.relative_to(old)),'sha256':digest(p)} for p in sorted(old.rglob('*')) if p.is_file())
        m.bake_directory=str(new);o.update_tag()
    return mapping
def bake(params,job):
    spec=normalize(params['manifest']);start=time.monotonic();frames=list(range(spec['frame_start'],spec['frame_end']+1))
    bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    if tuple(bpy.data.version[:2])!=tuple(bpy.app.version[:2]):raise Failure('UNSUPPORTED','Zone cache requires matching Blender major/minor')
    scene,layer,resolved,vertices=preflight(params,spec)
    with ExitStack() as stack:
        hashes=nodes.resource_guards(params,stack);resources=[{'file':p,'expected_sha256':sha} for p,sha in sorted(hashes.items())]
        receipt=read_json(spec['receipt']['file']) if 'receipt' in spec else None
        if not receipt:configure(resolved,spec,job)
        bpy.context.window.scene=scene;bpy.context.window.view_layer=layer
        if receipt:
            verify_receipt(params,spec,receipt,resolved,resources)
            # Blender's blob reader requires Windows write sharing. Keep the
            # source cache locked by the supervisor and evaluate owned copies.
            mapping=readable_copy(resolved,job,'node-cache' if spec['mode']=='relocate' else 'cache-read')
            for row in mapping:
                if digest(row['to'])!=row['sha256']:raise Failure('CONFLICT','Cache copy differs from guarded source')
            copied_members=members(resolved)
            if cache_states(resolved,frames)!=receipt['states']:raise Failure('CONFLICT','Copied state metadata differs from original receipt')
            # A failed read of the locked source is cached inside the modifier.
            # Reopen only the relinked owned candidate to discard that history.
            read_candidate=job/'cache-read-candidate.blend'
            bpy.context.preferences.filepaths.save_version=0
            modeling.finished(bpy.ops.wm.save_as_mainfile(filepath=str(read_candidate),copy=True,relative_remap=True,check_existing=False))
            bpy.ops.wm.open_mainfile(filepath=str(read_candidate),load_ui=False,use_scripts=False)
            scene,layer,resolved,vertices=preflight(params,spec);bpy.context.window.scene=scene;bpy.context.window.view_layer=layer
            observed=sorted(sample(resolved,spec,list(reversed(frames))),key=lambda x:x['frame'])
            mismatch=scenes.compare(receipt['samples'],observed)
            if mismatch:raise Failure('VALIDATION_FAILED','Reverse zone cache sample mismatch: '+mismatch)
            if copied_members!=members(resolved):raise Failure('CONFLICT','Cache copy changed while reading')
            atomic_json(job/'zone-cache-samples.json',observed)
            if spec['mode']=='reuse':return {'reuse':'pass','candidate_sha256':digest(params['file']),'samples':str(job/'zone-cache-samples.json'),'rebaked':False,'adapter':'GEOMETRY_ZONE_V1','read_copy':mapping}
            atomic_json(job/'zone-cache-relocation.json',{'source_candidate':params['file'],'mapping':mapping,'originals_retained':True});expected=receipt['samples']
        else:
            scene.frame_start=spec['frame_start'];scene.frame_end=spec['frame_end'];scene.frame_set(scene.frame_start)
            import zone_state_oracle
            expected_states=zone_state_oracle.capture(resolved,spec,job)
            # Temporary observer groups must never affect the authored graph,
            # bake IDs or final candidate. Reset native history from the source.
            bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
            scene,layer,resolved,vertices=preflight(params,spec);configure(resolved,spec,job)
            bpy.context.window.scene=scene;bpy.context.window.view_layer=layer
            scene.frame_start=spec['frame_start'];scene.frame_end=spec['frame_end'];scene.frame_set(scene.frame_start)
            expected=sample(resolved,spec,frames);atomic_json(job/'zone-cache-oracle.json',expected)
            scene.frame_set(scene.frame_start)
            # Each selected object has one controlled modifier. Native background
            # bake is synchronous and writes only newly owned directories.
            for index,(o,m) in enumerate(resolved):
                atomic_json(job/'zone-cache-progress.json',{'state':'baking','object':o.name,'index':index,'total':len(resolved)})
                for item in layer.objects:item.select_set(False)
                o.select_set(True);layer.objects.active=o
                with bpy.context.temp_override(scene=scene,view_layer=layer,active_object=o,object=o,selected_objects=[o],selected_editable_objects=[o]):modeling.finished(bpy.ops.object.simulation_nodes_cache_bake(selected=True))
        baked_states=cache_states(resolved,frames);members(resolved)
        state_mismatch=scenes.compare(receipt['states'] if receipt else expected_states,baked_states)
        if state_mismatch:raise Failure('VALIDATION_FAILED','Cached state differs from native oracle: '+state_mismatch)
        before_bindings=bindings(resolved);candidate=job/'zone-cache-candidate.blend'
        bpy.context.preferences.filepaths.save_version=0;modeling.finished(bpy.ops.wm.save_as_mainfile(filepath=str(candidate),copy=True,relative_remap=True,check_existing=False))
        bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False);scene,layer,resolved,vertices=preflight(params,spec);bpy.context.window.scene=scene;bpy.context.window.view_layer=layer
        if before_bindings!=bindings(resolved):raise Failure('VALIDATION_FAILED','Zone bindings changed on save/reopen')
        files=members(resolved)
        observed=sorted(sample(resolved,spec,list(reversed(frames))),key=lambda x:x['frame']);mismatch=scenes.compare(expected,observed)
        if mismatch:raise Failure('VALIDATION_FAILED','Saved zone cache differs from forward oracle: '+mismatch)
        if files!=members(resolved):raise Failure('CONFLICT','Owned cache changed during verification')
        states=cache_states(resolved,frames);atomic_json(job/'zone-cache-samples.json',observed)
        if receipt and states!=receipt['states']:raise Failure('CONFLICT','Relocated states differ from original receipt')
        result={'simulation_cache_version':'1.2','adapter':'GEOMETRY_ZONE_V1','source':receipt['source'] if receipt else {'file':params['file'],'expected_sha256':digest(params['file'])},'candidate':str(candidate),'candidate_sha256':digest(candidate),'blender_build':bpy.app.build_hash.decode(),'implementation_sha256':implementation(),'request':signature(spec),'resources':resources,'bindings':bindings(resolved),'files':files,'samples':observed,'states':states,'reopen':'pass','seconds':time.monotonic()-start,'resource_policy':{'input_vertices':vertices,'max_frames':32,'max_bakes':8,'cache_bytes':sum(f['bytes'] for f in files),'sampling_tolerance':'abs 1e-5 / rel 1e-6; finite values and exact topology/attribute structure'},'scope':'owned Geometry Nodes disk cache; full integer-frame reverse verification; no original save/deletion or partial-cache continuation'}
        atomic_json(job/'simulation-receipt.json',result);atomic_json(job/'zone-cache-progress.json',{'state':'verified','frames':len(frames)});return result
