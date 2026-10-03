# SPDX-License-Identifier: GPL-3.0-or-later
"""New native curve copies, constrained projection and live evaluated skin bind."""
import json,math,hashlib
import bpy,numpy as np
from mathutils import Vector,Matrix
from mathutils.bvhtree import BVHTree
import hair_surface,hair_dynamics,modeling,scenes,hair_shape_nodes
from hair_shape_contract import normalize
from protocol import Failure
KEY='blenderctl_triangle_groom_v1'
def fail(s):raise Failure('UNSUPPORTED',s)
def hashed(a):return hashlib.sha256(a.tobytes()).hexdigest()
def floats(data,prop,width):
    a=np.empty((len(data),width),dtype=np.float32);data.foreach_get(prop,a.ravel());return a
def surface_check(o):
    if o.type!='MESH' or o.library or o.override_library or o.data.library or o.data.users!=1 or not o.visible_get():fail('Shape surface requires a visible local single-user mesh')
    if bpy.context.scene.render.use_simplify:fail('Shape binding excludes simplify-dependent render geometry')
    from rig_cloth import NODES
    for m in o.modifiers:
        if m.show_viewport!=m.show_render:fail('Shape surface viewport/render modifier visibility must match')
        if not m.show_viewport:continue
        if m.type=='SUBSURF':
            if m.levels!=m.render_levels or m.levels>2:fail('Shape surface viewport/render subdivision must match, at most two levels')
        elif m.type=='NODES':
            if not m.node_group or any(n.bl_idname not in NODES for n in m.node_group.nodes):fail('Shape surface geometry nodes outside admitted profile')
        elif m.type!='ARMATURE':fail('Shape surface supports shapes, ARMATURE, SUBSURF and admitted native attribute nodes only')
    modeling.bounded(o.data)
def mesh(o,dg):
    surface_check(o);e=o.evaluated_get(dg);d=e.data
    if not 3<=len(d.vertices)<=250000:fail('Shape surface exceeds 250000 evaluated vertices')
    if abs(e.matrix_world.determinant())<1e-10:fail('Shape surface transform is singular')
    co=floats(d.vertices,'co',3)
    if not np.isfinite(co).all():fail('Nonfinite evaluated shape surface')
    d.calc_loop_triangles();tri=np.empty((len(d.loop_triangles),3),np.int32);d.loop_triangles.foreach_get('vertices',tri.ravel())
    loops=np.empty(len(d.loops),np.int32);d.loops.foreach_get('vertex_index',loops)
    sizes=np.empty(len(d.polygons),np.int32);d.polygons.foreach_get('loop_total',sizes)
    topology=hashlib.sha256(str(len(co)).encode()+sizes.tobytes()+loops.tobytes()).hexdigest()
    return e,co,tri,{'vertices':len(co),'triangles':len(tri),'topology_sha256':topology,'triangulation_sha256':hashed(tri),'positions_sha256':hashed(co),'matrix_world':[list(r) for r in e.matrix_world]}
def frame_basis(co,indices):
    a,b,c=[Vector(co[i]) for i in indices];ab=b-a;normal=ab.cross(c-a)
    if ab.length<1e-9 or normal.length<1e-12:fail('Degenerate evaluated anchor triangle')
    t=ab.normalized();n=normal.normalized();return a,b,c,t,n.cross(t),n
def radius_at(profile,u):
    for a,b in zip(profile,profile[1:]):
        if u<=b['u']:return a['radius']+(b['radius']-a['radius'])*(u-a['u'])/(b['u']-a['u'])
    return profile[-1]['radius']
def data_signature(d):
    props={'FLOAT_VECTOR':('vector',3),'FLOAT':('value',1),'INT':('value',1)};rows=[]
    for a in d.attributes:
        if a.data_type not in props:fail('Unknown groom attribute')
        prop,width=props[a.data_type];values=np.empty(len(a.data)*width,np.int32 if a.data_type=='INT' else np.float32);a.data.foreach_get(prop,values)
        rows.append((a.name,a.data_type,a.domain,hashed(values)))
    return hair_dynamics.digest({'sizes':[len(c.points) for c in d.curves],'attributes':rows})
def create(op,ctx):
    normalize(op);source=scenes.find(bpy.data.objects,op['source'],True);surface=scenes.find(bpy.data.objects,op['surface'],True)
    if source.type!='CURVES' or not (hair_surface.KEY in source or KEY in source):fail('Shape source must be a managed, non-simulated guide')
    hair_surface.validate_bindings();validate_bindings();surface_check(surface)
    if op['name'] in (source.name,surface.name):fail('Shape output must be a new distinct object')
    for data in [bpy.data.objects,bpy.data.hair_curves,bpy.data.node_groups]:scenes.fresh(data,op['name'])
    collection=scenes.find(bpy.data.collections,op['collection'],True);scene=bpy.context.scene
    if (ctx['frame'],scene.frame_start,scene.frame_end)!=(op['frame_start'],op['frame_start'],op['frame_end']):fail('Shape prepare context and scene must match the declared range start/end')
    dg=bpy.context.evaluated_depsgraph_get();e,co,tri,state=mesh(surface,dg);src=source.evaluated_get(dg);ids=[x.value for x in src.data.attributes['root_id'].data]
    if not 1<=len(ids)<=128 or len(set(ids))!=len(ids):fail('Shape requires 1..128 unique root IDs')
    overrides={r['id']:r['points'] for r in op['controls']}
    if set(overrides)-set(ids):fail('Shape controls reference unknown root IDs')
    region=op['region_triangles']
    if max(region)>=len(tri):fail('Shape region triangle out of range')
    world=[e.matrix_world@Vector(p) for p in co];polys=[tuple(int(v) for v in tri[i]) for i in region];tree=BVHTree.FromPolygons(world,polys,all_triangles=True)
    prepared=[]
    for curve,identifier in zip(src.data.curves,ids):
        root=src.matrix_world@curve.points[0].position;hit,normal,index,distance=tree.find_nearest(root)
        if hit is None or distance>op['max_projection_distance']:fail('Root projection exceeds the declared region/distance budget: '+str(identifier))
        for point,n,idx,dist in tree.find_nearest_range(root,distance+1e-7):
            if idx!=index and abs(dist-distance)<=1e-7 and ((point-hit).length>1e-6 or n.dot(normal)<.9999):fail('Ambiguous equidistant surface projection')
        triangle=region[index];indices=[int(i) for i in tri[triangle]];a,b,c,t,bitan,n=frame_basis(co,indices)
        local=e.matrix_world.inverted()@hit
        # Solve about the triangle origin in float64. mathutils' float32
        # barycentric_transform loses partition-of-unity on millimetre triangles
        # far from the coordinate origin (observed sum error 6.9e-5 on real skin).
        origin=np.asarray(a,dtype=np.float64);edges=np.column_stack((np.asarray(b,dtype=np.float64)-origin,np.asarray(c,dtype=np.float64)-origin))
        if np.linalg.cond(edges)>1e6:fail('Ill-conditioned projection triangle')
        uv=np.linalg.lstsq(edges,np.asarray(local,dtype=np.float64)-origin,rcond=None)[0];bary=np.array([1-uv.sum(),*uv])
        if not np.isfinite(bary).all() or min(bary)<-1e-5 or np.linalg.norm(origin+edges@uv-np.asarray(local))>1e-6:fail('Projection barycentric coordinates are invalid')
        bary=np.maximum(bary,0);bary/=bary.sum()
        if identifier in overrides:path=[Vector(v) for v in overrides[identifier]]
        else:
            inv=e.matrix_world.inverted();origin=inv@root;path=[]
            for p in curve.points:
                v=inv@(src.matrix_world@p.position)-origin;path.append(Vector((v.dot(t),v.dot(bitan),v.dot(n))))
        path=hair_surface._resample(path,op['points_per_curve'],op['length']);arc=[0.]
        for p,q in zip(path,path[1:]):arc.append(arc[-1]+(q-p).length)
        radii=[radius_at(op['radius_profile'],u/arc[-1]) for u in arc]
        offset=[list(p+Vector((0,0,op['root_offset']))) for p in path]
        prepared.append({'id':identifier,'triangle':triangle,'vertices':indices,'barycentric':list(bary),'projection_distance_world':distance,'offsets':offset,'radii':radii})
    before=scene.frame_current;witnesses=[]
    try:
        for f in range(op['frame_start'],op['frame_end']+1):
            scene.frame_set(f);_,coords,triangles,witness=mesh(surface,bpy.context.evaluated_depsgraph_get())
            if witness['topology_sha256']!=state['topology_sha256']:fail('Evaluated surface topology changed across shape frames')
            for r in prepared:
                if list(triangles[r['triangle']])!=r['vertices']:fail('Selected anchor triangle changed across shape frames')
                frame_basis(coords,r['vertices'])
            witnesses.append({'frame':f,**witness})
    finally:scene.frame_set(before)
    d=bpy.data.hair_curves.new(op['name']);d.add_curves([op['points_per_curve']]*len(prepared));o=bpy.data.objects.new(op['name'],d);collection.objects.link(o);o.parent=surface;o.matrix_basis=Matrix.Identity(4);o.matrix_parent_inverse=Matrix.Identity(4)
    for material in source.data.materials:d.materials.append(material)
    def attr(name,typ,domain,prop,values):
        a=d.attributes.get(name) or d.attributes.new(name,typ,domain);a.data.foreach_set(prop,values)
    for j in range(3):attr('groom_v'+str(j),'INT','POINT','value',[r['vertices'][j] for r in prepared for _ in r['offsets']])
    attr('groom_weights','FLOAT_VECTOR','POINT','vector',[v for r in prepared for _ in r['offsets'] for v in r['barycentric']])
    attr('groom_offset','FLOAT_VECTOR','POINT','vector',[v for r in prepared for p in r['offsets'] for v in p])
    attr('radius','FLOAT','POINT','value',[v for r in prepared for v in r['radii']]);attr('root_id','INT','CURVE','value',ids)
    positions=[]
    for r in prepared:
        a,b,c,t,bitan,n=frame_basis(co,r['vertices']);base=a*r['barycentric'][0]+b*r['barycentric'][1]+c*r['barycentric'][2]
        positions.extend(v for p in r['offsets'] for v in base+t*p[0]+bitan*p[1]+n*p[2])
    attr('position','FLOAT_VECTOR','POINT','vector',positions)
    o.modifiers.new('EvaluatedTriangleBinding','NODES').node_group=hair_dynamics.graph(op['name'],'shape',surface,blueprint=hair_shape_nodes.definition(surface))
    record={'adapter':op['adapter'],'object':o.name,'source':source.name,'surface':surface.name,'recipe':op,'roots':prepared,'witnesses':witnesses,'data_sha256':data_signature(d),'space':'SURFACE_LOCAL_ORTHONORMAL_TRIANGLE_FRAME'}
    o[KEY]=json.dumps(record,sort_keys=True,separators=(',',':'));d.update_tag();bpy.context.view_layer.update()
    if op['hide_source']:source.hide_render=True
    validate_bindings();sample([before]);return record
def metadata(o):
    try:return json.loads(o[KEY])
    except Exception as e:raise Failure('UNSUPPORTED','Malformed shape metadata') from e
def validate_bindings():
    rows=[]
    for o in bpy.context.scene.objects:
        if KEY not in o:continue
        r=metadata(o);op=normalize(r['recipe']);surface=scenes.find(bpy.data.objects,r['surface'],True);surface_check(surface)
        if o.type!='CURVES' or o.data.surface or o.library or o.override_library or o.data.library or o.data.users!=1 or o.data.animation_data:fail('Managed groom data ownership/binding changed')
        hair_dynamics.identity(o,surface)
        if o.name!=r['object'] or [m.type for m in o.modifiers]!=['NODES'] or not o.modifiers[0].show_viewport or not o.modifiers[0].show_render:fail('Managed groom identity/modifier changed')
        hair_dynamics.check_graph(o.modifiers[0].node_group,'shape',surface,blueprint=hair_shape_nodes.definition(surface))
        if data_signature(o.data)!=r['data_sha256']:fail('Managed groom geometry, radii, IDs or binding attributes changed; create a new shape recipe')
        if [f['frame'] for f in r['witnesses']]!=list(range(op['frame_start'],op['frame_end']+1)):fail('Shape witness frame inventory changed')
        rows.append({'object':o.name,'surface':surface.name,'curve_count':len(o.data.curves),'point_count':len(o.data.points),'adapter':op['adapter'],'binding':'pass'})
    return rows
def managed_objects():return {r['object'] for r in validate_bindings()}
def sample(frames):
    validate_bindings();scene=bpy.context.scene;before=scene.frame_current;rows=[]
    try:
        for frame in frames:
            scene.frame_set(frame);dg=bpy.context.evaluated_depsgraph_get();objects=[];cache={}
            for o in scene.objects:
                if KEY not in o:continue
                r=metadata(o);op=r['recipe']
                if not op['frame_start']<=frame<=op['frame_end']:fail('Shape sample outside the declared range')
                surface=bpy.data.objects[r['surface']]
                if surface.name not in cache:cache[surface.name]=mesh(surface,dg)
                e,co,tri,witness=cache[surface.name]
                if {'frame':frame,**witness}!=r['witnesses'][frame-op['frame_start']]:fail('Evaluated shape surface differs from its frame witness')
                evaluated=o.evaluated_get(dg);curves=[]
                if len(evaluated.data.curves)!=len(r['roots']):fail('Evaluated groom curve count changed')
                if [v.value for v in evaluated.data.attributes['root_id'].data]!=[v['id'] for v in r['roots']]:fail('Evaluated groom IDs changed')
                for c,root in zip(evaluated.data.curves,r['roots']):
                    if len(c.points)!=op['points_per_curve']:fail('Evaluated groom point count changed')
                    a,b,cc,t,bitan,n=frame_basis(co,root['vertices']);expected=e.matrix_world@(a*root['barycentric'][0]+b*root['barycentric'][1]+cc*root['barycentric'][2]+n*op['root_offset'])
                    pts=[list(evaluated.matrix_world@p.position) for p in c.points];error=(Vector(pts[0])-expected).length
                    if not all(math.isfinite(v) for p in pts for v in p) or error>1e-5:raise Failure('VALIDATION_FAILED','Native groom root differs from evaluated triangle attachment')
                    origin=a*root['barycentric'][0]+b*root['barycentric'][1]+cc*root['barycentric'][2]
                    for p,xyz,offset,radius in zip(c.points,pts,root['offsets'],root['radii']):
                        predicted=e.matrix_world@(origin+t*offset[0]+bitan*offset[1]+n*offset[2])
                        if (Vector(xyz)-predicted).length>1e-5 or not math.isfinite(p.radius) or abs(p.radius-radius)>1e-7:raise Failure('VALIDATION_FAILED','Native groom shape or radius profile differs from its recipe')
                    curves.append({'id':root['id'],'root':pts[0],'points':pts,'radii':[p.radius for p in c.points]})
                objects.append({'object':o.name,'surface':surface.name,'coordinate_space':'WORLD','curves':curves})
            rows.append({'frame':frame,'objects':objects})
    finally:scene.frame_set(before)
    return rows
def verify_all():
    frames=sorted({f['frame'] for o in bpy.context.scene.objects if KEY in o for f in metadata(o)['witnesses']})
    if not frames:return []
    if len(frames)>32:fail('Combined groom witness range exceeds 32 frames')
    return sample(list(reversed(frames)))
