# SPDX-License-Identifier: GPL-3.0-or-later
"""Isolated physics candidates. Embedded point caches and owned GAS VDB bundles."""
import math,time
from pathlib import Path
import bpy
import scenes,modeling
from inspection import value
from protocol import Failure,atomic_json,digest,read_json
from simulation_contract import normalize,bake_contract,MODEL_OPS,SCENE_OPS

def context(scene,layer,frame):return {'scene':scene,'view_layer':layer,'frame':frame,'mode':'OBJECT','active_object':None,'selected_objects':[]}
def activate_object(o):
    for item in bpy.context.view_layer.objects:item.select_set(False)
    o.select_set(True);bpy.context.view_layer.objects.active=o
def points():
    s=bpy.context.scene;result=[]
    if s.rigidbody_world:result.append(('RIGID_BODY',s.name,None,s.rigidbody_world.point_cache))
    for o in s.objects:
        for m in o.modifiers:
            if m.type in ('CLOTH','SOFT_BODY'):result.append((m.type,o.name,o,m.point_cache))
    return result
def fluids():return [(o,m) for o in bpy.context.scene.objects for m in o.modifiers if m.type=='FLUID' and m.fluid_type=='DOMAIN']
def safe():
    import liquid
    from rigging import animation_safe
    animation_safe()
    if len(bpy.data.scenes)!=1:raise Failure('UNSUPPORTED','Simulation candidate requires one scene')
    if bpy.context.mode!='OBJECT':raise Failure('UNSUPPORTED','Simulation requires OBJECT mode')
    if bpy.data.libraries:raise Failure('UNSUPPORTED','Simulation requires local dependencies')
    total=0
    for o in bpy.data.objects:
        if o.library or o.override_library or o.instance_type!='NONE' or o.particle_systems and not liquid.native_particles(o):raise Failure('UNSUPPORTED','Linked, instanced or particle objects need separate adapters')
        if o.type=='MESH':total+=len(o.data.vertices)
        elif o.type in ('CURVES','POINTCLOUD'):total+=len(o.data.points)
        elif o.type=='GREASEPENCIL':total+=sum(len(f.drawing.attributes['position'].data) for l in o.data.layers for f in l.frames)
        for m in o.modifiers:
            if m.type=='PARTICLE_SYSTEM' and liquid.native_particles(o) and m.particle_system==o.particle_systems[0]:continue
            if m.type not in ('CLOTH','SOFT_BODY','FLUID','COLLISION'):raise Failure('UNSUPPORTED','Apply other modifiers in a separate modeling candidate first: '+m.type)
            if not m.show_viewport or not m.show_render:raise Failure('UNSUPPORTED','Physics modifiers must be enabled in viewport and render')
            if m.type=='CLOTH' and (m.settings.quality>20 or m.collision_settings.use_self_collision):raise Failure('UNSUPPORTED','Cloth quality <=20 without self-collision is the bounded scope')
        if o.rigid_body and any(m.type in ('CLOTH','SOFT_BODY','FLUID') for m in o.modifiers):raise Failure('UNSUPPORTED','Rigid and deforming solvers cannot share a mesh')
    if total>20000:raise Failure('UNSUPPORTED','Simulation input exceeds 20000 mesh vertices')
    for _,_,_,c in points():
        if c.use_external or c.use_disk_cache:raise Failure('UNSUPPORTED','Point caches must be embedded; external source caches are not writable')
    if len(fluids())>1:raise Failure('UNSUPPORTED','Only one fluid domain per candidate')
    for o,m in fluids():
        d=m.domain_settings
        if d.domain_type not in ('GAS','LIQUID') or d.resolution_max>32 or d.use_noise or d.use_adaptive_domain:raise Failure('UNSUPPORTED','Only fixed fluid grids <=32 without noise/adaptive domain')
        if d.cache_type!='ALL' or d.cache_data_format!='OPENVDB':raise Failure('UNSUPPORTED','Only ALL/OPENVDB caches; REPLAY may write on frame evaluation')
    for o in bpy.context.scene.objects:
        for m in o.modifiers:
            if m.type=='FLUID' and m.fluid_type=='FLOW' and (m.flow_settings.flow_type not in ('SMOKE','LIQUID') or m.flow_settings.flow_behavior!='GEOMETRY'):raise Failure('UNSUPPORTED','Only SMOKE or LIQUID GEOMETRY flows')
    import liquid
    liquid.validate()
    return total
def free_points():
    for _,_,o,c in points():
        if o:activate_object(o)
        if c.is_baked:
            with bpy.context.temp_override(point_cache=c):modeling.finished(bpy.ops.ptcache.free_bake())
        if c.is_baked:raise Failure('VALIDATION_FAILED','Point cache failed to invalidate')
def execute(op,ctx):
    k=op['op']
    if k in MODEL_OPS or k in SCENE_OPS:return modeling.execute(op,ctx)
    if k=='physics.world':
        s=bpy.context.scene;s.gravity=op['gravity']
        if not s.rigidbody_world:raise Failure('NOT_FOUND','Create a rigid body before configuring its world')
        s.rigidbody_world.substeps_per_frame=op['substeps'];s.rigidbody_world.solver_iterations=op['iterations'];return
    if k.startswith('special.'):
        scenes.fresh(bpy.data.objects,op['name']);collection=scenes.find(bpy.data.collections,op['collection'],True)
        sets={'special.grease_pencil':bpy.data.grease_pencils,'special.hair':bpy.data.hair_curves,'special.points':bpy.data.pointclouds};data_set=sets[k];scenes.fresh(data_set,op['name']);d=scenes.named(data_set.new(op['name']),op['name']);o=scenes.named(bpy.data.objects.new(op['name'],d),op['name']);collection.objects.link(o)
        if k=='special.grease_pencil':
            drawing=scenes.named(d.layers.new(op['layer']),op['layer']).frames.new(op['frame']).drawing;curves=op['strokes'];drawing.add_strokes([len(c) for c in curves]);attrs=drawing.attributes
            for stroke in drawing.strokes:
                for p in stroke.points:p.radius=op['radius'];p.opacity=1
        elif k=='special.hair':curves=op['curves'];d.add_curves([len(c) for c in curves]);attrs=d.attributes
        else:curves=[op['positions']];d.resize(len(op['positions']));attrs=d.attributes
        attrs['position'].data.foreach_set('vector',[v for c in curves for p in c for v in p])
        if k!='special.grease_pencil':
            radius=attrs.get('radius') or attrs.new('radius','FLOAT','POINT');radius.data.foreach_set('value',[op['radius']]*sum(map(len,curves)))
        d.update_tag();return
    o=scenes.find(bpy.data.objects,op['object'],True)
    if o.type!='MESH' or o.name not in bpy.context.view_layer.objects:raise Failure('UNSUPPORTED','Physics requires a mesh in the current view layer')
    activate_object(o)
    if k=='physics.remove':
        m=scenes.find(o.modifiers,op['modifier'])
        if m.type not in ('CLOTH','SOFT_BODY','FLUID','COLLISION'):raise Failure('UNSUPPORTED','Not a managed physics modifier')
        o.modifiers.remove(m);return
    if k=='physics.rigid':
        if o.rigid_body:raise Failure('CONFLICT','Rigid body already exists')
        modeling.finished(bpy.ops.rigidbody.object_add());r=o.rigid_body;r.type=op['type'];r.mass=op['mass'];r.collision_shape=op['shape'];r.friction=op['friction'];r.restitution=op['restitution'];r.use_margin=True;r.collision_margin=.01;return
    scenes.fresh(o.modifiers,op['name'])
    if any(m.type in ('CLOTH','SOFT_BODY','FLUID') for m in o.modifiers) and k!='physics.collision':raise Failure('CONFLICT','Only one solver modifier per mesh')
    if k=='physics.cloth':
        m=o.modifiers.new(op['name'],'CLOTH');m.settings.quality=op['quality'];m.settings.mass=op['mass'];m.settings.air_damping=op['air_damping']
    elif k=='physics.soft':
        m=o.modifiers.new(op['name'],'SOFT_BODY');m.settings.use_goal=False;m.settings.mass=op['mass'];m.settings.friction=op['friction'];m.settings.use_edges=op['use_edges']
    elif k=='physics.collision':m=o.modifiers.new(op['name'],'COLLISION');o.collision.thickness_outer=op['thickness']
    elif k in ('physics.fluid_domain','physics.fluid_flow','physics.liquid_domain','physics.liquid_flow'):
        if not bpy.app.build_options.fluid or not bpy.app.build_options.openvdb:raise Failure('UNSUPPORTED','Fluid adapter requires Fluid/OpenVDB backends')
        m=o.modifiers.new(op['name'],'FLUID');m.fluid_type='DOMAIN' if k.endswith('domain') else 'FLOW';bpy.context.view_layer.update()
        if m.fluid_type=='DOMAIN':
            d=m.domain_settings;d.domain_type='LIQUID' if k=='physics.liquid_domain' else 'GAS';d.resolution_max=op['resolution'];d.cache_type='ALL';d.cache_data_format='OPENVDB';d.use_noise=False;d.use_adaptive_domain=False;d.cache_directory='';d.cache_frame_start=1;d.cache_frame_end=2
            if d.domain_type=='LIQUID':
                import liquid
                liquid.configure(o,d,op)
        else:
            m.flow_settings.flow_type='LIQUID' if k=='physics.liquid_flow' else 'SMOKE';m.flow_settings.flow_behavior='GEOMETRY'
            if k=='physics.fluid_flow':m.flow_settings.density=op['density']
    else:raise Failure('INVALID_REQUEST','Unknown simulation operation')

def state():
    s=bpy.context.scene;objects=[]
    for o in sorted(s.objects,key=lambda x:x.name):
        row={'name':o.name,'type':o.type,'matrix_world':value(o.matrix_world),'modifiers':[]}
        if o.type=='MESH':row['topology_sha256']=modeling.topology(o.data)
        if o.rigid_body:row['rigid']={k:getattr(o.rigid_body,k) for k in ('type','mass','collision_shape','friction','restitution')}
        for m in o.modifiers:
            x={'name':m.name,'type':m.type}
            if m.type=='CLOTH':x['settings']={k:getattr(m.settings,k) for k in ('quality','mass','air_damping')}
            elif m.type=='SOFT_BODY':x['settings']={k:getattr(m.settings,k) for k in ('use_goal','mass','friction','use_edges')}
            elif m.type=='FLUID':
                x['fluid_type']=m.fluid_type
                if m.fluid_type=='DOMAIN':
                    x['settings']={k:getattr(m.domain_settings,k) for k in ('domain_type','resolution_max','cache_frame_start','cache_frame_end','has_cache_baked_data')};x['settings']['cache_directory']=str(Path(bpy.path.abspath(m.domain_settings.cache_directory)).resolve())
            row['modifiers'].append(x)
        if o.type in ('CURVES','POINTCLOUD'):
            row['positions']=[value(x.vector) for x in o.data.attributes['position'].data];row['radii']=[x.value for x in o.data.attributes['radius'].data]
            if o.type=='CURVES':row['curve_sizes']=[len(c.points) for c in o.data.curves]
        if o.type=='GREASEPENCIL':row['drawings']=[{'layer':l.name,'frame':f.frame_number,'strokes':[[{'position':value(p.position),'radius':p.radius,'opacity':p.opacity} for p in stroke.points] for stroke in f.drawing.strokes]} for l in o.data.layers for f in l.frames]
        objects.append(row)
    return {'simulation_report_version':'1.0','scene':s.name,'gravity':value(s.gravity),'objects':objects,'caches':[{'kind':k,'owner':n,'is_baked':c.is_baked,'start':c.frame_start,'end':c.frame_end} for k,n,o,c in points()]}
def save(path):
    bpy.context.preferences.filepaths.save_version=0
    modeling.finished(bpy.ops.wm.save_as_mainfile(filepath=str(path),relative_remap=True,check_existing=False))
def peak_memory():
    from worker_metrics import peak_memory as observe_peak_memory
    return observe_peak_memory()
def inspect(params,job):
    if 'hair_frames' in params:
        import hair
        return hair.inspect(params,job)
    bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    safe()
    if 'context' in params:scenes.activate(params['context'])
    r=state();atomic_json(job/'simulation-report.json',r);return r
def prepare(params,job):
    spec=normalize(params['manifest'])
    if any(op['op'].startswith('hair.') for op in spec['operations']):
        import hair
        return hair.prepare(params,job)
    if params.get('file'):
        bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
        if tuple(bpy.data.version[:2])!=tuple(bpy.app.version[:2]):raise Failure('UNSUPPORTED','Convert a separate candidate before simulation edits')
    else:bpy.ops.wm.read_factory_settings(use_empty=True);scenes.named(bpy.context.scene,spec.get('initial_scene','Scene'))
    safe();atomic_json(job/'simulation-before.json',state());free_points()
    # A fresh domain replaces an old one without calling free_all on source files.
    for o,m in fluids():
        if m.domain_settings.has_cache_baked_data and not any(op['op']=='physics.remove' and op['object']==o.name and op['modifier']==m.name for op in spec['operations']):raise Failure('UNSUPPORTED','Remove and recreate a baked fluid domain to invalidate it; original VDB files are retained')
    for i,op in enumerate(spec['operations']):
        scenes.prepare_context(spec['context'])
        try:execute(op,spec['context']);safe()
        except Failure as ex:raise Failure(ex.code,f'Operation {i} ({op["op"]}): {ex}') from ex
    scenes.activate(spec['context']);safe()
    for o,m in fluids():m.domain_settings.cache_directory=str(job/'unbaked-fluid-cache')
    expected=state();atomic_json(job/'simulation-expected.json',expected);candidate=job/'simulation-candidate.blend';save(candidate)
    bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False);scenes.activate(spec['context']);observed=state();atomic_json(job/'simulation-report.json',observed)
    mismatch=scenes.compare(expected,observed)
    if mismatch:raise Failure('VALIDATION_FAILED','Simulation reopen mismatch: '+mismatch)
    return {'candidate':str(candidate),'candidate_sha256':digest(candidate),'operations':len(spec['operations']),'reopen':'pass','report':str(job/'simulation-report.json'),'cache_policy':'all embedded caches invalidated; external originals retained'}
def sample(frames,driver_profile=None):
    rows=[];s=bpy.context.scene
    for frame in frames:
        s.frame_set(frame);graph=bpy.context.evaluated_depsgraph_get();row={'frame':frame,'objects':[],'density':[]}
        if driver_profile:
            from drivers import evaluated
            evaluated(driver_profile)
        for o in sorted(s.objects,key=lambda x:x.name):
            if o.type!='MESH' or any(m.type=='FLUID' for m in o.modifiers):continue
            e=o.evaluated_get(graph);mesh=e.to_mesh()
            try:
                if len(mesh.vertices)>(500000 if driver_profile else 20000):raise Failure('UNSUPPORTED','Evaluated mesh exceeds sample limit')
                vertices=[value(e.matrix_world@v.co) for v in mesh.vertices]
                if any(not isinstance(v,(int,float)) or not math.isfinite(v) for p in vertices for v in p):raise Failure('VALIDATION_FAILED','Nonfinite simulation result')
                row['objects'].append({'name':o.name,'vertices':vertices})
            finally:e.to_mesh_clear()
        for o,m in fluids():
            if m.domain_settings.domain_type=='LIQUID':
                import liquid
                row.setdefault('liquid',[]).append(liquid.sample(o,graph));continue
            d=o.evaluated_get(graph).modifiers[m.name].domain_settings;grid=list(d.density_grid)
            if not grid or not all(math.isfinite(v) for v in grid):raise Failure('VALIDATION_FAILED','Missing or nonfinite GAS density grid')
            row['density'].append({'name':o.name,'count':len(grid),'sum':sum(grid),'max':max(grid)})
        rows.append(row)
    return rows
def bake(params,job):
    if params.get('manifest',{}).get('adapter')=='GEOMETRY_ZONE_V1':
        import zone_cache
        return zone_cache.bake(params,job)
    import faulthandler
    faulthandler.enable()
    spec=bake_contract(params['manifest']);started=time.monotonic();receipt=read_json(spec['receipt']['file']) if spec['mode']=='reuse' else None
    mixed=spec.get('adapter')=='RIG_CLOTH_V1';hair_mode=spec.get('adapter')=='HAIR_CLOTH_V1';liquid_mode=spec.get('adapter')=='NATIVE_LIQUID_V1';profile=params.get('driver_profile')
    if mixed and not profile or profile and not (mixed or hair_mode):raise Failure('INVALID_REQUEST','Driver profile requires a matching rig or hair adapter')
    signature={k:spec[k] for k in ('scene','view_layer','frame_start','frame_end')}
    if mixed or hair_mode or liquid_mode:signature['adapter']=spec['adapter']
    if hair_mode and 'source_cloth' in spec:signature['source_cloth']=spec['source_cloth']
    if receipt and (receipt.get('candidate_sha256')!=digest(params['file']) or receipt.get('request')!=signature or receipt.get('blender_build')!=bpy.app.build_hash.decode()):raise Failure('CONFLICT','Cache receipt invalidated by file, range, context or Blender build')
    bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    if tuple(bpy.data.version[:2])!=tuple(bpy.app.version[:2]):raise Failure('UNSUPPORTED','Simulation bake requires matching Blender major/minor')
    # Reject external point caches and replay writes before any frame evaluation.
    if hair_mode:
        import hair
        vertices=hair.preflight(params,job,for_bake=True)
        if receipt and receipt.get('driver_profile')!=profile:raise Failure('CONFLICT','Hair cache driver profile differs from candidate profile')
    elif mixed:
        from rig_cloth import preflight
        vertices=preflight(params,job)
        if receipt and receipt.get('driver_profile')!=profile:raise Failure('CONFLICT','Cache driver profile differs from candidate profile')
    else:vertices=safe()
    has_liquid=any(m.domain_settings.domain_type=='LIQUID' for o,m in fluids())
    if has_liquid!=liquid_mode:raise Failure('UNSUPPORTED','Liquid bake requires explicit NATIVE_LIQUID_V1 adapter and liquid domain')
    flow_types=[m.flow_settings.flow_type for o in bpy.context.scene.objects for m in o.modifiers if m.type=='FLUID' and m.fluid_type=='FLOW']
    if fluids() and (not flow_types or any(t!=('LIQUID' if liquid_mode else 'SMOKE') for t in flow_types)):raise Failure('UNSUPPORTED','Fluid bake requires at least one matching geometry flow')
    scenes.activate(context(spec['scene'],spec['view_layer'],spec['frame_start']))
    if not points() and not fluids():raise Failure('NOT_FOUND','No supported solver')
    if vertices*(spec['frame_end']-spec['frame_start']+1)>1000000:raise Failure('UNSUPPORTED','Simulation exceeds one million input vertex-frames')
    if fluids() and spec['frame_end']-spec['frame_start']>23:raise Failure('UNSUPPORTED','Fluid bake is limited to 24 frames')
    if any(o.name not in bpy.context.view_layer.objects or o.hide_viewport or not o.visible_get() for o in bpy.context.scene.objects):raise Failure('UNSUPPORTED','Bake requires every scene object visible in the selected view layer')
    frames=list(range(spec['frame_start'],spec['frame_end']+1)) if mixed or hair_mode or liquid_mode else sorted(set([spec['frame_start'],(spec['frame_start']+spec['frame_end'])//2,spec['frame_end']]))
    if receipt:
        actual_paths={str(p.resolve()).casefold() for o,m in fluids() for p in Path(bpy.path.abspath(m.domain_settings.cache_directory)).rglob('*') if p.is_file()}
        if hair_mode:
            import hair_volume
            actual_paths.update(str(Path(d['file']).resolve()).casefold() for d in hair_volume.documents())
        declared_paths={str(Path(d['file']).resolve()).casefold() for d in receipt['files']}
        if actual_paths!=declared_paths:raise Failure('CONFLICT','Receipt does not match the complete external cache file set')
        for document in receipt.get('files',[]):
            if digest(document['file'])!=document['expected_sha256']:raise Failure('CONFLICT','Cache file hash differs')
        if not all(c.is_baked for _,_,_,c in points()) or not all(m.domain_settings.has_cache_baked_data for o,m in fluids()):raise Failure('VALIDATION_FAILED','Native baked flag missing')
        if liquid_mode:
            import liquid
            for o,m in fluids():liquid.cached(o,m,spec['frame_start'],spec['frame_end'])
        observed=sorted(sample(list(reversed(frames)),profile),key=lambda x:x['frame']);mismatch=scenes.compare(receipt.get('samples'),observed)
        if mismatch:raise Failure('VALIDATION_FAILED','Cache reuse sample mismatch: '+mismatch)
        if hair_mode:
            hair_observed=sorted(hair.samples(list(reversed(frames)),profile),key=lambda x:x['frame'])
            mismatch=scenes.compare(receipt.get('hair_samples'),hair_observed)
            if mismatch:raise Failure('VALIDATION_FAILED','Hair cache reuse sample mismatch: '+mismatch)
            hair.check_clearance(hair_observed,job)
            atomic_json(job/'hair-report.json',hair.report(frames,profile))
        atomic_json(job/'simulation-samples.json',observed)
        return {'reuse':'pass','candidate_sha256':digest(params['file']),'samples':str(job/'simulation-samples.json'),'rebaked':False}
    free_points();s=bpy.context.scene;s.frame_start=spec['frame_start'];s.frame_end=spec['frame_end'];s.frame_set(s.frame_start)
    # safe() already requires embedded caches. Even assigning False again calls
    # Blender's disk-toggle callback; rigid-body caches without an active object
    # can crash there in 5.2.1. Do not invoke that unrelated storage transition.
    for _,_,_,c in points():c.frame_start=s.frame_start;c.frame_end=s.frame_end
    for o,m in fluids():
        d=m.domain_settings
        if d.has_cache_baked_data:raise Failure('UNSUPPORTED','Prepare a fresh domain before rebaking a fluid source')
        d.cache_directory=str(job/'fluid-cache');d.cache_type='ALL';d.cache_frame_start=s.frame_start;d.cache_frame_end=s.frame_end
    save(job/'simulation-unbaked.blend');atomic_json(job/'simulation-progress.json',{'state':'baking','frame_start':s.frame_start,'frame_end':s.frame_end})
    for _,_,o,c in points():
        if o:activate_object(o)
        with bpy.context.temp_override(point_cache=c):modeling.finished(bpy.ops.ptcache.bake(bake=True))
        if not c.is_baked:raise Failure('VALIDATION_FAILED','Native bake flag not set')
    for o,m in fluids():
        activate_object(o);modeling.finished(bpy.ops.fluid.bake_all())
        if not m.domain_settings.has_cache_baked_data:raise Failure('VALIDATION_FAILED','GAS bake flag not set')
    expected=sample(frames,profile)
    if hair_mode:
        hair_expected=hair.samples(frames,profile)
        hair.check_clearance(hair_expected,job)
    candidate=job/'simulation-candidate.blend';save(candidate)
    bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False);scenes.activate(context(spec['scene'],spec['view_layer'],spec['frame_start']));observed=sorted(sample(list(reversed(frames)),profile),key=lambda x:x['frame'])
    mismatch=scenes.compare(expected,observed)
    if mismatch:raise Failure('VALIDATION_FAILED','Baked candidate random-access reopen mismatch: '+mismatch)
    if hair_mode:
        hair_observed=sorted(hair.samples(list(reversed(frames)),profile),key=lambda x:x['frame'])
        mismatch=scenes.compare(hair_expected,hair_observed)
        if mismatch:raise Failure('VALIDATION_FAILED','Baked hair random-access reopen mismatch: '+mismatch)
    files=[{'file':str(p),'expected_sha256':digest(p),'bytes':p.stat().st_size} for p in sorted((job/'fluid-cache').rglob('*')) if p.is_file()]
    if hair_mode:
        import hair_volume
        files.extend(hair_volume.documents())
    if sum(p['bytes'] for p in files)>128*1024*1024:raise Failure('VALIDATION_FAILED','Cache exceeds 128 MiB acceptance limit')
    if fluids():
        for f in range(spec['frame_start'],spec['frame_end']+1):
            if not (job/'fluid-cache/data'/f'fluid_data_{f:04}.vdb').is_file():raise Failure('VALIDATION_FAILED','Missing expected VDB frame')
        if liquid_mode:
            import liquid
            for o,m in fluids():liquid.cached(o,m,spec['frame_start'],spec['frame_end'])
            if not any(x['vertices'] for row in observed for x in row.get('liquid',[])):raise Failure('VALIDATION_FAILED','Liquid cache contains no surface')
        elif not any(x['max']>0 for row in observed for x in row['density']):raise Failure('VALIDATION_FAILED','GAS cache contains no density')
    result={'simulation_cache_version':'1.0','candidate':str(candidate),'candidate_sha256':digest(candidate),'blender_build':bpy.app.build_hash.decode(),'request':signature,'files':files,'samples':observed,'reopen':'pass','seconds':time.monotonic()-started,'resource_policy':{'threads':2,'input_vertices':vertices,'max_frames':120,'gas_max_resolution':32,'gas_max_frames':24,'cache_bytes':sum(x['bytes'] for x in files),'peak_working_set_bytes':peak_memory(),'memory_hard_limit':False,'disk_limit':'post-bake acceptance; supervisor timeout/cancel enforced'},'scope':'exact file/build/range receipt; sampled frame verification; restart cancelled work from immutable input'}
    if mixed:
        import drivers
        rebound=drivers.audit(candidate)['profile_template']
        if rebound['inventory_sha256']!=profile['inventory_sha256'] or rebound['acknowledged_unresolved']!=profile['acknowledged_unresolved']:raise Failure('VALIDATION_FAILED','Rig driver inventory changed during cloth bake')
        result.update(simulation_cache_version='1.1',driver_profile=rebound,scope='RIG_CLOTH_V1: complete integer frame range, evaluated geometry and native drivers; exact candidate/build/range binding; no subframe guarantee')
        result['resource_policy']['max_frames']=32
        atomic_json(job/'driver-profile.json',rebound)
    if liquid_mode:
        result.update(simulation_cache_version='1.4',scope='NATIVE_LIQUID_V1: fixed native liquid domain, complete integer surface samples, exact owned data/mesh cache inventory and reopen; no coupled hair/fluid solver')
        result['resource_policy'].update(max_frames=24,liquid_max_resolution=32,liquid_max_frames=24)
    if hair_mode:
        rebound=hair.rebound(profile,candidate,job)
        result.update(simulation_cache_version='1.3',hair_samples=hair_observed,scope='HAIR_CLOTH_V1: native ribbon cloth, complete integer frames, exact candidate/build/range receipt; restart cancelled work from immutable input')
        if rebound:result['driver_profile']=rebound
        result['resource_policy']['max_frames']=32
        atomic_json(job/'hair-report.json',hair.report(frames,rebound))
    atomic_json(job/'simulation-receipt.json',result);atomic_json(job/'simulation-progress.json',{'state':'verified'});atomic_json(job/'simulation-report.json',state())
    return {'candidate':str(candidate),'candidate_sha256':result['candidate_sha256'],'receipt':str(job/'simulation-receipt.json'),'receipt_sha256':digest(job/'simulation-receipt.json'),'reopen':'pass','cache_files':len(files),'seconds':result['seconds'],**({'driver_profile':str(job/'driver-profile.json')} if mixed or hair_mode and profile else {})}
