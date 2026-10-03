# SPDX-License-Identifier: GPL-3.0-or-later
"""Isolated CPU previews and UV baking; only write new artifacts in the job directory."""
import math
from contextlib import ExitStack
import bpy,bmesh
from mathutils import Vector
from protocol import Failure,atomic_json,digest
import scenes,modeling,nodes
from node_contract import NODE_TYPES

def safe_tree(tree,seen=None):
    seen=set() if seen is None else seen
    if tree.as_pointer() in seen:raise Failure('CONFLICT','Recursive material group')
    if len(seen)>50:raise Failure('UNSUPPORTED','Material graph depth exceeds limit')
    if tree.animation_data:raise Failure('UNSUPPORTED','Animated node trees require separate animation validation')
    for n in tree.nodes:
        if n.bl_idname not in NODE_TYPES['ShaderNodeTree']:raise Failure('UNSUPPORTED','Preview/bake node outside supported shader types: '+n.bl_idname)
        if getattr(n,'node_tree',None):safe_tree(n.node_tree,seen|{tree.as_pointer()})

def safe_material(material):
    if not material or not material.use_nodes or not material.node_tree:raise Failure('INVALID_REQUEST','A node material is required')
    if material.animation_data:raise Failure('UNSUPPORTED','Animated material is outside this output workflow')
    safe_tree(material.node_tree)

def output_check(path,colorspace=None):
    image=bpy.data.images.load(str(path),check_existing=False)
    if colorspace:image.colorspace_settings.name=colorspace
    try:
        pixels=list(image.pixels)
        if not pixels or any(not math.isfinite(v) for v in pixels):raise Failure('VALIDATION_FAILED','Invalid rendered pixels')
        rgb=[v for i,v in enumerate(pixels) if i%4!=3]
        return {'file':str(path),'sha256':digest(path),'size':list(image.size),'rgb_min':min(rgb),'rgb_max':max(rgb),'channels':image.channels,'decoded_colorspace':image.colorspace_settings.name}
    finally:bpy.data.images.remove(image)

def preview(params,job):
    bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    mat=scenes.find(bpy.data.materials,params['material']);safe_material(mat)
    with ExitStack() as guards:
        resource_hashes=nodes.resource_guards(params,guards)
        sequence_evidence=nodes.sequence_bindings(params['frame'])
        scene=bpy.data.scenes.new('blenderctl-preview-'+job.name[:8]);bpy.context.window.scene=scene
        mesh=bpy.data.meshes.new('PreviewSphere');bm=bmesh.new()
        try:bmesh.ops.create_uvsphere(bm,u_segments=64,v_segments=32,radius=1);bm.to_mesh(mesh)
        finally:bm.free()
        obj=bpy.data.objects.new('PreviewSphere',mesh);scene.collection.objects.link(obj);mesh.materials.append(mat)
        for p in mesh.polygons:p.use_smooth=True
        # UV sphere BMesh construction does not create a UV map; deterministic angular mapping.
        uv=mesh.uv_layers.new(name='UVMap')
        for polygon in mesh.polygons:
            coords=[]
            for loop_index in polygon.loop_indices:
                co=mesh.vertices[mesh.loops[loop_index].vertex_index].co;coords.append([(math.atan2(co.y,co.x)/(2*math.pi))%1,math.acos(max(-1,min(1,co.z)))/math.pi])
            if max(c[0] for c in coords)-min(c[0] for c in coords)>.5:
                for c in coords:
                    if c[0]<.5:c[0]+=1
            for i,co in zip(polygon.loop_indices,coords):uv.data[i].uv=co
        camera=bpy.data.objects.new('PreviewCamera',bpy.data.cameras.new('PreviewCamera'));scene.collection.objects.link(camera);camera.location=(3,-4,2);camera.rotation_euler=(-camera.location).to_track_quat('-Z','Y').to_euler();camera.data.lens=55;scene.camera=camera
        for name,location,energy,size in [('Key',(2,-3,4),650,4),('Fill',(-3,-1,1),250,3)]:
            light=bpy.data.objects.new('Preview'+name,bpy.data.lights.new('Preview'+name,'AREA'));scene.collection.objects.link(light);light.location=location;light.rotation_euler=(-light.location).to_track_quat('-Z','Y').to_euler();light.data.energy=energy;light.data.shape='DISK';light.data.size=size
        world=bpy.data.worlds.new('PreviewWorld');world.use_nodes=True;world.node_tree.nodes['Background'].inputs['Color'].default_value=(.055,.055,.055,1);world.node_tree.nodes['Background'].inputs['Strength'].default_value=.4;scene.world=world
        scene.frame_set(params['frame']);scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=params['samples'];scene.cycles.use_denoising=False
        scene.render.resolution_x=scene.render.resolution_y=params['resolution'];scene.render.resolution_percentage=100;scene.render.image_settings.file_format='PNG';scene.render.film_transparent=True;scene.render.use_compositing=False
        path=job/'material-preview.png';scene.render.filepath=str(path);modeling.finished(bpy.ops.render.render(write_still=True,scene=scene.name))
        report={**output_check(path),'material':mat.name,'frame':params['frame'],'engine':'CYCLES_CPU','samples':params['samples'],'scene':'isolated_sphere_studio','uv_map':'UVMap','color_management':{'display':scene.display_settings.display_device,'view_transform':scene.view_settings.view_transform,'look':scene.view_settings.look,'exposure':scene.view_settings.exposure,'gamma':scene.view_settings.gamma},'sequence_bindings':sequence_evidence,'resource_hashes':resource_hashes,'source_saved':False,'scope':'visual_material_preview_not_publication_approval'}
        atomic_json(job/'material-preview.json',report);return report

def bake(params,job):
    bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    obj=scenes.find(bpy.data.objects,params['object']);nodes.mutable(obj)
    if obj.type!='MESH' or obj.modifiers or obj.data.shape_keys:raise Failure('UNSUPPORTED','Bake requires a mesh without modifiers or shape keys; apply to a working candidate first')
    uv=scenes.find(obj.data.uv_layers,params['uv_layer']);uv.active_render=True;obj.data.uv_layers.active=uv
    if not obj.material_slots or any(s.material is None for s in obj.material_slots):raise Failure('INVALID_REQUEST','Every bake slot must have a material')
    for slot in obj.material_slots:safe_material(slot.material)
    report_mesh=modeling.mesh_report(obj.data);uv_report=next(u for u in report_mesh['uv_layers'] if u['name']==params['uv_layer'])
    if not obj.data.polygons or uv_report['degenerate_triangles'] or any(v<-.00001 or v>1.00001 for v in uv_report['bounds']):raise Failure('VALIDATION_FAILED','Bake requires nondegenerate UVs inside 0..1')
    with ExitStack() as guards:
        resource_hashes=nodes.resource_guards(params,guards)
        nodes.sequence_bindings(params['frame'])
        context={'scene':params['scene'],'view_layer':params['view_layer'],'frame':params['frame'],'mode':'OBJECT','active_object':obj.name,'selected_objects':[obj.name]};scenes.activate(context);scene=bpy.context.scene
        image=bpy.data.images.new('blenderctl-bake-'+job.name[:8],width=params['resolution'],height=params['resolution'],alpha=True,float_buffer=True);image.colorspace_settings.name='Non-Color'
        # Work in copies of the data/materials; even in-memory shared siblings keep their original slots.
        obj.data=obj.data.copy()
        for slot in obj.material_slots:
            material=slot.material.copy();slot.material=material;target=material.node_tree.nodes.new('ShaderNodeTexImage');target.image=image
            for n in material.node_tree.nodes:n.select=n==target
            material.node_tree.nodes.active=target
        scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=params['samples'];scene.render.bake.margin=params['margin'];scene.render.bake.use_selected_to_active=False;scene.render.bake.use_clear=True
        kind='DIFFUSE' if params['type']=='DIFFUSE_COLOR' else params['type'];kwargs={'type':kind,'margin':params['margin'],'use_selected_to_active':False,'use_clear':True,'target':'IMAGE_TEXTURES','save_mode':'INTERNAL','normal_space':'TANGENT','normal_r':'POS_X','normal_g':'POS_Y','normal_b':'POS_Z','uv_layer':params['uv_layer']}
        if kind=='DIFFUSE':kwargs['pass_filter']={'COLOR'}
        modeling.finished(bpy.ops.object.bake(**kwargs))
        pixels=list(image.pixels)
        if not pixels or any(not math.isfinite(v) for v in pixels):raise Failure('VALIDATION_FAILED','Bake buffer invalid')
        path=job/'baked-texture.png';image.filepath_raw=str(path);image.file_format='PNG';image.save()
        report={**output_check(path,'Non-Color'),'object':obj.name,'context':context,'type':params['type'],'uv_layer':params['uv_layer'],'engine':'CYCLES_CPU','samples':params['samples'],'margin':params['margin'],'colorspace':'Non-Color','normal_convention':'tangent +X +Y +Z (OpenGL)' if kind=='NORMAL' else None,'linear_pixel_mean':[sum(pixels[i::4])/(len(pixels)/4) for i in range(4)],'resource_hashes':resource_hashes,'source_saved':False,'uv_overlap':'not_checked','scope':'single_object_single_tile; image_artifact_only'}
        atomic_json(job/'bake-report.json',report);return report
