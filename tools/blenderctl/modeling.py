# SPDX-License-Identifier: GPL-3.0-or-later
"""Local static modeling candidates, explicit data ownership and geometry evidence."""
import hashlib,json,math
from contextlib import contextmanager
import bpy,bmesh
from mathutils import Vector
from protocol import Failure,atomic_json,digest
from inspection import all_ids,identity
import scenes
from model_contract import normalize,SCENE_OPS

MAX_VERTICES=200000
MAX_LOOPS=1000000

def sha(value):return hashlib.sha256(json.dumps(value,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def topology(mesh):return sha({'vertices':len(mesh.vertices),'edges':[list(e.vertices) for e in mesh.edges],'faces':[list(f.vertices) for f in mesh.polygons]})
def bounded(mesh):
    if len(mesh.vertices)>MAX_VERTICES or len(mesh.loops)>MAX_LOOPS:raise Failure('UNSUPPORTED','Mesh exceeds 200000 vertices / 1000000 loops')

def local_static(item):
    if item.library or item.override_library or item.animation_data:raise Failure('UNSUPPORTED','Modeling requires local unanimated data')

def editable(op,types=('MESH',),copy_data=True):
    item=scenes.find(bpy.data.objects,op['object'],True);local_static(item)
    if item.type not in types:raise Failure('INVALID_REQUEST','Object type incompatible with operation')
    data=item.data;local_static(data)
    if getattr(data,'shape_keys',None) or item.vertex_groups:raise Failure('UNSUPPORTED','Shape keys and vertex groups require a deformation-aware workflow')
    users=[o for o in bpy.data.objects if o.data==data]
    policy=op.get('data_scope','reject_shared')
    if copy_data and data.users>1:
        if policy=='reject_shared':raise Failure('CONFLICT','Shared data requires explicit single_user or shared policy')
        if policy=='single_user':
            if data.asset_data or data.get('asset_id'):raise Failure('UNSUPPORTED','Cannot duplicate asset data identity')
            name=item.name+'.Model';group=bpy.data.meshes if item.type=='MESH' else bpy.data.curves
            scenes.fresh(group,name);item.data=scenes.named(data.copy(),name);data=item.data
        else:
            for user in users:
                local_static(user)
                if user.vertex_groups:raise Failure('UNSUPPORTED','Shared data has a deformed user')
    if item.type=='MESH':bounded(data)
    return item

def select(mesh,elements,selection):
    if selection=='ALL':return list(elements)
    if topology(mesh)!=selection['topology_sha256']:raise Failure('CONFLICT','Stale topology hash; inspect current candidate before index editing')
    indices=selection['indices']
    if max(indices)>=len(elements):raise Failure('INVALID_REQUEST','Selection index outside current domain')
    return [elements[i] for i in indices]

@contextmanager
def operator_context(item,context):
    ctx={**context,'mode':'OBJECT','active_object':item.name,'selected_objects':[item.name]}
    scenes.activate(ctx)
    try:yield
    finally:
        if bpy.context.mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')

def finished(result):
    if result!={'FINISHED'}:raise Failure('VALIDATION_FAILED','Operator did not finish: '+str(result))

def mesh_edit(item,op):
    mesh=item.data;kind=op['op']
    if kind=='mesh.positions':
        vertices=select(mesh,mesh.vertices,op['selection'])
        if len(vertices)!=len(op['positions']):raise Failure('INVALID_REQUEST','Positions count differs from selected vertices')
        for vertex,co in zip(vertices,op['positions']):vertex.co=co
        mesh.update();return
    bm=bmesh.new()
    try:
        bm.from_mesh(mesh)
        for domain in (bm.verts,bm.edges,bm.faces):domain.ensure_lookup_table()
        domain=bm.edges if kind in ('mesh.bevel','mesh.subdivide') else bm.faces
        chosen=select(mesh,domain,op['selection']) if 'selection' in op else []
        if 'selection' in op and not chosen:raise Failure('INVALID_REQUEST','Operation requires a nonempty selection')
        if kind=='mesh.extrude':
            result=bmesh.ops.extrude_face_region(bm,geom=chosen,use_keep_orig=False)
            verts=[v for v in result['geom'] if isinstance(v,bmesh.types.BMVert)]
            bmesh.ops.translate(bm,verts=verts,vec=Vector(op['offset']))
        elif kind=='mesh.inset':bmesh.ops.inset_region(bm,faces=chosen,thickness=op['thickness'],depth=op['depth'],use_even_offset=True,use_boundary=True,use_interpolate=True)
        elif kind=='mesh.bevel':bmesh.ops.bevel(bm,geom=chosen,offset=op['offset'],segments=op['segments'],affect='EDGES',clamp_overlap=True)
        elif kind=='mesh.subdivide':
            if len(bm.verts)+len(chosen)*(op['cuts']+1)**2*4>MAX_VERTICES:raise Failure('UNSUPPORTED','Subdivision estimate exceeds geometry budget')
            bmesh.ops.subdivide_edges(bm,edges=chosen,cuts=op['cuts'],use_grid_fill=True)
        elif kind=='mesh.delete_faces':bmesh.ops.delete(bm,geom=chosen,context='FACES_ONLY')
        elif kind=='mesh.triangulate':bmesh.ops.triangulate(bm,faces=chosen,quad_method='BEAUTY',ngon_method='BEAUTY')
        elif kind=='mesh.merge_distance':bmesh.ops.remove_doubles(bm,verts=list(bm.verts),dist=op['distance'])
        elif kind=='mesh.normals':
            if op['recalculate']:bmesh.ops.recalc_face_normals(bm,faces=list(bm.faces))
            for f in bm.faces:f.smooth=op['smooth']
        else:raise Failure('INVALID_REQUEST','Unknown mesh operation')
        if len(bm.verts)>MAX_VERTICES or sum(len(f.loops) for f in bm.faces)>MAX_LOOPS:raise Failure('UNSUPPORTED','Operation exceeded geometry budget')
        bm.normal_update();bm.to_mesh(mesh);mesh.update()
    finally:bm.free()

def modifier_add(op):
    item=editable(op,copy_data=False);scenes.fresh(item.modifiers,op['name'])
    typ=op['type'];settings=op['settings'];target=None
    if typ=='BOOLEAN':
        target=scenes.find(bpy.data.objects,settings['object'])
        if target==item or target.type!='MESH':raise Failure('INVALID_REQUEST','Boolean operand must be another mesh')
        # Includes proposed boolean edges, existing object constraints and parent dependencies.
        graph={o.name:([o.parent.name] if o.parent else [])+[m.object.name for m in o.modifiers if m.type=='BOOLEAN' and m.object]+[c.target.name for c in o.constraints if getattr(c,'target',None)] for o in bpy.data.objects}
        graph[item.name].append(target.name)
        def visit(node,trail):
            if node in trail:raise Failure('CONFLICT','Boolean dependency cycle')
            if len(trail)>200:raise Failure('UNSUPPORTED','Dependency hierarchy too deep')
            for n in graph[node]:visit(n,trail|{node})
        visit(item.name,set())
    if typ=='SUBSURF' and max(1,len(item.data.polygons))*4**settings['levels']*4>MAX_VERTICES:raise Failure('UNSUPPORTED','Subdivision estimate exceeds budget')
    mod=scenes.named(item.modifiers.new(op['name'],typ),op['name'])
    if typ=='BEVEL':mod.width=settings['width'];mod.segments=settings['segments'];mod.limit_method='ANGLE';mod.angle_limit=math.radians(settings['angle_limit_deg'])
    elif typ=='BOOLEAN':mod.object=target;mod.operation=settings['operation'];mod.solver='EXACT'
    elif typ=='SOLIDIFY':mod.thickness=settings['thickness'];mod.offset=settings['offset']
    elif typ=='SUBSURF':mod.levels=settings['levels'];mod.render_levels=settings['levels']
    elif typ=='MIRROR':mod.use_axis=tuple(a in settings['axes'] for a in 'XYZ');mod.merge_threshold=settings['merge_threshold'];mod.use_clip=settings['clip']
    elif typ=='DECIMATE':mod.decimate_type='COLLAPSE';mod.ratio=settings['ratio']
    elif typ=='WELD':mod.merge_threshold=settings['merge_threshold']

def uv_edit(item,op,context):
    mesh=item.data;kind=op['op']
    if kind=='uv.layer':
        if op['action']=='create':
            scenes.fresh(mesh.uv_layers,op['name'])
            if len(mesh.uv_layers)>=8:raise Failure('UNSUPPORTED','Blender supports at most eight UV maps')
            scenes.named(mesh.uv_layers.new(name=op['name'],do_init=False),op['name'])
        else:
            layer=scenes.find(mesh.uv_layers,op['name'])
            if op['action']=='remove':mesh.uv_layers.remove(layer);return
        mesh.uv_layers.active=mesh.uv_layers[op['name']];mesh.uv_layers.active.active_render=True;return
    if kind=='uv.seams':
        for e in select(mesh,mesh.edges,op['selection']):e.use_seam=op['marked']
        return
    layer=scenes.find(mesh.uv_layers,op['layer']);mesh.uv_layers.active=layer;layer.active_render=True
    if kind=='uv.set':
        if topology(mesh)!=op['topology_sha256']:raise Failure('CONFLICT','Stale UV topology hash')
        if len(op['coordinates'])!=len(mesh.loops):raise Failure('INVALID_REQUEST','UV coordinates must cover every face corner in loop order')
        for loop,co in zip(layer.data,op['coordinates']):loop.uv=co
        mesh.update();return
    if not mesh.polygons:raise Failure('INVALID_REQUEST','UV operation requires faces')
    with operator_context(item,context):
        bpy.context.scene.tool_settings.use_uv_select_sync=False
        for f in mesh.polygons:f.select=True
        if any(loop.pin_uv for loop in layer.data):raise Failure('UNSUPPORTED','Pinned UVs require a pin-aware workflow; no pins are silently cleared')
        finished(bpy.ops.object.mode_set(mode='EDIT'));finished(bpy.ops.mesh.select_all(action='SELECT'));finished(bpy.ops.uv.select_all(action='SELECT'))
        if kind=='uv.pack':result=bpy.ops.uv.pack_islands(rotate=op['rotate'],scale=True,margin_method='FRACTION',margin=op['margin'],udim_source='ORIGINAL_AABB',pin=False)
        elif op['method']=='SMART':result=bpy.ops.uv.smart_project(angle_limit=math.radians(66),island_margin=op['margin'],correct_aspect=False,scale_to_bounds=True)
        else:result=bpy.ops.uv.unwrap(method=op['method'],margin=op['margin'],correct_aspect=False)
        finished(result)

def execute(op,context):
    kind=op['op']
    if kind=='mesh.retopology':
        from retopology import execute as remesh
        return remesh(op,context)
    if kind in SCENE_OPS:
        scenes.execute(op);return
    if kind=='mesh.create':
        scenes.fresh(bpy.data.objects,op['name']);scenes.fresh(bpy.data.meshes,op['name']+'.Mesh');collection=scenes.find(bpy.data.collections,op['collection'],True)
        mesh=bpy.data.meshes.new(op['name']+'.Mesh');mesh.from_pydata(op['vertices'],op.get('edges',[]),op['faces']);mesh.update()
        if mesh.validate(verbose=False):raise Failure('INVALID_REQUEST','Input required automatic mesh repair; provide valid topology')
        collection.objects.link(scenes.named(bpy.data.objects.new(op['name'],mesh),op['name']))
    elif kind in ('curve.create','text.create'):
        scenes.fresh(bpy.data.objects,op['name']);scenes.fresh(bpy.data.curves,op['name']+'.Curve');collection=scenes.find(bpy.data.collections,op['collection'],True)
        data=bpy.data.curves.new(op['name']+'.Curve','FONT' if kind=='text.create' else 'CURVE')
        if kind=='text.create':
            data.body=op['body'];data.size=op['size'];data.extrude=op['extrude']
            if 'font' in op:
                from font_curves import create_font
                data.font=create_font(op)
            if 'layout' in op:
                for key,v in op['layout'].items():setattr(data,'resolution_u' if key=='resolution' else key,v)
        else:
            data.dimensions='3D';data.resolution_u=op['resolution'];data.bevel_depth=op['bevel_depth'];data.bevel_resolution=2
            if 'settings' in op:
                for key,v in op['settings'].items():setattr(data,key,v)
            spline=data.splines.new(op['type']);spline.use_cyclic_u=op['cyclic']
            if op['type']=='POLY':
                spline.points.add(len(op['points'])-1)
                for point,co in zip(spline.points,op['points']):point.co=(*co,1)
            else:
                spline.bezier_points.add(len(op['points'])-1)
                for point,co in zip(spline.bezier_points,op['points']):point.co=co;point.handle_left_type='AUTO';point.handle_right_type='AUTO'
        collection.objects.link(scenes.named(bpy.data.objects.new(op['name'],data),op['name']))
    elif kind=='object.to_mesh':
        if 'verify' in op:
            from font_curves import convert
            return convert(op,context)
        item=editable(op,types=('CURVE','FONT'))
        if op['data_scope']=='shared':raise Failure('UNSUPPORTED','Conversion is per object; use single_user or reject_shared')
        if item.modifiers:raise Failure('UNSUPPORTED','Apply modifiers explicitly before conversion')
        old=item.data
        if old.asset_data or old.get('asset_id'):raise Failure('UNSUPPORTED','Conversion of identified asset data requires identity policy')
        name=item.name+'.Converted';scenes.fresh(bpy.data.meshes,name)
        # Object.data RNA is type-specific; object.convert performs the type transition.
        with operator_context(item,context):finished(bpy.ops.object.convert(target='MESH',keep_original=False,merge_customdata=False))
        item=scenes.find(bpy.data.objects,op['object'])
        if item.type!='MESH':raise Failure('VALIDATION_FAILED','Conversion did not produce a mesh')
        bounded(item.data);scenes.named(item.data,name)
    elif kind=='modifier.add':modifier_add(op)
    elif kind=='modifier.remove':
        item=editable(op,copy_data=False);item.modifiers.remove(scenes.find(item.modifiers,op['name']))
    elif kind=='modifier.apply':
        item=editable(op);mod=scenes.find(item.modifiers,op['name'])
        if item.modifiers[0]!=mod:raise Failure('CONFLICT','Apply modifiers in stack order only')
        if mod.type not in ('BEVEL','BOOLEAN','SOLIDIFY','SUBSURF','MIRROR','DECIMATE','TRIANGULATE','WELD'):raise Failure('UNSUPPORTED','Modifier application outside supported types')
        if item.data.users>1:raise Failure('UNSUPPORTED','Modifier apply requires single-user mesh data')
        with operator_context(item,context):finished(bpy.ops.object.modifier_apply(modifier=op['name'],single_user=False))
        bounded(item.data)
    elif kind=='mesh.lod':
        source=editable(op,copy_data=False)
        if source.modifiers:raise Failure('UNSUPPORTED','LOD requires an explicitly applied source stack')
        if source.asset_data or source.get('asset_id') or source.data.asset_data or source.data.get('asset_id'):raise Failure('UNSUPPORTED','LOD copy cannot duplicate asset identity')
        scenes.fresh(bpy.data.objects,op['name']);scenes.fresh(bpy.data.meshes,op['name']+'.LOD');collection=scenes.find(bpy.data.collections,op['collection'],True)
        item=scenes.named(source.copy(),op['name']);item.data=scenes.named(source.data.copy(),op['name']+'.LOD');collection.objects.link(item)
        modifier_add({'object':item.name,'name':'LOD_Decimate','type':'DECIMATE','settings':{'ratio':op['ratio']}})
        with operator_context(item,context):finished(bpy.ops.object.modifier_apply(modifier='LOD_Decimate'))
    elif kind=='uv.layout':
        import uv_layout
        result=uv_layout.run(op);scenes.update();return result
    elif kind.startswith('uv.'):
        item=editable(op);uv_edit(item,op,context);item.data.update()
    elif kind.startswith('mesh.'):mesh_edit(editable(op),op)
    else:raise Failure('INVALID_REQUEST','Unknown modeling operation')
    scenes.update()

def mesh_report(mesh,include_coordinates=False):
    bounded(mesh);bm=bmesh.new()
    try:
        bm.from_mesh(mesh);bm.normal_update()
        boundaries=sum(e.is_boundary for e in bm.edges);wire=sum(e.is_wire for e in bm.edges);nonmanifold=sum(not e.is_manifold for e in bm.edges)
        inconsistent=sum(e.is_manifold and not e.is_contiguous for e in bm.edges)
        degenerate=sum(f.calc_area()<1e-12 for f in bm.faces)
        volume=bm.calc_volume(signed=True) if len(bm.faces) and not nonmanifold and not inconsistent else None
    finally:bm.free()
    coords=[list(v.co) for v in mesh.vertices];mesh.calc_loop_triangles()
    uvs=[]
    for layer in mesh.uv_layers:
        uv=[list(x.uv) for x in layer.data]
        areas=[]
        for tri in mesh.loop_triangles:
            a,b,c=(uv[i] for i in tri.loops);areas.append(abs((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))/2)
        from uv_layout import inventory
        uvs.append({'name':layer.name,'active':mesh.uv_layers.active==layer,'active_render':layer.active_render,'islands':inventory(mesh,layer),
            'coordinates':uv,'sha256':sha(uv),'bounds':[min(p[0] for p in uv),min(p[1] for p in uv),max(p[0] for p in uv),max(p[1] for p in uv)] if uv else None,
            'degenerate_triangles':sum(a<1e-12 for a in areas),'triangle_area_sum':sum(areas),'overlap':'not_checked','pinned_loops':sum(x.pin_uv for x in layer.data)})
    return {'vertices':len(mesh.vertices),'edges':len(mesh.edges),'faces':len(mesh.polygons),'triangles':len(mesh.loop_triangles),'loops':len(mesh.loops),
        'topology_sha256':topology(mesh),'positions_sha256':sha(coords),'bounds':[min(v[i] for v in coords) for i in range(3)]+[max(v[i] for v in coords) for i in range(3)] if coords else None,
        'boundary_edges':boundaries,'wire_edges':wire,'nonmanifold_edges':nonmanifold,'inconsistent_winding_edges':inconsistent,'degenerate_faces':degenerate,
        'signed_volume':volume,'smooth_faces':sum(f.use_smooth for f in mesh.polygons),'seam_edges':[e.index for e in mesh.edges if e.use_seam],'uv_layers':uvs,
        'self_intersection':'not_checked','normal_scope':'face_winding_and_smooth_flags; custom_split_normals_not_authored',**({'coordinates':coords} if include_coordinates else {})}

def state(evaluation_coordinates=False):
    base=scenes.state();graph=bpy.context.evaluated_depsgraph_get();layer=bpy.context.view_layer;objects=[]
    for item in sorted(bpy.data.objects,key=lambda o:o.name):
        if item.type not in ('MESH','CURVE','FONT'):continue
        entry={'name':item.name,'type':item.type,'data':identity(item.data),'data_users':sorted(o.name for o in bpy.data.objects if o.data==item.data),
            'base':mesh_report(item.data) if item.type=='MESH' else None,'evaluated':None}
        if item.type in ('CURVE','FONT'):
            from font_curves import curve_info
            entry['curve']=curve_info(item)
        if item.name in layer.objects:
            evaluated=item.evaluated_get(graph);mesh=evaluated.to_mesh(preserve_all_data_layers=True,depsgraph=graph)
            try:
                if mesh:entry['evaluated']=mesh_report(mesh,evaluation_coordinates)
            finally:evaluated.to_mesh_clear()
        objects.append(entry)
    return {'model_report_version':'1.0','scene':base,'objects':objects,'coverage':'local_mesh_topology_geometry_UV_and_active_view_layer_evaluation; no_self_intersection_or_UV_overlap_proof'}

def inspect(params,job):
    bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    if 'context' in params:scenes.activate(params['context'])
    report=state();atomic_json(job/'model-report.json',report);return report

def prepare(params,job):
    manifest=normalize(params['manifest'])
    uv_only=all(op['op']=='uv.layout' for op in manifest['operations'])
    if params.get('file'):
        bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
        if tuple(bpy.data.version[:2])!=tuple(bpy.app.version[:2]):raise Failure('UNSUPPORTED','Modeling requires matching Blender major/minor')
    else:bpy.ops.wm.read_factory_settings(use_empty=True);scenes.named(bpy.context.scene,manifest.get('initial_scene','Scene'))
    if bpy.context.mode!='OBJECT':raise Failure('UNSUPPORTED','Source must be saved in object mode')
    atomic_json(job/'model-before.json',state());actions=[];layouts=[];conversions=[]
    for index,op in enumerate(manifest['operations']):
        scenes.prepare_context(manifest['context'])
        try:detail=execute(op,manifest['context'])
        except Failure as exc:raise Failure(exc.code,f'Operation {index} ({op["op"]}): {exc}') from exc
        actions.append({'index':index,'op':op['op'],'status':'applied_in_candidate_memory'})
        if detail is not None and op['op']=='uv.layout':
            layouts.append({'operation_index':index,**detail})
            atomic_json(job/'uv-layout-report.json',{'version':'1.0','layouts':layouts})
        elif detail is not None:
            conversions.append({'operation_index':index,**detail});atomic_json(job/'geometry-operation-report.json',{'version':'1.0','operations':conversions})
        atomic_json(job/'model-actions.json',actions)
    scenes.activate(manifest['context']);retained=[]
    for item in all_ids():
        if not item.library and not item.is_embedded_data and item.users==0 and not item.use_fake_user:
            item.use_fake_user=True;retained.append(identity(item))
    expected=state(uv_only);atomic_json(job/'model-expected.json',expected);candidate=job/'model-candidate.blend'
    if candidate.exists():raise Failure('CONFLICT','Candidate already exists')
    bpy.context.preferences.filepaths.save_version=0
    bpy.ops.wm.save_as_mainfile(filepath=str(candidate),copy=True,relative_remap=True,check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False);scenes.activate(manifest['context']);observed=state(uv_only);atomic_json(job/'model-report.json',observed)
    if uv_only:
        from uv_layout import compare_evaluation_reopen
        mismatch,evaluation=compare_evaluation_reopen(expected,observed)
        atomic_json(job/'model-evaluation-reopen.json',evaluation)
    else:mismatch=scenes.compare(expected,observed)
    if mismatch:raise Failure('VALIDATION_FAILED','Model candidate reopen mismatch at '+mismatch)
    atomic_json(job/'model-change.json',{'operations':manifest['operations'],'context':manifest['context'],'retained_orphans':retained,'reopen':'pass'})
    return {'candidate':str(candidate),'candidate_sha256':digest(candidate),'operations':len(actions),'context':manifest['context'],'reopen':'pass','report':str(job/'model-report.json'),'retained_orphans':retained,'publication':'working_candidate_only','next_step':'project plan-copy/plan-files; transaction apply; model inspect/validate final path',**({'uv_layout_report':str(job/'uv-layout-report.json')} if layouts else {})}
