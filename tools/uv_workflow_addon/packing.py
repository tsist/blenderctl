"""Joint native packing on disposable copies; full-candidate review stays external."""
import bpy
import bmesh
import numpy as np
from . import core
from .contract import fail
from .local_solver import uv_area


def pack_with_reuse(objects,layer,old_uv,face_scopes,constraints,action,width,height,border,margin,settings=None):
    """Pack one representative of each declared stack, then restore its aliases."""
    import copy
    groups=settings.get('reuse_groups',[]) if settings else []
    if not groups:return pack(objects,layer,old_uv,face_scopes,constraints,action,width,height,border,margin)
    from .policies import resolve
    # Membership/footprint must already be valid before asking a native solver.
    resolve(objects,layer,{'width':width,'height':height},settings['binding']['unit_scale'],settings)
    selected={n:set(fs) for n,fs in face_scopes.items()};scopes=copy.deepcopy(face_scopes);cs=copy.deepcopy(constraints)
    by_name={o.name:o for o in objects};copies=[];coincident=[]
    for group in groups:
        members=group['members'];participating=[bool(set(m['faces'])&selected.get(m['object'],set())) for m in members]
        if not any(participating):continue
        if not all(participating):fail('Pack must select all members of a declared stack, or leave the entire group unselected','CONFLICT')
        ref=members[0];ref_obj=by_name[ref['object']]
        ref_ids=[i for f in ref['faces'] for i in ref_obj.data.polygons[f].loop_indices]
        # If any alias is fixed, lock the entire representative island.
        if any(any(str(i) in cs[m['object']]['fixed'] for f in m['faces'] for i in by_name[m['object']].data.polygons[f].loop_indices) for m in members):
            cs[ref['object']]['fixed'].update({str(i):old_uv[ref['object']][i].tolist() for i in ref_ids})
        buckets={}
        for i in ref_ids:buckets.setdefault(tuple(old_uv[ref['object']][i]),[]).append(i)
        mapping={key:ids[0] for key,ids in buckets.items()}
        coincident.extend((ref['object'],ids) for ids in buckets.values() if len(ids)>1)
        for m in members[1:]:
            scopes[m['object']]=sorted(set(scopes[m['object']])-set(m['faces']))
            ids=[i for f in m['faces'] for i in by_name[m['object']].data.polygons[f].loop_indices]
            copies.append((m['object'],ids,ref['object'],[mapping[tuple(old_uv[m['object']][i])] for i in ids]))
    out,details=pack(objects,layer,old_uv,scopes,cs,action,width,height,border,margin)
    for name,ids in coincident:
        if not np.all(out[name][ids]==out[name][ids[0]]):fail('Packing split coincident reference corners; alias correspondence is ambiguous','CONFLICT')
    for name,ids,ref,source in copies:
        out[name][ids]=out[ref][source]
        details[name]['stack_alias_loops']=details[name].get('stack_alias_loops',0)+len(ids)
    # Native packing checked representatives; verify alias cuts independently.
    with core.scratch(objects) as (_,clones):
        for obj,clone in zip(objects,clones):
            core._write_uv(clone.data,layer,out[obj.name])
            if core.islands(clone,layer)[1]!=core.islands(obj,layer)[1]:fail('Stack-preserving pack changed existing numeric cuts','CONFLICT')
    return out,details


def pack(objects, layer, old_uv_by_name, faces_by_name, constraints_by_name,
         action, width, height, border_px, margin_px):
    """Pack complete selected islands, preserving all other coordinates exactly.

    Native packing uses an inscribed pixel square. Unselected islands are not
    promised to participate as native obstacles; the caller checks every island.
    """
    span=float(min(width,height)-2*border_px)
    if not np.isfinite(span) or span<=0:fail('Pack border leaves no usable pixel square')
    size=np.asarray([width,height],dtype=np.float64)
    outputs={o.name:old_uv_by_name[o.name].copy() for o in objects}
    selection={};locked={};cuts={};details={}
    for o in objects:
        uv=old_uv_by_name[o.name];groups,numeric_cuts=core.islands(o,layer);cuts[o.name]=numeric_cuts
        faces=set(faces_by_name.get(o.name,[]))
        selected_groups={groups[f] for f in faces}
        resolved={f for f,g in enumerate(groups) if g in selected_groups}
        if faces!=resolved:fail('Pack requires complete numeric UV islands: '+o.name)
        chosen={i for f in faces for i in o.data.polygons[f].loop_indices}
        fixed=set(map(int,constraints_by_name[o.name]['fixed']))
        fixed_groups={groups[p.index] for p in o.data.polygons if any(i in fixed for i in p.loop_indices)}
        frozen={i for p in o.data.polygons if groups[p.index] in selected_groups&fixed_groups for i in p.loop_indices}
        selection[o.name]=chosen;locked[o.name]=frozen
        details[o.name]={'kind':'pack','selected_loops':len(chosen),'locked_island_loops':len(frozen),
                         'changed_loops':0,'native_obstacle_guarantee':False,
                         'pixel_aspect':[width,height],'packing_region':'inscribed_pixel_square',
                         'packing_square_px':span,'long_axis_utilization':'not_optimized',
                         'pin_method':'LOCKED','rotate':action['rotate'],'scale':action['scale']}
    if action['scale'] and any(locked.values()):
        fail('Pack with selected pinned or locked islands requires scale=False','UNSUPPORTED')
    if not any(selection[o.name]-locked[o.name] for o in objects):
        for d in details.values():d['solver']='not_run_no_movable_islands'
        return outputs,details
    work={o.name:((old_uv_by_name[o.name].astype(np.float64)*size-border_px)/span).astype(np.float32) for o in objects}
    if not all(np.isfinite(a).all() for a in work.values()):fail('Nonfinite pack input UV','VALIDATION_FAILED')
    # CLOSEST_UDIM must see the movable batch centered in native tile 1001.
    # Never translate fixed islands; with fixed islands the final checker owns
    # the feasibility verdict for the resulting tile/boundary placement.
    if not any(locked.values()):
        all_selected=np.concatenate([work[o.name][sorted(selection[o.name])] for o in objects if selection[o.name]])
        offset=.5-(all_selected.min(axis=0).astype(float)+all_selected.max(axis=0).astype(float))/2
        for o in objects:
            ids=sorted(selection[o.name]);work[o.name][ids]=(work[o.name][ids].astype(float)+offset).astype(np.float32)
    with core.scratch(objects) as (scene,clones):
        for source,clone in zip(objects,clones):
            n=source.name;m=clone.data
            m.materials.clear()
            while m.uv_layers:m.uv_layers.remove(m.uv_layers[0])
            m.uv_layers.new(name=layer);core._write_uv(m,layer,work[n])
            held=(set(range(len(work[n])))-selection[n])|locked[n]
            for i in held:m.uv_layers[layer].data[i].pin_uv=True
            clone.select_set(True)
        bpy.context.view_layer.objects.active=clones[0]
        ts=scene.tool_settings;ts.use_uv_select_sync=False;ts.uv_select_mode='VERTEX';ts.mesh_select_mode=(False,False,True)
        try:
            bpy.ops.object.mode_set(mode='EDIT')
            for source,clone in zip(objects,clones):
                n=source.name;m=clone.data;bm=bmesh.from_edit_mesh(m)
                bm.faces.ensure_lookup_table();bm.faces.index_update();bm.verts.index_update()
                for v in bm.verts:v.hide_set(False)
                for e in bm.edges:e.hide_set(False)
                bm.select_mode={'FACE'};face_set=set(faces_by_name.get(n,[]));indexed=[]
                for f in bm.faces:
                    ids=list(m.polygons[f.index].loop_indices)
                    if len(ids)!=len(f.loops) or any(l.vert.index!=m.loops[i].vertex_index for l,i in zip(f.loops,ids)):
                        fail('Pack scratch corner mapping differs from source','VALIDATION_FAILED')
                    f.hide_set(False);f.select_set(f.index in face_set);f.uv_select_set(False)
                    for l,i in zip(f.loops,ids):
                        l.uv_select_vert_set(i in selection[n]);indexed.append((i,l))
                bm.uv_select_flush_mode(flush_down=False);bmesh.update_edit_mesh(m)
                if {i for i,l in indexed if l.uv_select_vert}!=selection[n]:
                    fail('Native pack selection differs from planned corners','VALIDATION_FAILED')
            with uv_area():
                result=bpy.ops.uv.pack_islands(udim_source='CLOSEST_UDIM',rotate=action['rotate'],
                    scale=action['scale'],margin_method='FRACTION',margin=core.native_pack_margin(margin_px,span,any(locked.values())),
                    shape_method='CONCAVE',pin=True,pin_method='LOCKED')
            if result!={'FINISHED'}:fail('Native pack did not finish: '+str(result),'VALIDATION_FAILED')
        finally:
            if bpy.context.object and bpy.context.object.mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
        for source,clone in zip(objects,clones):
            n=source.name;solved=core.coordinates(clone.data,layer)
            if not np.isfinite(solved).all():fail('Native pack produced nonfinite UV','VALIDATION_FAILED')
            held=sorted((set(range(len(solved)))-selection[n])|locked[n])
            expected=work[n][held]
            ulp=np.maximum(np.abs(np.nextafter(expected,np.float32(np.inf))-expected),
                           np.abs(np.nextafter(expected,np.float32(-np.inf))-expected))
            if np.any(np.abs(solved[held]-expected)>ulp):
                fail('Native pack moved unselected or locked island UV: '+n,'VALIDATION_FAILED')
            movable=sorted(selection[n]-locked[n]);out=outputs[n]
            out[movable]=((solved[movable].astype(np.float64)*span+border_px)/size).astype(np.float32)
            core._write_uv(clone.data,layer,out)
            if core.islands(clone,layer)[1]!=cuts[n]:
                fail('Native pack changed existing numeric UV cuts: '+n,'VALIDATION_FAILED')
            details[n].update(solver='finished',changed_loops=int(np.count_nonzero(np.any(out!=old_uv_by_name[n],axis=1))),
                              protected_roundoff_restored=int(np.count_nonzero(np.any(solved[held]!=expected,axis=1))))
    return outputs,details
