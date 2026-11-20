"""Read-only timeline/legacy table observer. No operators, register, frame_set, or save."""

def need(v,s):
    if not v:raise ValueError(s)

def inspect(scene,frame):
    need(type(frame) is int and scene.frame_start<=frame<=scene.frame_end,"probe frame outside scene")
    lo,hi=scene.frame_start,scene.frame_end
    def cam(o):
        need(o is not None and getattr(o,"type",None)=="CAMERA" and scene.objects.get(o.name)==o and not o.library and not o.override_library,"invalid or nonlocal scene camera")
        return o.name
    need(len(scene.timeline_markers)<=4096,"marker budget exceeded")
    shots=[];notes=[];ms=[]
    for m in scene.timeline_markers:
        if m.camera:ms.append((m.frame,m.name,cam(m.camera)))
        else:notes.append([m.frame,m.name])
    ms.sort()
    need(len(set(f for f,n,c in ms))==len(ms),"ambiguous simultaneous native camera markers")
    # Blender extrapolates the first bound camera backwards and the last forwards.
    for i,(f,n,c) in enumerate(ms):
        a=lo if i==0 else max(lo,f)
        z=min(hi,ms[i+1][0]-1) if i+1<len(ms) else hi
        if a<=z:shots.append([n,c,a,z])
    if not ms and scene.camera:shots.append([None,cam(scene.camera),lo,hi])
    live=[];storage='not-observable-without-schema'
    if hasattr(scene,'camera_live_shots'):
        r=scene.camera_live_shots;storage='registered-rna'
        need(len(r)<=100,'Live table budget')
        for x in r:live.append([x.name,cam(x.camera),x.first,x.last])
    elif 'camera_live_shots' in scene.keys():
        r=scene['camera_live_shots'];storage='stored-idproperties'
        need(len(r)<=100,'Live table budget')
        for x in r:
            need(hasattr(x,'keys') and set(x.keys())=={'name','camera','first','last'},'raw rows require explicit complete legacy fields; no guessing hidden defaults')
            live.append([x['name'],cam(x['camera']),x['first'],x['last']])
    names=set()
    for i,(n,c,a,z) in enumerate(live):
        need(type(n) is str and n==n.strip() and n and n not in names,"invalid/duplicate Live shot name")
        need(type(a) is int and type(z) is int and -1048574<=a<=z<=1048574 and (not i or a==live[i-1][3]+1),"invalid or noncontiguous Live shot range")
        names.add(n)
    def at(rows):return next((r for r in rows if r[2]<=frame<=r[3]),None)
    native=at(shots); legacy=at(live)
    return dict(schema='shot-table-inspection/1',scene=scene.name,range=[lo,hi],frame=frame,active_pointer=scene.camera.name if scene.camera else None,native_mode='timeline-markers' if ms else 'scene-camera',native_markers=[list(row) for row in ms],native_rows=shots,notes=sorted(notes),legacy_storage=storage,legacy_rows=live,native_at_frame=native,legacy_at_frame=legacy,cameras_agree_at_frame=native[1]==legacy[1] if native and legacy else None,legacy_viewport_rejects_bound_markers=bool(ms))
