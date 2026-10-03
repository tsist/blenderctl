# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic surface children driven live by one nearest parent guide."""
import bpy,numpy as np,json,random,math
from mathutils import Vector,Matrix
import scenes,hair_shape,hair_shape_nodes,hair_dynamics
from hair_volume_contract import normalize
from protocol import Failure
KEY='blenderctl_triangle_children_v1'
def fail(msg):raise Failure('UNSUPPORTED',msg)
def metadata(o):
    try:return json.loads(o[KEY])
    except Exception as e:raise Failure('UNSUPPORTED','Malformed density metadata') from e
def source_info(source):
    if source.type!='CURVES':fail('Density requires native managed Curves')
    if hair_dynamics.KEY in source:
        r=json.loads(source[hair_dynamics.KEY]);guide=bpy.data.objects[r['source']]
        if r['adapter']!='RIBBON_COLLIDERS_V1' or not bpy.data.objects[r['proxy']].modifiers[-1].point_cache.is_baked:fail('Density dynamic source must have a baked multi-collider cache')
    else:guide=source
    if hair_shape.KEY not in guide:fail('Density parent must use TRIANGLE_GROOM_V1 final surface binding')
    r=hair_shape.metadata(guide);return bpy.data.objects[r['surface']],r['recipe']
def coordinates(o,dg):
    e=o.evaluated_get(dg);d=e.data
    sizes=[len(c.points) for c in d.curves]
    if not sizes or len(sizes)>512 or min(sizes)<2 or max(sizes)>64 or sum(sizes)>32768:fail('Source exceeds 512 curves / 64 points / 32768 total points')
    p=hair_shape.floats(d.points,'position',3);r=hair_shape.floats(d.attributes['radius'].data,'value',1).ravel();ids=[v.value for v in d.attributes['root_id'].data]
    if len(ids)!=len(sizes) or len(set(ids))!=len(ids) or not np.isfinite(p).all() or not np.isfinite(r).all() or min(r)<0:fail('Invalid source curve positions/radii/IDs')
    witness=hair_dynamics.digest({'sizes':sizes,'positions':hair_shape.hashed(p),'radii':hair_shape.hashed(r),'ids':ids,'matrix':[list(v) for v in e.matrix_world]})
    return e,p,r,sizes,ids,witness
def definition(surface,source):
    nodes,links=hair_shape_nodes.definition(surface)
    nodes['Guide']=('GeometryNodeObjectInfo',{'transform_space':'RELATIVE'},{0:source,1:False})
    for name,attr in [('ParentPoint','density_source_index'),('ParentRoot','density_root_index')]:
        nodes[name+'Index']=('GeometryNodeInputNamedAttribute',{'data_type':'INT'},{0:attr})
        nodes[name]=('GeometryNodeSampleIndex',{'data_type':'FLOAT_VECTOR','domain':'POINT','clamp':False},{})
        links.extend([('Guide',4,name,0),('Position',0,name,1),(name+'Index',0,name,2)])
    nodes['ParentDelta']=('ShaderNodeVectorMath',{'operation':'SUBTRACT'},{2:[0,0,0],3:1.0})
    nodes['ChildPoint']=('ShaderNodeVectorMath',{'operation':'ADD'},{2:[0,0,0],3:1.0})
    links.remove(('Result',0,'Set',2));links.extend([('ParentPoint',0,'ParentDelta',0),('ParentRoot',0,'ParentDelta',1),('ParentDelta',0,'ChildPoint',0),('Result',0,'ChildPoint',1),('ChildPoint',0,'Set',2)])
    return nodes,links
def create(op,ctx):
    normalize(op);source=scenes.find(bpy.data.objects,op['source'],True);surface,spec=source_info(source);s=bpy.context.scene
    if (op['frame_start'],op['frame_end'])!=(spec['frame_start'],spec['frame_end']) or (ctx['frame'],s.frame_start,s.frame_end)!=(op['frame_start'],op['frame_start'],op['frame_end']):fail('Density source, scene and context must share the complete frame window')
    for data in [bpy.data.objects,bpy.data.hair_curves,bpy.data.node_groups]:scenes.fresh(data,op['name'])
    collection=scenes.find(bpy.data.collections,op['collection'],True);dg=bpy.context.evaluated_depsgraph_get();e,co,tri,state=hair_shape.mesh(surface,dg);parent,points,radii,sizes,ids,_=coordinates(source,dg)
    if len(set(sizes))!=1 or op['count']*sizes[0]>32768:fail('Density requires equal parent point counts and at most 32768 child points')
    if max(op['region_triangles'])>=len(tri):fail('Density triangle region out of bounds')
    size=sizes[0];root_world=[parent.matrix_world@Vector(points[i*size]) for i in range(len(sizes))];weights=[]
    for index in op['region_triangles']:
        a,b,c=[e.matrix_world@Vector(co[i]) for i in tri[index]];area=(b-a).cross(c-a).length/2
        if area<1e-12:fail('Degenerate density region triangle')
        weights.append(area)
    rng=random.Random(op['seed']);roots=[]
    for identifier in range(op['count']):
        index=rng.choices(op['region_triangles'],weights=weights,k=1)[0];q=math.sqrt(rng.random());v=rng.random();bary=[1-q,q*(1-v),q*v];vertices=list(map(int,tri[index]));a,b,c,t,side,n=hair_shape.frame_basis(co,vertices);root=a*bary[0]+b*bary[1]+c*bary[2]+n*op['root_offset'];world=e.matrix_world@root
        which=min(range(len(root_world)),key=lambda i:((world-root_world[i]).length_squared,ids[i]))
        if (world-root_world[which]).length>op['max_guide_distance']:fail('Child root exceeds nearest-guide distance budget')
        roots.append({'id':identifier,'parent_id':ids[which],'parent_start':which*size,'triangle':index,'vertices':vertices,'barycentric':bary})
    witnesses=[];before=s.frame_current
    try:
        for f in range(op['frame_start'],op['frame_end']+1):
            s.frame_set(f);dg=bpy.context.evaluated_depsgraph_get();_,coords,triangles,w=hair_shape.mesh(surface,dg);*_,pw=coordinates(source,dg)
            if w['topology_sha256']!=state['topology_sha256']:fail('Density surface topology changed')
            for r in roots:
                if list(triangles[r['triangle']])!=r['vertices']:fail('Density anchor triangle changes with animation')
                hair_shape.frame_basis(coords,r['vertices'])
            witnesses.append({'frame':f,'surface':w,'source_sha256':pw})
    finally:s.frame_set(before)
    d=bpy.data.hair_curves.new(op['name']);d.add_curves([size]*len(roots));o=bpy.data.objects.new(op['name'],d);collection.objects.link(o);o.parent=surface;o.matrix_basis=Matrix.Identity(4);o.matrix_parent_inverse=Matrix.Identity(4)
    for m in source.data.materials:d.materials.append(m)
    def attr(name,typ,domain,prop,values):
        a=d.attributes.get(name) or d.attributes.new(name,typ,domain);a.data.foreach_set(prop,values)
    for j in range(3):attr('groom_v'+str(j),'INT','POINT','value',[r['vertices'][j] for r in roots for _ in range(size)])
    attr('groom_weights','FLOAT_VECTOR','POINT','vector',[v for r in roots for _ in range(size) for v in r['barycentric']])
    attr('groom_offset','FLOAT_VECTOR','POINT','vector',[v for _ in range(size*len(roots)) for v in [0,0,op['root_offset']]])
    attr('density_source_index','INT','POINT','value',[r['parent_start']+j for r in roots for j in range(size)])
    attr('density_root_index','INT','POINT','value',[r['parent_start'] for r in roots for _ in range(size)])
    attr('root_id','INT','CURVE','value',[r['id'] for r in roots]);attr('parent_id','INT','CURVE','value',[r['parent_id'] for r in roots])
    attr('radius','FLOAT','POINT','value',[radii[r['parent_start']+j]*op['radius_scale'] for r in roots for j in range(size)])
    o.modifiers.new('LiveSurfaceChildren','NODES').node_group=hair_dynamics.graph(op['name'],'children',surface,source,blueprint=definition(surface,source));d.update_tag()
    r={'op':op,'object':o.name,'source':source.name,'surface':surface.name,'size':size,'roots':roots,'witnesses':witnesses,'data_sha256':hair_shape.data_signature(d),'algorithm':'area-weighted seeded roots, nearest-guide translated local offsets; no child collision solver'};o[KEY]=json.dumps(r,sort_keys=True,separators=(',',':'));bpy.context.view_layer.update()
    if op['hide_source']:source.hide_render=True
    sample([before]);return r
def validate():
    rows=[]
    for o in bpy.context.scene.objects:
        if KEY not in o:continue
        r=metadata(o);op=normalize(r['op']);source=scenes.find(bpy.data.objects,r['source'],True);surface,_=source_info(source)
        if o.type!='CURVES' or o.data.surface or o.library or o.data.library or o.override_library or o.data.users!=1 or o.data.animation_data:fail('Density data ownership changed')
        hair_dynamics.identity(o,surface)
        if o.name!=r['object'] or surface.name!=r['surface'] or [m.type for m in o.modifiers]!=['NODES'] or not o.modifiers[0].show_viewport or not o.modifiers[0].show_render:fail('Density object/stack changed')
        hair_dynamics.check_graph(o.modifiers[0].node_group,'children',surface,source,blueprint=definition(surface,source))
        if hair_shape.data_signature(o.data)!=r['data_sha256']:fail('Density attributes, radii or topology changed')
        if [x['frame'] for x in r['witnesses']]!=list(range(op['frame_start'],op['frame_end']+1)):fail('Density witness frame inventory changed')
        rows.append({'object':o.name,'surface':surface.name,'adapter':op['adapter'],'curve_count':len(o.data.curves),'point_count':len(o.data.points),'binding':'pass'})
    return rows
def managed_objects():return {r['object'] for r in validate()}
def sample(frames):
    validate();s=bpy.context.scene;before=s.frame_current;rows=[]
    try:
        for f in frames:
            s.frame_set(f);dg=bpy.context.evaluated_depsgraph_get();objects=[]
            for o in s.objects:
                if KEY not in o:continue
                r=metadata(o);op=r['op']
                if not op['frame_start']<=f<=op['frame_end']:fail('Density sample outside witness window')
                surface=bpy.data.objects[r['surface']];source=bpy.data.objects[r['source']];e,co,tri,w=hair_shape.mesh(surface,dg);parent,pts,radii,sizes,ids,pw=coordinates(source,dg)
                if {'frame':f,'surface':w,'source_sha256':pw}!=r['witnesses'][f-op['frame_start']]:fail('Density source or final surface differs from frame witness')
                out=o.evaluated_get(dg);curves=[]
                if len(out.data.curves)!=len(r['roots']):fail('Density evaluated count changed')
                for c,root in zip(out.data.curves,r['roots']):
                    if len(c.points)!=r['size']:fail('Density evaluated size changed')
                    a,b,cc,t,side,n=hair_shape.frame_basis(co,root['vertices']);w=root['barycentric'];origin=a*w[0]+b*w[1]+cc*w[2]+n*op['root_offset'];start=root['parent_start'];points=[]
                    for j,p in enumerate(c.points):
                        actual=out.matrix_world@p.position;expected=e.matrix_world@(origin+Vector(pts[start+j])-Vector(pts[start]));radius=float(radii[start+j])*op['radius_scale']
                        if not all(math.isfinite(x) for x in actual) or (actual-expected).length>1e-5 or abs(p.radius-radius)>1e-7:raise Failure('VALIDATION_FAILED','Native child differs from its anchored parent guide')
                        points.append(list(actual))
                    curves.append({'id':root['id'],'root':points[0],'points':points,'radii':[p.radius for p in c.points]})
                objects.append({'object':o.name,'surface':surface.name,'coordinate_space':'WORLD','curves':curves})
            rows.append({'frame':f,'objects':objects})
    finally:s.frame_set(before)
    return rows
def verify_all():
    frames=sorted({f['frame'] for o in bpy.context.scene.objects if KEY in o for f in metadata(o)['witnesses']})
    return sample(list(reversed(frames))) if frames else []
