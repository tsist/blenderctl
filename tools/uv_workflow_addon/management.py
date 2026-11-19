"""Explicit candidate imports and managed seam/settings transactions."""
import copy
from .contract import fail


def copy_uv(scene,objects,s,v,unit,progress):
    from . import core
    from .consumers import inspect_objects
    inspection=inspect_objects(objects);layer='UVW_'+v['candidate'];created=[];old_state=scene.get(core.STATE)
    if v['candidate'] in s['candidates']:fail('Candidate already exists','CONFLICT')
    for o in objects:
        if o.data.users!=1:fail('Shared mesh requires an isolated working copy','UNSUPPORTED')
        source=v['source_layers'][o.name]
        if source not in o.data.uv_layers:fail('Missing source UV: '+o.name,'NOT_FOUND')
        if inspection[o.name].get('unresolved_udim') or source in inspection[o.name]['protected_udim_layers']:
            fail('Existing UDIM layer is protected; choose a different layer: '+o.name+'/'+source,'UNSUPPORTED')
        if layer in o.data.uv_layers or len(o.data.uv_layers)>=8:fail('Candidate layer collision or layer limit','CONFLICT')
    before=core.protection(objects)
    previous_active={o.name:o.data.uv_layers.active.name if o.data.uv_layers.active else None for o in objects}
    manifest=copy.deepcopy(v)
    for o in objects:
        c=manifest['constraints'].setdefault(o.name,{})
        if c.get('source_uv') and c['source_uv']!=v['source_layers'][o.name]:fail('Constraint source conflicts with imported layer')
        c['source_uv']=v['source_layers'][o.name]
    # Normalize generated constraint fields so future explicit requests compare
    # the same representation. This is an import, not a solver invocation.
    from .contract import normalize
    manifest=normalize(manifest);cs=[core._constraints(o,manifest) for o in objects]
    try:
        for o in objects:
            source=v['source_layers'][o.name];uv=core.coordinates(o.data,source)
            o.data.uv_layers.new(name=layer,do_init=False);created.append(o)
            core._write_uv(o.data,layer,uv)
            from .editing import _pin_write
            c=cs[objects.index(o)];_pin_write(o.data,layer,sorted(map(int,c['fixed'])))
            if previous_active[o.name]:o.data.uv_layers.active=o.data.uv_layers[previous_active[o.name]]
        q=core._quality(objects,layer,manifest,cs,unit,progress)
        record={'objects':v['objects'],'layer':layer,'method':'COPY_EXISTING','metrics':q,
            'geometry':{o.name:core.geometry(o) for o in objects},'uv_hash':{o.name:core._uv_fingerprint(o.data,layer) for o in objects},
            'meshes':{o.name:o.data.name for o in objects},'constraints':cs,'manifest':manifest,
            'source_layers':v['source_layers'],'source_consumers':inspection,
            'status':'needs_review' if q['status']=='pass' else q['status'],
            'previous_active':previous_active,
            'seam_plan':{o.name:[e.index for e in o.data.edges if e.use_seam] for o in objects}}
        if core.protection(objects,excluded=[layer])!=before:fail('Copy changed protected original UV data','VALIDATION_FAILED')
        s['candidates'][v['candidate']]=record;core._store(scene,s)
        return {'status':'candidate_copied','candidate':v['candidate'],'record':record,'quality_acceptance':'requires_visual_and_purpose_review'}
    except Exception:
        for o in created:
            if layer in o.data.uv_layers:o.data.uv_layers.remove(o.data.uv_layers[layer])
        if old_state is None:
            if core.STATE in scene:del scene[core.STATE]
        else:scene[core.STATE]=old_state
        raise


def update(scene,objects,record,s,v,unit,progress):
    from . import core
    if any(core._uv_fingerprint(o.data,record['layer'])!=record['uv_hash'][o.name] for o in objects):
        fail('Native edits require revalidation before updating managed rules','CONFLICT')
    previous=scene.get(core.STATE);edited=copy.deepcopy(record);layer=record['layer'];adopted=s.get('adopted')
    old_seams={o.name:[e.index for e in o.data.edges if e.use_seam] for o in objects}
    old_sharp={o.name:[e.index for e in o.data.edges if e.use_edge_sharp] for o in objects}
    if adopted and adopted['candidate']==v['candidate'] and any(core._hash(old_seams[o.name])!=adopted['applied_seams'].get(o.name) for o in objects):
        fail('Native seam changes require revalidation first','CONFLICT')
    if v['operation']=='settings':
        from .policies import resolve
        resolve(objects,layer,record['manifest'],unit,v['settings'])
        edited['settings']=v['settings']
    else:
        operation=v['seam_action'];mode=operation['mode'];cs={o.name:c for o,c in zip(objects,edited['constraints'])}
        plan=copy.deepcopy(record.get('seam_plan',record['metrics']['seams']))
        if mode in ('MARK_BY_ANGLE','MARK_FROM_SHARP'):
            from .seams_helper import resolve_seam_indices
            clear_existing=bool(operation.get('clear_existing',False))
            sync_sharp=bool(operation.get('sync_sharp',False)) and mode=='MARK_BY_ANGLE'
            for o in objects:
                ids,sharp_ids=resolve_seam_indices(o,operation)
                plan[o.name]=sorted(ids) if clear_existing else sorted(set(plan.get(o.name,[]))|ids)
                if sync_sharp:
                    for e in o.data.edges:
                        if e.index in sharp_ids:e.use_edge_sharp=True
        else:
            for o in objects:
                ids=set(operation['edges'].get(o.name,[]))
                if any(i>=len(o.data.edges) for i in ids):fail('Seam edge out of range: '+o.name)
                if mode in ('MARK','CLEAR'):
                    plan[o.name]=sorted(set(plan.get(o.name,[]))|ids if mode=='MARK' else set(plan.get(o.name,[]))-ids)
                elif mode=='FREE':
                    for key in ('must_cut','must_not_cut'):cs[o.name][key]=sorted(set(cs[o.name][key])-ids)
                else:
                    key='must_cut' if mode=='REQUIRE_CUT' else 'must_not_cut';opposite='must_not_cut' if key=='must_cut' else 'must_cut'
                    cs[o.name][key]=sorted(set(cs[o.name][key])|ids);cs[o.name][opposite]=sorted(set(cs[o.name][opposite])-ids)
                    plan[o.name]=sorted(set(plan.get(o.name,[]))|ids if mode=='REQUIRE_CUT' else set(plan.get(o.name,[]))-ids)
        edited['seam_plan']=plan
    try:
        q=core._quality(objects,layer,record['manifest'],edited['constraints'],unit,progress,edited.get('recipe'),edited.get('settings'))
        # Explicit rule changes can expose a pending repair. No UV is modified;
        # such candidates remain fail/unknown and cannot be adopted as passing.
        edited['metrics']=q;edited['status']='needs_review' if q['status']=='pass' else q['status']
        edited['revision']=int(record.get('revision',0))+1
        edited['last_management']={'operation':v['operation'],'change':copy.deepcopy(v.get('settings',v.get('seam_action'))),'uv':'unchanged'}
        if v['operation']=='seams' and adopted and adopted['candidate']==v['candidate']:
            for o in objects:
                edges=set(edited['seam_plan'][o.name])
                for e in o.data.edges:e.use_seam=e.index in edges
            s['adopted']['applied_seams']={n:core._hash(edges) for n,edges in edited['seam_plan'].items()}
        s['candidates'][v['candidate']]=edited;core._store(scene,s)
        return {'status':'rules_updated','candidate':v['candidate'],'record':edited,'quality_acceptance':'rules_changed_not_quality_approval'}
    except Exception:
        for o in objects:
            edges=set(old_seams[o.name]);sharps=set(old_sharp[o.name])
            for e in o.data.edges:
                e.use_seam=e.index in edges
                e.use_edge_sharp=e.index in sharps
        scene[core.STATE]=previous
        raise
