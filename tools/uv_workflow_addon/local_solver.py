"""Bounded native UV solves on disposable world-space copies only."""
from contextlib import contextmanager
import bpy
import bmesh
import numpy as np
from . import core
from .contract import fail


def boundary_pins(obj, uv, faces, fixed, seam_plan):
    """Freeze patch and chart boundaries, including all continuous UV aliases."""
    mesh=obj.data;selected=set(faces);nodes={};keys={};edges={}
    for p in mesh.polygons:
        ls=list(p.loop_indices)
        for a,b in zip(ls,ls[1:]+ls[:1]):
            key=(mesh.loops[a].vertex_index,*map(float,uv[a]))
            keys[a]=key;nodes.setdefault(key,[]).append(a)
            edges.setdefault(mesh.loops[a].edge_index,[]).append((p.index,a,b))
    anchors=set(map(int,fixed));cuts=set(seam_plan)
    for ei,rows in edges.items():
        chosen=[r for r in rows if r[0] in selected]
        if not chosen:continue
        continuous=len(rows)==2 and {keys[rows[0][1]],keys[rows[0][2]]}=={keys[rows[1][1]],keys[rows[1][2]]}
        if len(chosen)!=2 or not continuous or ei in cuts:
            for _,a,b in chosen:anchors.update((a,b))
    # Pinning one corner pins its continuous UV vertex, without changing the
    # persistent user pin flags or borrowing aliases from the other seam side.
    return sorted({i for anchor in anchors for i in nodes[keys[anchor]]})


@contextmanager
def uv_area():
    screen=bpy.context.screen
    if screen is None:fail('Native local solve requires an available screen','UNSUPPORTED')
    area=next((a for a in screen.areas if a.type=='IMAGE_EDITOR'),None)
    if area is None:area=next(iter(screen.areas),None)
    if area is None:fail('Native local solve requires an editor area','UNSUPPORTED')
    old_type,old_ui=area.type,area.ui_type
    try:
        area.type='IMAGE_EDITOR';area.ui_type='UV'
        region=next(r for r in area.regions if r.type=='WINDOW')
        with bpy.context.temp_override(area=area,region=region):yield
    finally:
        area.type=old_type;area.ui_type=old_ui


def solve(obj, layer, uv, faces, constraints, seam_plan, action, width, height):
    chosen=sorted({i for f in faces for i in obj.data.polygons[f].loop_indices})
    selected=set(chosen)
    pins=boundary_pins(obj,uv,faces,constraints['fixed'],seam_plan)
    free=sorted(selected-set(pins))
    detail={'kind':action['kind'],'boundary_policy':'preserve_chart_and_patch_boundary',
            'temporary_pins':len(pins),'free_loops':len(free),'changed_loops':0,
            'pixel_aspect':[width,height],'world_space_geometry':True}
    if not free:
        detail['solver']='not_run_no_free_interior';return uv.copy(),detail
    if not pins:fail('Local solve needs chart boundaries or fixed anchors','UNSUPPORTED')
    ratio=np.array([width,height],dtype=np.float64)/max(width,height)
    working=(uv.astype(np.float64)*ratio).astype(np.float32)
    if not np.isfinite(working).all():fail('Nonfinite input UV','VALIDATION_FAILED')
    with core.scratch([obj]) as (scene,clones):
        clone=clones[0];mesh=clone.data
        # minimize_stretch internally enables material-image aspect correction.
        # The working UV already uses manifest pixel aspect; remove consumers
        # from this disposable mesh to prevent an unrelated image scaling twice.
        mesh.materials.clear()
        while mesh.uv_layers:mesh.uv_layers.remove(mesh.uv_layers[0])
        mesh.uv_layers.new(name=layer);core._write_uv(mesh,layer,working)
        # Native unwrap reads mesh seams; use this candidate's plan and numeric
        # chart cuts, never unrelated source-layer seam flags.
        _,numeric_cuts=core.islands(obj,layer)
        cuts=set(seam_plan)
        for e in mesh.edges:e.use_seam=e.index in cuts or tuple(sorted(e.vertices)) in numeric_cuts
        held=sorted((set(range(len(uv)))-selected)|set(pins))
        for i in held:mesh.uv_layers[layer].data[i].pin_uv=True
        clone.select_set(True);bpy.context.view_layer.objects.active=clone
        ts=scene.tool_settings;ts.use_uv_select_sync=False;ts.uv_select_mode='VERTEX';ts.mesh_select_mode=(False,False,True)
        try:
            bpy.ops.object.mode_set(mode='EDIT')
            bm=bmesh.from_edit_mesh(mesh);bm.faces.ensure_lookup_table();bm.faces.index_update()
            bm.verts.index_update()
            for v in bm.verts:v.hide_set(False)
            for e in bm.edges:e.hide_set(False)
            bm.select_mode={'FACE'};face_set=set(faces)
            native_selection=set(free) if action['kind']=='relax' else selected
            indexed_loops=[]
            for f in bm.faces:
                source_loops=list(mesh.polygons[f.index].loop_indices)
                if len(source_loops)!=len(f.loops) or any(loop.vert.index!=mesh.loops[i].vertex_index for loop,i in zip(f.loops,source_loops)):
                    fail('Native edit mesh corner mapping differs from source','VALIDATION_FAILED')
                f.hide_set(False);f.select_set(f.index in face_set);f.uv_select_set(False)
                for loop,loop_index in zip(f.loops,source_loops):
                    # Blender 5.2 stretch_begin deliberately clears user pins.
                    # Its minimizer also excludes unselected UV vertices, so
                    # hold every protected alias by selection as well as pin.
                    loop.uv_select_vert_set(loop_index in native_selection);indexed_loops.append((loop_index,loop))
            bm.uv_select_flush_mode(flush_down=False);bmesh.update_edit_mesh(mesh)
            actual={i for i,l in indexed_loops if l.uv_select_vert}
            if actual!=native_selection:fail('Native UV selection differs from planned free corners','VALIDATION_FAILED')
            with uv_area():
                if action['kind']=='relax':
                    result=bpy.ops.uv.minimize_stretch(iterations=action['iterations'],fill_holes=True,blend=0.)
                elif action['method']=='MINIMUM_STRETCH':
                    result=bpy.ops.uv.unwrap(
                        method='MINIMUM_STRETCH',
                        iterations=int(action.get('iterations',10)),
                        no_flip=bool(action.get('no_flip',True)),
                        correct_aspect=False,
                        margin=0.,
                    )
                else:result=bpy.ops.uv.unwrap(method=action['method'],correct_aspect=False,margin=0.)
            if result!={'FINISHED'}:fail('Native local solve did not finish: '+str(result),'VALIDATION_FAILED')
        finally:
            if clone.mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
        solved=core.coordinates(mesh,layer)
        if not np.isfinite(solved).all():fail('Native local solve produced nonfinite UV','VALIDATION_FAILED')
        expected=working[held]
        ulp=np.maximum(np.abs(np.nextafter(expected,np.float32(np.inf))-expected),
                       np.abs(np.nextafter(expected,np.float32(-np.inf))-expected))
        if np.any(np.abs(solved[held]-expected)>ulp):
            bad=np.any(np.abs(solved[held]-expected)>ulp,axis=1)
            moved=np.asarray(held)[bad].tolist()
            delta=float(np.max(np.abs(solved[held]-expected)))*max(width,height)
            fail('Native local solve moved protected UV beyond one float32 ULP: '+str(moved[:8])+'; max pixel drift '+str(delta),'VALIDATION_FAILED')
        out=uv.copy();out[free]=(solved[free].astype(np.float64)/ratio).astype(np.float32)
        core._write_uv(mesh,layer,out)
        if core.islands(clone,layer)[1]!=numeric_cuts:
            fail('Local solve changed existing numeric UV cuts','VALIDATION_FAILED')
        detail.update(solver='finished',changed_loops=int(np.count_nonzero(np.any(out!=uv,axis=1))),
                      protected_roundoff_restored=int(np.count_nonzero(np.any(solved[held]!=expected,axis=1))))
        return out,detail
