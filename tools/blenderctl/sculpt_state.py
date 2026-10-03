# SPDX-License-Identifier: GPL-3.0-or-later
"""Preservation oracle for explicitly limited local static mesh scenes."""
import bpy,math
from protocol import Failure
from review_contract import key

def simple(v):
    if v is None or isinstance(v,(str,int,float,bool)):return v
    if isinstance(v,bpy.types.ID):return {'id':v.name,'type':v.bl_rna.identifier}
    if hasattr(v,'to_dict'):return {k:simple(x) for k,x in v.to_dict().items()}
    if hasattr(v,'items'):return {k:simple(x) for k,x in v.items()}
    try:return [simple(x) for x in v]
    except TypeError:raise Failure('UNSUPPORTED','Unsupported custom data value')

def attributes(mesh):
    rows={}
    for a in mesh.attributes:
        # Position is compared separately with topology, preserving all other attributes.
        if a.name=='position':continue
        data=[]
        for d in a.data:
            props={p.identifier:simple(getattr(d,p.identifier)) for p in d.bl_rna.properties
                   if p.identifier in ('value','vector','color')}
            data.append(props)
        rows[a.name]={'type':a.data_type,'domain':a.domain,'data':data}
    return rows

def node_tree(tree):
    if tree is None:return None
    allowed={'ShaderNodeBsdfPrincipled','ShaderNodeOutputMaterial','ShaderNodeBackground','ShaderNodeOutputWorld'}
    if tree.animation_data or len(tree.nodes)>8 or any(n.bl_idname not in allowed for n in tree.nodes):
        raise Failure('UNSUPPORTED','Only simple Principled/Background output node graphs are supported')
    nodes={}
    for n in tree.nodes:
        props={p.identifier:simple(getattr(n,p.identifier)) for p in n.bl_rna.properties
               if p.type in ('BOOLEAN','INT','FLOAT','STRING','ENUM') and p.identifier not in ('rna_type','select')}
        nodes[n.name]={'type':n.bl_idname,'properties':props,
            'inputs':[[s.identifier,simple(s.default_value) if hasattr(s,'default_value') else None] for s in n.inputs],
            'custom':simple(dict(n.items()))}
    return {'nodes':nodes,'links':sorted([l.from_node.name,l.from_socket.identifier,l.to_node.name,l.to_socket.identifier] for l in tree.links)}

def capture(target):
    if len(bpy.data.scenes)!=1 or len(bpy.context.scene.view_layers)!=1:
        raise Failure('UNSUPPORTED','Replay requires one scene and one view layer')
    if bpy.data.libraries or bpy.data.actions or bpy.data.node_groups or bpy.data.shape_keys:
        raise Failure('UNSUPPORTED','Linked, animated, node-group or shape-key scenes are outside replay scope')
    for coll in (bpy.data.objects,bpy.data.meshes,bpy.data.materials,bpy.data.worlds,bpy.data.scenes):
        if any(d.animation_data for d in coll):raise Failure('UNSUPPORTED','Animation/drivers unsupported in replay')
    if bpy.context.scene.compositing_node_group or (bpy.context.scene.sequence_editor and len(bpy.context.scene.sequence_editor.strips)):
        raise Failure('UNSUPPORTED','Compositor and sequencer scenes unsupported')
    if any(im.source not in ('GENERATED','VIEWER') for im in bpy.data.images):
        raise Failure('UNSUPPORTED','External/packed image dependencies unsupported')
    if len(bpy.data.objects)>32 or sum(len(m.vertices) for m in bpy.data.meshes)>100000:
        raise Failure('UNSUPPORTED','Replay scene exceeds 32 objects / 100000 vertices')
    objects={}
    for o in bpy.data.objects:
        if o.type!='MESH' or o.parent or o.constraints or o.modifiers or o.data.shape_keys or o.data.users!=1:
            raise Failure('UNSUPPORTED','Replay requires plain single-user unparented meshes')
        mesh=o.data
        if o.mode!='OBJECT':raise Failure('UNSUPPORTED','Source objects must be in Object mode')
        if o.name==target and (o.hide_get() or o.hide_viewport or o.hide_select or not o.visible_get()):
            raise Failure('UNSUPPORTED','Replay target must be visible and selectable')
        if any(a.name.startswith('.sculpt') for a in mesh.attributes):
            raise Failure('UNSUPPORTED','Sculpt masks/face sets are outside first replay scope')
        coords=[list(v.co) for v in mesh.vertices]
        if any(not math.isfinite(c) or abs(c)>10000 for p in coords for c in p):raise Failure('UNSUPPORTED','Nonfinite or unbounded geometry')
        objects[o.name]={'vertices':coords,'edges':[list(e.vertices) for e in mesh.edges],
            'faces':[list(p.vertices) for p in mesh.polygons],'attributes':attributes(mesh),
            'uv_layers':[[u.name,u.active_render,u.active_clone] for u in mesh.uv_layers],
            'uv_active':mesh.uv_layers.active_index,
            'weights':[[[g.group,g.weight] for g in v.groups] for v in mesh.vertices],
            'groups':[[g.name,g.lock_weight] for g in o.vertex_groups],
            'materials':[m.name if m else None for m in mesh.materials],
            'face_flags':[[p.material_index,p.use_smooth] for p in mesh.polygons],
            'matrix':[list(r) for r in o.matrix_world],'mesh_name':mesh.name,
            'visibility':[o.hide_get(),o.hide_render,o.hide_viewport,o.hide_select],
            'collections':sorted(c.name for c in o.users_collection),
            'custom':simple(dict(o.items())),'mesh_custom':simple(dict(mesh.items()))}
    if target not in objects:raise Failure('NOT_FOUND','Replay target not found')
    o=bpy.data.objects[target]
    if not o.data.polygons or not o.data.vertices:raise Failure('UNSUPPORTED','Replay target needs a surface')
    if abs(o.matrix_world.determinant())<1e-10:raise Failure('UNSUPPORTED','Singular target transform')
    mats={m.name:{'color':list(m.diffuse_color),'metallic':m.metallic,'roughness':m.roughness,'custom':simple(dict(m.items())),
                  'nodes':node_tree(m.node_tree)} for m in bpy.data.materials}
    row={'objects':objects,'materials':mats,'scene':bpy.context.scene.name,
         'worlds':{w.name:{'color':list(w.color),'nodes':node_tree(w.node_tree)} for w in bpy.data.worlds},
         'frame':bpy.context.scene.frame_current,'collections':{c.name:sorted(o.name for o in c.objects) for c in bpy.data.collections}}
    key(row)
    return row

def compare(before,after,target,limit):
    from mathutils import Vector
    import copy
    a=before['objects'][target]['vertices'];b=after['objects'].get(target,{}).get('vertices',[])
    if len(a)!=len(b):raise Failure('VALIDATION_FAILED','Target vertex count changed')
    protected=copy.deepcopy(after);protected['objects'][target]['vertices']=a
    if key(before)!=key(protected):raise Failure('VALIDATION_FAILED','Protected geometry/attributes/materials/transforms changed')
    delta=[(Vector(x)-Vector(y)).length for x,y in zip(a,b)]
    changed=sum(d>1e-7 for d in delta);maximum=max(delta,default=0)
    if not changed:raise Failure('VALIDATION_FAILED','Native stroke produced no measurable geometry change')
    if maximum>limit:raise Failure('VALIDATION_FAILED','Native displacement exceeds explicit request bound')
    return {'changed_vertices':changed,'max_displacement_local':maximum,'protected_fields_unchanged':True}
