"""Read-only mesh reviews, including saved Multires and texture-paint evidence."""
import hashlib
import bpy,math,shutil
from pathlib import Path
from mathutils import Vector
from protocol import Failure,atomic_json,digest
from review_contract import normalize,key

def _paint_state(obj):
    images = {}
    materials = [slot.material for slot in obj.material_slots if slot.material and slot.material.node_tree]
    pending = [material.node_tree for material in materials]
    visited = set()
    while pending:
        tree = pending.pop()
        if tree in visited:
            continue
        visited.add(tree)
        for node in tree.nodes:
            if node.type == 'GROUP' and node.node_tree:
                pending.append(node.node_tree)
            if node.type == 'TEX_IMAGE' and node.image:
                image = node.image
                images[image.name] = {
                    'source': image.source, 'color_space': image.colorspace_settings.name,
                    'files': [{'tile': packed.tile_number, 'sha256': hashlib.sha256(packed.packed_file.data).hexdigest()}
                              for packed in image.packed_files if packed.packed_file],
                    'external_path': None if image.packed_file else image.filepath,
                }
    return dict(sorted(images.items()))


def snapshot(path,target,advanced=False):
    bpy.ops.wm.open_mainfile(filepath=path,load_ui=False,use_scripts=False)
    if bpy.data.libraries or any(getattr(d,'animation_data',None) for coll in (bpy.data.objects,bpy.data.meshes,bpy.data.scenes) for d in coll):
        raise Failure('UNSUPPORTED','Sculpt geometry review requires local meshes without animation')
    objects={}
    depsgraph = bpy.context.evaluated_depsgraph_get() if advanced else None
    for o in bpy.data.objects:
        if advanced and o.type != 'MESH':
            continue
        modifiers = list(o.modifiers)
        allow_modifiers = advanced and all(mod.type == 'MULTIRES' for mod in modifiers)
        if o.type!='MESH' or modifiers and not allow_modifiers or o.constraints or not advanced and o.parent or o.data.shape_keys:
            raise Failure('UNSUPPORTED','Review scope is plain unparented meshes only; no modifiers, constraints or shape keys')
        if len(o.data.vertices)>100000 or len(objects)>=32:raise Failure('UNSUPPORTED','Review mesh bounds exceeded')
        row={'vertices':[list(v.co) for v in o.data.vertices],'edges':[list(e.vertices) for e in o.data.edges],'faces':[list(p.vertices) for p in o.data.polygons],'matrix':[list(r) for r in o.matrix_world]}
        if advanced:
            row['multires'] = [{'name': mod.name, 'viewport': mod.levels, 'sculpt': mod.sculpt_levels,
                                'render': mod.render_levels, 'total': mod.total_levels,
                                'external_displacement': bool(mod.is_external)} for mod in modifiers]
            row['paint_images'] = _paint_state(o)
            evaluated = o.evaluated_get(depsgraph)
            mesh = evaluated.to_mesh()
            try:
                if len(mesh.vertices) > 100000 or len(mesh.loops) > 400000:
                    raise Failure('RESOURCE_LIMIT','Multires review exceeds saved viewport mesh bounds')
                row['evaluated'] = {'vertices': [list(vertex.co) for vertex in mesh.vertices],
                                    'edges': [list(edge.vertices) for edge in mesh.edges],
                                    'faces': [list(face.vertices) for face in mesh.polygons],
                                    'matrix': [list(r) for r in evaluated.matrix_world]}
            finally:
                evaluated.to_mesh_clear()
        key(row);objects[o.name]=row
    if target not in objects:raise Failure('NOT_FOUND','Review target not found')
    return objects

def previews(row,label,job,center,radius,*,samples=8,size=192):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene=bpy.context.scene
    mesh=bpy.data.meshes.new('Review Geometry');mesh.from_pydata(row['vertices'],row['edges'],row['faces']);mesh.update()
    o=bpy.data.objects.new('Review Target',mesh);scene.collection.objects.link(o)
    from mathutils import Matrix
    o.matrix_world=Matrix(row['matrix'])
    mat=bpy.data.materials.new('Neutral Clay');mat.diffuse_color=(.5,.55,.6,1);o.data.materials.append(mat)
    scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=samples
    scene.render.resolution_x=size;scene.render.resolution_y=size;scene.render.resolution_percentage=100
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
    advanced = spec.get('mode') == 'MULTIRES_PAINT'
    before=snapshot(spec['before']['file'],target,advanced);after=snapshot(spec['after']['file'],target,advanced)
    non_target_before={k:key(v) for k,v in before.items() if k!=target};non_target_after={k:key(v) for k,v in after.items() if k!=target}
    a,b=before[target],after[target]
    topology_same=key([a['edges'],a['faces']])==key([b['edges'],b['faces']]) and len(a['vertices'])==len(b['vertices'])
    delta=[(Vector(x)-Vector(y)).length for x,y in zip(a['vertices'],b['vertices'])] if topology_same else []
    from mathutils import Matrix
    visual = (a['evaluated'], b['evaluated']) if advanced else (a, b)
    pts=[Matrix(row['matrix'])@Vector(v) for row in visual for v in row['vertices']]
    if not pts:raise Failure('UNSUPPORTED','Empty target cannot be previewed')
    low=Vector([min(p[i] for p in pts) for i in range(3)]);high=Vector([max(p[i] for p in pts) for i in range(3)]);center=(low+high)*.5;radius=(high-low).length*.5
    if not math.isfinite(radius) or not 1e-5<radius<1e5:raise Failure('UNSUPPORTED','Target bounds cannot be previewed')
    preview_settings = {'samples': 2, 'size': 144} if advanced else {}
    images=previews(visual[0],'before',job,center,radius,**preview_settings)+previews(visual[1],'after',job,center,radius,**preview_settings)
    recovery=job/'sculpt-recovery.blend';shutil.copyfile(spec['before']['file'],recovery)
    if digest(recovery)!=spec['before']['expected_sha256']:raise Failure('CONFLICT','Recovery copy differs from before source')
    report={'version':'1.0','kind':'sculpt_geometry_review','inputs':spec,'target':target,'topology_unchanged':topology_same,'changed_vertices':sum(x>1e-7 for x in delta) if topology_same else None,'max_displacement_local':max(delta,default=0) if topology_same else None,'target_transform_unchanged':key(a['matrix'])==key(b['matrix']),'non_target_geometry_unchanged':key(non_target_before)==key(non_target_after),'non_target_before':non_target_before,'non_target_after':non_target_after,'previews':images,'recovery':{'file':str(recovery),'expected_sha256':digest(recovery)},'artistic_quality':'pending_human_review','brush_replay_verified':False,'scope':'Base vertex/edge/face geometry and object transforms only. Neutral reconstructed geometry previews; materials, UV, visibility, custom data and full-scene preservation are not assessed. Recovery is a new byte-identical source copy; no original overwritten.'}
    if advanced:
        report['scope'] = 'Saved base and viewport-evaluated geometry, Multires levels, and packed material image fingerprints.'
        report['multires_paint'] = {
            label: {'modifiers': row['multires'], 'paint_images': row['paint_images'],
                    'evaluated_vertices': len(row['evaluated']['vertices']),
                    'evaluated_faces': len(row['evaluated']['faces']),
                    'evaluated_sha256': key(row['evaluated'])}
            for label, row in (('before', a), ('after', b))}
    atomic_json(job/'sculpt-review.json',report);return report
