"""Native candidate creation on temporary meshes; original render consumers stay bound."""
import copy
import hashlib
import json
import math
from contextlib import contextmanager
import bpy
import bmesh
import numpy as np
from .contract import normalize, fail

STATE = "uv_workflow_state_v1"

def native_pack_margin(gap_px, pixel_span, has_locked_islands):
    """Blender 5.2 uses two FRACTION margins for free/free, one with locks.

    Retain one pixel of total safety. Independent measurement remains binding.
    """
    return (gap_px + 1.) / (pixel_span * (1. if has_locked_islands else 2.))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()

def state(scene):
    return json.loads(scene.get(STATE, '{"candidates":{},"adopted":null}'))

def _store(scene, value):
    scene[STATE] = json.dumps(value, ensure_ascii=False, allow_nan=False)

def coordinates(mesh, layer):
    a = np.empty(len(mesh.loops)*2, dtype=np.float32)
    mesh.uv_layers[layer].uv.foreach_get('vector', a)
    return a.reshape(-1,2)

def _write_uv(mesh, layer, values):
    mesh.uv_layers[layer].uv.foreach_set('vector', np.asarray(values,dtype=np.float32).ravel())
    mesh.update()

def _pins(mesh, layer):
    items=mesh.uv_layers[layer].pin
    if not len(items): return []
    values=np.zeros(len(mesh.loops), dtype=bool);items.foreach_get('value',values)
    return np.flatnonzero(values).tolist()

def _uv_fingerprint(mesh,layer):
    return _hash({'uv':coordinates(mesh,layer).tolist(),'pins':_pins(mesh,layer)})

def geometry(o):
    m=o.data
    return _hash({"positions":[list(v.co) for v in m.vertices],"edges":[list(e.vertices) for e in m.edges],
                  "faces":[list(p.vertices) for p in m.polygons],"world":[list(r) for r in o.matrix_world]})

def protection(objects, excluded=()):
    objects=list(objects)
    protected=integrity_snapshot(objects)
    return {o.name:{"geometry":geometry(o),"mesh":o.data.name,
        "editable_data":protected['objects'][o.name],
        "rig_dependencies":_object_dependencies(protected,o.name),
        "layers":{u.name:_hash(coordinates(o.data,u.name).tolist()) for u in o.data.uv_layers if u.name not in excluded},
        "pins":{u.name:_pins(o.data,u.name) for u in o.data.uv_layers if u.name not in excluded},
        "render":[u.name for u in o.data.uv_layers if u.active_render and u.name not in excluded],
        "materials":[s.material.name if s.material else None for s in o.material_slots],
        "seams":[e.index for e in o.data.edges if e.use_seam]} for o in objects}

def integrity_snapshot(objects):
    from .integrity import snapshot, IntegrityError
    try:return snapshot(objects)
    except IntegrityError as exc:fail(exc.message,exc.code)

def _object_dependencies(protected,name):
    deps=protected['dependencies'];links=deps['target_links'][name]
    refs=[r['object'] for r in links['armature_modifiers']]+links['parent_chain']
    names={r['name'] for r in refs if r}
    return {'target_links':links,'armatures':{n:row for n,row in deps['armatures'].items() if n in names}}

def _resolve(v):
    scene=bpy.data.scenes.get(v['scene'])
    if not scene or v['view_layer'] not in scene.view_layers: fail("Missing scene/view layer", "NOT_FOUND")
    vl=scene.view_layers[v['view_layer']];objects=[]
    for name in v['objects']:
        o=vl.objects.get(name)
        if not o or o.type!='MESH': fail("Missing mesh target: "+name,"NOT_FOUND")
        if o.mode!='OBJECT':
            fail("UV workflow requires Object Mode; target %s is in %s mode. "
                 "Switch to Object Mode; for CLI use, save the working copy after switching." %
                 (o.name, o.mode), "CONFLICT")
        if o.library or o.data.library or o.override_library:
            fail("UV workflow requires local mesh targets; source retained", "UNSUPPORTED")
        objects.append(o)
    return scene,objects

def islands(o, layer):
    m=o.data;uv=coordinates(m,layer);parent=list(range(len(m.polygons)));edges={}
    def find(i):
        while parent[i]!=i:parent[i]=parent[parent[i]];i=parent[i]
        return i
    for f in m.polygons:
        ls=list(f.loop_indices)
        for a,b in zip(ls,ls[1:]+ls[:1]):
            va,vb=m.loops[a].vertex_index,m.loops[b].vertex_index
            pair=(tuple(uv[a]),tuple(uv[b]))
            if va>vb:va,vb=vb,va;pair=pair[::-1]
            edges.setdefault((va,vb),[]).append((f.index,pair))
    cuts=set()
    for key,rows in edges.items():
        if len(rows)==2 and rows[0][1]==rows[1][1]:parent[find(rows[0][0])]=find(rows[1][0])
        elif len(rows)==2:cuts.add(key)
    return [find(f.index) for f in m.polygons],cuts

def world_positions(o,unit):
    """Transform stored mesh coordinates in float64 before world translation.

    mathutils performs the transform in float32 and can collapse real narrow
    bevel triangles far from the origin before NumPy ever receives them.
    """
    local=np.empty(len(o.data.vertices)*3,dtype=np.float32)
    o.data.vertices.foreach_get('co',local)
    matrix=np.asarray(o.matrix_world,dtype=np.float64)
    return (local.reshape(-1,3).astype(np.float64)@matrix[:3,:3].T+matrix[:3,3])*unit


def measurement_input(objects,layer,unit):
    rows=[]
    for o in objects:
        m=o.data;m.calc_loop_triangles();uv=coordinates(m,layer);ids,_=islands(o,layer)
        rows.append({'name':o.name,'positions':world_positions(o,unit),
                     'triangles':np.array([list(t.vertices) for t in m.loop_triangles],dtype=int),
                     'uv':np.array([uv[list(t.loops)] for t in m.loop_triangles]),
                     'islands':np.array([ids[t.polygon_index] for t in m.loop_triangles])})
    return rows

def _check_geometry(objects,unit):
    rows=[]
    for o in objects:
        m=o.data;m.calc_loop_triangles();bad=[];positions=world_positions(o,unit)
        for i,t in enumerate(m.loop_triangles):
            a,b,c=positions[list(t.vertices)]
            area=np.linalg.norm(np.cross(b-a,c-a))/2
            if not math.isfinite(area) or area<=0:bad.append({'triangle':i,'face':t.polygon_index})
        if bad:rows.append({'object':o.name,'count':len(bad),'examples':bad[:32]})
    if rows:fail("Geometry repair required before UV: "+json.dumps(rows,ensure_ascii=False),"VALIDATION_FAILED")

def _constraints(o,v):
    c=dict(v['constraints'].get(o.name,{}));m=o.data
    source=c.get('source_uv') or (m.uv_layers.active.name if m.uv_layers.active else None)
    if source and source not in m.uv_layers:fail("Missing constraint source UV", "NOT_FOUND")
    c.update(source_uv=source)
    for key in ('must_cut','must_not_cut','pins','locked_faces'):c.setdefault(key,[])
    if source:c['pins']=sorted(set(c['pins'])|set(_pins(m,source)))
    for key,size in (('must_cut',len(m.edges)),('must_not_cut',len(m.edges)),('pins',len(m.loops)),('locked_faces',len(m.polygons))):
        if any(i>=size for i in c[key]):fail("Constraint index out of range: "+o.name+"/"+key)
    if (c['pins'] or c['locked_faces']) and not source:fail("Fixed constraints require source_uv")
    fixed=set(c['pins'])
    for f in c['locked_faces']:fixed.update(m.polygons[f].loop_indices)
    uv=coordinates(m,source) if source else None
    c['fixed']={str(i):uv[i].tolist() for i in sorted(fixed)}
    return c

@contextmanager
def scratch(objects):
    window=bpy.context.window;old_scene=window.scene;old_vl=window.view_layer
    temporary=bpy.data.scenes.new('UV_Workflow_Scratch');clones=[]
    try:
        window.scene=temporary
        for o in objects:
            m=o.data.copy();clone=bpy.data.objects.new(o.name,m);temporary.collection.objects.link(clone)
            clones.append(clone)
            # Native edit-mode unwrap must operate on base vertices. Clear only
            # the independent scratch Key copy; source Keys and rig remain intact.
            if m.shape_keys:clone.shape_key_clear()
            # Translation has no effect on the intrinsic solve metric. Adding
            # it to float32 Mesh coordinates can collapse valid narrow faces at
            # large world origins. Apply the linear metric in float64, then
            # recenter before the one unavoidable float32 Mesh write. Each
            # object is solved independently; their world-space separation is
            # not an unwrap constraint. Source geometry/transform stays intact.
            positions=np.empty(len(m.vertices)*3,dtype=np.float64)
            m.vertices.foreach_get('co',positions)
            linear=np.asarray(o.matrix_world,dtype=np.float64)[:3,:3]
            transformed=positions.reshape(-1,3)@linear.T
            if len(transformed):
                transformed-=transformed.min(axis=0)+(transformed.max(axis=0)-transformed.min(axis=0))*.5
            if not np.isfinite(transformed).all():
                fail('Nonfinite scratch geometry transform','VALIDATION_FAILED')
            with np.errstate(over='ignore',invalid='ignore'):
                native_positions=transformed.astype(np.float32)
            if not np.isfinite(native_positions).all():
                fail('Scratch geometry exceeds native float32 coordinate range','UNSUPPORTED')
            m.vertices.foreach_set('co',native_positions.ravel())
            m.update()
        yield temporary,clones
    finally:
        try:
            if bpy.context.object and bpy.context.object.mode!='OBJECT':bpy.ops.object.mode_set(mode='OBJECT')
        finally:
            window.scene=old_scene;window.view_layer=old_vl
            for o in clones:
                m=o.data;bpy.data.objects.remove(o,do_unlink=True);bpy.data.meshes.remove(m)
            bpy.data.scenes.remove(temporary)

def _edit(objects):
    bpy.ops.object.select_all(action='DESELECT')
    for o in objects:o.select_set(True)
    bpy.context.view_layer.objects.active=objects[0]
    bpy.context.scene.tool_settings.mesh_select_mode=(False,False,True)
    bpy.ops.object.mode_set(mode='EDIT')
    for o in objects:
        bm=bmesh.from_edit_mesh(o.data);bm.select_mode={'FACE'}
        for f in bm.faces:f.select_set(True);f.uv_select_set(True)
        bm.uv_select_flush_mode(flush_down=True);bmesh.update_edit_mesh(o.data)

def _seam_plan(o,c,automatic=False):
    m=o.data;adj={e.index:[] for e in m.edges}
    for p in m.polygons:
        for li in p.loop_indices:adj[m.loops[li].edge_index].append(p)
    for e in m.edges:
        fs=adj[e.index]
        # Manual unwrap methods preserve authored seams. A sharp joint alone
        # is not permission to cut a continuous character surface.
        e.use_seam=e.use_seam or len(fs)!=2 or (automatic and fs[0].normal.angle(fs[1].normal)>math.radians(55))
    for i in c['must_cut']:m.edges[i].use_seam=True
    for i in c['must_not_cut']:m.edges[i].use_seam=False

def _density(objects,layer,unit,width,height):
    for o in objects:
        m=o.data;m.calc_loop_triangles();uv=coordinates(m,layer);ids,_=islands(o,layer);groups={}
        positions=world_positions(o,unit)
        for t in m.loop_triangles:
            g=groups.setdefault(ids[t.polygon_index],{'loops':set(),'world':0.,'pixel':0.})
            g['loops'].update(t.loops)
            a,b,c=positions[list(t.vertices)]
            g['world']+=float(np.linalg.norm(np.cross(b-a,c-a)))/2
            p=uv[list(t.loops)].astype(float)*[width,height];x,y=p[1]-p[0],p[2]-p[0]
            g['pixel']+=abs(x[0]*y[1]-x[1]*y[0])/2
        for g in groups.values():
            if g['pixel']<=0 or g['world']<=0:continue
            ix=sorted(g['loops']);center=np.mean(uv[ix],axis=0);scale=math.sqrt(g['world']/g['pixel'])*256
            uv[ix]=center+(uv[ix]-center)*scale
        _write_uv(m,layer,uv)

def _solve(objects,cs,method,v,unit,solver_details=None,progress=None):
    layer='UV_Work'
    for o,c in zip(objects,cs):
        m=o.data;old=coordinates(m,c['source_uv']) if c['source_uv'] else np.zeros((len(m.loops),2),dtype=np.float32)
        while m.uv_layers:m.uv_layers.remove(m.uv_layers[0])
        m.uv_layers.new(name=layer);_write_uv(m,layer,old)
        for i in c['fixed']:m.uv_layers[layer].data[int(i)].pin_uv=True
        _seam_plan(o,c,automatic=method in ('SMART','SMART_ABF','SMART_REPAIR'))
    if method in ('SMART','SMART_REPAIR') and any(c['must_cut'] or c['must_not_cut'] or c['fixed'] for c in cs):
        fail("Smart projection cannot promise seam or fixed-coordinate constraints", "UNSUPPORTED")
    if method=='SMART_ABF' and any(c['fixed'] for c in cs):fail('Smart seam planning with fixed coordinates is not yet supported', 'UNSUPPORTED')
    _edit(objects)
    try:
        if method in ('SMART','SMART_ABF','SMART_REPAIR'):outcome=bpy.ops.uv.smart_project(angle_limit=math.radians(66),island_margin=0.,correct_aspect=False)
        elif method=='MINIMUM_STRETCH':outcome=bpy.ops.uv.unwrap(method='MINIMUM_STRETCH',iterations=v.get('slim_iterations',10),no_flip=v.get('slim_no_flip',True),correct_aspect=False,margin=0.)
        else:outcome=bpy.ops.uv.unwrap(method=method,correct_aspect=False,margin=0.)
        if outcome!={'FINISHED'}:fail('Native unwrap did not finish: '+str(outcome),'VALIDATION_FAILED')
    finally:bpy.ops.object.mode_set(mode='OBJECT')
    if method=='SMART_ABF':
        for o,c in zip(objects,cs):
            _,cuts=islands(o,layer)
            for e in o.data.edges:e.use_seam=tuple(sorted(e.vertices)) in cuts
            for i in c['must_cut']:o.data.edges[i].use_seam=True
            for i in c['must_not_cut']:o.data.edges[i].use_seam=False
        _edit(objects)
        try:
            outcome=bpy.ops.uv.unwrap(method='ANGLE_BASED',correct_aspect=False,margin=0.)
            if outcome!={'FINISHED'}:fail('Native ABF refinement did not finish', 'VALIDATION_FAILED')
        finally:bpy.ops.object.mode_set(mode='OBJECT')
    if method=='SMART_REPAIR':
        from .auto_repair import repair
        for o in objects:
            detail=repair(o,layer,progress=progress,pair_budget=v['pair_budget'])
            if solver_details is not None:solver_details[o.name]=detail
    fixed=any(c['fixed'] for c in cs)
    if not fixed:_density(objects,layer,unit,v['width'],v['height'])
    # Closest tile is deterministic after centering all unlocked islands in tile 1001.
    if not fixed:
        alluv=np.concatenate([coordinates(o.data,layer) for o in objects])
        low=alluv.min(axis=0);span=float(np.max(alluv.max(axis=0)-low))
        if not math.isfinite(span) or span<=0:fail("Degenerate solver output", "VALIDATION_FAILED")
        for o in objects:
            uv=coordinates(o.data,layer);_write_uv(o.data,layer,(uv-low)*(.8/span)+.1)
    _edit(objects)
    try:
        outcome=bpy.ops.uv.pack_islands(udim_source='CLOSEST_UDIM',rotate=True,scale=not fixed,
            margin_method='FRACTION',margin=native_pack_margin(v['margin_px'],min(v['width'],v['height']),fixed),shape_method='CONCAVE',
            pin=fixed,pin_method='LOCKED')
        if outcome!={'FINISHED'}:fail('Native packing did not finish: '+str(outcome),'VALIDATION_FAILED')
    finally:bpy.ops.object.mode_set(mode='OBJECT')
    if not fixed:
        alluv=np.concatenate([coordinates(o.data,layer) for o in objects]);lo=alluv.min(axis=0);hi=alluv.max(axis=0)
        border=np.array([(v['border_px']+.25)/v['width'],(v['border_px']+.25)/v['height']]);scale=float(np.min((1-2*border)/(hi-lo)))
        for o in objects:
            uv=coordinates(o.data,layer);_write_uv(o.data,layer,(uv-lo)*scale+border)
        if 'density_px_m' in v:
            rows=measurement_input(objects,layer,unit);world_area=pixel_area=0.
            for row in rows:
                p=row['positions'][row['triangles']];world_area+=float(np.sum(np.linalg.norm(np.cross(p[:,1]-p[:,0],p[:,2]-p[:,0]),axis=1)))/2
                t=row['uv']*np.array([v['width'],v['height']]);a=t[:,1]-t[:,0];b=t[:,2]-t[:,0]
                pixel_area+=float(np.sum(np.abs(a[:,0]*b[:,1]-a[:,1]*b[:,0])))/2
            factor=v['density_px_m']/math.sqrt(pixel_area/world_area)
            for o in objects:
                uv=coordinates(o.data,layer);_write_uv(o.data,layer,(uv-.5)*factor+.5)
    for o,c in zip(objects,cs):
        uv=coordinates(o.data,layer)
        for i,value in c['fixed'].items():
            expected=np.asarray(value,dtype=np.float32);actual=uv[int(i)]
            ulp=np.maximum(np.abs(np.nextafter(expected,np.float32(np.inf))-expected),np.abs(np.nextafter(expected,np.float32(-np.inf))-expected))
            if np.any(np.abs(actual-expected)>ulp):fail('Native solver moved a fixed UV beyond one float32 ULP: '+o.name+'/'+i,'VALIDATION_FAILED')
            if np.any(actual!=expected):c.setdefault('roundoff_restored',[]).append({'loop':int(i),'before':actual.tolist(),'exact':value})
            uv[int(i)]=expected
        _write_uv(o.data,layer,uv)
    return layer

def _quality(objects,layer,v,cs,unit,progress,recipe=None,settings=None):
    from .metrics import measure
    from . import policies
    from .contract import WorkflowError
    usage=None;targets={};directions=[];settings_error=None
    try:usage,targets,directions=policies.resolve(objects,layer,v,unit,settings)
    except WorkflowError as exc:settings_error=str(exc)
    kw={'usage':usage} if usage is not None else {}
    q=measure(measurement_input(objects,layer,unit),v['width'],v['height'],v['pair_budget'],cancel=lambda:progress('checking'),**kw)
    violations=[];seams={}
    for o,c in zip(objects,cs):
        _,cuts=islands(o,layer);m=o.data;uv=coordinates(m,layer)
        seams[o.name]=[e.index for e in m.edges if tuple(sorted(e.vertices)) in cuts]
        for i in c['must_cut']:
            if tuple(sorted(m.edges[i].vertices)) not in cuts:violations.append([o.name,'must_cut',i])
        for i in c['must_not_cut']:
            if tuple(sorted(m.edges[i].vertices)) in cuts:violations.append([o.name,'must_not_cut',i])
        for i,val in c['fixed'].items():
            if uv[int(i)].tolist()!=val:violations.append([o.name,'fixed',int(i)])
        expected_pins=set(map(int,c['fixed']))
        if set(_pins(m,layer))!=expected_pins:violations.append([o.name,'pin_flags_changed',None])
    q['constraints']={'status':'fail' if violations else 'pass','violations':violations[:64],'count':len(violations)}
    if violations:q['status']='fail'
    if settings_error:
        q['constraints']['violations'].append(['settings_binding',settings_error]);q['constraints']['count']+=1;q['constraints']['status']='fail';q['status']='fail'
    else:policies.evaluate(q,objects,layer,v,settings,targets,directions)
    q['hard_checks_status']=q['status']
    q['measurement_surface']='base_mesh';q['evaluated_surface']='not_checked'
    policy=recipe['policy'] if recipe else {}
    density,density_complete=policies.density_value(q,policy.get('density_basis'))
    if not recipe and 'density_px_m' in v and 'configured' not in q['density']:
        ds=[r['px_per_m'] for r in q['density']['islands']]
        density=max((abs(d/v['density_px_m']-1) for d in ds),default=None) if all(d is not None for d in ds) else None
    border=policy.get('border_px_min',v['border_px']);gap=policy.get('gap_px_min',v['margin_px'])
    density_limit=policy.get('density_relative_max',.05)
    if 'density_px_m' in v:
        ds=[r['px_per_m'] for r in q['density']['islands']]
        absolute=max((abs(d/v['density_px_m']-1) for d in ds),default=None) if all(d is not None for d in ds) else None
        q['manifest_density_check']={'target_px_m':v['density_px_m'],'relative_max_deviation':absolute,
            'relative_limit':density_limit,'status':'unknown' if absolute is None else 'fail' if absolute>density_limit else 'pass',
            'semantics':'original candidate target remains binding; copy to a new candidate to replace it explicitly'}
        if q['manifest_density_check']['status']=='fail':q['status']='fail'
        elif absolute is None and q['status']!='fail':q['status']='unknown'
    if settings and 'configured' in q['density']:density_limit=min(density_limit,settings['density']['relative_tolerance'])
    q['recipe_checks']={'boundary_px':border,'gap_px':gap,'density_relative_limit':density_limit,
                        'density_deviation':density,'pixel_roundoff_tolerance':.001,'violations':[],
                        'basis':'explicit_purpose_policy' if recipe else 'legacy_candidate_defaults',
                        'density_basis':policy.get('density_basis') or ('configured_island_targets' if 'configured' in q['density'] else 'manifest_target' if 'density_px_m' in v else 'island_area_reference')}
    for name,actual,limit in [('boundary',q['boundary_margin_px'],border),('gap',q['gap']['min_px'],gap)]:
        if settings and settings['mode']=='TILED':continue
        if actual is not None and actual+.001<limit:q['recipe_checks']['violations'].append(name)
    if density is not None and density>density_limit:q['recipe_checks']['violations'].append('density')
    if not density_complete and q['status']!='fail':q['status']='unknown'
    if q['recipe_checks']['violations']:q['status']='fail'
    q['visual']='not_reviewed';q['purpose_stretch_threshold']='not_assigned'
    q['seams']=seams
    from .recipes import evaluate,binding
    evaluate(q,recipe,binding(objects,v,unit) if recipe else None,settings)
    return q

def _comparable_integrity_snapshot(snapshot,allow_sharp=False):
    if not allow_sharp:return snapshot
    out=copy.deepcopy(snapshot)
    for row in out.get('objects',{}).values():
        mesh_row=row.get('mesh') if isinstance(row,dict) else None
        if isinstance(mesh_row,dict) and isinstance(mesh_row.get('attributes'),list):
            mesh_row['attributes']=[a for a in mesh_row['attributes'] if a.get('name')!='sharp_edge']
    return out

def execute(manifest,progress=None):
    v=normalize(manifest);progress=progress or (lambda *a,**k:None);scene,objects=_resolve(v);s=state(scene)
    if v['operation']=='inspect':return _execute(v,progress,scene,objects,s,manifest)
    from .integrity import local_supported, IntegrityError
    try:local_supported(objects)
    except IntegrityError as exc:fail(exc.message,exc.code)
    allow_sharp=(v['operation']=='seams' and v['seam_action'].get('mode')=='MARK_BY_ANGLE' and bool(v['seam_action'].get('sync_sharp')))
    before=_comparable_integrity_snapshot(integrity_snapshot(objects),allow_sharp)
    try:return _execute(v,progress,scene,objects,s,manifest)
    finally:
        if _comparable_integrity_snapshot(integrity_snapshot(objects),allow_sharp)!=before:
            fail('Editable shape, weights, animation, rig or non-UV data changed','VALIDATION_FAILED')

def _execute(v,progress,scene,objects,s,supplied):
    if v['operation']=='inspect':
        from .consumers import inspect_objects
        from .transfer import identities
        result={'status':'inspected','unit_scale':scene.unit_settings.scale_length,'objects':[{ 'name':o.name,'mesh':o.data.name,'users':o.data.users,
            'vertices':len(o.data.vertices),'faces':len(o.data.polygons),'layers':[u.name for u in o.data.uv_layers],
            'active_render':[u.name for u in o.data.uv_layers if u.active_render]} for o in objects], 'state':s,'consumers':inspect_objects(objects),
            'transfer_identity':identities(objects) if not ('surface' in v or 'diagnostics' in v) else {'status':'not_requested_for_diagnostics'}}
        if 'diagnostics' in v:
            from .geometry_diagnostics import inspect as inspect_geometry
            result['diagnostics']=inspect_geometry(objects,scene.unit_settings.scale_length,v['diagnostics']['max_examples'],progress)
        if 'surface' in v:
            from .surface import inspect as inspect_surface
            result['surface']=inspect_surface(scene,objects,v,progress)
        if 'seam_suggestions' in v:
            from .seams_helper import suggest_seams
            spec=v['seam_suggestions']
            result['seam_suggestions']=suggest_seams(objects,angle_degrees=spec['angle_degrees'],include_boundaries=spec['include_boundaries'],max_examples=spec['max_examples'])
        return result
    cid=v.get('candidate');unit=scene.unit_settings.scale_length
    if v['operation']=='lightmap':
        from .lightmap import generate_lightmap
        return generate_lightmap(scene,objects,s,v,unit,progress)
    if v['operation']=='seams' and v['seam_action']['mode'] in ('MARK_BY_ANGLE','MARK_FROM_SHARP') and not cid:
        for o in objects:
            if o.data.users!=1:fail("Shared mesh needs an explicit isolated working copy: "+o.name,"UNSUPPORTED")
        from .seams_helper import apply_base_mesh_seams
        return apply_base_mesh_seams(objects,s,v)
    if v['operation']=='copy_uv':
        from .management import copy_uv
        return copy_uv(scene,objects,s,v,unit,progress)
    if v['operation']=='transfer':
        from .transfer import execute as transfer_candidate
        return transfer_candidate(scene,objects,s,v,unit,progress)
    if v['operation']=='generate':
        for o in objects:
            if o.data.users!=1:fail("Shared mesh needs an explicit isolated working copy: "+o.name,"UNSUPPORTED")
            if len(o.data.uv_layers)+len(v['methods'])>8:fail("Not enough UV layer slots", "RESOURCE_LIMIT")
        _check_geometry(objects,unit);before=protection(objects);cs=[_constraints(o,v) for o in objects]
        results=[];created=[]
        try:
            for method in v['methods']:
                key=cid+'_'+method.lower();layer='UVW_'+key
                if key in s['candidates'] or any(layer in o.data.uv_layers for o in objects):fail("Candidate already exists", "CONFLICT")
                progress('solve',method=method)
                solver_details={}
                with scratch(objects) as (_,clones):
                    try:temp=_solve(clones,cs,method,v,unit,solver_details=solver_details,progress=progress)
                    except Exception as e:
                        from .contract import WorkflowError
                        if not isinstance(e,WorkflowError):raise
                        if e.code=='CANCELLED':raise
                        results.append({'candidate':key,'status':'rejected','reason':str(e)});continue
                    # Restore semantic object names for measurement and constraint reports.
                    q=_quality(clones,temp,v,cs,unit,progress)
                    names={clone.name:o.name for o,clone in zip(objects,clones)}
                    solver_details={names.get(n,n):d for n,d in solver_details.items()}
                    for detail in solver_details.values():
                        detail['repair_stage_status']=detail.pop('status')
                        detail.update(status='intrinsic_repair_complete',pack_completed=True,
                                      global_layout_status=q['status'])
                    q['seams']={o.name:q['seams'][clone.name] for o,clone in zip(objects,clones)}
                    for row in q['density']['islands']:row['object']=names[row['object']]
                    for pair in q['overlap']['examples']:
                        for side in ('first','second'):pair[side]['object']=names[pair[side]['object']]
                    arrays=[coordinates(o.data,temp) for o in clones]
                for o,uv,c in zip(objects,arrays,cs):
                    u=o.data.uv_layers.new(name=layer,do_init=False)
                    if u.name!=layer:fail("UV name collision", "CONFLICT")
                    created.append((o,layer));_write_uv(o.data,layer,uv)
                    for i in c['fixed']:o.data.uv_layers[layer].data[int(i)].pin_uv=True
                record={'objects':v['objects'],'layer':layer,'method':method,'metrics':q,'geometry':{o.name:geometry(o) for o in objects},
                    'uv_hash':{o.name:_uv_fingerprint(o.data,layer) for o in objects},'meshes':{o.name:o.data.name for o in objects},
                    'constraints':cs,'manifest':v,'status':'needs_review' if q['status']=='pass' else q['status'],
                    'previous_active':{o.name:next((u for u in before[o.name]['layers'] if u==o.data.uv_layers.active.name),None) for o in objects}}
                if solver_details:record['solver_details']=solver_details
                s['candidates'][key]=record;results.append({'candidate':key,**record})
            excluded=[l for _,l in created]
            if protection(objects,excluded)!=before:fail("Original data protection mismatch", "VALIDATION_FAILED")
            _store(scene,s)
            return {'status':'candidates_created','candidates':results,'protection':'pass','quality_acceptance':'requires_visual_and_purpose_review'}
        except Exception:
            for o,l in reversed(created):
                if l in o.data.uv_layers:o.data.uv_layers.remove(o.data.uv_layers[l])
            raise
    rec=s['candidates'].get(cid)
    if not rec or rec['objects']!=v['objects']:fail("Missing candidate or target identity changed", "CONFLICT")
    if any(geometry(o)!=rec['geometry'][o.name] or rec['layer'] not in o.data.uv_layers for o in objects):fail("Geometry or candidate layer changed", "CONFLICT")
    if any(o.data.users!=1 or o.data.name!=rec.get('meshes',{}).get(o.name) for o in objects):fail('Candidate mesh identity or sharing changed', 'CONFLICT')
    if 'constraints' in v and v['constraints']!=rec['manifest']['constraints']:
        fail('Explicit constraints differ from candidate generation constraints; use managed pin/lock/sync_pins edits or generate a new candidate', 'CONFLICT')
    layer=rec['layer']
    if v['operation'] in ('edit','adopt'):
        from .consumers import inspect_objects
        consumers=inspect_objects(objects)
        if any(consumers[o.name].get('unresolved_udim') or layer in consumers[o.name]['protected_udim_layers'] for o in objects):
            fail('Candidate is now used by an existing UDIM consumer; preserve it and select a separate ordinary UV layer','UNSUPPORTED')
    if v['operation'] in ('settings','seams'):
        from .management import update
        return update(scene,objects,rec,s,v,unit,progress)
    if v['operation']=='recipe':
        from .recipes import binding,require_binding,usage_binding
        require_binding(v['recipe'],binding(objects,rec['manifest'],unit))
        usage=usage_binding(rec.get('settings'),v['recipe']['policy']['density_basis'])
        if 'usage_binding' in v['recipe'] and v['recipe']['usage_binding']!=usage:fail('Recipe usage, reuse or configured targets changed; review and explicitly rebind its policy','CONFLICT')
        v['recipe']['usage_binding']=usage
        rec['recipe']=v['recipe']
        rec['metrics']=_quality(objects,layer,rec['manifest'],rec['constraints'],unit,progress,rec['recipe'],rec.get('settings'))
        rec['uv_hash']={o.name:_uv_fingerprint(o.data,layer) for o in objects}
        rec['status']='needs_review' if rec['metrics']['status']=='pass' else rec['metrics']['status']
        rec['revision']=int(rec.get('revision',0))+1
        _store(scene,s)
        return {'status':'recipe_bound','candidate':cid,'record':rec,'quality_acceptance':'numeric_rules_only_visual_not_reviewed'}
    if v['operation']=='edit':
        from .editing import edit_candidate
        return edit_candidate(scene,objects,rec,s,v,unit,progress)
    if v['operation']=='validate':
        check_manifest=dict(rec['manifest'])
        if 'pair_budget' in supplied:check_manifest['pair_budget']=v['pair_budget']
        rec['metrics']=_quality(objects,layer,check_manifest,rec['constraints'],unit,progress,rec.get('recipe'),rec.get('settings'))
        rec['uv_hash']={o.name:_uv_fingerprint(o.data,layer) for o in objects}
        if s['adopted'] and s['adopted']['candidate']==cid:
            # Explicit revalidation captures a user's seam edits as this layer's
            # plan before restore removes their temporary mesh-level display.
            rec['seam_plan']={o.name:[e.index for e in o.data.edges if e.use_seam] for o in objects}
            s['adopted']['applied_seams']={n:_hash(edges) for n,edges in rec['seam_plan'].items()}
        rec['status']='needs_review' if rec['metrics']['status']=='pass' else rec['metrics']['status'];_store(scene,s)
        return {'status':'validated','candidate':cid,'record':rec}
    if v['operation']=='adopt':
        if rec.get('settings'):
            from .policies import resolve as resolve_settings
            resolve_settings(objects,layer,rec['manifest'],unit,rec['settings'])
        if rec.get('recipe'):
            from .recipes import binding,require_binding
            require_binding(rec['recipe'],binding(objects,rec['manifest'],unit))
        if rec['metrics']['status']!='pass':fail("Candidate has failed or incomplete hard checks", "VALIDATION_FAILED")
        if any(_uv_fingerprint(o.data,layer)!=rec['uv_hash'][o.name] for o in objects):fail("Native edits require revalidation", "CONFLICT")
        if s['adopted']:fail("Restore the active candidate before adopting another", "CONFLICT")
        previous={o.name:{'active':o.data.uv_layers.active.name,'seams':[e.index for e in o.data.edges if e.use_seam]} for o in objects}
        for o in objects:
            o.data.uv_layers.active=o.data.uv_layers[layer];cuts=set(rec.get('seam_plan',rec['metrics']['seams'])[o.name])
            for e in o.data.edges:e.use_seam=e.index in cuts
        s['adopted']={'candidate':cid,'previous':previous,'applied_seams':{o.name:_hash([e.index for e in o.data.edges if e.use_seam]) for o in objects}};_store(scene,s)
        return {'status':'adopted_for_editing','candidate':cid,'render_bindings':'retained','quality_acceptance':'not_automatically_approved'}
    if not s['adopted'] or s['adopted']['candidate']!=cid:fail("Candidate is not currently adopted", "CONFLICT")
    if any(_hash([e.index for e in o.data.edges if e.use_seam])!=s['adopted'].get('applied_seams',{}).get(o.name) for o in objects):fail('Native seam edits detected; retain or capture them before restore', 'CONFLICT')
    for o in objects:
        prev=s['adopted']['previous'][o.name]
        if prev['active'] not in o.data.uv_layers:fail("Previous layer missing", "CONFLICT")
    for o in objects:
        prev=s['adopted']['previous'][o.name];o.data.uv_layers.active=o.data.uv_layers[prev['active']]
        for e in o.data.edges:e.use_seam=e.index in prev['seams']
    s['adopted']=None;_store(scene,s)
    return {'status':'restored','candidate_edits':'retained'}
