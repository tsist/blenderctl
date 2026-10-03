# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit render execution; all outputs stay in the isolated job directory."""
import time,math
from pathlib import Path
from contextlib import ExitStack
import bpy
from protocol import Failure,atomic_json,digest,read_json
from render_contract import normalize_run
import scenes,modeling,nodes

def devices(params=None,job=None):
    p=bpy.context.preferences.addons['cycles'].preferences;rows=[{'backend':'CPU','id':None,'name':'CPU','status':'available_not_render_verified'}]
    for backend in ('CUDA','OPTIX'):
        try:
            rows.extend({'backend':backend,'id':d.id,'name':d.name,'status':'detected_not_render_verified'} for d in p.get_devices_for_type(backend) if d.type==backend)
        except Exception as ex:rows.append({'backend':backend,'error':str(ex)})
    return {'blender':bpy.app.version_string,'build':bpy.app.build_hash.decode(),'devices':rows,'graphics_engines':['BLENDER_EEVEE','BLENDER_WORKBENCH'],'graphics_device_selection':'host graphics context; no physical GPU selection guarantee'}
def select_device(scene,spec):
    scene.render.engine=spec['engine'];d=spec['device']
    if spec['engine']!='CYCLES':return {'backend':'GRAPHICS','id':None,'name':'host graphics context'}
    if d['backend']=='CPU':scene.cycles.device='CPU';return {'backend':'CPU','id':None,'name':'CPU'}
    p=bpy.context.preferences.addons['cycles'].preferences
    try:p.compute_device_type=d['backend'];available=p.get_devices_for_type(d['backend'])
    except Exception as ex:raise Failure('UNSUPPORTED','Cycles backend unavailable: '+str(ex)) from ex
    selected=[x for x in available if x.type==d['backend'] and x.id==d['id']]
    if len(selected)!=1:raise Failure('NOT_FOUND','Exact requested GPU not detected')
    for x in p.devices:x.use=False
    selected[0].use=True;scene.cycles.device='GPU'
    return {'backend':d['backend'],'id':selected[0].id,'name':selected[0].name}
def color_settings(scene,spec):
    scene.display_settings.display_device='sRGB';scene.view_settings.view_transform=spec['view'];scene.view_settings.look='None';scene.view_settings.exposure=spec['exposure'];scene.view_settings.gamma=spec['gamma'];scene.view_settings.use_curve_mapping=False
    scene.render.dither_intensity=0
def disable_external_writes(scene):
    scene.render.use_render_cache=False;scene.render.use_texture_cache=False;scene.render.use_auto_generate_texture_cache=False
    scene.render.use_stamp=False;scene.render.use_compositing=False;scene.render.use_sequencer=False
def image_report(path):
    import OpenImageIO as oiio
    import numpy as np
    inp=oiio.ImageInput.open(str(path))
    if inp is None:raise Failure('VALIDATION_FAILED','Cannot decode output: '+oiio.geterror())
    images=[];index=0
    try:
        while inp.seek_subimage(index,0):
            spec=inp.spec();data=inp.read_image(format=oiio.FLOAT)
            if data is None:raise Failure('VALIDATION_FAILED','Image decode failed')
            a=np.asarray(data);finite=a[np.isfinite(a)]
            if not len(finite) or np.isnan(a).any():raise Failure('VALIDATION_FAILED','Image contains invalid pixels')
            images.append({'width':spec.width,'height':spec.height,'channels':list(spec.channelnames),'format':str(spec.format),'min':float(finite.min()),'max':float(finite.max()),'infinite_values':int(np.isinf(a).sum()),'mean':[float(a[:,:,i][np.isfinite(a[:,:,i])].mean()) if np.isfinite(a[:,:,i]).any() else None for i in range(spec.nchannels)]});index+=1
    finally:inp.close()
    if not images:raise Failure('VALIDATION_FAILED','No decoded image/subimage')
    return {'file':str(path),'sha256':digest(path),'bytes':path.stat().st_size,'images':images}
def preflight(params,job,spec,stack):
    if params.get('driver_profile') and spec['motion_blur']:
        raise Failure('UNSUPPORTED','Native driver numeric validation covers requested frames; motion-blur subframes require a separate profile')
    from rigging import animation_safe
    animation_safe(params.get('driver_profile'),params['file'],job)
    import hair_shape
    if hair_shape.managed_objects():
        if spec['motion_blur']:raise Failure('UNSUPPORTED','Triangle groom witnesses cover integer frames, not motion-blur subframes')
        atomic_json(job/'hair-shape-render-checks.json',hair_shape.sample(spec['frames']))
    import hair_density,hair_volume
    volume_objects=hair_volume.managed_objects()
    if volume_objects or hair_density.managed_objects():
        if spec['motion_blur']:raise Failure('UNSUPPORTED','Density/volume witnesses cover integer frames, not motion blur')
        atomic_json(job/'hair-volume-render-checks.json',{'density':hair_density.sample(spec['frames']),'volume':hair_volume.sample(spec['frames'])})
    if bpy.data.libraries:raise Failure('UNSUPPORTED','Render requires local or packed data; linked libraries need an explicit adapter')
    hashes=nodes.resource_guards(params,stack)
    hashes.update({d['file']:d['expected_sha256'] for d in hair_volume.documents()})
    declared={str(Path(x['file']).resolve()):x['expected_sha256'] for x in params.get('resources',[])}
    from filesystem import FileGuard
    for item in [*bpy.data.fonts,*bpy.data.volumes]:
        if any(o.name in volume_objects and o.data==item for o in bpy.context.scene.objects):continue
        path=getattr(item,'filepath','')
        if not path or path=='<builtin>' or getattr(item,'packed_file',None):continue
        if getattr(item,'is_sequence',False):raise Failure('UNSUPPORTED','Volume sequence adapter not implemented')
        p=str(Path(bpy.path.abspath(path)).resolve())
        if p not in declared:raise Failure('INVALID_REQUEST','External font/volume requires a resource hash')
        guard=stack.enter_context(FileGuard(p));h=guard.sha256()
        if h!=declared[p]:raise Failure('CONFLICT','External font/volume changed')
        hashes[p]=h
    has_sim=False
    for s in bpy.data.scenes:
        has_sim |= bool(s.rigidbody_world)
    for o in bpy.data.objects:
        if o.particle_systems:
            import liquid
            if not liquid.native_particles(o):raise Failure('UNSUPPORTED','Legacy particle render cache needs a dedicated adapter')
        for m in o.modifiers:
            if m.type in ('CLOTH','SOFT_BODY','FLUID'):has_sim=True
            if m.type in ('MESH_CACHE','MESH_SEQUENCE_CACHE','DYNAMIC_PAINT'):raise Failure('UNSUPPORTED','Unmanaged cache modifier')
    trees=[*bpy.data.node_groups,*[m.node_tree for m in bpy.data.materials if m.node_tree],*[w.node_tree for w in bpy.data.worlds if w.node_tree]]
    import node_zones
    node_zones.validate_all()
    has_zone=False
    for t in trees:
        for n in t.nodes:
            if n.bl_idname in ('ShaderNodeScript','GeometryNodeBake'):raise Failure('UNSUPPORTED','Unmanaged script or bake node')
            if n.bl_idname in ('GeometryNodeSimulationInput','GeometryNodeSimulationOutput'):has_zone=True;has_sim=True
    if has_zone and spec['motion_blur']:raise Failure('UNSUPPORTED','Zone cache covers integer frames; motion-blur subframes require a separate adapter')
    if has_sim:
        if 'simulation_receipt' not in params:raise Failure('INVALID_REQUEST','Physics rendering requires a verified simulation receipt')
        import simulation
        receipt=read_json(params['simulation_receipt']['file']);request=receipt['request']
        if request.get('adapter')=='NATIVE_LIQUID_V1' and spec['motion_blur']:raise Failure('UNSUPPORTED','Native liquid receipt covers integer frames, not motion-blur subframes')
        if has_zone and request.get('adapter')!='GEOMETRY_ZONE_V1':raise Failure('CONFLICT','Node simulation requires GEOMETRY_ZONE_V1 receipt')
        if request['scene']!=spec['scene'] or request['view_layer']!=spec['view_layer'] or min(spec['frames'])<request['frame_start'] or max(spec['frames'])>request['frame_end']:raise Failure('CONFLICT','Render frames/context lie outside simulation receipt')
        simulation.bake({'file':params['file'],'manifest':{**request,'mode':'reuse','receipt':params['simulation_receipt']},**({'resources':params.get('resources',[])} if has_zone else {}),**({'driver_profile':params['driver_profile']} if params.get('driver_profile') else {})},job)
    return hashes
def compositor_safe(scene,layer):
    from node_contract import NODE_TYPES
    seen=set()
    def walk(t):
        if t.as_pointer() in seen:raise Failure('UNSUPPORTED','Recursive/reused compositor group requires flattening')
        seen.add(t.as_pointer())
        if len(seen)>32:raise Failure('UNSUPPORTED','Too many compositor groups')
        for n in t.nodes:
            if n.bl_idname not in NODE_TYPES['CompositorNodeTree']:raise Failure('UNSUPPORTED','Compositor output/external node is outside the render allowlist: '+n.bl_idname)
            if n.bl_idname=='CompositorNodeRLayers' and ((n.scene and n.scene!=scene) or n.layer!=layer.name):raise Failure('UNSUPPORTED','Render Layers must target this scene and view layer')
            if getattr(n,'node_tree',None):walk(n.node_tree)
    if not scene.compositing_node_group:raise Failure('NOT_FOUND','No compositor group bound to scene')
    walk(scene.compositing_node_group)
def run(params,job,*,prepared_hashes=None):
    # Internal prepared scenes have already passed source/resource/physics
    # preflight. Public request params cannot select this keyword-only path.
    spec=normalize_run(params['manifest']);started=time.monotonic()
    if prepared_hashes is None:bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    if tuple(bpy.data.version[:2])!=tuple(bpy.app.version[:2]):raise Failure('UNSUPPORTED','Render requires matching Blender major/minor')
    s=scenes.find(bpy.data.scenes,spec['scene']);layer=scenes.find(s.view_layers,spec['view_layer']);cam=scenes.find(bpy.data.objects,spec['camera'])
    if cam.type!='CAMERA' or cam.name not in layer.objects:raise Failure('INVALID_REQUEST','Camera must belong to requested view layer')
    with ExitStack() as stack:
        hashes=preflight(params,job,spec,stack) if prepared_hashes is None else prepared_hashes
        # Receipt verification may reopen the source; never retain freed RNA references.
        s=scenes.find(bpy.data.scenes,spec['scene']);layer=scenes.find(s.view_layers,spec['view_layer']);cam=scenes.find(bpy.data.objects,spec['camera'])
        bpy.context.window.scene=s;bpy.context.window.view_layer=layer;s.camera=cam;disable_external_writes(s);device=select_device(s,spec)
        for view in s.view_layers:view.use=view==layer
        for marker in s.timeline_markers:marker.camera=None
        s.render.resolution_x=spec['width'];s.render.resolution_y=spec['height'];s.render.resolution_percentage=100;s.render.pixel_aspect_x=s.render.pixel_aspect_y=1;s.render.use_border=False;s.render.use_multiview=False;s.render.film_transparent=spec['transparent'];s.render.use_motion_blur=bool(spec['motion_blur']);s.render.motion_blur_shutter=spec['motion_blur'];s.render.motion_blur_position='CENTER';s.render.use_persistent_data=False
        if spec['engine']=='CYCLES':s.cycles.samples=spec['samples'];s.cycles.use_adaptive_sampling=False;s.cycles.use_denoising=spec['denoise'];s.cycles.denoiser='OPENIMAGEDENOISE';s.cycles.seed=0;s.cycles.use_animated_seed=False
        elif spec['engine']=='BLENDER_EEVEE':s.eevee.taa_render_samples=spec['samples']
        else:s.display.render_aa='8'
        color_settings(s,spec['color']);s.render.compositor_device='CPU';s.render.compositor_precision='FULL'
        if spec['compositor']:compositor_safe(s,layer);s.render.use_compositing=True
        flags={'Z':'use_pass_z','NORMAL':'use_pass_normal','DIFFUSE_COLOR':'use_pass_diffuse_color','EMISSION':'use_pass_emit','OBJECT_INDEX':'use_pass_object_index'}
        for p in layer.bl_rna.properties:
            if p.identifier.startswith('use_pass_') and p.type=='BOOLEAN' and not p.is_readonly:setattr(layer,p.identifier,p.identifier=='use_pass_combined')
        if len(layer.aovs) or len(layer.lightgroups):raise Failure('UNSUPPORTED','Custom AOV/light groups need a dedicated pass contract')
        for name,prop in flags.items():setattr(layer,prop,name in spec['passes'])
        fmt=s.render.image_settings;fmt.media_type='MULTI_LAYER_IMAGE' if spec['format']=='OPEN_EXR_MULTILAYER' else 'IMAGE';fmt.file_format=spec['format'];fmt.color_mode='RGB' if spec['format']=='JPEG' else 'RGBA';fmt.color_depth=spec['depth'];fmt.color_management='FOLLOW_SCENE';fmt.exr_codec='ZIP';fmt.quality=95
        outputs=[];suffix='.exr' if 'EXR' in spec['format'] else '.jpg' if spec['format']=='JPEG' else '.png'
        for frame in spec['frames']:
            atomic_json(job/'render-progress.json',{'state':'rendering','frame':frame,'completed':len(outputs),'total':len(spec['frames'])});s.frame_set(frame);nodes.sequence_bindings(frame)
            bpy.context.view_layer.update()
            import drivers
            drivers.evaluated(params.get('driver_profile'))
            path=job/f'render-{frame:06d}{suffix}'
            if path.exists():raise Failure('CONFLICT','Output already exists')
            s.render.filepath=str(path);modeling.finished(bpy.ops.render.render(write_still=True,scene=s.name,layer=layer.name));out=image_report(path)
            drivers.evaluated(params.get('driver_profile'))
            if any(x['width']!=spec['width'] or x['height']!=spec['height'] for x in out['images']):raise Failure('VALIDATION_FAILED','Rendered dimensions differ')
            if spec['passes']:
                channels=[c for im in out['images'] for c in im['channels']];required={'Z':'.Depth.Z','NORMAL':'.Normal.X','DIFFUSE_COLOR':'.Diffuse Color.R','EMISSION':'.Emission.R','OBJECT_INDEX':'.Object Index.X'}
                for p in spec['passes']:
                    if not any(c.endswith(required[p]) for c in channels):raise Failure('VALIDATION_FAILED','Missing EXR pass: '+p)
            out['frame']=frame;outputs.append(out)
        for p,h in hashes.items():
            if digest(p)!=h:raise Failure('CONFLICT','Resource changed during rendering')
        from simulation import peak_memory
        report={'render_report_version':'1.0','settings':spec,'device':device,'outputs':outputs,'resource_hashes':hashes,'seconds':time.monotonic()-started,'peak_working_set_bytes':peak_memory(),'source_saved':False,'color_scope':'PNG/JPEG display-referred; EXR scene-linear, view transform not baked','limitations':['parameter caps and supervisor timeout, no OS memory/VRAM hard quota','no arbitrary File Output nodes; output paths owned by this job']};atomic_json(job/'render-report.json',report);atomic_json(job/'render-progress.json',{'state':'verified','completed':len(outputs)});return report
