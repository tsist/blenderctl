"""Checker image of frozen VIEWPORT evaluation, including live rig/shape state.

The image is not a claim about a configured renderer's displacement or subdivision.
"""
import math
import hashlib
import bpy
import numpy as np
from mathutils import Vector
from . import core

def render(scene,candidate,path,size=1024):
    if not 64<=size<=2048:raise ValueError('Diagnostic size must be 64–2048')
    if bpy.context.window.scene!=scene:raise ValueError('Checker scene must be the active scene')
    rec=core.state(scene)['candidates'][candidate]
    old_scene=bpy.context.window.scene;old_vl=bpy.context.window.view_layer
    preview=bpy.data.scenes.new('UV_Workflow_Checker');owned=[];meshes=[];camera=None;mat=None;world=None
    try:
        mat=bpy.data.materials.new('UVW_Checker');mat.use_nodes=True
        nt=mat.node_tree;nt.nodes.clear()
        uv=nt.nodes.new('ShaderNodeUVMap');uv.uv_map=rec['layer']
        aspect=nt.nodes.new('ShaderNodeVectorMath');aspect.operation='MULTIPLY'
        w,h=rec['manifest']['width'],rec['manifest']['height']
        aspect.inputs[1].default_value=(w/max(w,h),h/max(w,h),1)
        checker=nt.nodes.new('ShaderNodeTexChecker');checker.inputs['Scale'].default_value=24
        checker.inputs['Color1'].default_value=(.06,.12,.24,1);checker.inputs['Color2'].default_value=(.8,.88,1,1)
        shader=nt.nodes.new('ShaderNodeBsdfDiffuse');out=nt.nodes.new('ShaderNodeOutputMaterial')
        nt.links.new(uv.outputs['UV'],aspect.inputs[0]);nt.links.new(aspect.outputs['Vector'],checker.inputs['Vector']);nt.links.new(checker.outputs['Color'],shader.inputs['Color']);nt.links.new(shader.outputs['BSDF'],out.inputs['Surface'])
        points=[];surface_objects=[]
        bpy.context.view_layer.update()
        graph=bpy.context.evaluated_depsgraph_get()
        if graph.mode!='VIEWPORT':raise ValueError('Checker snapshot requires a VIEWPORT graph')
        for name in rec['objects']:
            src=scene.objects[name]
            if not src.visible_get(view_layer=bpy.context.view_layer):raise ValueError('Checker target is hidden: '+name)
            ev=src.evaluated_get(graph)
            if not ev.is_evaluated:raise ValueError('Checker target was not evaluated: '+name)
            mesh=bpy.data.meshes.new_from_object(ev,preserve_all_data_layers=True,depsgraph=graph)
            meshes.append(mesh)
            if rec['layer'] not in mesh.uv_layers:raise ValueError('Candidate UV absent from evaluated mesh: '+name)
            o=bpy.data.objects.new('UVW_Checker_'+name,mesh)
            preview.collection.objects.link(o);owned.append(o)
            o.matrix_world=ev.matrix_world.copy()
            o.data.materials.clear();o.data.materials.append(mat)
            for p in o.data.polygons:p.material_index=0
            points.extend(o.matrix_world@v.co for v in mesh.vertices)
            mesh.calc_loop_triangles()
            coordinates=np.asarray([list(v.co) for v in mesh.vertices],dtype=np.float64)
            matrix=np.asarray(o.matrix_world,dtype=np.float64)
            positions=(coordinates@matrix[:3,:3].T+matrix[:3,3])*scene.unit_settings.scale_length
            triangles=np.asarray([list(t.vertices) for t in mesh.loop_triangles],dtype=np.int32)
            surface_objects.append({'object':name,'vertices':len(mesh.vertices),'triangles':len(mesh.loop_triangles),
                'world_geometry_sha256':hashlib.sha256(positions.tobytes()+triangles.tobytes()).hexdigest(),
                'uv_sha256':hashlib.sha256(core.coordinates(mesh,rec['layer']).tobytes()).hexdigest()})
        if not points:raise ValueError('Checker targets have no evaluated vertices')
        center=sum(points,Vector())/len(points);radius=max((p-center).length for p in points)
        world=bpy.data.worlds.new('UVW_CheckerWorld');world.use_nodes=True;world.node_tree.nodes['Background'].inputs[0].default_value=(.35,.35,.35,1);world.node_tree.nodes['Background'].inputs[1].default_value=.8;preview.world=world
        camera=bpy.data.cameras.new('UVW_Camera');cam=bpy.data.objects.new('UVW_Camera',camera);preview.collection.objects.link(cam);owned.append(cam)
        cam.location=center+Vector((1,-1,.72)).normalized()*max(radius*3,.1)
        cam.rotation_euler=(center-cam.location).to_track_quat('-Z','Y').to_euler();camera.type='ORTHO';camera.ortho_scale=max(radius*2.2,.1);preview.camera=cam
        # Studio illumination remains confined to the disposable scene.
        light=bpy.data.lights.new('UVW_Area','AREA');lamp=bpy.data.objects.new('UVW_Area',light);preview.collection.objects.link(lamp);owned.append(lamp)
        lamp.location=center+Vector((0,-2,3))*max(radius,1);lamp.rotation_euler=(center-lamp.location).to_track_quat('-Z','Y').to_euler();light.energy=100*max(radius,1)**2;light.shape='DISK';light.size=max(radius*3,1)
        preview.render.engine='CYCLES';preview.cycles.device='CPU';preview.cycles.samples=16
        preview.render.resolution_x=size;preview.render.resolution_y=size;preview.render.resolution_percentage=100
        preview.render.threads_mode='FIXED';preview.render.threads=2;preview.render.image_settings.file_format='PNG';preview.render.filepath=path
        preview.view_settings.view_transform='Standard';bpy.context.window.scene=preview
        bpy.ops.render.render(write_still=True,scene=preview.name)
        return {'file':path,'size':size,'candidate':candidate,'surface':'frozen_evaluated_viewport',
                'frame':scene.frame_current,'subframe':scene.frame_subframe,'objects':surface_objects,
                'configured_renderer_equivalence':'not_verified','purpose':'checker_only'}
    finally:
        bpy.context.window.scene=old_scene;bpy.context.window.view_layer=old_vl
        lights=[o.data for o in owned if o.type=='LIGHT']
        for o in owned:bpy.data.objects.remove(o,do_unlink=True)
        for m in meshes:bpy.data.meshes.remove(m)
        for light in lights:bpy.data.lights.remove(light)
        if camera:bpy.data.cameras.remove(camera)
        bpy.data.scenes.remove(preview)
        if mat:bpy.data.materials.remove(mat)
        if world:bpy.data.worlds.remove(world)
