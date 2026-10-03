# SPDX-License-Identifier: GPL-3.0-or-later
"""Managed native Cloth ribbons and exact sampled centerline Curves."""
import json,math,hashlib
import bpy
from mathutils import Matrix,Vector
from protocol import Failure
import modeling,scenes

KEY='blenderctl_hair_dynamics_v1'
OFFSET='s09_ribbon_offset'
PIN='S09_RootPin'
ROOT_ATTR='hair_collider_root'

def fail(msg):raise Failure('UNSUPPORTED',msg)
def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'))
def digest(x):return hashlib.sha256(canonical(x).encode()).hexdigest()
def vec(x):return [float(v) for v in x]
def settings_snapshot(settings):
    result={}
    for p in settings.bl_rna.properties:
        if p.identifier=='rna_type' or p.is_readonly or p.type not in ('BOOLEAN','INT','FLOAT','ENUM','STRING'):continue
        v=getattr(settings,p.identifier)
        result[p.identifier]=list(v) if getattr(p,'is_array',False) else v
    return result
def definitions(kind,ref,guide=None):
    nodes={'Input':('NodeGroupInput',{},{}),'Output':('NodeGroupOutput',{},{}),'Index':('GeometryNodeInputIndex',{},{}),
      'Object':('GeometryNodeObjectInfo',{'transform_space':'RELATIVE'},{0:ref,1:False}),
      'Position':('GeometryNodeInputPosition',{},{}),
      'Set':('GeometryNodeSetPosition',{},{1:True,3:[0,0,0]}),
      'Sample':('GeometryNodeSampleIndex',{'data_type':'FLOAT_VECTOR','domain':'POINT','clamp':False},{}),
      'Math':('ShaderNodeMath',{'operation':'DIVIDE' if kind=='proxy' else 'MULTIPLY','use_clamp':False},{1:2.0,2:0.0})}
    links=[('Input',0,'Set',0),('Set',0,'Output',0),('Index',0,'Math',0),('Object',4,'Sample',0),('Position',0,'Sample',1)]
    if kind=='proxy':
        nodes.update({'Floor':('ShaderNodeMath',{'operation':'FLOOR','use_clamp':False},{1:0.0,2:0.0}),
            'Offset':('GeometryNodeInputNamedAttribute',{'data_type':'FLOAT_VECTOR'},{0:OFFSET})})
        links.extend([('Math',0,'Floor',0),('Floor',0,'Sample',2),('Sample',0,'Set',2),('Offset',0,'Set',3)])
        nodes['Set'][2].pop(3)
    else:
        nodes.update({'Plus':('ShaderNodeMath',{'operation':'ADD','use_clamp':False},{1:1.0,2:0.0}),
            'Sample2':('GeometryNodeSampleIndex',{'data_type':'FLOAT_VECTOR','domain':'POINT','clamp':False},{}),
            'Add':('ShaderNodeVectorMath',{'operation':'ADD'},{2:[0,0,0],3:1.0}),
            'Half':('ShaderNodeVectorMath',{'operation':'SCALE'},{1:[0,0,0],2:[0,0,0],3:.5})})
        links.extend([('Math',0,'Sample',2),('Math',0,'Plus',0),('Plus',0,'Sample2',2),('Object',4,'Sample2',0),('Position',0,'Sample2',1),('Sample',0,'Add',0),('Sample2',0,'Add',1),('Add',0,'Half',0),('Half',0,'Set',2)])
    if kind=='output_pinned':
        nodes.update({'GuideObject':('GeometryNodeObjectInfo',{'transform_space':'RELATIVE'},{0:guide,1:False}),
            'GuideSample':('GeometryNodeSampleIndex',{'data_type':'FLOAT_VECTOR','domain':'POINT','clamp':False},{}),
            'RootMask':('GeometryNodeInputNamedAttribute',{'data_type':'BOOLEAN'},{0:ROOT_ATTR}),
            'RootSet':('GeometryNodeSetPosition',{},{3:[0,0,0]})})
        links.remove(('Set',0,'Output',0))
        links.extend([('GuideObject',4,'GuideSample',0),('Position',0,'GuideSample',1),('Index',0,'GuideSample',2),('Set',0,'RootSet',0),('RootMask',0,'RootSet',1),('GuideSample',0,'RootSet',2),('RootSet',0,'Output',0)])
    return nodes,links

def graph(name,kind,ref,guide=None,blueprint=None):
    group=bpy.data.node_groups.new(name,'GeometryNodeTree')
    group.interface.new_socket(name='Geometry',in_out='INPUT',socket_type='NodeSocketGeometry')
    group.interface.new_socket(name='Geometry',in_out='OUTPUT',socket_type='NodeSocketGeometry')
    nodes,links=blueprint or definitions(kind,ref,guide)
    for name,(typ,props,defaults) in nodes.items():
        n=group.nodes.new(typ);n.name=name
        for p,v in props.items():setattr(n,p,v)
        for i,v in defaults.items():n.inputs[i].default_value=v
    for a,i,b,j in links:group.links.new(group.nodes[a].outputs[i],group.nodes[b].inputs[j])
    return group

def check_graph(group,kind,ref,guide=None,blueprint=None):
    if not group or group.animation_data or group.library:fail('Managed graph must be local and unanimated')
    nodes,links=blueprint or definitions(kind,ref,guide)
    if set(n.name for n in group.nodes)!=set(nodes):fail('Managed graph node inventory changed')
    for name,(typ,props,defaults) in nodes.items():
        n=group.nodes[name]
        if n.bl_idname!=typ or n.mute:fail('Managed graph node type or mute changed')
        for p,v in props.items():
            if getattr(n,p)!=v:fail('Managed graph property changed: '+name+'.'+p)
        for i,v in defaults.items():
            actual=n.inputs[i].default_value
            if isinstance(v,(tuple,list)):same=max(abs(a-b) for a,b in zip(actual,v))<1e-7
            elif isinstance(v,float):same=abs(actual-v)<1e-7
            else:same=actual==v
            if not same:fail('Managed graph default changed: '+name)
    got=[]
    for l in group.links:
        if l.is_muted or not l.is_valid:fail('Managed graph invalid link')
        got.append((l.from_node.name,list(l.from_node.outputs).index(l.from_socket),l.to_node.name,list(l.to_node.inputs).index(l.to_socket)))
    if sorted(got)!=sorted(links):fail('Managed graph connections changed')
    interface=[(s.in_out,s.socket_type) for s in group.interface.items_tree if s.item_type=='SOCKET']
    if interface!=[('OUTPUT','NodeSocketGeometry'),('INPUT','NodeSocketGeometry')]:fail('Managed graph interface changed')

def positions(data):return [vec(p.position) for p in data.points]
def topology_signature(data):return digest({'sizes':[len(c.points) for c in data.curves],'positions':positions(data),'attributes':[(a.name,a.data_type,a.domain) for a in data.attributes],'radii':[x.value for x in data.attributes['radius'].data],'root_ids':[x.value for x in data.attributes['root_id'].data]})
def identity(o,parent):
    if o.parent!=parent or max(abs(o.matrix_basis[i][j]-Matrix.Identity(4)[i][j]) for i in range(4) for j in range(4))>1e-7 or max(abs(o.matrix_parent_inverse[i][j]-Matrix.Identity(4)[i][j]) for i in range(4) for j in range(4))>1e-7:fail('Managed hair requires common parent with identity local transform')
    if o.animation_data or o.constraints or o.rigid_body:fail('Managed dynamics objects cannot have extra animation/constraints/rigid body')

def create(op,ctx):
    from scene_contract import validate as validate_schema
    from hair_dynamics_contract import OP
    validate_schema(op,OP)
    source=scenes.find(bpy.data.objects,op['source'],True)
    import hair_surface,hair_shape
    hair_surface.validate_bindings()
    hair_shape.validate_bindings()
    if hair_shape.KEY in source:
        shape=hair_shape.metadata(source)['recipe']
        if op['adapter']!='RIBBON_COLLIDERS_V1':fail('Triangle groom dynamics requires RIBBON_COLLIDERS_V1')
        if (op['frame_start'],op['frame_end'])!=(shape['frame_start'],shape['frame_end']):fail('Dynamics must cover the complete groom witness range')
    elif hair_surface.KEY not in source or source.type!='CURVES' or not source.data.surface or source.parent!=source.data.surface:
        fail('Dynamics source must be a managed surface guide or triangle groom')
    identity(source,source.parent)
    sizes=[len(c.points) for c in source.data.curves]
    if not sizes or len(sizes)>64 or any(n<2 or n>32 for n in sizes) or 2*sum(sizes)>4096:fail('Dynamics exceeds 64 strands, 32 points per strand or 4096 proxy vertices')
    multi=op['adapter']=='RIBBON_COLLIDERS_V1'
    if multi:
        import hair_collision
        if len({op['source'],op['name'],op['proxy_name']})!=3:fail('Dynamics object names must be distinct')
    else:
        collider=scenes.find(bpy.data.objects,op['collision'],True)
        if collider.type!='MESH' or collider.parent or collider.animation_data or collider.data.animation_data or collider.data.shape_keys or collider.constraints or collider.rigid_body:fail('Collider must be one static local mesh')
        if any(m.type!='COLLISION' for m in collider.modifiers) or len(collider.modifiers)>1:fail('Collider has unknown modifiers')
        for obj in bpy.data.objects:
            if obj!=collider and any(m.type=='COLLISION' for m in obj.modifiers):fail('Only one explicit collision object is supported')
        if len(set([op['name'],op['proxy_name'],op['source'],op['collision']]))!=4:fail('Dynamics object names must be distinct')
    for name in [op['name'],op['proxy_name']]:
        scenes.fresh(bpy.data.objects,name)
    collection=scenes.find(bpy.data.collections,op['collection'],True)
    if multi:collision_collection,collision_record=hair_collision.prepare(op)
    points=positions(source.data);verts=[];faces=[];offsets=[];pins=[];start=0
    for size in sizes:
        pins.extend([2*start,2*start+1])
        for j in range(size):
            i=start+j;t=Vector(points[start+min(j+1,size-1)])-Vector(points[start+max(0,j-1)])
            if t.length<1e-7:fail('Ribbon tangent is degenerate')
            t.normalize();axis=Vector((0,0,1)) if abs(t.z)<.9 else Vector((1,0,0));side=t.cross(axis).normalized()*op['width']/2
            for sign in [-1,1]:offsets.append(vec(side*sign));verts.append(vec(Vector(points[i])+side*sign))
            if j<size-1:faces.append((2*i,2*i+2,2*i+3,2*i+1))
        start+=size
    data=bpy.data.meshes.new(op['proxy_name']);data.from_pydata(verts,[],faces);data.update()
    proxy=bpy.data.objects.new(op['proxy_name'],data);collection.objects.link(proxy);proxy.parent=source.parent;proxy.hide_render=True
    attr=data.attributes.new(OFFSET,'FLOAT_VECTOR','POINT')
    for item,p in zip(attr.data,offsets):item.vector=p
    vg=proxy.vertex_groups.new(name=PIN);vg.add(pins,1,'REPLACE')
    proxy.modifiers.new('S09_Guide','NODES').node_group=graph(op['proxy_name']+'_Guide','proxy',source)
    cloth=proxy.modifiers.new('S09_Cloth','CLOTH');s=cloth.settings
    s.quality=op['quality'];s.mass=op['mass'];s.air_damping=op['air_damping'];s.tension_stiffness=op['tension'];s.compression_stiffness=op['tension'];s.shear_stiffness=op['tension'];s.bending_stiffness=op['bending'];s.vertex_group_mass=PIN;s.pin_stiffness=1
    cloth.collision_settings.use_collision=True;cloth.collision_settings.use_self_collision=False;cloth.collision_settings.distance_min=op['collision_distance']
    cloth.point_cache.frame_start=bpy.context.scene.frame_start;cloth.point_cache.frame_end=bpy.context.scene.frame_end
    if multi:
        cloth.collision_settings.collection=collision_collection
        cloth.collision_settings.collision_quality=op['collision_quality']
    else:
        added=not bool(collider.modifiers)
        if added:collider.modifiers.new('S09_Collision','COLLISION')
    outdata=source.data.copy();outdata.name=op['name'];outdata.surface=None;outdata.surface_uv_map=''
    if multi:
        mask=outdata.attributes.new(ROOT_ATTR,'BOOLEAN','POINT');roots={p//2 for p in pins}
        for i,p in enumerate(mask.data):p.value=i in roots
    output=bpy.data.objects.new(op['name'],outdata);collection.objects.link(output);output.parent=source.parent
    output.modifiers.new('S09_Centerline','NODES').node_group=graph(op['name']+'_Centerline','output_pinned' if multi else 'output',proxy,source if multi else None)
    if op['hide_source']:source.hide_render=True
    report={'adapter':op['adapter'],'source':source.name,'output':output.name,'proxy':proxy.name,'surface':source.parent.name,'op':op,'sizes':sizes,'pins':pins,'proxy_topology':modeling.topology(data),'offsets_sha256':digest(offsets),'source_topology':topology_signature(source.data),'output_topology':topology_signature(outdata)}
    # Serialize read-back float32 offsets, not the pre-storage Python values.
    report['offsets_sha256']=digest([vec(x.vector) for x in attr.data])
    report['cloth_settings']=settings_snapshot(s);report['cloth_collision_settings']=settings_snapshot(cloth.collision_settings)
    report['effector_weights']=settings_snapshot(s.effector_weights)
    if multi:report.update(collision_record)
    else:report.update(collision=collider.name,collision_modifier_added=added,collision_topology=modeling.topology(collider.data),collision_matrix=[vec(r) for r in collider.matrix_world],collision_settings=settings_snapshot(collider.collision),collision_positions=digest([vec(v.co) for v in collider.data.vertices]))
    output[KEY]=canonical(report)
    validate()
    return report

def validate():
    checks=[]
    for output in bpy.data.objects:
        if KEY not in output:continue
        try:r=json.loads(output[KEY]);op=r['op'];source=bpy.data.objects[r['source']];proxy=bpy.data.objects[r['proxy']];surface=bpy.data.objects[r['surface']]
        except Exception as e:fail('Invalid hair dynamics metadata: '+str(e))
        from scene_contract import validate as validate_schema
        from hair_dynamics_contract import OP
        validate_schema(op,OP)
        multi=op['adapter']=='RIBBON_COLLIDERS_V1'
        if r['adapter']!=op['adapter'] or output.name!=r['output'] or source.type!='CURVES' or output.type!='CURVES' or proxy.type!='MESH':fail('Managed dynamics identity changed')
        for o in [source,proxy,output]:identity(o,surface)
        if any(o.library or o.override_library or o.data.library or o.data.users!=1 for o in [source,proxy,output]):fail('Managed dynamics requires local single-user data')
        if [m.type for m in proxy.modifiers]!=['NODES','CLOTH'] or [m.type for m in output.modifiers]!=['NODES']:fail('Managed dynamics modifier stack changed')
        if any(not m.show_viewport or not m.show_render for o in [proxy,output] for m in o.modifiers):fail('Managed dynamics modifier disabled')
        if output.data.surface:fail('Centerline output cannot also bind to surface')
        check_graph(proxy.modifiers[0].node_group,'proxy',source);check_graph(output.modifiers[0].node_group,'output_pinned' if multi else 'output',proxy,source if multi else None)
        if modeling.topology(proxy.data)!=r['proxy_topology'] or topology_signature(source.data)!=r['source_topology'] or topology_signature(output.data)!=r['output_topology']:fail('Managed hair topology changed')
        a=proxy.data.attributes.get(OFFSET)
        if not a or a.data_type!='FLOAT_VECTOR' or a.domain!='POINT' or digest([vec(x.vector) for x in a.data])!=r['offsets_sha256']:fail('Managed ribbon offsets changed')
        if len(proxy.vertex_groups)!=1 or proxy.vertex_groups[0].name!=PIN:fail('Managed root pin groups changed')
        sizes=[len(c.points) for c in source.data.curves]
        if not sizes or len(sizes)>64 or any(n<2 or n>32 for n in sizes) or 2*sum(sizes)>4096:fail('Managed strand bounds changed')
        pins=set();start=0
        for size in sizes:pins.update([2*start,2*start+1]);start+=size
        if pins!=set(r['pins']) or len(proxy.data.vertices)!=2*sum(sizes):fail('Managed ribbon root mapping changed')
        if multi:
            mask=output.data.attributes.get(ROOT_ATTR)
            if not mask or mask.data_type!='BOOLEAN' or mask.domain!='POINT' or [p.value for p in mask.data]!=[2*i in pins for i in range(sum(sizes))]:fail('Managed exact-root mask changed')
        for v in proxy.data.vertices:
            got=[(g.group,g.weight) for g in v.groups]
            if got!=([(0,1.0)] if v.index in pins else []):fail('Managed root pin weights changed')
        cloth=proxy.modifiers[1];s=cloth.settings
        expected={'quality':op['quality'],'mass':op['mass'],'air_damping':op['air_damping'],'tension_stiffness':op['tension'],'compression_stiffness':op['tension'],'shear_stiffness':op['tension'],'bending_stiffness':op['bending'],'vertex_group_mass':PIN,'pin_stiffness':1}
        for key,want in expected.items():
            got=getattr(s,key)
            if got!=want and not isinstance(want,str) and abs(got-want)<=1e-6:continue
            if got!=want:fail('Managed Cloth setting changed: '+key)
        cc=cloth.collision_settings;cache=cloth.point_cache
        if settings_snapshot(s)!=r['cloth_settings'] or settings_snapshot(cc)!=r['cloth_collision_settings']:fail('Managed complete Cloth configuration changed')
        multi=op['adapter']=='RIBBON_COLLIDERS_V1'
        if s.rest_shape_key or (cc.collection and not multi) or s.effector_weights.collection:fail('Managed Cloth cannot add rest-shape or implicit effector/collision collection dependencies')
        if settings_snapshot(s.effector_weights)!=r.get('effector_weights'):fail('Managed Cloth effector weights changed')
        if not cc.use_collision or cc.use_self_collision or abs(cc.distance_min-op['collision_distance'])>1e-7:fail('Managed Cloth collision settings changed')
        if cache.use_external or cache.use_disk_cache or cache.frame_start>cache.frame_end or cache.frame_end-cache.frame_start>1000:fail('Managed Cloth cache must remain bounded and embedded')
        if multi:
            import hair_collision
            hair_collision.validate(op,r,cloth)
        else:
            collision=bpy.data.objects.get(r['collision'])
            if not collision or [m.type for m in collision.modifiers]!=['COLLISION'] or collision.animation_data or collision.data.animation_data or collision.constraints or collision.parent or collision.data.shape_keys:fail('Managed collider changed')
            if modeling.topology(collision.data)!=r['collision_topology'] or [vec(row) for row in collision.matrix_world]!=r['collision_matrix']:fail('Managed collider geometry/transform changed')
            if digest([vec(v.co) for v in collision.data.vertices])!=r['collision_positions'] or settings_snapshot(collision.collision)!=r['collision_settings']:fail('Managed collider coordinates/settings changed')
        checks.append({'output':output.name,'proxy':proxy.name,'source':source.name,'validated':True})
    return checks

def managed_objects():
    validate();result=set()
    for o in bpy.data.objects:
        if KEY in o:
            r=json.loads(o[KEY]);result.update([r['source'],r['proxy'],r['output']])
    return result

def sample(frames):
    validate();rows=[]
    for frame in frames:
        bpy.context.scene.frame_set(frame);dg=bpy.context.evaluated_depsgraph_get();objects=[]
        for o in bpy.data.objects:
            if KEY not in o:continue
            r=json.loads(o[KEY]);evaluated=o.evaluated_get(dg);d=evaluated.data;curves=[];radius=d.attributes.get('radius');ids=d.attributes.get('root_id');start=0
            if len(d.points)!=len(o.data.points) or len(d.curves)!=len(o.data.curves):fail('Evaluated dynamic hair topology changed')
            for i,c in enumerate(d.curves):
                pts=[vec(evaluated.matrix_world@p.position) for p in c.points]
                if not all(math.isfinite(v) for p in pts for v in p):fail('Dynamic hair produced nonfinite positions')
                radii=[radius.data[start+j].value for j in range(len(c.points))]
                curves.append({'id':ids.data[i].value,'root':pts[0],'points':pts,'radii':radii});start+=len(c.points)
            row={'object':o.name,'surface':r['surface'],'coordinate_space':'WORLD','curves':curves}
            if r['adapter']=='RIBBON_COLLIDERS_V1':
                import hair_collision
                row['collisions']=hair_collision.observe(r['op'],r,bpy.data.objects[r['proxy']],curves,dg,frame)
            objects.append(row)
        rows.append({'frame':frame,'objects':objects})
    return rows
