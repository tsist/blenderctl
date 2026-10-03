# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded native surface-attached curves. No UV or mesh-coordinate writes."""
import hashlib,json,math
import bpy
from mathutils import Vector,Matrix
import modeling,scenes
from protocol import Failure

KEY='blenderctl_surface_guides_v1'
def sha(x):return hashlib.sha256(json.dumps(x,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def fail(message):raise Failure('VALIDATION_FAILED',message)
def finite(v):return all(math.isfinite(float(x)) for x in v)
def uv_signature(mesh,name):
    uv=mesh.uv_layers.get(name)
    if uv is None:fail('Surface UV map is missing: '+name)
    return sha([list(x.uv) for x in uv.data])
def rest_signature(mesh):return sha([list(v.co) for v in mesh.vertices])
def surface_check(o):
    if o.type!='MESH' or o.library or o.override_library or o.data.library or o.data.users!=1:raise Failure('UNSUPPORTED','Surface requires a local single-user mesh')
    if o.name not in bpy.context.view_layer.objects:raise Failure('UNSUPPORTED','Surface must belong to the active view layer')
    if any(m.type!='ARMATURE' for m in o.modifiers):raise Failure('UNSUPPORTED','Surface supports shape keys and ARMATURE modifiers only')
    if not finite([x for row in o.matrix_world for x in row]) or abs(o.matrix_world.determinant())<1e-10:fail('Surface transform must be finite and nonsingular')
    modeling.bounded(o.data)
def _inside(p,a,b,c):
    x=b-a;y=c-a;z=p-a;det=x.x*y.y-x.y*y.x
    if abs(det)<1e-12:return False
    u=(z.x*y.y-z.y*y.x)/det;v=(x.x*z.y-x.y*z.x)/det
    return u>=-1e-7 and v>=-1e-7 and u+v<=1+1e-7
def _unit(seed,identifier,kind):return int(hashlib.sha256(f'{seed}:{identifier}:{kind}'.encode()).hexdigest()[:13],16)/float(16**13)
def _resample(points,count,length):
    distances=[0.]
    for a,b in zip(points,points[1:]):
        span=(b-a).length
        if span<1e-12:fail('Guide control line contains duplicate consecutive points')
        distances.append(distances[-1]+span)
    if distances[-1]<1e-10:fail('Guide has zero arc length')
    out=[];j=0
    for i in range(count):
        d=distances[-1]*i/(count-1)
        while j<len(points)-2 and distances[j+1]<d:j+=1
        span=distances[j+1]-distances[j]
        if span<1e-12:fail('Guide control line contains duplicate consecutive points')
        out.append(points[j].lerp(points[j+1],(d-distances[j])/span))
    actual=sum((b-a).length for a,b in zip(out,out[1:]))
    if actual<1e-10:fail('Resampling collapsed the guide')
    return [p*(length/actual) for p in out]
def create(op,ctx):
    source=scenes.find(bpy.data.objects,op['surface'],True);surface_check(source);mesh=source.data
    if not source.add_rest_position_attribute and not op['enable_rest_position']:fail('Surface rest_position generation is disabled; explicit enable_rest_position is required')
    for data in (bpy.data.objects,bpy.data.hair_curves,bpy.data.node_groups):scenes.fresh(data,op['name'])
    collection=scenes.find(bpy.data.collections,op['collection'],True)
    if modeling.topology(mesh)!=op['topology_sha256']:fail('Surface topology SHA mismatch')
    uv_sha=uv_signature(mesh,op['uv_map']);uv=mesh.uv_layers[op['uv_map']];mesh.calc_loop_triangles()
    triangles=list(mesh.loop_triangles);uvtri=[[Vector(uv.data[i].uv) for i in t.loops] for t in triangles]
    ids=[r['id'] for r in op['roots']];controlids=[r['id'] for r in op['controls']]
    if len(set(ids))!=len(ids) or len(set(controlids))!=len(controlids):fail('Root/control IDs must be unique')
    if set(controlids)-set(ids):fail('Control references an unknown root ID')
    controls={c['id']:c['points'] for c in op['controls']}
    direction=Vector(op['direction']);bend=Vector(op['bend'])
    if not finite(direction) or not finite(bend) or direction.length<1e-10:fail('Guide direction must be finite and nonzero')
    direction.normalize();prepared=[]
    for root in op['roots']:
        weights=root['barycentric']
        if not finite(weights) or any(x<0 or x>1 for x in weights) or abs(sum(weights)-1)>1e-7:fail('Invalid root barycentric coordinates')
        if root['triangle']>=len(triangles):fail('Root triangle is out of range')
        t=triangles[root['triangle']];a,b,c=[mesh.vertices[i].co.copy() for i in t.vertices]
        tangent=b-a;normal=tangent.cross(c-a)
        if normal.length<1e-10:fail('Degenerate root triangle')
        normal.normalize();tangent.normalize();bitangent=normal.cross(tangent)
        coords=uvtri[root['triangle']];p=sum((x*w for x,w in zip(coords,weights)),Vector((0,0)))
        if not finite(p) or not _inside(p,*coords):fail('Degenerate or invalid root UV triangle')
        for j,other in enumerate(uvtri):
            if j==root['triangle']:continue
            if min(x.x for x in other)-1e-7<=p.x<=max(x.x for x in other)+1e-7 and min(x.y for x in other)-1e-7<=p.y<=max(x.y for x in other)+1e-7 and _inside(p,*other):fail('Ambiguous surface UV root overlaps another triangle')
        if root['id'] in controls:
            path=[Vector(x) for x in controls[root['id']]]
            if any(not finite(x) for x in path) or path[0].length>1e-10:fail('Control line must be finite and start at local zero')
        else:path=[direction*(i/128)+bend*(i/128)**2 for i in range(129)]
        length=op['length']*(1+op['length_variation']*(2*_unit(op['seed'],root['id'],'length')-1))
        path=_resample(path,op['points_per_curve'],length)
        origin=a*weights[0]+b*weights[1]+c*weights[2]
        points=[origin+tangent*x.x+bitangent*x.y+normal*x.z for x in path]
        if _unit(op['seed'],root['id'],'density')<op['density']:
            prepared.append({'id':root['id'],'triangle':root['triangle'],'vertices':list(t.vertices),'loops':list(t.loops),'barycentric':weights,'uv':list(p),'length':length,'points':[list(x) for x in points]})
    if not prepared:fail('Deterministic density selection produced no guides')
    # All source/root validation precedes mutation. This flag generates evaluated
    # rest_position; it does not add attributes to the original mesh data.
    previous=source.add_rest_position_attribute;source.add_rest_position_attribute=True
    d=bpy.data.hair_curves.new(op['name']);o=bpy.data.objects.new(op['name'],d);collection.objects.link(o)
    o.parent=source;o.matrix_parent_inverse=Matrix.Identity(4);o.matrix_basis=Matrix.Identity(4)
    d.add_curves([op['points_per_curve']]*len(prepared));d.surface=source;d.surface_uv_map=op['uv_map']
    d.attributes['position'].data.foreach_set('vector',[v for r in prepared for p in r['points'] for v in p])
    radius=d.attributes.new('radius','FLOAT','POINT');radius.data.foreach_set('value',[op['radius']]*len(d.points))
    roots=d.attributes.new('root_id','INT','CURVE');roots.data.foreach_set('value',[r['id'] for r in prepared])
    surfaceuv=d.attributes.new('surface_uv_coordinate','FLOAT2','CURVE')
    for attr,r in zip(surfaceuv.data,prepared):attr.vector=r['uv']
    g=bpy.data.node_groups.new(op['name'],'GeometryNodeTree');g.interface.new_socket(name='Geometry',in_out='INPUT',socket_type='NodeSocketGeometry');g.interface.new_socket(name='Geometry',in_out='OUTPUT',socket_type='NodeSocketGeometry')
    a=g.nodes.new('NodeGroupInput');b=g.nodes.new('GeometryNodeDeformCurvesOnSurface');c=g.nodes.new('NodeGroupOutput');g.links.new(a.outputs['Geometry'],b.inputs['Curves']);g.links.new(b.outputs['Curves'],c.inputs['Geometry'])
    mod=o.modifiers.new('SurfaceBinding','NODES');mod.node_group=g
    meta={'version':'1.0','adapter':'SURFACE_GUIDES_V1','surface':source.name,'uv_map':op['uv_map'],'topology_sha256':op['topology_sha256'],'uv_sha256':uv_sha,'rest_sha256':rest_signature(mesh),'recipe':op,'roots':prepared,'rest_position_flag_before':previous,'rest_position_flag_after':True,'coordinate_space':'SURFACE_LOCAL','node_group':g.name}
    o[KEY]=json.dumps(meta,sort_keys=True,separators=(',',':'));d.update_tag();bpy.context.view_layer.update()
    validate_bindings()
    return {'object':o.name,**meta,'curve_count':len(prepared),'point_count':len(d.points)}

def metadata(o):
    try:return json.loads(o[KEY])
    except Exception as ex:raise Failure('VALIDATION_FAILED','Malformed surface guide metadata: '+o.name) from ex
def _graph(o,m):
    if len(o.modifiers)!=1 or o.modifiers[0].type!='NODES':fail('Managed surface guide modifiers changed')
    g=o.modifiers[0].node_group
    if not g or g.library or g.name!=m['node_group']:fail('Surface binding node group changed')
    expected={'NodeGroupInput','GeometryNodeDeformCurvesOnSurface','NodeGroupOutput'}
    if len(g.nodes)!=3 or {n.bl_idname for n in g.nodes}!=expected or any(n.mute for n in g.nodes):fail('Surface binding graph contains unsupported nodes')
    links={(l.from_node.bl_idname,l.from_socket.name,l.to_node.bl_idname,l.to_socket.name) for l in g.links}
    if links!={('NodeGroupInput','Geometry','GeometryNodeDeformCurvesOnSurface','Curves'),('GeometryNodeDeformCurvesOnSurface','Curves','NodeGroupOutput','Geometry')}:fail('Surface binding graph links changed')
    if not o.modifiers[0].show_viewport or not o.modifiers[0].show_render:fail('Surface binding modifier is disabled')
def validate_bindings():
    rows=[]
    for o in bpy.context.scene.objects:
        if KEY not in o:continue
        m=metadata(o)
        if o.type!='CURVES' or o.library or o.override_library or o.data.users!=1 or o.animation_data or o.data.animation_data or o.constraints:fail('Managed guides must be local single-user unanimated unconstrained curves')
        source=bpy.data.objects.get(m['surface'])
        if source is None:fail('Bound surface is missing')
        surface_check(source)
        if o.parent!=source or o.data.surface!=source or o.data.surface_uv_map!=m['uv_map'] or not source.add_rest_position_attribute:fail('Surface binding relation or rest-position flag changed')
        if any(abs(matrix[i][j]-(1 if i==j else 0))>1e-7 for matrix in (o.matrix_basis,o.matrix_parent_inverse) for i in range(4) for j in range(4)):fail('Managed guides require identity local transforms')
        if modeling.topology(source.data)!=m['topology_sha256'] or uv_signature(source.data,m['uv_map'])!=m['uv_sha256'] or rest_signature(source.data)!=m['rest_sha256']:fail('Bound surface topology, UV or base rest geometry changed')
        _graph(o,m);roots=m['roots'];recipe=m['recipe'];d=o.data
        if len(d.curves)!=len(roots) or any(len(c.points)!=recipe['points_per_curve'] for c in d.curves):fail('Guide topology changed')
        for name,typ,domain in [('root_id','INT','CURVE'),('surface_uv_coordinate','FLOAT2','CURVE'),('radius','FLOAT','POINT')]:
            attr=d.attributes.get(name)
            if attr is None or attr.data_type!=typ or attr.domain!=domain:fail('Guide binding attribute changed: '+name)
        if [x.value for x in d.attributes['root_id'].data]!=[r['id'] for r in roots]:fail('Guide root IDs changed')
        if any((Vector(x.vector)-Vector(r['uv'])).length>1e-7 for x,r in zip(d.attributes['surface_uv_coordinate'].data,roots)):fail('Guide root UV coordinates changed')
        if any((p.position-Vector(expected)).length>1e-7 for p,expected in zip(d.points,[p for r in roots for p in r['points']])):fail('Guide rest positions changed without a new recipe')
        if any(not math.isfinite(x.value) or abs(x.value-recipe['radius'])>1e-7 for x in d.attributes['radius'].data):fail('Guide radii changed')
        rows.append({'object':o.name,'surface':source.name,'curve_count':len(roots),'point_count':len(d.points),'topology_sha256':m['topology_sha256'],'uv_sha256':m['uv_sha256'],'binding':'pass'})
    return rows

def sample(frames):
    validate_bindings();scene=bpy.context.scene;before=scene.frame_current;rows=[]
    try:
        for frame in frames:
            scene.frame_set(frame);bpy.context.view_layer.update();dg=bpy.context.evaluated_depsgraph_get();objects=[]
            for o in scene.objects:
                if KEY not in o:continue
                m=metadata(o);evaluated=o.evaluated_get(dg);curves=[]
                if len(evaluated.data.curves)!=len(m['roots']) or len(evaluated.data.points)!=sum(len(r['points']) for r in m['roots']):fail('Evaluated surface guide topology changed')
                for c,r in zip(evaluated.data.curves,m['roots']):
                    positions=[list(evaluated.matrix_world@p.position) for p in c.points]
                    if any(not finite(p) for p in positions):fail('Nonfinite evaluated guide point')
                    curves.append({'id':r['id'],'root':positions[0],'points':positions,'radii':[p.radius for p in c.points]})
                objects.append({'object':o.name,'surface':m['surface'],'coordinate_space':'WORLD','curves':curves})
            rows.append({'frame':frame,'objects':objects})
    finally:scene.frame_set(before)
    return rows

def managed_objects():return {r['object'] for r in validate_bindings()}
