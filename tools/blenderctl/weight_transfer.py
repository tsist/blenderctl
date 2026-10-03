# SPDX-License-Identifier: GPL-3.0-or-later
"""Weight transfer from immutable base-rest triangles with per-vertex evidence.

This adapter never invokes automatic binding, never reads evaluated deformation,
and never prunes excess influences silently. Distance is measured in world units.
"""
import math
from mathutils.bvhtree import BVHTree
from protocol import Failure
import modeling,scenes

def fail(code,message):raise Failure(code,message)
def rows(item):
    names={g.index:g.name for g in item.vertex_groups}
    return [{names[g.group]:float(g.weight) for g in v.groups} for v in item.data.vertices]

def transfer_rig(name):
    import bpy
    rig=scenes.find(bpy.data.objects,name,True)
    if rig.type!='ARMATURE':fail('INVALID_REQUEST','Weight adapter requires an Armature')
    if any(item.library or item.override_library for item in (rig,rig.data)):fail('UNSUPPORTED','Weight adapter requires a local non-override rig')
    if len(rig.data.bones)>4096:fail('UNSUPPORTED','Weight adapter rig exceeds 4096 bones; explicit selected bones remain bounded to 256')
    return rig

def mesh(name):
    item=scenes.find(__import__('bpy').data.objects,name,True)
    if item.type!='MESH':fail('INVALID_REQUEST','Weight transfer requires meshes')
    for data in (item,item.data):
        if data.library or data.override_library or data.animation_data:fail('UNSUPPORTED','Weight transfer requires local unanimated objects and mesh data')
    if item.data.shape_keys or item.constraints:fail('UNSUPPORTED','Weight transfer excludes shape keys and object constraints')
    # Parent motion can change the coordinate frame independently of base data.
    parent=item.parent
    while parent:
        if parent.animation_data or parent.constraints:fail('UNSUPPORTED','Weight transfer excludes animated or constrained ancestors')
        parent=parent.parent
    modeling.bounded(item.data)
    if not item.data.vertices:fail('INVALID_REQUEST','Weight transfer requires nonempty meshes')
    if not all(math.isfinite(x) for row in item.matrix_world for x in row):fail('VALIDATION_FAILED','Nonfinite world transform')
    if abs(item.matrix_world.determinant())<1e-12:fail('UNSUPPORTED','Singular world transform')
    return item

def barycentric(point,a,b,c):
    u=b-a;v=c-a;w=point-a
    uu=u.dot(u);uv=u.dot(v);vv=v.dot(v);wu=w.dot(u);wv=w.dot(v);den=uu*vv-uv*uv
    if den<=0:fail('VALIDATION_FAILED','Degenerate source triangle')
    y=(vv*wu-uv*wv)/den;z=(uu*wv-uv*wu)/den
    values=[1-y-z,y,z]
    if not all(math.isfinite(x) and -1e-5<=x<=1.00001 for x in values):fail('VALIDATION_FAILED','Invalid closest-point barycentric coordinates')
    values=[max(0,min(1,float(x))) for x in values];total=sum(values)
    return [x/total for x in values]

def execute(op,context=None):
    if op.get('op')=='skin.auto_weights':return auto_weights(op,context)
    from weight_transfer_contract import OP
    from scene_contract import validate
    validate(op,OP)
    source=mesh(op['source']);target=mesh(op['target']);rig=transfer_rig(op['rig'])
    if source==target:fail('CONFLICT','Transfer source and target must differ')
    if target.modifiers:fail('UNSUPPORTED','Transfer target must be unbound and have no modifiers')
    if any(m.type!='ARMATURE' for m in source.modifiers):fail('UNSUPPORTED','Transfer source permits only Armature modifiers; base rest coordinates are used')
    if any(m.object!=rig for m in source.modifiers):fail('CONFLICT','Source Armature modifier must reference the declared rig')
    if target.parent:fail('UNSUPPORTED','Transfer target must have no parent before binding')
    if modeling.topology(source.data)!=op['source_topology_sha256'] or modeling.topology(target.data)!=op['target_topology_sha256']:fail('CONFLICT','Weight transfer topology SHA mismatch')
    mapping=op['mapping'];src_names=[r['source'] for r in mapping];dst_names=[r['target'] for r in mapping]
    if len(set(src_names))!=len(mapping) or len(set(dst_names))!=len(mapping):fail('INVALID_REQUEST','Weight mapping must be one-to-one')
    for name in src_names:
        if name not in source.vertex_groups:fail('NOT_FOUND','Source group missing: '+name)
    for name in dst_names:
        if name not in rig.data.bones or not rig.data.bones[name].use_deform:fail('INVALID_REQUEST','Target group must name a declared rig deform bone: '+name)
    if any(name in target.vertex_groups for name in dst_names) and not op['replace']:fail('CONFLICT','Replacing target groups requires replace=true')
    if any(target.vertex_groups[name].lock_weight for name in dst_names if name in target.vertex_groups):fail('CONFLICT','Target mapped group is locked')
    # Retained deformation groups would alter normalized skinning after transfer.
    deform={b.name for b in rig.data.bones if b.use_deform}
    before=rows(target);source_weights=rows(source)
    if any(any(name in deform and name not in dst_names and weight>0 for name,weight in row.items()) for row in before):fail('CONFLICT','Unmapped target deform weights would violate normalization')
    tolerance=op['max_normalization_error']
    for row in source_weights:
        if any(not math.isfinite(weight) or weight<0 or weight>1 for weight in row.values()):fail('VALIDATION_FAILED','Source weights must be finite and within [0,1]')
        if abs(sum(row.get(name,0) for name in src_names)-1)>tolerance:fail('VALIDATION_FAILED','Selected source groups must normalize at every source vertex')
    if target.data.users>1:
        if op['data_scope']=='reject_shared':fail('CONFLICT','Shared target weights require single_user')
        if target.data.asset_data or target.data.get('asset_id'):fail('UNSUPPORTED','Cannot duplicate target mesh asset identity')
    points=[source.matrix_world@v.co for v in source.data.vertices]
    targets=[target.matrix_world@v.co for v in target.data.vertices]
    if not all(math.isfinite(x) for p in [*points,*targets] for x in p):fail('VALIDATION_FAILED','Nonfinite rest geometry')
    source.data.calc_loop_triangles();triangles=[tuple(t.vertices) for t in source.data.loop_triangles]
    if not triangles:fail('UNSUPPORTED','Transfer source requires a surface')
    if any((points[b]-points[a]).cross(points[c]-points[a]).length_squared<=1e-20 for a,b,c in triangles):fail('UNSUPPORTED','Source has degenerate triangles')
    tree=BVHTree.FromPolygons(points,triangles,all_triangles=True)
    evidence=[];planned=[]
    for index,point in enumerate(targets):
        near,normal,triangle,distance=tree.find_nearest(point)
        if triangle is None or not math.isfinite(distance) or distance>op['max_distance']:fail('VALIDATION_FAILED','Rest-surface distance budget exceeded at target vertex '+str(index))
        ids=triangles[triangle];basis=barycentric(near,*(points[i] for i in ids))
        weights={dst:sum(basis[k]*source_weights[vi].get(src,0) for k,vi in enumerate(ids)) for src,dst in zip(src_names,dst_names)}
        total=sum(weights.values())
        if not math.isfinite(total) or abs(total-1)>tolerance or total<=0:fail('VALIDATION_FAILED','Transferred normalization budget exceeded')
        weights={name:weight/total for name,weight in weights.items() if weight>0}
        if len(weights)>op['max_influences']:fail('VALIDATION_FAILED','Transferred influence budget exceeded; no automatic pruning')
        # Equidistant disconnected surfaces with differing weights are ambiguous.
        tie_epsilon=max(1e-7,abs(distance)*1e-6)
        for alt,_,tri2,dist2 in tree.find_nearest_range(point,distance+tie_epsilon):
            if tri2==triangle or abs(dist2-distance)>tie_epsilon:continue
            ids2=triangles[tri2];basis2=barycentric(alt,*(points[i] for i in ids2))
            other={dst:sum(basis2[k]*source_weights[vi].get(src,0) for k,vi in enumerate(ids2)) for src,dst in zip(src_names,dst_names)}
            if any(abs(other[name]-weights.get(name,0))>max(tolerance,1e-6) for name in dst_names):fail('CONFLICT','Ambiguous equidistant source surfaces at target vertex '+str(index))
        planned.append(weights);evidence.append({'vertex':index,'distance':float(distance),'source_triangle':triangle,'source_vertices':list(ids),'barycentric':basis,'weights':weights})
    # All validation precedes mutation. Single-user policy isolates mesh storage.
    copied=target.data.users>1
    if copied:target.data=target.data.copy()
    for name in dst_names:
        group=target.vertex_groups.get(name) or target.vertex_groups.new(name=name)
        group.remove(list(range(len(target.data.vertices))))
        for index,weight in enumerate(planned):
            if name in weight:group.add([index],weight[name],'REPLACE')
    target.data.update();scenes.update();after=rows(target)
    error=max(abs(sum(row.get(name,0) for name in dst_names)-1) for row in after)
    if error>tolerance:fail('VALIDATION_FAILED','Stored float weights exceed normalization budget')
    if modeling.topology(target.data)!=op['target_topology_sha256'] or rows(source)!=source_weights:fail('VALIDATION_FAILED','Topology or source weights changed during transfer')
    for old,new in zip(before,after):
        if {k:v for k,v in old.items() if k not in dst_names}!={k:v for k,v in new.items() if k not in dst_names}:fail('VALIDATION_FAILED','Unmapped group weights changed')
    return {'weight_transfer_report_version':'1.0','adapter':op['adapter'],'purpose':op['purpose'],
        'coordinate_space':'WORLD_BASE_REST','source_modifiers':'ARMATURE_IGNORED_BASE_REST',
        'source':source.name,'target':target.name,'rig':rig.name,'mapping':mapping,'bound':False,
        'source_topology_sha256':op['source_topology_sha256'],'target_topology_sha256':op['target_topology_sha256'],
        'single_user_copy':copied,'max_distance':max(r['distance'] for r in evidence),'distance_budget':op['max_distance'],
        'max_normalization_error':error,'normalization_budget':tolerance,'max_influences':max(len(w) for w in planned),
        'vertices':evidence,'before_weights':before,'after_weights':after,'source_unchanged':True,
        'scope':'Nearest base-rest triangle interpolation only; no binding, deformation or clothing-fit claim'}

def auto_weights(op,context=None):
    from weight_transfer_contract import AUTO_OP
    from scene_contract import validate
    validate(op,AUTO_OP)
    target=mesh(op['target']);rig=transfer_rig(op['rig']);names=op['bones']
    if len(names)!=len(set(names)):fail('INVALID_REQUEST','Automatic weight bones must be unique')
    if target.modifiers or target.parent:fail('UNSUPPORTED','Automatic weight target must be unbound, unparented and have no modifiers')
    if any(name not in rig.data.bones or not rig.data.bones[name].use_deform for name in names):fail('INVALID_REQUEST','Automatic weight bones must be explicit deform bones')
    if any(name in target.vertex_groups for name in names) and not op['replace']:fail('CONFLICT','Replacing target groups requires replace=true')
    if any(target.vertex_groups[name].lock_weight for name in names if name in target.vertex_groups):fail('CONFLICT','Target mapped group is locked')
    before=rows(target);deform={b.name for b in rig.data.bones if b.use_deform}
    if any(any(n in deform and n not in names and w>0 for n,w in row.items()) for row in before):fail('CONFLICT','Unselected target deform weights would violate normalization')
    if target.data.users>1:
        if op['data_scope']=='reject_shared':fail('CONFLICT','Shared target weights require single_user')
        if target.data.asset_data or target.data.get('asset_id'):fail('UNSUPPORTED','Cannot duplicate target mesh asset identity')
    segments=[(name,rig.matrix_world@rig.data.bones[name].head_local,rig.matrix_world@rig.data.bones[name].tail_local) for name in names]
    if any(not all(math.isfinite(x) for p in (a,b) for x in p) or (b-a).length_squared<1e-20 for _,a,b in segments):fail('UNSUPPORTED','Invalid rest bone world segment')
    topology=modeling.topology(target.data);evidence=[];planned=[]
    for vertex in target.data.vertices:
        point=target.matrix_world@vertex.co
        if not all(math.isfinite(x) for x in point):fail('VALIDATION_FAILED','Nonfinite rest vertex')
        distances=[]
        for name,a,b in segments:
            edge=b-a;t=max(0,min(1,(point-a).dot(edge)/edge.length_squared));distance=(point-(a+t*edge)).length
            if not math.isfinite(distance):fail('VALIDATION_FAILED','Nonfinite bone distance')
            distances.append((distance,name,float(t)))
        distances.sort();nearest=distances[0][0]
        if nearest<=1e-8:fail('CONFLICT','Zero-distance bone assignment requires explicit manual weights')
        if nearest>op['max_distance']:fail('VALIDATION_FAILED','Bone distance budget exceeded at target vertex '+str(vertex.index))
        selected=[row for row in distances if row[0]<=op['max_distance']][:op['max_influences']]
        tie_epsilon=max(1e-7,selected[-1][0]*1e-6)
        for i in range(len(distances)-1):
            if i>=len(selected):break
            if abs(distances[i][0]-distances[i+1][0])<=tie_epsilon:fail('CONFLICT','Equidistant bone segments require explicit manual weights at target vertex '+str(vertex.index))
        # Scale by nearest distance to avoid overflow at very small distances.
        raw={name:(nearest/distance)**op['power'] for distance,name,_ in selected};total=sum(raw.values());weights={name:w/total for name,w in raw.items()}
        planned.append(weights);evidence.append({'vertex':vertex.index,'distances':[{'bone':name,'distance':distance,'segment_t':t} for distance,name,t in distances],'weights':weights})
    copied=target.data.users>1
    if copied:target.data=target.data.copy()
    for name in names:
        group=target.vertex_groups.get(name) or target.vertex_groups.new(name=name);group.remove(list(range(len(target.data.vertices))))
        for index,weights in enumerate(planned):
            if name in weights:group.add([index],weights[name],'REPLACE')
    target.data.update();scenes.update();after=rows(target)
    error=max(abs(sum(row.get(name,0) for name in names)-1) for row in after)
    if error>1e-6 or modeling.topology(target.data)!=topology:fail('VALIDATION_FAILED','Stored auto weights or topology differ from contract')
    if any({k:v for k,v in a.items() if k not in names}!={k:v for k,v in b.items() if k not in names} for a,b in zip(before,after)):fail('VALIDATION_FAILED','Unselected groups changed')
    return {'auto_weights_report_version':'1.0','adapter':op['adapter'],'purpose':'BODY','target':target.name,'rig':rig.name,'bones':names,
        'coordinate_space':'WORLD_BASE_REST','bound':False,'single_user_copy':copied,'topology_sha256':topology,
        'distance_budget':op['max_distance'],'max_nearest_distance':max(row['distances'][0]['distance'] for row in evidence),
        'power':op['power'],'max_influences':max(len(w) for w in planned),'max_normalization_error':error,
        'vertices':evidence,'before_weights':before,'after_weights':after,
        'scope':'Inverse-distance rest bone segments only; no heat solver, anatomy inference, binding or deformation-quality claim'}
