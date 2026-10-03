# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit mixed rig/cloth cache admission. No changes to rig or solver settings."""
import bpy
from protocol import Failure
NODES={'NodeGroupInput','NodeGroupOutput','NodeFrame','GeometryNodeObjectInfo','ShaderNodeCombineXYZ','ShaderNodeVectorRotate','GeometryNodeInputMeshEdgeAngle','GeometryNodeStoreNamedAttribute'}
def preflight(params,job):
    profile=params.get('driver_profile')
    if not profile:raise Failure('INVALID_REQUEST','RIG_CLOTH_V1 requires a source-bound driver profile')
    from rigging import animation_safe
    animation_safe(profile,params['file'],job)
    if len(bpy.data.scenes)!=1 or bpy.context.mode!='OBJECT' or bpy.data.libraries:raise Failure('UNSUPPORTED','Rig cloth requires one local scene in OBJECT mode')
    if bpy.context.scene.rigidbody_world:raise Failure('UNSUPPORTED','Rig cloth does not mix rigid-body solvers')
    vertices=0;solver_vertices=0;solvers=0
    for o in bpy.data.objects:
        if o.type not in ('MESH','ARMATURE','EMPTY','CAMERA','LIGHT'):raise Failure('UNSUPPORTED','Rig cloth samples mesh surfaces; other geometry types require a separate adapter')
        if o.library or o.override_library or o.instance_type!='NONE' or o.particle_systems:raise Failure('UNSUPPORTED','Linked, instanced or particle data outside rig cloth profile')
        if o.rigid_body:raise Failure('UNSUPPORTED','Rigid-body data outside rig cloth profile')
        if o.type=='MESH':vertices+=len(o.data.vertices)
        count=0
        ad=o.animation_data
        if ad and ad.use_tweak_mode:raise Failure('UNSUPPORTED','NLA tweak mode is unsupported')
        for m in o.modifiers:
            if m.type=='CLOTH':
                for prior in list(o.modifiers)[:list(o.modifiers).index(m)]:
                    if prior.show_viewport!=prior.show_render or prior.type=='SUBSURF' and prior.levels!=prior.render_levels:raise Failure('UNSUPPORTED','Cloth input topology must match between viewport and render')
                count+=1;solvers+=1;solver_vertices+=len(o.data.vertices)
                if not m.show_viewport or not m.show_render or m.settings.quality>20 or m.collision_settings.collision_quality>8:raise Failure('UNSUPPORTED','Cloth must be enabled with bounded solver quality')
                c=m.point_cache
                if c.use_external or c.use_disk_cache:raise Failure('UNSUPPORTED','Rig cloth requires embedded point caches')
            elif m.type=='DATA_TRANSFER':
                if m.show_viewport or m.show_render:raise Failure('UNSUPPORTED','Active data transfer requires a separate cache dependency adapter')
            elif m.type=='NODES':
                if not m.node_group or any(n.bl_idname not in NODES for n in m.node_group.nodes):raise Failure('UNSUPPORTED','Geometry node group outside rig cloth profile')
            elif m.type=='SUBSURF':
                if max(m.levels,m.render_levels)>2:raise Failure('UNSUPPORTED','Rig cloth subdivision is bounded to two levels')
            elif m.type not in ('ARMATURE','COLLISION'):raise Failure('UNSUPPORTED','Modifier outside rig cloth profile: '+m.type)
        if count>1:raise Failure('UNSUPPORTED','One cloth solver per mesh is required')
    if not 1<=solvers<=8 or solver_vertices>10000 or vertices>100000:raise Failure('UNSUPPORTED','Rig cloth exceeds 8 solvers/10000 solver vertices/100000 total vertices')
    for image in bpy.data.images:
        if image.source not in ('GENERATED','VIEWER') and not image.packed_file and not image.packed_files:raise Failure('UNSUPPORTED','Rig cloth input images must be packed')
    return vertices
