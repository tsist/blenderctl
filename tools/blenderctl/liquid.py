# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded native Mantaflow liquid surface and owned cache validation."""
import bpy,bmesh,math,json
from pathlib import Path
import modeling
from protocol import Failure
KEY='blenderctl_native_liquid_v1'
SETTINGS={'use_mesh':True,'mesh_scale':1,'mesh_particle_radius':2.,'particle_number':2,'particle_randomness':0.,'particle_min':8,'particle_max':16,'use_spray_particles':False,'use_foam_particles':False,'use_bubble_particles':False,'use_viscosity':False,'use_diffusion':False,'use_guide':False,'cache_mesh_format':'BOBJECT'}
def configure(o,d,op):
    for k,v in SETTINGS.items():
        if getattr(d,k)!=v:setattr(d,k,v)
    o[KEY]=json.dumps(op,sort_keys=True)
def native_particles(o):
    return KEY in o and len(o.particle_systems)==1 and o.particle_systems[0].settings.type=='FLIP' and sum(m.type=='FLUID' and m.fluid_type=='DOMAIN' and m.domain_settings.domain_type=='LIQUID' for m in o.modifiers)==1
def validate():
    domains=[(o,m) for o in bpy.context.scene.objects for m in o.modifiers if m.type=='FLUID' and m.fluid_type=='DOMAIN' and m.domain_settings.domain_type=='LIQUID']
    if not domains:return
    if not bpy.app.build_options.fluid or not bpy.app.build_options.openvdb:raise Failure('UNSUPPORTED','Native liquid requires Fluid and OpenVDB build backends')
    for o,m in domains:
        d=m.domain_settings
        if KEY not in o:raise Failure('UNSUPPORTED','Liquid domain requires the explicit native adapter')
        for k,v in SETTINGS.items():
            if getattr(d,k)!=v:raise Failure('UNSUPPORTED','Liquid domain setting outside bounded profile: '+k)
    flows=[]
    for o in bpy.context.scene.objects:
        if o.type not in ('MESH','EMPTY','CAMERA','LIGHT') or o.field and o.field.type!='NONE' or o.particle_systems and not native_particles(o) or o.rigid_body:raise Failure('UNSUPPORTED','Native liquid excludes other solvers and force fields')
        for m in o.modifiers:
            if m.type=='PARTICLE_SYSTEM' and native_particles(o) and m.particle_system==o.particle_systems[0]:continue
            if m.type!='FLUID' or m.fluid_type not in ('DOMAIN','FLOW'):raise Failure('UNSUPPORTED','Native liquid supports domain and geometry flows only')
            if o.animation_data or o.constraints or o.parent or o.data.shape_keys or o.data.animation_data:raise Failure('UNSUPPORTED','Native liquid requires static domain/emitter meshes')
            if any(abs(v)>1e-7 for v in o.rotation_euler) or any(abs(v-1)>1e-7 for v in o.scale):raise Failure('UNSUPPORTED','Apply native liquid domain/emitter rotation and scale before preparation')
            if m.fluid_type=='FLOW':
                if m.flow_settings.flow_type!='LIQUID' or m.flow_settings.flow_behavior!='GEOMETRY':raise Failure('UNSUPPORTED','Native liquid requires LIQUID/GEOMETRY flows')
                flows.append(o)
            elif m.domain_settings.domain_type!='LIQUID':raise Failure('UNSUPPORTED','Do not mix GAS and liquid domains')
    if len(domains)!=1 or len(flows)>8:raise Failure('UNSUPPORTED','Native liquid requires one domain and at most eight flows')
    for o in [domains[0][0],*flows]:
        bm=bmesh.new()
        try:
            bm.from_mesh(o.data)
            if not bm.faces or any(not e.is_manifold for e in bm.edges) or abs(bm.calc_volume())<1e-9:raise Failure('UNSUPPORTED','Liquid input meshes must enclose nonzero volume')
        finally:bm.free()
def sample(o,graph):
    e=o.evaluated_get(graph);mesh=e.to_mesh()
    try:
        if len(mesh.vertices)>100000 or len(mesh.polygons)>200000:raise Failure('UNSUPPORTED','Liquid surface exceeds 100000 vertices / 200000 faces')
        points=[list(e.matrix_world@v.co) for v in mesh.vertices]
        if not all(math.isfinite(v) for p in points for v in p):raise Failure('VALIDATION_FAILED','Nonfinite liquid surface')
        return {'name':o.name,'vertices':points,'faces':len(mesh.polygons),'topology_sha256':modeling.topology(mesh)}
    finally:e.to_mesh_clear()
def cached(o,m,start,end):
    d=m.domain_settings
    if not d.has_cache_baked_data or not d.has_cache_baked_mesh:raise Failure('VALIDATION_FAILED','Liquid requires baked data and surface flags')
    directory=Path(bpy.path.abspath(d.cache_directory))
    for frame in range(start,end+1):
        if not (directory/'mesh'/f'fluid_mesh_{frame:04}.bobj.gz').is_file():raise Failure('VALIDATION_FAILED','Missing native liquid surface cache frame')
