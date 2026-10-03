# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit collider sets, bounded evaluated witnesses and surface distances.

Geometry witnesses bind the declared integer frame range, not arbitrary
continuous animation. Oriented nearest-surface distance is not a solid-inside
test; open surfaces and normals are an explicit caller choice.
"""
import math
import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree
import modeling,scenes
from hair_dynamics import fail,digest,vec,settings_snapshot

ADAPTER='RIBBON_COLLIDERS_V1'

def objects(op):
    names=[c['object'] for c in op['colliders']]
    if len(names)!=len(set(names)):fail('Collider names must be unique')
    if set(names)&{op['source'],op['name'],op['proxy_name']}:fail('Hair and collider objects must be distinct')
    return [scenes.find(bpy.data.objects,n,True) for n in names]

def admission(o,created=False):
    if o.type!='MESH' or o.library or o.override_library or o.data.library or o.data.users!=1 or o.constraints or o.rigid_body or o.data.animation_data:
        fail('Colliders require local single-user meshes without object constraints, rigid bodies or mesh-data animation')
    if o.name not in bpy.context.view_layer.objects or not o.visible_get():fail('Collider is outside the visible active view layer')
    if o.parent and o.parent_type!='OBJECT':fail('Collider bone/vertex parenting is unsupported')
    types=[m.type for m in o.modifiers]
    if any(t not in ('ARMATURE','SUBSURF','COLLISION') for t in types) or types.count('COLLISION')>1 or ('COLLISION' in types and types[-1]!='COLLISION'):
        fail('Collider supports shape keys, ARMATURE/SUBSURF and a final COLLISION only')
    if created and (not types or types[-1]!='COLLISION'):fail('Managed collider collision modifier is missing')
    for m in o.modifiers:
        if not m.show_viewport or not m.show_render:fail('Collider modifiers must be enabled for viewport and render')
        if m.type=='ARMATURE' and (not m.object or m.object.type!='ARMATURE' or m.object.library):fail('Collider armature must be local and explicit')
        if m.type=='SUBSURF' and (m.levels!=m.render_levels or m.levels>2):fail('Collider subdivision must match render, at most two levels')
    modeling.bounded(o.data)

def structure(o):
    admission(o,True)
    keys=o.data.shape_keys
    return {'object':o.name,'topology_sha256':modeling.topology(o.data),
        'rest_sha256':digest([vec(v.co) for v in o.data.vertices]),
        'weights_sha256':digest({'groups':[g.name for g in o.vertex_groups],'weights':[[(g.group,g.weight) for g in v.groups] for v in o.data.vertices]}),
        'shape_sha256':digest({'relative':keys.use_relative,'keys':[{'name':k.name,'relative':k.relative_key.name,'group':k.vertex_group,'mute':k.mute,'positions':[vec(v.co) for v in k.data]} for k in keys.key_blocks]}) if keys else None,
        'parent':o.parent.name if o.parent else None,
        'modifiers':[{'type':m.type,'settings':settings_snapshot(m),'armature':m.object.name if m.type=='ARMATURE' else None} for m in o.modifiers],
        'collision_settings':settings_snapshot(o.collision)}

def geometry(o,dg):
    e=o.evaluated_get(dg);d=e.data
    if not d.vertices or not d.polygons or len(d.vertices)>100000:fail('Collider evaluated mesh requires faces and 1..100000 vertices')
    if abs(e.matrix_world.determinant())<1e-10:fail('Collider evaluated transform is singular')
    pts=[e.matrix_world@v.co for v in d.vertices]
    if any(not math.isfinite(x) for p in pts for x in p):fail('Nonfinite evaluated collider')
    faces=[list(p.vertices) for p in d.polygons]
    return pts,faces,{'vertices':len(pts),'topology_sha256':modeling.topology(d),'world_positions_sha256':digest([vec(p) for p in pts])}

def prepare(op):
    scene=bpy.context.scene
    if not 1<=op['frame_end']-op['frame_start']<=31:fail('Collider witnesses require 2..32 contiguous integer frames')
    if (scene.frame_start,scene.frame_end)!=(op['frame_start'],op['frame_end']):fail('Collider witness range must match the scene range')
    colliders=objects(op)
    for o in colliders:admission(o)
    scenes.fresh(bpy.data.collections,op['collision_collection'])
    if any(not any(m.type=='COLLISION' for m in o.modifiers) for o in colliders):
        if any(m.type=='CLOTH' and m.collision_settings.use_collision and m.collision_settings.collection is None for o in bpy.data.objects for m in o.modifiers):
            fail('Adding collider modifiers would widen an existing implicit Cloth collision set; isolate that solver explicitly or supply existing collision modifiers')
    # Creation is confined to a disposable prepare candidate. Existing collision
    # settings are preserved; each newly added modifier is reported explicitly.
    added=[]
    for o in colliders:
        if not any(m.type=='COLLISION' for m in o.modifiers):o.modifiers.new('HairCollision','COLLISION');added.append(o.name)
    collection=bpy.data.collections.new(op['collision_collection'])
    scene.collection.children.link(collection)
    for o in colliders:collection.objects.link(o)
    before=scene.frame_current;sub=scene.frame_subframe;witnesses=[];total=0
    try:
        for frame in range(op['frame_start'],op['frame_end']+1):
            scene.frame_set(frame);bpy.context.view_layer.update();dg=bpy.context.evaluated_depsgraph_get();row=[]
            for o in colliders:
                _,_,witness=geometry(o,dg);total+=witness['vertices'];row.append({'object':o.name,**witness})
            if total>1000000:fail('Collider witnesses exceed one million evaluated vertex-frames')
            witnesses.append({'frame':frame,'objects':row})
    finally:scene.frame_set(before,subframe=sub)
    for row in witnesses[1:]:
        if any(a['topology_sha256']!=b['topology_sha256'] for a,b in zip(witnesses[0]['objects'],row['objects'])):fail('Collider evaluated topology changes across the declared frame range')
    return collection,{'collision_collection':collection.name,'colliders':[structure(o) for o in colliders],
        'collision_modifiers_added':added,'collision_witnesses':witnesses,
        'collision_witness_scope':'Exact evaluated geometry at every declared integer frame; no subframe or arbitrary edit guarantee'}

def validate(op,record,cloth):
    colliders=objects(op);collection=cloth.collision_settings.collection
    if not collection or collection.library or collection.name!=op['collision_collection'] or collection.children or set(collection.objects)!=set(colliders):fail('Managed collision collection membership changed')
    if [structure(o) for o in colliders]!=record['colliders']:fail('Managed collider input geometry/settings/dependencies changed')
    if cloth.collision_settings.collision_quality!=op['collision_quality']:fail('Managed collision quality changed')
    if [r['frame'] for r in record['collision_witnesses']]!=list(range(op['frame_start'],op['frame_end']+1)):fail('Collider witness frame inventory changed')
    names=[o.name for o in colliders]
    if any([r.get('object') for r in frame.get('objects',[])]!=names for frame in record['collision_witnesses']):fail('Collider witness object inventory changed')

def observe(op,record,proxy,curves,dg,frame):
    if frame!=int(frame) or not op['frame_start']<=frame<=op['frame_end']:fail('Hair collider samples must be inside the declared integer frame range')
    refs=record['collision_witnesses'][frame-op['frame_start']]['objects'];rows=[]
    e=proxy.evaluated_get(dg);proxypts=[e.matrix_world@v.co for v in e.data.vertices]
    # Four subdivisions per centerline segment plus the final endpoint. This
    # detects sampled radius clearance, never continuous strand/tube contact.
    probes=[]
    for c in curves:
        for i,(a,b) in enumerate(zip(c['points'],c['points'][1:])):
            for j in range(4):
                t=j/4;probes.append((Vector(a).lerp(Vector(b),t),c['radii'][i]*(1-t)+c['radii'][i+1]*t))
        probes.append((Vector(c['points'][-1]),c['radii'][-1]))
    for spec,o,ref in zip(op['colliders'],objects(op),refs):
        pts,faces,witness=geometry(o,dg)
        if {'object':o.name,**witness}!=ref:fail('Collider evaluated geometry changed at frame '+str(frame)+': '+o.name)
        tree=BVHTree.FromPolygons(pts,faces)
        def distance(p):
            hit,normal,index,d=tree.find_nearest(p)
            if hit is None:fail('Collider nearest-surface query failed')
            return -d if spec['distance_mode']=='ORIENTED_SURFACE' and (p-hit).dot(normal)<0 else d
        raw=[distance(p) for p in proxypts];clearance=[distance(p)-radius for p,radius in probes]
        row={'object':o.name,'distance_mode':spec['distance_mode'],**witness,
            'minimum_proxy_vertex_distance':min(raw),'minimum_sampled_curve_clearance':min(clearance),
            'negative_curve_samples':sum(d<0 for d in clearance),'curve_samples':len(clearance)}
        if 'minimum_clearance' in op:row.update(clearance_threshold=op['minimum_clearance'],clearance_pass=min(clearance)>=op['minimum_clearance'])
        rows.append(row)
    return rows
