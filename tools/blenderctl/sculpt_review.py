# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only base mesh comparisons and neutral geometry previews, not brush replay."""
import bpy,math,shutil
from pathlib import Path
from mathutils import Vector
from protocol import Failure,atomic_json,digest
from review_contract import normalize,key

def snapshot(path,target):
    bpy.ops.wm.open_mainfile(filepath=path,load_ui=False,use_scripts=False)
    if bpy.data.libraries or any(getattr(d,'animation_data',None) for coll in (bpy.data.objects,bpy.data.meshes,bpy.data.scenes) for d in coll):
        raise Failure('UNSUPPORTED','Sculpt geometry review requires local meshes without animation')
    objects={}
    for o in bpy.data.objects:
        if o.type!='MESH' or o.modifiers or o.constraints or o.parent or o.data.shape_keys:
            raise Failure('UNSUPPORTED','Review scope is plain unparented meshes only; no modifiers, constraints or shape keys')
        if len(o.data.vertices)>100000 or len(objects)>=32:raise Failure('UNSUPPORTED','Review mesh bounds exceeded')
        row={'vertices':[list(v.co) for v in o.data.vertices],'edges':[list(e.vertices) for e in o.data.edges],'faces':[list(p.vertices) for p in o.data.polygons],'matrix':[list(r) for r in o.matrix_world]}
        key(row);objects[o.name]=row
    if target not in objects:raise Failure('NOT_FOUND','Review target not found')
    return objects

def previews(row,label,job,center,radius):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene=bpy.context.scene
    mesh=bpy.data.meshes.new('Review Geometry');mesh.from_pydata(row['vertices'],row['edges'],row['faces']);mesh.update()
    o=bpy.data.objects.new('Review Target',mesh);scene.collection.objects.link(o)
    from mathutils import Matrix
    o.matrix_world=Matrix(row['matrix'])
    mat=bpy.data.materials.new('Neutral Clay');mat.diffuse_color=(.5,.55,.6,1);o.data.materials.append(mat)
    scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=8
    scene.render.resolution_x=192;scene.render.resolution_y=192;scene.render.resolution_percentage=100
    scene.world=bpy.data.worlds.new('Review World');scene.world.use_nodes=True;scene.world.node_tree.nodes['Background'].inputs['Color'].default_value=(.15,.15,.15,1)
    light=bpy.data.lights.new('Review Key','AREA');light.energy=700*radius*radius;light.shape='DISK';light.size=radius*4
    lo=bpy.data.objects.new('Review Key',light);scene.collection.objects.link(lo);lo.location=center+Vector((2,-3,4))*radius;lo.rotation_euler=(center-lo.location).to_track_quat('-Z','Y').to_euler()
    camera=bpy.data.cameras.new('Review Camera');camera.type='ORTHO';camera.ortho_scale=radius*2.5
    co=bpy.data.objects.new('Review Camera',camera);scene.collection.objects.link(co);scene.camera=co
    outputs=[]
    for name,direction in [('front',(0,-1,0)),('oblique',(1,-1,1))]:
        co.location=center+Vector(direction).normalized()*radius*5;co.rotation_euler=(center-co.location).to_track_quat('-Z','Y').to_euler()
        p=job/('sculpt-'+label+'-'+name+'.png');scene.render.filepath=str(p);scene.render.image_settings.file_format='PNG'
        bpy.ops.render.render(write_still=True)
        outputs.append({'view':name,'file':str(p),'expected_sha256':digest(p)})
    return outputs

def run(params,job):
    spec=normalize(params,'sculpt.review')['manifest'];target=spec['object']
    before=snapshot(spec['before']['file'],target);after=snapshot(spec['after']['file'],target)
    non_target_before={k:key(v) for k,v in before.items() if k!=target};non_target_after={k:key(v) for k,v in after.items() if k!=target}
    a,b=before[target],after[target]
    topology_same=key([a['edges'],a['faces']])==key([b['edges'],b['faces']]) and len(a['vertices'])==len(b['vertices'])
    delta=[(Vector(x)-Vector(y)).length for x,y in zip(a['vertices'],b['vertices'])] if topology_same else []
    from mathutils import Matrix
    pts=[Matrix(row['matrix'])@Vector(v) for row in (a,b) for v in row['vertices']]
    if not pts:raise Failure('UNSUPPORTED','Empty target cannot be previewed')
    low=Vector([min(p[i] for p in pts) for i in range(3)]);high=Vector([max(p[i] for p in pts) for i in range(3)]);center=(low+high)*.5;radius=(high-low).length*.5
    if not math.isfinite(radius) or not 1e-5<radius<1e5:raise Failure('UNSUPPORTED','Target bounds cannot be previewed')
    images=previews(a,'before',job,center,radius)+previews(b,'after',job,center,radius)
    recovery=job/'sculpt-recovery.blend';shutil.copyfile(spec['before']['file'],recovery)
    if digest(recovery)!=spec['before']['expected_sha256']:raise Failure('CONFLICT','Recovery copy differs from before source')
    report={'version':'1.0','kind':'sculpt_geometry_review','inputs':spec,'target':target,'topology_unchanged':topology_same,'changed_vertices':sum(x>1e-7 for x in delta) if topology_same else None,'max_displacement_local':max(delta,default=0) if topology_same else None,'target_transform_unchanged':key(a['matrix'])==key(b['matrix']),'non_target_geometry_unchanged':key(non_target_before)==key(non_target_after),'non_target_before':non_target_before,'non_target_after':non_target_after,'previews':images,'recovery':{'file':str(recovery),'expected_sha256':digest(recovery)},'artistic_quality':'pending_human_review','brush_replay_verified':False,'scope':'Base vertex/edge/face geometry and object transforms only. Neutral reconstructed geometry previews; materials, UV, visibility, custom data and full-scene preservation are not assessed. Recovery is a new byte-identical source copy; no original overwritten.'}
    atomic_json(job/'sculpt-review.json',report);return report
