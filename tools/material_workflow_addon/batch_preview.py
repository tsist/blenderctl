# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only multi-object static studio, reusing the single studio renderer.

Caller builds one rig per explicit visible-object set and cleans it after render.
No proxy object, source saves, instance realization or GPU setup is performed.
"""
import copy
import math
from . import preview

KINDS = ('scenes','objects','meshes','cameras','lights','worlds','materials')

def _inventory():
    import bpy
    return {kind:set(getattr(bpy.data,kind)) for kind in KINDS}

def _created(before):
    import bpy
    return {kind:[item for item in getattr(bpy.data,kind) if item not in before[kind]] for kind in KINDS}

def _remove(owned):
    import bpy
    # Only this call's allocations are removed; source materials remain referenced.
    for kind in KINDS:
        collection=getattr(bpy.data,kind)
        for item in owned.get(kind,[]):
            try:
                if collection.get(item.name) is item: collection.remove(item,do_unlink=True)
            except ReferenceError: pass

def cleanup(rig):
    """Idempotently remove the studio and its exclusively owned data blocks."""
    _remove(rig.get('_owned',{})); rig['_owned']={}

def _uv_names(manifest,obj):
    return {a['target']['uv_layer'] for a in manifest.get('assignments',[]) if a.get('target',{}).get('object')==obj.name and a['target'].get('uv_layer')}

def _uv(manifest,obj):
    requests=_uv_names(manifest,obj)
    return next(iter(requests)) if len(requests)==1 else None

def _validate(obj,layer,graph):
    if obj.type!='MESH' or layer.objects.get(obj.name) is not obj: raise ValueError('Explicit source view-layer mesh required: '+obj.name)
    if obj.instance_type!='NONE': raise ValueError('Batch preview does not realize instances: '+obj.name)
    for mod in obj.modifiers:
        if mod.show_viewport!=mod.show_render: raise ValueError('Viewport/render modifier mismatch: '+obj.name+'/'+mod.name)
        if mod.type=='SUBSURF' and mod.levels!=mod.render_levels: raise ValueError('Subdivision viewport/render levels differ: '+obj.name)
    if any(i.is_instance and i.parent and i.parent.original==obj for i in graph.object_instances): raise ValueError('Evaluated instances require explicit realization: '+obj.name)

def build_preview(batch_manifest,objects):
    import bpy
    from mathutils import Vector
    context=batch_manifest['context']; source=bpy.context.scene; layer=bpy.context.view_layer
    if (source.name!=context['scene'] or layer.name!=context['view_layer'] or source.frame_current!=context['frame']): raise ValueError('Source scene/view layer/frame must be active before batch preview capture')
    if not objects or len({o.as_pointer() for o in objects})!=len(objects): raise ValueError('Preview requires a nonempty unique explicit object list')
    graph=bpy.context.evaluated_depsgraph_get()
    for obj in objects: _validate(obj,layer,graph)
    preview.validate_materials([obj.evaluated_get(graph) for obj in objects], batch_manifest['preview']['engine'])
    before=_inventory()
    try:
        single=copy.deepcopy(batch_manifest); single['target']={'uv_layer':_uv(batch_manifest,objects[0])}
        rig=preview.build_preview(single,objects[0]); old_size=rig['size']; old_center=Vector(rig['source_center'])
        for uv in _uv_names(batch_manifest,objects[0]):
            if rig['object'].data.uv_layers.get(uv) is None: raise ValueError('Requested UV missing from evaluated mesh: '+objects[0].name+'/'+uv)
        for index,slot in enumerate(objects[0].evaluated_get(graph).material_slots):
            rig['object'].data.materials[index]=slot.material.original if slot.material else None
        mapping={objects[0].name:rig['object']}
        world_points=[p+old_center for p in rig['points']]
        for original in objects[1:]:
            evaluated=original.evaluated_get(graph)
            mesh=bpy.data.meshes.new_from_object(evaluated,preserve_all_data_layers=True,depsgraph=graph)
            if not mesh.polygons: raise ValueError('Preview mesh contains no surface: '+original.name)
            for index,slot in enumerate(evaluated.material_slots):
                while len(mesh.materials)<=index: mesh.materials.append(None)
                mesh.materials[index]=slot.material.original if slot.material else None
            uv=_uv(batch_manifest,original)
            for name in _uv_names(batch_manifest,original):
                if mesh.uv_layers.get(name) is None: raise ValueError('Requested UV missing from evaluated mesh: '+original.name+'/'+name)
            if uv:
                selected=mesh.uv_layers.get(uv)
                if selected is None: raise ValueError('Requested UV missing from evaluated mesh: '+original.name+'/'+uv)
                mesh.uv_layers.active=selected; selected.active_render=True
            matrix=evaluated.matrix_world.copy()
            world_points.extend(matrix@v.co for v in mesh.vertices)
            if len(world_points)>500000: raise ValueError('Batch preview total vertex budget exceeds 500000')
            obj=bpy.data.objects.new('MW_Batch_'+original.name,mesh); rig['scene'].collection.objects.link(obj); obj.matrix_world=matrix
            mapping[original.name]=obj
        if any(not math.isfinite(x) for p in world_points for x in p): raise ValueError('Batch preview geometry must be finite')
        lo=Vector([min(p[i] for p in world_points) for i in range(3)]); hi=Vector([max(p[i] for p in world_points) for i in range(3)])
        size=(hi-lo).length
        if not 1e-5<=size<=1e6: raise ValueError('Batch preview bounds outside supported extent')
        center=(lo+hi)*0.5
        mapping[objects[0].name].location+=old_center-center
        for original in objects[1:]: mapping[original.name].location-=center
        rig.update(objects=mapping,points=[p-center for p in world_points],bounds=(lo-center,hi-center),size=size,source_center=list(center),manifest=batch_manifest)
        # Refit the existing shared studio to the combined bounds and relative size.
        z=rig['bounds'][0].z-size*1e-4
        floor=[(-size*20,-size*20,z),(size*20,-size*20,z),(size*20,size*20,z),(-size*20,size*20,z)]
        for vertex,co in zip(rig['ground'].data.vertices,floor): vertex.co=co
        rig['ground'].data.update()
        ratio=size/old_size
        for lights in rig['lights'].values():
            for light in lights:
                light.location*=ratio; light.data.size*=ratio; light.data.energy*=ratio*ratio
        rig['limitations']=['multiple static evaluated meshes; no instances; maximum 500000 total vertices','viewport and render modifier settings must match','bounded static surface shaders only; context-dependent nodes are rejected','each view object subset requires its own studio; no hidden-source mutation']
        rig['_owned']=_created(before)
        return rig
    except Exception:
        _remove(_created(before)); raise

def configure_view(rig,view):
    if view.get('objects') is not None and set(view['objects'])!=set(rig['objects']): raise ValueError('Rebuild batch studio for the requested view object set')
    return preview.configure_view(rig,view)

compose_sheet=preview.compose_sheet
