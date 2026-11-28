"""Explicit render execution; all outputs stay in the isolated job directory."""
import time,math
from pathlib import Path
from contextlib import ExitStack
import bpy
from protocol import VERSION,Failure,atomic_json,digest,read_json
from render_contract import normalize_run,is_source_run,is_c5_run
from render_batch_delivery import validate_channels,validate_dwaa_crypto_storage
from source_render_settings import assert_source_preservable,capture_source,preserved_source_signature,verify_source_spec
import scenes,modeling,nodes
from timing import Trace

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
def guard_aov_emitter_sampling(layer, device, stack):
    """Avoid the GPU NEE shader path for surfaces that also write custom AOVs."""
    if device['backend'] not in {'CUDA', 'OPTIX'} or 'RTX 5080' not in device['name'] or not layer.aovs:
        return []
    materials = {slot.material for obj in layer.objects for slot in obj.material_slots if slot.material}
    changes = []
    for material in sorted(materials, key=lambda item: item.name):
        tree = material.node_tree
        if tree is None or material.cycles.emission_sampling == 'NONE':
            continue
        writes_aov = any(node.type == 'OUTPUT_AOV' and node.aov_name in layer.aovs for node in tree.nodes)
        if not writes_aov:
            continue
        # Animated strength may cross Cycles' AUTO emitter threshold mid-shot.
        # Preserve the emission shader, its keyframes, and all requested AOVs.
        emits = any(node.type == 'EMISSION' or
                    (node.type == 'BSDF_PRINCIPLED' and
                     (node.inputs['Emission Strength'].is_linked or
                      node.inputs['Emission Strength'].default_value > 0 or tree.animation_data))
                    for node in tree.nodes)
        if emits:
            previous = material.cycles.emission_sampling
            stack.callback(setattr, material.cycles, 'emission_sampling', previous)
            material.cycles.emission_sampling = 'NONE'
            changes.append({'material': material.name, 'from': previous, 'to': 'NONE',
                            'scope': 'mesh-light importance sampling during this render only'})
    return changes


def color_settings(scene,spec):
    scene.display_settings.display_device='sRGB';scene.view_settings.view_transform=spec['view'];scene.view_settings.look=spec.get('look','None');scene.view_settings.exposure=spec['exposure'];scene.view_settings.gamma=spec['gamma'];scene.view_settings.use_curve_mapping=False
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
            metadata = {item.name: str(item.value) for item in spec.extra_attribs
                        if item.name.startswith('cryptomatte/') or item.name in {'oiio:ColorSpace', 'oiio:subimagename', 'compression', 'colorInteropID'}}
            images.append({'width':spec.width,'height':spec.height,'channels':list(spec.channelnames),'format':str(spec.format),'channel_formats':[str(fmt) for fmt in spec.channelformats],'metadata':metadata,'min':float(finite.min()),'max':float(finite.max()),'infinite_values':int(np.isinf(a).sum()),'mean':[float(a[:,:,i][np.isfinite(a[:,:,i])].mean(dtype=np.float64)) if np.isfinite(a[:,:,i]).any() else None for i in range(spec.nchannels)]});index+=1
    finally:inp.close()
    if not images:raise Failure('VALIDATION_FAILED','No decoded image/subimage')
    return {'file':str(path),'sha256':digest(path),'bytes':path.stat().st_size,'images':images}

def _geometry_cache(params,spec,stack,job):
    """Validate source-bound managed caches and retain read locks while rendering."""
    if 'cache_manifest' not in params:return set(),set(),None
    if spec['motion_blur']:raise Failure('UNSUPPORTED','Managed geometry caches cover integer frames, not motion-blur subframes')
    from geometry_temporal import validate_cache,validate_loaded_bindings
    from geometry_identity import implementation_sha256
    from filesystem import FileGuard
    manifest=validate_cache(params['cache_manifest'],candidate={'file':params['file'],'sha256':params['expected_sha256']},identity=implementation_sha256())
    if spec['scene']!=bpy.context.scene.name or spec['view_layer']!=bpy.context.view_layer.name:
        raise Failure('CONFLICT','Managed geometry cache requires the saved active Scene/ViewLayer')
    if min(spec['frames'])<manifest['frame_start'] or max(spec['frames'])>manifest['frame_end']:
        raise Failure('CONFLICT','Render frames outside managed geometry cache domain')
    validate_loaded_bindings(manifest)
    members=[params['cache_manifest'],*[{'file':str((Path(manifest['root'])/row['path']).resolve()),'expected_sha256':row['sha256']} for row in manifest['files']]]
    hashes={}; guards={}
    for member in members:
        guard=stack.enter_context(FileGuard(member['file']));actual=guard.sha256()
        if actual!=member['expected_sha256']:raise Failure('CONFLICT','Managed geometry cache member changed')
        hashes[member['file']]=actual
        guards[member['file']]=guard
    allowed_trees=set();allowed_modifiers=set()
    def visit(tree,trail=()):
        pointer=tree.as_pointer()
        if pointer in trail:raise Failure('UNSUPPORTED','Recursive managed geometry tree')
        if pointer in allowed_trees:return
        allowed_trees.add(pointer)
        for node in tree.nodes:
            child=getattr(node,'node_tree',None)
            if child:visit(child,(*trail,pointer))
    for target in manifest['targets']:
        obj=bpy.data.objects.get(target['object'])
        if not obj or obj.name not in bpy.context.view_layer.objects:raise Failure('CONFLICT','Managed cache target outside render view layer')
        modifier=obj.modifiers.get(target['modifier'])
        if not modifier or modifier.type!='NODES':raise Failure('CONFLICT','Managed cache modifier missing')
        allowed_modifiers.add((obj.as_pointer(),modifier.as_pointer()))
        visit(modifier.node_group)
    for obj in bpy.data.objects:
        for modifier in obj.modifiers:
            if modifier.type!='NODES' or not modifier.node_group:continue
            def dynamic(tree,trail=()):
                if tree.as_pointer() in trail:raise Failure('UNSUPPORTED','Recursive geometry modifier')
                return any(n.bl_idname in ('GeometryNodeBake','GeometryNodeSimulationInput','GeometryNodeSimulationOutput') or
                    (getattr(n,'node_tree',None) and dynamic(n.node_tree,(*trail,tree.as_pointer()))) for n in tree.nodes)
            if dynamic(modifier.node_group) and (obj.as_pointer(),modifier.as_pointer()) not in allowed_modifiers:
                raise Failure('CONFLICT','Dynamic geometry modifier is not covered by cache manifest: '+obj.name+'/'+modifier.name)
    return allowed_trees,hashes,{'manifest':manifest,'guards':guards}

def _managed_zones(tree):
    """Local RNA checks for fingerprint-bound new Simulation/Bake contracts."""
    import node_zones
    if any(n.bl_idname in ('GeometryNodeRepeatInput','GeometryNodeRepeatOutput') for n in tree.nodes):
        # Retain legacy bounded-repeat safety; no new repeat allowance is implied.
        node_zones.validate_tree(tree)
    inputs=[n for n in tree.nodes if n.bl_idname=='GeometryNodeSimulationInput']
    outputs=[n for n in tree.nodes if n.bl_idname=='GeometryNodeSimulationOutput']
    for node in inputs:
        paired=node.paired_output
        if not paired or paired.id_data!=tree or paired.bl_idname!='GeometryNodeSimulationOutput':
            raise Failure('VALIDATION_FAILED','Managed Simulation pairing invalid')
    for node in outputs:
        if len([n for n in inputs if n.paired_output==node])!=1:
            raise Failure('VALIDATION_FAILED','Managed Simulation output must have one input')
    for node in [*outputs,*[n for n in tree.nodes if n.bl_idname=='GeometryNodeBake']]:
        items=list(node.state_items if node.bl_idname=='GeometryNodeSimulationOutput' else node.bake_items)
        sockets=[s for s in node.outputs if s.identifier.startswith('Item_')]
        if not 1<=len(items)<=64 or len(items)!=len(sockets) or len({i.name for i in items})!=len(items) or len({s.identifier for s in sockets})!=len(sockets):
            raise Failure('VALIDATION_FAILED','Managed temporal items/sockets are ambiguous')
        for item in items:
            valid={e.identifier for e in item.bl_rna.properties['socket_type'].enum_items}
            if item.socket_type not in valid:raise Failure('VALIDATION_FAILED','Managed temporal item type invalid')
def preflight(params,job,spec,stack):
    if params.get('driver_profile') and spec['motion_blur']:
        raise Failure('UNSUPPORTED','Native driver numeric validation covers requested frames; motion-blur subframes require a separate profile')
    from rigging import animation_safe
    animation_safe(params.get('driver_profile'),params['file'],job)
    from hair_compat import hair_shape
    if hair_shape.managed_objects():
        if spec['motion_blur']:raise Failure('UNSUPPORTED','Triangle groom witnesses cover integer frames, not motion-blur subframes')
        atomic_json(job/'hair-shape-render-checks.json',hair_shape.sample(spec['frames']))
    from hair_compat import hair_density, hair_volume
    volume_objects=hair_volume.managed_objects()
    if volume_objects or hair_density.managed_objects():
        if spec['motion_blur']:raise Failure('UNSUPPORTED','Density/volume witnesses cover integer frames, not motion blur')
        atomic_json(job/'hair-volume-render-checks.json',{'density':hair_density.sample(spec['frames']),'volume':hair_volume.sample(spec['frames'])})
    if bpy.data.libraries:raise Failure('UNSUPPORTED','Render requires local or packed data; linked libraries need an explicit adapter')
    hashes=nodes.resource_guards(params,stack)
    managed_trees,cache_hashes,cache_input=_geometry_cache(params,spec,stack,job)
    hashes.update(cache_hashes)
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
    # Retained source groups have no modifier consumer and cannot run in this
    # worker. Gate the actual modifier/group forest; preserve all old datablocks.
    bound_geometry=set(); roots=[]
    def visit_geometry(tree,trail=()):
        pointer=tree.as_pointer()
        if pointer in trail:raise Failure('UNSUPPORTED','Recursive bound geometry tree')
        if pointer in bound_geometry:return
        bound_geometry.add(pointer)
        for node in tree.nodes:
            child=getattr(node,'node_tree',None)
            if child:visit_geometry(child,(*trail,pointer))
    for obj in bpy.data.objects:
        for modifier in obj.modifiers:
            if modifier.type=='NODES' and modifier.node_group:
                roots.append({'object':obj.name,'modifier':modifier.name,'group':modifier.node_group.name})
                visit_geometry(modifier.node_group)
    unbound=[t.name for t in bpy.data.node_groups if t.bl_idname=='GeometryNodeTree' and t.as_pointer() not in bound_geometry]
    atomic_json(job/'geometry-node-reachability.json',{'roots':roots,'unbound_groups_preserved':sorted(unbound),
        'scope':'All saved object modifiers and recursive group references; no visibility-based exemption. Global scripted-driver and shader-script checks remain.'})
    trees=[*bpy.data.node_groups,*[m.node_tree for m in bpy.data.materials if m.node_tree],*[w.node_tree for w in bpy.data.worlds if w.node_tree]]
    import node_zones
    # Every tree still passes a gate. Only receipt-bound managed trees use the
    # new local RNA contract, because the old global validator rejects wrappers.
    for tree in bpy.data.node_groups:
        if tree.bl_idname=='GeometryNodeTree':
            if tree.as_pointer() not in bound_geometry:continue
            if tree.as_pointer() in managed_trees:_managed_zones(tree)
            else:node_zones.validate_tree(tree)
    has_zone=False
    for t in trees:
        for n in t.nodes:
            if n.bl_idname=='ShaderNodeScript':raise Failure('UNSUPPORTED','Unmanaged script node')
            if t.bl_idname=='GeometryNodeTree' and t.as_pointer() not in bound_geometry:continue
            if t.as_pointer() in managed_trees:continue
            if n.bl_idname=='GeometryNodeBake':raise Failure('UNSUPPORTED','Unmanaged bake node')
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
    if cache_input:
        # Physics receipt reuse can reopen the source: bind the reader only after
        # that final reopen, while original cache handles remain held.
        from geometry_temporal import validate_loaded_bindings
        validate_loaded_bindings(cache_input['manifest'])
        from geometry_cache_reader import stage_read_snapshot
        stage_read_snapshot(cache_input['manifest'],cache_input['guards'],job,
                            {'file':params['file'],'sha256':params['expected_sha256']},spec['frames'])
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


def c5_compositor_safe(scene, layer):
    trees = [effect.node_group for effect in scene.compositor_effects if effect.enable_for_render and effect.node_group]
    if scene.compositing_node_group:
        trees.append(scene.compositing_node_group)
    if not trees:
        raise Failure('NOT_FOUND', 'No enabled render compositor group or scene effect')
    checked = set()
    while trees:
        tree = trees.pop()
        if tree.as_pointer() in checked:
            continue
        checked.add(tree.as_pointer())
        if len(checked) > 64:
            raise Failure('UNSUPPORTED', 'Too many compositor groups')
        for node in tree.nodes:
            if node.bl_idname in {'CompositorNodeOutputFile', 'CompositorNodeFileOutput', 'ShaderNodeScript'}:
                raise Failure('UNSUPPORTED', 'Batch compositor may write only the job-owned render artifacts')
            if node.bl_idname == 'CompositorNodeRLayers' and ((node.scene and node.scene != scene) or node.layer != layer.name):
                raise Failure('UNSUPPORTED', 'Render Layers must target this scene and view layer')
            if getattr(node, 'node_tree', None):
                trees.append(node.node_tree)


def c5_review_image(scene, job, frame):
    path = job / f'review-{frame:06d}.png'
    if path.exists():
        raise Failure('CONFLICT', 'Review PNG already exists')
    fmt = scene.render.image_settings
    depth = fmt.color_depth
    codec = fmt.exr_codec
    try:
        fmt.media_type = 'IMAGE'
        fmt.file_format = 'PNG'
        fmt.color_depth = '8'
        bpy.data.images['Render Result'].save_render(str(path), scene=scene)
    finally:
        fmt.media_type = 'MULTI_LAYER_IMAGE'
        fmt.file_format = 'OPEN_EXR_MULTILAYER'
        fmt.color_depth = depth
        fmt.exr_codec = codec
    result = image_report(path)
    return {key: result[key] for key in ('file','sha256','bytes')}
def _run_timed(params,job,trace,*,prepared_hashes=None,preserve_color_settings=False):
    # Internal prepared scenes have already passed source/resource/physics
    # preflight. Public request params cannot select this keyword-only path.
    spec=normalize_run(params['manifest']);source=is_source_run(spec);c5=is_c5_run(spec);source_settings=None;source_signature=None;started=time.monotonic()
    with trace.span('source_open', prepared=prepared_hashes is not None):
        if prepared_hashes is None:bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
        if tuple(bpy.data.version[:2])!=tuple(bpy.app.version[:2]):raise Failure('UNSUPPORTED','Render requires matching Blender major/minor')
        s=scenes.find(bpy.data.scenes,spec['scene']);layer=scenes.find(s.view_layers,spec['view_layer']);cam=scenes.find(bpy.data.objects,spec['camera'])
        if cam.type!='CAMERA' or cam.name not in layer.objects:raise Failure('INVALID_REQUEST','Camera must belong to requested view layer')
    with ExitStack() as stack:
        with trace.span('preflight', prepared=prepared_hashes is not None):
            hashes=preflight(params,job,spec,stack) if prepared_hashes is None else prepared_hashes
        with trace.span('device_settings'):
            # Receipt verification may reopen the source; never retain freed RNA references.
            s=scenes.find(bpy.data.scenes,spec['scene']);layer=scenes.find(s.view_layers,spec['view_layer']);cam=scenes.find(bpy.data.objects,spec['camera'])
            if source:
                # Capture and verify the final reopened source before any
                # request-owned output/device change is applied.
                source_settings=capture_source(s,layer)
                assert_source_preservable(s,layer,cam,spec['frames'],allow_compositor=c5)
                verify_source_spec(spec,source_settings)
                source_signature=preserved_source_signature(source_settings)
            bpy.context.window.scene=s;bpy.context.window.view_layer=layer;disable_external_writes(s);device=select_device(s,spec)
            emitter_changes = guard_aov_emitter_sampling(layer, device, stack) if c5 else []
            if source:
                # Other active layers are rejected above; enable only the declared one.
                layer.use=True
            else:
                for view in s.view_layers:view.use=view==layer
            if not source:
                for marker in s.timeline_markers:marker.camera=None
            # Clearing a marker can reselect Scene.camera; bind only after all clears.
            s.camera=cam
            s.render.resolution_x=spec['width'];s.render.resolution_y=spec['height'];s.render.resolution_percentage=100
            if not source:
                s.render.pixel_aspect_x=s.render.pixel_aspect_y=1;s.render.use_border=False;s.render.use_multiview=False;s.render.film_transparent=spec['transparent'];s.render.use_motion_blur=bool(spec['motion_blur']);s.render.motion_blur_shutter=spec['motion_blur'];s.render.motion_blur_position='CENTER';s.render.use_persistent_data=False
                if spec['engine']=='CYCLES':
                    s.cycles.samples=spec['samples'];s.cycles.use_adaptive_sampling=False;s.cycles.use_denoising=spec['denoise'];s.cycles.denoiser=spec.get('denoiser','OPENIMAGEDENOISE');s.cycles.seed=0;s.cycles.use_animated_seed=False
                    if 'denoising_use_gpu' in spec:
                        if not hasattr(s.cycles,'denoising_use_gpu'):raise Failure('UNSUPPORTED','This Blender runtime cannot declare GPU denoising')
                        s.cycles.denoising_use_gpu=spec['denoising_use_gpu']
                    if s.cycles.denoiser!=spec.get('denoiser','OPENIMAGEDENOISE') or ('denoising_use_gpu' in spec and s.cycles.denoising_use_gpu!=spec['denoising_use_gpu']):raise Failure('CONFLICT','Requested denoising settings did not persist; no fallback is allowed')
                elif spec['engine']=='BLENDER_EEVEE':s.eevee.taa_render_samples=spec['samples']
                else:s.display.render_aa='8'
            if not source:
                if preserve_color_settings:
                    actual={'view':s.view_settings.view_transform,'exposure':s.view_settings.exposure,'gamma':s.view_settings.gamma}
                    if 'look' in spec['color']:actual['look']=s.view_settings.look
                    if actual!=spec['color']:raise Failure('CONFLICT','Preserved source color settings differ from declared render')
                else:
                    color_settings(s,spec['color'])
            if not source:
                s.render.compositor_device='CPU';s.render.compositor_precision='FULL'
                if spec['compositor']:compositor_safe(s,layer);s.render.use_compositing=True
            if not c5 and (len(layer.aovs) or len(layer.lightgroups)):raise Failure('UNSUPPORTED','Custom AOV/light groups need a dedicated pass contract')
            if c5 and spec['compositor']:
                c5_compositor_safe(s,layer)
                s.render.use_compositing=True
            if c5 and spec.get('adaptive_sampling'):
                override=spec['adaptive_sampling']
                s.cycles.use_adaptive_sampling=override['enabled']
                s.cycles.adaptive_threshold=override['threshold']
                s.cycles.adaptive_min_samples=override['min_samples']
                source_signature=preserved_source_signature(capture_source(s,layer))
            if not source:
                flags={'Z':'use_pass_z','NORMAL':'use_pass_normal','DIFFUSE_COLOR':'use_pass_diffuse_color','EMISSION':'use_pass_emit','OBJECT_INDEX':'use_pass_object_index'}
                for p in layer.bl_rna.properties:
                    if p.identifier.startswith('use_pass_') and p.type=='BOOLEAN' and not p.is_readonly:setattr(layer,p.identifier,p.identifier=='use_pass_combined')
                for name,prop in flags.items():setattr(layer,prop,name in spec['passes'])
            fmt=s.render.image_settings
            if source and not c5:
                fmt.media_type='IMAGE';fmt.file_format='PNG';fmt.color_mode='RGBA';fmt.color_depth=spec['depth'];fmt.color_management='FOLLOW_SCENE'
            else:
                fmt.media_type='MULTI_LAYER_IMAGE' if spec['format']=='OPEN_EXR_MULTILAYER' else 'IMAGE';fmt.file_format=spec['format'];fmt.color_mode='RGB' if spec['format']=='JPEG' else 'RGBA';fmt.color_depth=spec['depth'];fmt.color_management='FOLLOW_SCENE';fmt.exr_codec=spec.get('exr_codec','ZIP');fmt.quality=95
        outputs=[];suffix='.exr' if 'EXR' in spec['format'] else '.jpg' if spec['format']=='JPEG' else '.png'
        snapshot_path=job/'geometry-cache-read-snapshot.json'
        snapshot_witness=read_json(snapshot_path) if snapshot_path.is_file() else None
        snapshot_manifest=read_json(snapshot_witness['manifest']['file']) if snapshot_witness else None
        reference={row['frame']:row for row in snapshot_manifest['samples']} if snapshot_manifest else {}
        for frame in spec['frames']:
            with trace.span('frame', frame=frame, checkpoint=True):
                with trace.span('frame_evaluation'):
                    atomic_json(job/'render-progress.json',{'state':'rendering','frame':frame,'completed':len(outputs),'total':len(spec['frames'])});s.frame_set(frame);nodes.sequence_bindings(frame)
                    bpy.context.view_layer.update()
                    import drivers
                    drivers.evaluated(params.get('driver_profile'))
                    if s.camera!=cam:raise Failure('CONFLICT','Active render camera changed during frame evaluation: '+str(frame))
                    if source:
                        current=capture_source(s,layer)
                        if preserved_source_signature(current)!=source_signature:
                            raise Failure('CONFLICT','SOURCE_V1 source settings changed during frame evaluation: '+str(frame))
                        assert_source_preservable(s,layer,cam,spec['frames'],allow_compositor=c5)
                with trace.span('cache_witness'):
                    if frame in reference:
                        from geometry_temporal import _samples
                        from geometry_verifier import near
                        actual=_samples(frame,snapshot_manifest['targets'],snapshot_manifest.get('observations',[]))
                        mismatch=near(reference[frame],actual)
                        snapshot_witness.setdefault('frame_checks',[]).append({'frame':frame,'status':'failed' if mismatch else 'passed','mismatch':mismatch})
                        atomic_json(snapshot_path,snapshot_witness)
                        if mismatch:raise Failure('VALIDATION_FAILED','Render cache reader witness differs: '+mismatch)
                path=job/f'render-{frame:06d}{suffix}'
                if path.exists():raise Failure('CONFLICT','Output already exists')
                s.render.filepath=str(path)
                with trace.span('bpy_render_write_still_composite'):
                    modeling.finished(bpy.ops.render.render(write_still=True,scene=s.name,layer=layer.name))
                    if s.camera!=cam:raise Failure('CONFLICT','Active render camera changed during render: '+str(frame))
                with trace.span('image_metrics_hash'):
                    out=image_report(path)
                with trace.span('frame_output_validation'):
                    drivers.evaluated(params.get('driver_profile'))
                    if any(x['width']!=spec['width'] or x['height']!=spec['height'] for x in out['images']):raise Failure('VALIDATION_FAILED','Rendered dimensions differ')
                    if spec['passes']:
                        channels=[c for im in out['images'] for c in im['channels']];required={'Z':'.Depth.Z','NORMAL':'.Normal.X','DIFFUSE_COLOR':'.Diffuse Color.R','EMISSION':'.Emission.R','OBJECT_INDEX':'.Object Index.X'}
                        for p in spec['passes']:
                            if not any(c.endswith(required[p]) for c in channels):raise Failure('VALIDATION_FAILED','Missing EXR pass: '+p)
                    if c5 and spec['format']=='OPEN_EXR_MULTILAYER':
                        names=spec.get('output_passes', [])
                        validate_channels(out['images'],names,spec['depth'],layer.name)
                        if spec.get('exr_codec')=='DWAA':
                            validate_dwaa_crypto_storage(out['images'])
                        if spec['compositor'] and not any('Composite.Combined.R' in image['channels'] for image in out['images']):
                            raise Failure('VALIDATION_FAILED','Scene effects did not produce Composite.Combined in the EXR')
                        out['review']=c5_review_image(s,job,frame)
                    if c5 and spec['format']=='PNG' and out['images'][0]['format']!='uint'+spec['depth']:
                        raise Failure('VALIDATION_FAILED','PNG channel depth differs from the delivery profile')
                out['frame']=frame;outputs.append(out)
                if source:
                    # The receipt is written only after image decoding,
                    # dimension/pass validation and hash calculation pass.
                    receipts=job/'render-frame-receipts';receipts.mkdir(exist_ok=True)
                    atomic_json(receipts/f'{frame:06d}.json',out)
        with trace.span('final_resource_guards'):
            for p,h in hashes.items():
                if digest(p)!=h:raise Failure('CONFLICT','Resource changed during rendering')
            snapshot_path=job/'geometry-cache-read-snapshot.json'
            snapshot_witness=read_json(snapshot_path) if snapshot_path.is_file() else None
            if snapshot_witness:
                from geometry_temporal import validate_cache
                from geometry_identity import implementation_sha256
                validate_cache(snapshot_witness['manifest'],identity=implementation_sha256())
                snapshot_witness['status']='revalidated_after_render'
                atomic_json(snapshot_path,snapshot_witness)
        from simulation import peak_memory
        report={'render_report_version':'1.0','settings':spec,'device':device,'outputs':outputs,'resource_hashes':hashes,'seconds':time.monotonic()-started,'peak_working_set_bytes':peak_memory(),'source_saved':False,'geometry_cache_read_snapshot':({'file':str(snapshot_path),'expected_sha256':digest(snapshot_path)} if snapshot_witness else None),'color_scope':'PNG/JPEG display-referred; EXR scene-linear, view transform not baked','limitations':['parameter caps and supervisor timeout, no OS memory/VRAM hard quota','no arbitrary File Output nodes; output paths owned by this job']}
        report['render_batch_release']=VERSION
        report['gpu_aov_emitter_sampling'] = emitter_changes
        if source:report['source_settings']=source_settings
        atomic_json(job/'render-report.json',report);atomic_json(job/'render-progress.json',{'state':'verified','completed':len(outputs)});return report


def run(params,job,*,prepared_hashes=None,preserve_color_settings=False):
    trace=Trace(job/'render-timing.json', metadata={'scope':'worker render.run; source open through report assembly; client latency outside scope'})
    try:
        with trace.span('render_run_total'):
            report=_run_timed(params,job,trace,prepared_hashes=prepared_hashes,preserve_color_settings=preserve_color_settings)
    except BaseException as ex:
        trace.finish(ex)
        raise
    trace.finish()
    report['color_settings_preserved']=preserve_color_settings or params['manifest'].get('settings_mode')=='SOURCE_V1'
    scene=bpy.data.scenes[params['manifest']['scene']]
    report['actual_display_settings']={'view':scene.view_settings.view_transform,'look':scene.view_settings.look,'exposure':scene.view_settings.exposure,'gamma':scene.view_settings.gamma,'use_curve_mapping':scene.view_settings.use_curve_mapping,'display_device':scene.display_settings.display_device}
    if scene.render.engine=='CYCLES':report['actual_denoising_settings']={'enabled':scene.cycles.use_denoising,'denoiser':scene.cycles.denoiser,'use_gpu':getattr(scene.cycles,'denoising_use_gpu',None),'evidence_scope':'Settings readback and completed native render; not kernel utilization telemetry'}
    report['timing']=trace.descriptor()
    report['frame_timing']=trace.frame_projection()
    atomic_json(job/'render-report.json',report)
    return report
