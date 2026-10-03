# SPDX-License-Identifier: GPL-3.0-or-later
"""Managed surface guides and native ribbon-cloth integration; no custom cache store."""
import bpy
import scenes, simulation, hair_surface, hair_dynamics, hair_shape, hair_density, hair_volume
from protocol import Failure, atomic_json, digest
from rigging import animation_safe

def preflight(params, job, for_bake=False):
    animation_safe(params.get('driver_profile'), params.get('file'), job)
    if len(bpy.data.scenes)!=1 or bpy.context.mode!='OBJECT' or bpy.data.libraries:
        raise Failure('UNSUPPORTED','Hair adapters require one local scene in OBJECT mode')
    if bpy.context.scene.rigidbody_world:
        raise Failure('UNSUPPORTED','Hair adapters do not mix rigid bodies')
    bindings=hair_surface.validate_bindings()+hair_shape.validate_bindings()+hair_density.validate(); dynamics=hair_dynamics.validate();hair_volume.validate()
    if for_bake:
        import json
        spec=params['manifest']
        for o in bpy.data.objects:
            if hair_dynamics.KEY not in o:continue
            op=json.loads(o[hair_dynamics.KEY])['op']
            if op['adapter']=='RIBBON_COLLIDERS_V1' and (spec['frame_start'],spec['frame_end'])!=(op['frame_start'],op['frame_end']):
                raise Failure('UNSUPPORTED','Hair bake range must match explicit collider witnesses')
        for o in bpy.data.objects:
            if hair_shape.KEY in o:
                op=hair_shape.metadata(o)['recipe']
                if (spec['frame_start'],spec['frame_end'])!=(op['frame_start'],op['frame_end']):raise Failure('UNSUPPORTED','Hair bake range must match groom surface witnesses')
        for o in bpy.context.scene.objects:
            if hair_density.KEY in o or hair_volume.KEY in o:
                op=(hair_density.metadata(o) if hair_density.KEY in o else hair_volume.metadata(o))['op']
                if (spec['frame_start'],spec['frame_end'])!=(op['frame_start'],op['frame_end']):raise Failure('UNSUPPORTED','Bake must cover complete density/volume witness range')
    managed=hair_surface.managed_objects() | hair_dynamics.managed_objects() | hair_shape.managed_objects() | hair_density.managed_objects() | hair_volume.managed_objects()
    declared=set(params.get('manifest',{}).get('source_cloth',[]))
    inherited={o.name for o in bpy.data.objects if o.name not in managed and any(m.type=='CLOTH' for m in o.modifiers)}
    if for_bake and declared!=inherited:
        raise Failure('UNSUPPORTED','HAIR_CLOTH_V1 source_cloth must exactly name inherited cloth owners')
    from rig_cloth import NODES
    count=0; solvers=0
    for o in bpy.data.objects:
        if o.library or o.override_library or o.instance_type!='NONE' or o.particle_systems or o.rigid_body:
            raise Failure('UNSUPPORTED','Linked, instanced, particle or rigid-body hair dependencies')
        if o.field and o.field.type!='NONE':raise Failure('UNSUPPORTED','Hair profile excludes external force fields')
        if o.type not in ('MESH','ARMATURE','EMPTY','CAMERA','LIGHT','CURVES','VOLUME'):
            raise Failure('UNSUPPORTED','Object type outside hair adapter: '+o.type)
        if o.type=='CURVES' and o.name not in managed:
            raise Failure('UNSUPPORTED','Unmanaged curves require an explicit adapter')
        if o.type=='VOLUME' and o.name not in managed:raise Failure('UNSUPPORTED','Unmanaged volumes require an explicit adapter')
        if o.type=='MESH':count+=len(o.data.vertices)
        if o.type=='CURVES':count+=len(o.data.points)
        for m in o.modifiers:
            if m.type=='CLOTH':
                solvers+=1
                if m.settings.quality>20 or m.collision_settings.collision_quality>8 or o.name in managed and m.collision_settings.use_self_collision:
                    raise Failure('UNSUPPORTED','Hair cloth solver quality outside bounded adapter')
                if for_bake:
                    for prior in list(o.modifiers)[:list(o.modifiers).index(m)]:
                        if prior.show_viewport!=prior.show_render or prior.type=='SUBSURF' and prior.levels!=prior.render_levels:
                            raise Failure('UNSUPPORTED','Cloth input topology must match viewport and render')
                if m.point_cache.use_external or m.point_cache.use_disk_cache:
                    raise Failure('UNSUPPORTED','Hair requires embedded point caches')
            elif m.type=='NODES':
                if o.name not in managed and (not m.node_group or any(n.bl_idname not in NODES for n in m.node_group.nodes)):
                    raise Failure('UNSUPPORTED','Unmanaged geometry nodes outside hair profile')
            elif m.type=='SUBSURF':
                if max(m.levels,m.render_levels)>2:
                    raise Failure('UNSUPPORTED','Background character subdivision exceeds two levels')
            elif m.type=='DATA_TRANSFER':
                if m.show_viewport or m.show_render:
                    raise Failure('UNSUPPORTED','Active data transfer outside hair profile')
            elif m.type not in ('ARMATURE','COLLISION'):
                raise Failure('UNSUPPORTED','Modifier outside hair profile: '+m.type)
    solver_vertices=sum(len(o.data.vertices) for o in bpy.data.objects if o.type=='MESH' and any(m.type=='CLOTH' for m in o.modifiers))
    if count>100000 or solvers>8 or solver_vertices>10000:
        raise Failure('UNSUPPORTED','Hair profile exceeds 100000 input points or eight cloth solvers')
    for image in bpy.data.images:
        if image.source not in ('GENERATED','VIEWER') and not image.packed_file and not image.packed_files:
            raise Failure('UNSUPPORTED','Hair scene images must be packed')
    return count

def samples(frames, profile=None):
    # Each module uses evaluated geometry; profiles are rechecked at every frame.
    rows=[]
    for frame in frames:
        bpy.context.scene.frame_set(frame)
        if profile:
            from drivers import evaluated
            evaluated(profile)
        row=hair_surface.sample([frame])[0]
        row['objects'].extend(hair_shape.sample([frame])[0]['objects'])
        row['objects'].extend(hair_density.sample([frame])[0]['objects'])
        row['objects'].extend(hair_dynamics.sample([frame])[0]['objects'])
        volumes=hair_volume.sample([frame])[0]['volumes']
        if volumes:row['volumes']=volumes
        rows.append(row)
    return rows

def report(frames, profile=None, operations=None):
    r={'hair_report_version':'1.0','bindings':hair_surface.validate_bindings()+hair_shape.validate_bindings()+hair_density.validate(),
       'dynamics':hair_dynamics.validate(),'samples':samples(frames,profile),
       'scope':'Explicit surface guides and native ribbon-cloth; sampled frames, no arbitrary hairstyle or self-collision guarantee'}
    if operations is not None:r['operations']=operations
    if hair_density.managed_objects() or hair_volume.managed_objects():
        r['volumes']=hair_volume.validate()
        r['backend_status']={'openvdb_build':bool(bpy.app.build_options.openvdb),'fluid_build':bool(bpy.app.build_options.fluid),'density_children':'native guide-driven curves; no independent child collision solver','volume_field':'native OpenVDB sampled curve envelope; no fluid advection','gas_cli':'fixed GAS resolution16..32, 2..24 frames','liquid_cli':'separate NATIVE_LIQUID_V1 fixed domain; no coupling to hair'}
    return r

def check_clearance(samples,job):
    rows=[{'frame':r['frame'],'hair':o['object'],**c} for r in samples for o in r['objects'] for c in o.get('collisions',[])]
    if rows:
        failed=[r for r in rows if r.get('clearance_pass') is False]
        atomic_json(job/'hair-collision-quality.json',{'scope':'Declared integer frames, four linear subdivisions per centerline segment; not continuous tube/solid intersection certification','checks':rows,'failed_checks':len(failed),'threshold_requested':any('clearance_pass' in r for r in rows)})
        if failed:raise Failure('VALIDATION_FAILED','Hair sampled collider clearance is below the declared minimum; see hair-collision-quality.json')

def rebound(profile, candidate, job):
    if profile is None:return None
    import drivers
    result=drivers.audit(candidate)['profile_template']
    if result['inventory_sha256']!=profile['inventory_sha256'] or result['acknowledged_unresolved']!=profile['acknowledged_unresolved']:
        raise Failure('VALIDATION_FAILED','Hair operation changed the driver inventory')
    atomic_json(job/'driver-profile.json',result)
    return result

def prepare(params,job):
    from simulation_contract import normalize
    spec=normalize(params['manifest'])
    bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    if tuple(bpy.data.version[:2])!=tuple(bpy.app.version[:2]):
        raise Failure('UNSUPPORTED','Hair prepare requires matching Blender major/minor')
    preflight(params,job);scenes.activate(spec['context'])
    atomic_json(job/'simulation-before.json',simulation.state())
    operations=[]
    for index,op in enumerate(spec['operations']):
        scenes.prepare_context(spec['context'])
        module={'hair.surface':hair_surface,'hair.shape':hair_shape,'hair.dynamics':hair_dynamics,'hair.density':hair_density,'hair.volume':hair_volume}[op['op']]
        try:detail=module.create(op,spec['context'],job) if module is hair_volume else module.create(op,spec['context']);preflight(params,job)
        except Failure as ex:raise Failure(ex.code,f'Operation {index} ({op["op"]}): {ex}') from ex
        operations.append({'index':index,'op':op['op'],'detail':detail})
    scenes.activate(spec['context'])
    expected=report([spec['context']['frame']],params.get('driver_profile'),operations)
    atomic_json(job/'hair-expected.json',expected)
    check_clearance(expected['samples'],job)
    shape_expected=hair_shape.verify_all()
    volume_expected={'density':hair_density.verify_all(),'volume':hair_volume.verify_all()}
    candidate=job/'simulation-candidate.blend';simulation.save(candidate)
    profile=rebound(params.get('driver_profile'),candidate,job)
    bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False)
    scenes.activate(spec['context'])
    preflight({**params,'file':str(candidate),'driver_profile':profile},job)
    shape_observed=hair_shape.verify_all()
    shape_mismatch=scenes.compare(shape_expected,shape_observed)
    atomic_json(job/'hair-shape-checks.json',{'before':shape_expected,'after':shape_observed,'reopen_pass':not shape_mismatch})
    if shape_mismatch:raise Failure('VALIDATION_FAILED','Groom multi-frame reopen mismatch: '+shape_mismatch)
    volume_observed={'density':hair_density.verify_all(),'volume':hair_volume.verify_all()}
    volume_mismatch=scenes.compare(volume_expected,volume_observed)
    atomic_json(job/'hair-volume-checks.json',{'before':volume_expected,'after':volume_observed,'reopen_pass':not volume_mismatch})
    if volume_mismatch:raise Failure('VALIDATION_FAILED','Density/volume multi-frame reopen mismatch: '+volume_mismatch)
    observed=report([spec['context']['frame']],profile,operations)
    mismatch=scenes.compare(expected,observed)
    if mismatch:raise Failure('VALIDATION_FAILED','Hair candidate reopen mismatch: '+mismatch)
    atomic_json(job/'hair-report.json',observed)
    atomic_json(job/'simulation-report.json',simulation.state())
    return {'candidate':str(candidate),'candidate_sha256':digest(candidate),'operations':len(operations),
            'reopen':'pass','report':str(job/'hair-report.json'),
            'cache_policy':'source caches retained; newly created dynamics remain unbaked',
            **({'driver_profile':str(job/'driver-profile.json')} if profile else {})}

def inspect(params,job):
    bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    preflight(params,job)
    if 'context' in params:scenes.activate(params['context'])
    frames=params['hair_frames']
    if len(frames)>1:
        managed=hair_dynamics.managed_objects()
        if any(o and o.name in managed and not c.is_baked for _,_,o,c in simulation.points()):
            raise Failure('UNSUPPORTED','Bake HAIR_CLOTH_V1 before random-access hair inspection')
    result=report(frames,params.get('driver_profile'))
    atomic_json(job/'hair-report.json',result)
    return result
