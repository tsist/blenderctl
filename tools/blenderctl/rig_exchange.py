# SPDX-License-Identifier: GPL-3.0-or-later
"""B-Bone cage transfer through affine bone factors and sampled native shape keys.

The complete editable rig stays in the source. Output is a deforming cage with
ordinary bones; two TRS factors preserve affine shear in each sampled joint.
"""
import math,time,itertools
from contextlib import ExitStack
import bpy,numpy as np
from mathutils import Matrix,Vector
from protocol import Failure,atomic_json,read_json,digest
from drivers import evaluated
from exchange import snapshot,compare_geometry

def finite(points):
    if not all(math.isfinite(x) for p in points for x in p):raise Failure('VALIDATION_FAILED','Nonfinite cage geometry')
def points(obj):
    bpy.context.view_layer.update();g=bpy.context.evaluated_depsgraph_get();e=obj.evaluated_get(g);m=e.to_mesh()
    try:return [v.co.copy() for v in m.vertices]
    finally:e.to_mesh_clear()
def capture(spec,profile,job):
    originals=[(m,m.show_viewport,m.show_render) for name in spec['objects'] for m in bpy.data.objects[name].modifiers]
    try:return _capture(spec,profile,job)
    finally:
        for m,viewport,render in originals:m.show_viewport=viewport;m.show_render=render
        bpy.context.view_layer.update()
def _capture(spec,profile,job):
    selected=[bpy.data.objects[n] for n in spec['objects']]
    meshes=[o for o in selected if o.type=='MESH'];rigs=[o for o in selected if o.type=='ARMATURE']
    if len(meshes)!=1 or len(rigs)!=1 or len(selected)!=2:raise Failure('UNSUPPORTED','BBONE_CAGE_V1 selects exactly one mesh and one armature')
    body,rig=meshes[0],rigs[0];mods=list(body.modifiers)
    if bpy.context.scene.render.fps_base!=1 or bpy.context.scene.unit_settings.scale_length!=1:raise Failure('UNSUPPORTED','Cage transfer requires integer fps and meter scale one')
    if not mods or mods[0].type!='ARMATURE' or mods[0].object!=rig:raise Failure('UNSUPPORTED','Cage adapter requires the selected armature as first modifier')
    arm=mods[0]
    if not arm.show_viewport or not arm.show_render:raise Failure('UNSUPPORTED','Cage armature must be enabled for viewport and render')
    if not arm.use_vertex_groups or arm.use_bone_envelopes or arm.use_deform_preserve_volume or arm.vertex_group or arm.use_multi_modifier:raise Failure('UNSUPPORTED','Cage transfer requires unmasked vertex-group LBS without envelopes/dual-quaternion/multi modifier')
    if len(body.data.vertices)>50000 or any(m.type not in ('SUBSURF','DATA_TRANSFER','NODES') for m in mods[1:]):raise Failure('UNSUPPORTED','Cage size or post-modifier stack outside adapter')
    omitted=[{'name':m.name,'type':m.type,'viewport':m.show_viewport,'render':m.show_render} for m in mods[1:]]
    for m in mods[1:]:m.show_viewport=False
    faces=[list(p.vertices) for p in body.data.polygons]
    uv={layer.name:[list(x.uv) for x in layer.data] for layer in body.data.uv_layers}
    weights=[]
    for v in body.data.vertices:
        row=[(body.vertex_groups[g.group].name,g.weight) for g in v.groups if g.weight>0 and body.vertex_groups[g.group].name in rig.data.bones and rig.data.bones[body.vertex_groups[g.group].name].use_deform]
        total=sum(w for _,w in row)
        if total<=0:raise Failure('UNSUPPORTED','Cage has a vertex without positive deform-bone weights')
        weights.append([(n,w/total) for n,w in row])
    frames=list(range(spec['frame_start'],spec['frame_end']+1));samples=[];expected=[];mapping=None;joints=None;mesh_world=None;rig_world=None;worst=0
    for frame in frames:
        bpy.context.scene.frame_set(frame);arm.show_viewport=False;pre=points(body);arm.show_viewport=True;after=points(body);evaluated(profile)
        if len(pre)!=len(weights) or len(after)!=len(weights):raise Failure('VALIDATION_FAILED','Cage topology changed across frames')
        finite(pre);finite(after)
        if mesh_world is None:mesh_world=body.matrix_world.copy();rig_world=rig.matrix_world.copy()
        if any(abs(a-b)>1e-7 for actual,base in ((body.matrix_world,mesh_world),(rig.matrix_world,rig_world)) for ra,rb in zip(actual,base) for a,b in zip(ra,rb)):raise Failure('UNSUPPORTED','Cage adapter requires constant object transforms; pose bones may animate')
        local=rig_world.inverted()@mesh_world;r=rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
        if mapping is None:
            mapping=[]
            for p,row in zip(pre,weights):
                influences=[]
                for n,w in row:
                    pb=r.pose.bones[n]
                    if pb.bone.bbone_segments>1:
                        i,t=pb.bbone_segment_index(local@p)
                        if not 0<=i<pb.bone.bbone_segments or not 0<=t<=1:raise Failure('UNSUPPORTED','B-Bone segment index is outside validated domain')
                        influences.extend([((n,i),w*(1-t)),((n,i+1),w*t)])
                    else:influences.append(((n,0),w))
                mapping.append([(j,w) for j,w in influences if w>0])
            joints=sorted({j for row in mapping for j,w in row})
            if len(joints)>2048:raise Failure('UNSUPPORTED','Cage transfer exceeds 2048 affine joints')
        transforms={}
        for ident in joints:
            n,k=ident;pb=r.pose.bones[n]
            deformation=pb.matrix@pb.bone.matrix_local.inverted()
            if pb.bone.bbone_segments>1:deformation=pb.matrix@pb.bbone_segment_matrix(k,rest=False)@pb.bbone_segment_matrix(k,rest=True).inverted()@pb.bone.matrix_local.inverted()
            transforms[ident]=rig_world@deformation@rig_world.inverted()
        world=[mesh_world@p for p in pre]
        reconstructed=[sum((w*(transforms[j]@p) for j,w in row),Vector()) for p,row in zip(world,mapping)]
        error=max((p-mesh_world@q).length for p,q in zip(reconstructed,after));worst=max(worst,error)
        if error>1e-5:raise Failure('VALIDATION_FAILED','Fixed segment weights do not reproduce native cage deformation')
        expected.append(snapshot(frame,profile,objects=[body.name]))
        samples.append({'frame':frame,'pre':world,'matrices':[transforms[j] for j in joints]})
    ids={j:i for i,j in enumerate(joints)};mapped=[[(ids[j],w) for j,w in row] for row in mapping]
    atomic_json(job/'cage-source-samples.json',expected)
    report={'adapter':'BBONE_CAGE_V1','source_mesh':body.name,'source_rig':rig.name,'source_bones':len(rig.data.bones),'source_shape_keys':len(body.data.shape_keys.key_blocks) if body.data.shape_keys else 0,'cage_vertices':len(weights),'affine_joints':len(joints),'export_bones':len(joints)*2,'frames':frames,'independent_lbs_max_error':worst,'omitted_post_modifiers':omitted,'joint_map':[{'index':i,'source_bone':n,'segment_joint':k,'transform_bone':f'A{i:04d}','deform_bone':f'D{i:04d}'} for i,(n,k) in enumerate(joints)],'weights':mapped,'scope':'Whole selected base cage, before explicitly listed post modifiers. Native shape deformation baked to sampled morph targets. Original rig controls/constraints remain only in source; integer sample frames only.'}
    atomic_json(job/'cage-transfer-map.json',report)
    return {'samples':samples,'expected':expected,'weights':mapped,'faces':faces,'uv':uv,'joints':joints,'report':report,'fps':bpy.context.scene.render.fps}

def action(owner,name,id_type):
    a=bpy.data.actions.new(name);slot=a.slots.new(id_type=id_type,name=owner.name);a.layers.new('Samples').strips.new(type='KEYFRAME');bag=a.layers[0].strips[0].channelbag(slot,ensure=True);ad=owner.animation_data_create();ad.action=a;ad.action_slot=slot;return bag
def curve(bag,path,index,frames,values):
    c=bag.fcurves.new(data_path=path,index=index);c.keyframe_points.add(len(frames));c.keyframe_points.foreach_set('co',[x for f,v in zip(frames,values) for x in (f,v)])
    for p in c.keyframe_points:p.interpolation='LINEAR'
    c.update()
def factors(matrix,stable_euler=False):
    u,scale,vt=np.linalg.svd(np.array(matrix,dtype=float)[:3,:3])
    if min(scale)<1e-8:raise Failure('UNSUPPORTED','Singular affine joint is outside TRS factor adapter')
    if np.linalg.det(u)<0:u[:,-1]*=-1;scale[-1]*=-1
    if np.linalg.det(vt)<0:vt[-1,:]*=-1;scale[-1]*=-1
    if stable_euler:
        # SVD permits matching proper signed axis permutations without changing
        # U*S*Vt. Move both factor rotations away from XYZ Euler poles used by
        # FBX, rather than changing the geometric error budget.
        best=None
        for order in itertools.permutations(range(3)):
            for signs in itertools.product((-1,1),repeat=3):
                p=np.eye(3)[:,order]*signs
                if np.linalg.det(p)<0:continue
                a=u@p;b=p.T@vt
                score=min(math.hypot(a[0,0],a[1,0]),math.hypot(b[0,0],b[1,0]))
                if best is None or score>best[0]:best=(score,a,scale[list(order)],b)
        _,u,scale,vt=best
    return list(matrix.translation),list(Matrix(u.tolist()).to_quaternion()),scale.tolist(),list(Matrix(vt.tolist()).to_quaternion())
def build(data,job,reset=True,suffix='',stable_euler=False):
    if reset:bpy.ops.wm.read_factory_settings(use_empty=True)
    s=bpy.context.scene;s.name='CageTransfer';s.render.fps=data['fps'];s.frame_start=data['samples'][0]['frame'];s.frame_end=data['samples'][-1]['frame']
    for o in s.objects:o.select_set(False)
    mesh=bpy.data.meshes.new('CageMesh'+suffix);mesh.from_pydata(data['samples'][0]['pre'],[],data['faces']);mesh.update();body=bpy.data.objects.new('CageBody'+suffix,mesh);s.collection.objects.link(body)
    for name,uv in data['uv'].items():mesh.uv_layers.new(name=name).data.foreach_set('uv',[v for p in uv for v in p])
    arm=bpy.data.armatures.new('CageSkeleton'+suffix);rig=bpy.data.objects.new('CageRig'+suffix,arm);s.collection.objects.link(rig);bpy.context.view_layer.objects.active=rig;rig.select_set(True);bpy.ops.object.mode_set(mode='EDIT')
    for i in range(len(data['joints'])):
        parent=arm.edit_bones.new(f'A{i:04d}');parent.head=(0,0,0);parent.tail=(0,.01,0);parent.use_deform=False
        child=arm.edit_bones.new(f'D{i:04d}');child.head=(0,0,0);child.tail=(0,.01,0);child.parent=parent;child.use_connect=False
    bpy.ops.object.mode_set(mode='OBJECT')
    for pb in rig.pose.bones:pb.rotation_mode='QUATERNION'
    groups=[body.vertex_groups.new(name=f'D{i:04d}') for i in range(len(data['joints']))]
    for v,row in enumerate(data['weights']):
        for i,w in row:groups[i].add([v],w,'REPLACE')
    body.parent=rig
    m=body.modifiers.new('TransferredLBS','ARMATURE');m.object=rig;m.use_deform_preserve_volume=False
    frames=[r['frame'] for r in data['samples']];bag=action(rig,'CagePoseSamples','OBJECT')
    for i in range(len(data['joints'])):
        values=[factors(r['matrices'][i],stable_euler) for r in data['samples']]
        for column in (1,3):
            for j in range(1,len(values)):
                if sum(a*b for a,b in zip(values[j-1][column],values[j][column]))<0:values[j][column][:]=[-x for x in values[j][column]]
        for name,prop,column,width in ((f'A{i:04d}','location',0,3),(f'A{i:04d}','rotation_quaternion',1,4),(f'A{i:04d}','scale',2,3),(f'D{i:04d}','rotation_quaternion',3,4)):
            path=rig.pose.bones[name].path_from_id(prop)
            for k in range(width):curve(bag,path,k,frames,[v[column][k] for v in values])
    body.shape_key_add(name='Basis');keys=[]
    for row in data['samples'][1:]:
        key=body.shape_key_add(name=f'NativeShape_{row["frame"]:06d}');key.data.foreach_set('co',[x for p in row['pre'] for x in p]);keys.append((row['frame'],key))
    if keys:
        keybag=action(body.data.shape_keys,'CageShapeSamples','KEY')
        for frame,key in keys:curve(keybag,key.path_from_id('value'),0,frames,[1.0 if f==frame else 0.0 for f in frames])
    s.frame_set(frames[0]);bpy.context.view_layer.update();checks=[]
    for ref in data['expected']:
        observed=snapshot(ref['frame'],objects=[body.name]);check=compare_geometry(ref,observed)
        error=max((Vector(p)-Vector(q)).length for p,q in zip(ref['vertices'],observed['vertices']))
        check['indexed_vertex_error']=error;check['ok'] &= len(ref['vertices'])==len(observed['vertices']) and error<=1e-5
        checks.append(check)
    atomic_json(job/'cage-native-checks.json',checks)
    if not all(c['ok'] for c in checks):raise Failure('VALIDATION_FAILED','Transferred affine bones/morphs differ from original cage')
    for o in s.objects:o.select_set(True)
    return body,rig

def structure(expected_bones,expected_shapes,body_name=None):
    meshes=[o for o in bpy.context.scene.objects if o.type=='MESH' and (body_name is None or o.name==body_name)]
    rigs=[o for o in bpy.context.scene.objects if o.type=='ARMATURE'] if body_name is None else list({m.object for o in meshes for m in o.modifiers if m.type=='ARMATURE' and m.object})
    if len(meshes)!=1 or len(rigs)!=1 or len(rigs[0].data.bones)!=expected_bones:raise Failure('VALIDATION_FAILED','Transferred skeleton or mesh missing after import')
    body,rig=meshes[0],rigs[0]
    shapes=len(body.data.shape_keys.key_blocks) if body.data.shape_keys else 1
    if shapes!=expected_shapes:raise Failure('VALIDATION_FAILED','Sampled morph targets missing after import')
    if len(body.modifiers)!=1 or body.modifiers[0].type!='ARMATURE' or body.modifiers[0].object!=rig:raise Failure('VALIDATION_FAILED','Imported skin is not bound to the transferred skeleton')
    if any(not any(g.weight>0 and body.vertex_groups[g.group].name in rig.data.bones for g in v.groups) for v in body.data.vertices):raise Failure('VALIDATION_FAILED','Imported skin has unweighted vertices')
    return {'bones':len(rig.data.bones),'vertices_after_uv_splits':len(body.data.vertices),'shape_keys':shapes,'all_vertices_weighted':True}

def export_cage(params,spec,job,started):
    import exchange,scenes,nodes,simulation
    profile=params.get('driver_profile')
    if not profile:raise Failure('INVALID_REQUEST','Cage transfer requires native driver profile')
    with ExitStack() as stack:
        hashes=nodes.resource_guards(params,stack)
        has_sim=any(s.rigidbody_world for s in bpy.data.scenes) or any(m.type in ('CLOTH','SOFT_BODY','FLUID') for o in bpy.data.objects for m in o.modifiers)
        if has_sim:
            descriptor=params.get('simulation_receipt')
            if not descriptor:raise Failure('INVALID_REQUEST','Cage physics source requires a verified simulation receipt')
            receipt=read_json(descriptor['file']);request=receipt['request']
            if request.get('adapter')!='RIG_CLOTH_V1' or request['scene']!=spec['scene'] or request['view_layer']!=spec['view_layer'] or spec['frame_start']<request['frame_start'] or spec['frame_end']>request['frame_end']:raise Failure('CONFLICT','Cage frames/context outside RIG_CLOTH_V1 receipt')
            simulation.bake({'file':params['file'],'manifest':{**request,'mode':'reuse','receipt':descriptor},'driver_profile':profile},job)
        else:
            if params.get('simulation_receipt'):raise Failure('INVALID_REQUEST','Receipt supplied for source without supported physics')
            exchange.safe_source(profile,params['file'],job)
        s=scenes.find(bpy.data.scenes,spec['scene']);layer=scenes.find(s.view_layers,spec['view_layer']);bpy.context.window.scene=s;bpy.context.window.view_layer=layer
        for name in spec['objects']:
            o=scenes.find(bpy.data.objects,name)
            if o.name not in layer.objects or o.hide_viewport or not o.visible_get():raise Failure('UNSUPPORTED','Cage selection must be visible in selected view layer')
        if spec.get('rig_adapter')=='BBONE_MULTI_CAGE_V1':
            from skin_exchange import export_multi
            return export_multi(params,spec,job,started,hashes)
        data=capture(spec,profile,job);build(data,job);s=bpy.context.scene;s.frame_set(spec['frame_start']);path=job/'export.glb'
        bpy.context.preferences.filepaths.save_version=0
        bpy.ops.wm.save_as_mainfile(filepath=str(job/'cage-transfer.blend'),relative_remap=False,check_existing=False)
        exchange.export_file(path,spec)
        bpy.ops.wm.read_factory_settings(use_empty=True);bpy.context.scene.render.fps=data['fps'];exchange.import_file(path,'GLB','ANIMATION')
        skeletal=structure(data['report']['export_bones'],len(data['samples']))
        observed=[snapshot(r['frame']) for r in data['expected']];checks=[compare_geometry(a,b) for a,b in zip(data['expected'],observed)]
        atomic_json(job/'exchange-before.json',data['expected']);atomic_json(job/'exchange-after.json',observed);atomic_json(job/'exchange-checks.json',checks)
        if not all(c['ok'] for c in checks):raise Failure('VALIDATION_FAILED','Fresh GLB import differs from native cage')
        candidate=exchange.save_candidate(job);bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False)
        structure(data['report']['export_bones'],len(data['samples']))
        reopened=[compare_geometry(r,snapshot(r['frame'])) for r in reversed(data['expected'])];atomic_json(job/'cage-reopen-checks.json',reopened)
        if not all(c['ok'] for c in reopened):raise Failure('VALIDATION_FAILED','Reopened cage differs from native source')
        report={'exchange_report_version':'1.0','operation':'export','format':'GLB','mode':'ANIMATION','settings':spec,'checks':checks,'outputs':[{'file':str(path),'sha256':digest(path),'bytes':path.stat().st_size}],'candidate':str(candidate),'candidate_sha256':digest(candidate),'resource_hashes':hashes,'seconds':time.monotonic()-started,'source_saved':False,'reopen':'pass','material_profile':[],'losses':[data['report']['scope'],'Post modifiers omitted only in transfer copy: '+str(data['report']['omitted_post_modifiers']),'Materials omitted by explicit NONE contract; full native rig and cloth remain in source.'],'rig_transfer':{'adapter':'BBONE_CAGE_V1','source_vertices':data['report']['cage_vertices'],'source_bones':data['report']['source_bones'],'affine_joints':len(data['joints']),'frames':[r['frame'] for r in data['samples']],'independent_lbs_max_error':data['report']['independent_lbs_max_error'],'map':str(job/'cage-transfer-map.json'),**skeletal}}
        atomic_json(job/'exchange-report.json',report);return report
