# SPDX-License-Identifier: GPL-3.0-or-later
"""Full evaluated USD surfaces sampled in Blender's actual native frame domain."""
import math,time,heapq,shutil,re
from collections import OrderedDict
from contextlib import ExitStack
import bpy,numpy as np
from pxr import Usd,UsdGeom,Vt,Sdf
import mesh_clip,exchange,nodes
from protocol import Failure,atomic_json,digest

ADAPTER='EVALUATED_MESH_CACHE_V1'
UV_TOLERANCE=1e-6


def source_topology(reference,observed):
    """Exact indexed topology, finite UV coordinates with a separate numeric budget."""
    if len(reference)!=len(observed):return False,0.
    error=0.
    for a,b in zip(reference,observed):
        if len(a['vertices'])!=len(b['vertices']) or a['faces']!=b['faces'] or a['loose_edges']!=b['loose_edges'] or set(a['uv'])!=set(b['uv']):return False,error
        for name in a['uv']:
            x,y=a['uv'][name],b['uv'][name]
            if x.shape!=y.shape or not np.isfinite(x).all() or not np.isfinite(y).all():return False,error
            error=max(error,float(np.abs(x.astype(np.float64)-y.astype(np.float64)).max()) if x.size else 0.)
    return error<=UV_TOLERANCE,error


def topology_differences(reference,observed):
    differences=[]
    for a,b in zip(reference,observed):
        if mesh_clip.topology_match(a,b):continue
        uv={}
        for name in sorted(set(a['uv']) & set(b['uv'])):
            x,y=a['uv'][name],b['uv'][name];same=x.shape==y.shape
            finite=bool(np.isfinite(x).all() and np.isfinite(y).all())
            uv[name]={'shapes':[list(x.shape),list(y.shape)],'equal':bool(same and np.array_equal(x,y)),'finite':finite,'max_error':float(np.abs(x-y).max()) if same and finite and x.size else None}
        differences.append({'object':a['name'],'vertices':[len(a['vertices']),len(b['vertices'])],'faces_equal':a['faces']==b['faces'],'loose_edges_equal':a['loose_edges']==b['loose_edges'],'uv_names':[list(a['uv']),list(b['uv'])],'uv':uv})
    return differences


class Samples:
    def __init__(self,params,spec,job):
        self.params=params;self.spec=spec;self.job=job;self.rows=[];self.times={};self.requests={};self.memory=OrderedDict();self.first=None;self.count=0;self.uv_max_error=0.

    def progress(self,phase,**extra):
        atomic_json(self.job/'exchange-cache-progress.json',{'phase':phase,'source_sha256':self.params['expected_sha256'],'actual_samples':len(self.rows),'vertex_samples':self.count*len(self.rows),'samples':self.rows,**extra})

    def data(self,actual):
        if actual not in self.memory:self.memory[actual]=np.load(self.times[actual]['file'],allow_pickle=False)
        self.memory.move_to_end(actual)
        while len(self.memory)>16:self.memory.popitem(last=False)
        return self.memory[actual]

    def sample(self,requested):
        if requested in self.requests:
            actual=self.requests[requested];return actual,self.data(actual)
        base=math.floor(requested);bpy.context.scene.frame_set(base,subframe=requested-base)
        actual=float(bpy.context.scene.frame_current_final);self.requests[requested]=actual
        if actual not in self.times:
            sampling=self.spec['sampling']
            if len(self.rows)>=sampling['max_evaluations'] or (self.count and (len(self.rows)+1)*self.count>sampling['max_vertex_samples']):
                self.progress('rejected',reason='sample_budget');raise Failure('UNSUPPORTED','Native mesh cache sampling budget exhausted; retained samples are not a verified export')
            rows=mesh_clip.capture(self.spec['objects'],requested,self.params['driver_profile'])
            if self.first is None:
                self.first=rows;self.count=sum(len(r['vertices']) for r in rows)
                if self.count>sampling['max_vertex_samples']:raise Failure('UNSUPPORTED','Native mesh cache initial vertex budget exceeded')
                if any(r['loose_edges'] for r in rows):raise Failure('UNSUPPORTED','USD mesh cache V1 rejects loose edges; isolated points are supported')
            matched,uv_error=source_topology(self.first,rows);self.uv_max_error=max(self.uv_max_error,uv_error)
            if not matched:
                atomic_json(self.job/'exchange-cache-topology-failure.json',{'phase':'capture','requested_frame':requested,'actual_frame':actual,'differences':topology_differences(self.first,rows)})
                self.progress('rejected',reason='topology_or_uv_changed',frame=requested);raise Failure('UNSUPPORTED','Native mesh cache source topology/UV changed')
            vertices=np.concatenate([r['vertices'] for r in rows]);path=self.job/('cache-sample-%04d.npy'%len(self.rows))
            with open(path,'xb') as f:np.save(f,vertices,allow_pickle=False)
            row={'actual_frame':actual,'first_request':requested,'file':str(path),'sha256':digest(path),'bytes':path.stat().st_size}
            self.rows.append(row);self.times[actual]=row;self.memory[actual]=vertices
            self.progress('capture')
        return actual,self.data(actual)


def adaptive(store):
    spec=store.spec;settings=spec['sampling'];limit=settings['max_position_error']/2
    leaves=[];pending=[];history=[];witnesses={}
    def assess(a,b,depth):
        ta,va=store.sample(a);tb,vb=store.sample(b)
        if ta!=a or tb!=b or not ta<tb:raise Failure('UNSUPPORTED','Cache key endpoints must be distinct exact native frame times')
        checks=[];va=va.astype(np.float64);vb=vb.astype(np.float64)
        for fraction in (.25,.5,.75):
            requested=a+(b-a)*fraction;actual,v=store.sample(requested);alpha=(actual-a)/(b-a)
            error=float(np.linalg.norm(v.astype(np.float64)-(va*(1-alpha)+vb*alpha),axis=1).max())
            if not math.isfinite(error):raise Failure('VALIDATION_FAILED','Non-finite cache interpolation error')
            checks.append({'requested_frame':requested,'actual_frame':actual,'error':error});witnesses[requested]=actual
        row={'start':a,'end':b,'depth':depth,'max_error':max(x['error'] for x in checks),'checks':checks};history.append(row)
        if row['max_error']<=limit:leaves.append(row)
        else:heapq.heappush(pending,(-row['max_error'],a,b,depth))
    start,end=spec['frame_start'],spec['frame_end'];steps=settings['seed_substeps'];n=(end-start)*steps
    try:
        for i in range(n):assess(start+i/steps,start+(i+1)/steps,0)
        while pending:
            error,a,b,depth=heapq.heappop(pending);mid,_=store.sample((a+b)/2)
            if depth>=settings['max_depth'] or not a<mid<b:raise Failure('VALIDATION_FAILED','Actual-time cache cannot satisfy declared interpolation budget within depth/time bounds')
            assess(a,mid,depth+1);assess(mid,b,depth+1)
        keys=sorted({t for r in leaves for t in (r['start'],r['end'])})
        if len(keys)>1024:raise Failure('UNSUPPORTED','Native cache exceeds 1024 output keys')
        return keys,leaves,witnesses
    finally:
        atomic_json(store.job/'exchange-cache-sampling.json',{'time_domain':'BLENDER_EVALUATED_FRAME','position_budget':settings['max_position_error'],'interpolation_budget':limit,'accepted':leaves,'pending':pending,'history':history,'continuous_time_verified':False})


def write_usd(path,store,keys,fps):
    if path.exists():raise Failure('CONFLICT','Cache output already exists')
    stage=Usd.Stage.CreateNew(str(path));stage.SetTimeCodesPerSecond(fps);stage.SetFramesPerSecond(fps)
    stage.SetStartTimeCode(keys[0]);stage.SetEndTimeCode(keys[-1]);UsdGeom.SetStageUpAxis(stage,UsdGeom.Tokens.z);UsdGeom.SetStageMetersPerUnit(stage,1.)
    stage.GetRootLayer().customLayerData={'blenderctl_adapter':ADAPTER,'source_sha256':store.params['expected_sha256']}
    meshes=[];offset=0
    for i,row in enumerate(store.first):
        mesh=UsdGeom.Mesh.Define(stage,'/Clip_%02d'%i);mesh.CreateSubdivisionSchemeAttr().Set(UsdGeom.Tokens.none)
        mesh.CreateFaceVertexCountsAttr().Set([len(face) for face in row['faces']]);mesh.CreateFaceVertexIndicesAttr().Set([v for face in row['faces'] for v in face])
        for j,(name,uv) in enumerate(row['uv'].items()):
            # Deliberately record a bounded layer order; interchange UV naming and
            # shading are not certified by this geometry adapter.
            pv=UsdGeom.PrimvarsAPI(mesh).CreatePrimvar('st' if j==0 else 'st%d'%j,Sdf.ValueTypeNames.TexCoord2fArray,UsdGeom.Tokens.faceVarying)
            pv.Set(Vt.Vec2fArray.FromNumpy(uv))
        meshes.append((mesh,offset,offset+len(row['vertices'])));offset+=len(row['vertices'])
    for frame in keys:
        vertices=store.data(frame)
        for mesh,a,b in meshes:
            mesh.CreatePointsAttr().Set(Vt.Vec3fArray.FromNumpy(vertices[a:b]),Usd.TimeCode(frame))
            mesh.CreateExtentAttr().Set(Vt.Vec3fArray.FromNumpy(np.array([vertices[a:b].min(axis=0),vertices[a:b].max(axis=0)],np.float32)),Usd.TimeCode(frame))
    stage.GetRootLayer().Save();stage=None
    if path.stat().st_size>1024**3:raise Failure('UNSUPPORTED','Native USD cache exceeds 1 GiB')


def capture_check(store,requested,actual,limit):
    names=['Clip_%02d'%i for i in range(len(store.first))]
    rows=mesh_clip.capture(names,requested,None);native=float(bpy.context.scene.frame_current_final)
    if native!=actual:raise Failure('VALIDATION_FAILED','USD import native time domain changed')
    expected=store.data(actual);offset=0;checks=[]
    for ref,row in zip(store.first,rows):
        n=len(ref['vertices']);topology=len(row['vertices'])==n and row['faces']==ref['faces'] and row['loose_edges']==ref['loose_edges']
        error=mesh_clip.maximum(expected[offset:offset+n],row['vertices']) if topology else None;offset+=n
        checks.append({'source':ref['name'],'output':row['name'],'requested_frame':requested,'actual_frame':actual,'indexed_vertex_error':error,'topology_ok':topology,'position_tolerance':limit,'ok':bool(topology and error<=limit),'scope':'USD_NATIVE_FRAME_INDEXED_SURFACE'})
    return checks


def read_cache(path,fps):
    if path.stat().st_size>1024**3:raise Failure('UNSUPPORTED','Native USD cache exceeds 1 GiB')
    layer=Sdf.Layer.FindOrOpen(str(path))
    if not layer or layer.GetExternalReferences() or layer.subLayerPaths:raise Failure('UNSUPPORTED','Native cache must be one self-contained USD layer')
    metadata=dict(layer.customLayerData)
    if set(metadata)!={'blenderctl_adapter','source_sha256'} or metadata['blenderctl_adapter']!=ADAPTER or not re.fullmatch('[0-9a-f]{64}',metadata['source_sha256']):raise Failure('UNSUPPORTED','Native cache requires the explicit Blender CLI layer contract')
    allowed={'points','extent','faceVertexCounts','faceVertexIndices','subdivisionScheme'}
    def inspect(path):
        obj=layer.GetObjectAtPath(path)
        if isinstance(obj,Sdf.PrimSpec):
            if str(path)=='/':return
            if obj.path.GetParentPath()!=Sdf.Path.absoluteRootPath or obj.typeName!='Mesh' or set(obj.ListInfoKeys())-{'specifier','typeName'} or not re.fullmatch('Clip_[0-9]{2}',obj.name):raise Failure('UNSUPPORTED','Native cache allows flat named Mesh prims without references, clips, variants or custom metadata')
        elif isinstance(obj,Sdf.AttributeSpec):
            if obj.name not in allowed and not re.fullmatch('primvars:st[0-7]?',obj.name):raise Failure('UNSUPPORTED','Unexpected USD cache property')
            if obj.typeName in (Sdf.ValueTypeNames.Asset,Sdf.ValueTypeNames.AssetArray):raise Failure('UNSUPPORTED','USD cache asset-valued attributes are forbidden')
            expected={'points':Sdf.ValueTypeNames.Point3fArray,'extent':Sdf.ValueTypeNames.Float3Array,'faceVertexCounts':Sdf.ValueTypeNames.IntArray,'faceVertexIndices':Sdf.ValueTypeNames.IntArray,'subdivisionScheme':Sdf.ValueTypeNames.Token}
            if obj.name in expected and obj.typeName!=expected[obj.name]:raise Failure('UNSUPPORTED','Invalid USD cache attribute type')
        else:raise Failure('UNSUPPORTED','Unexpected USD cache composition spec')
    layer.Traverse('/',inspect)
    stage=Usd.Stage.Open(layer,load=Usd.Stage.LoadNone)
    if stage.GetTimeCodesPerSecond()!=fps or stage.GetFramesPerSecond()!=fps or UsdGeom.GetStageUpAxis(stage)!=UsdGeom.Tokens.z or UsdGeom.GetStageMetersPerUnit(stage)!=1:raise Failure('CONFLICT','Native USD cache fps/units/axis must match its declared source domain')
    prims=sorted(stage.Traverse(),key=lambda p:p.GetPath().pathString)
    if not 1<=len(prims)<=16:raise Failure('UNSUPPORTED','Native cache requires 1..16 meshes')
    meshes=[UsdGeom.Mesh(p) for p in prims];keys=meshes[0].GetPointsAttr().GetTimeSamples()
    if not 2<=len(keys)<=1024 or any(not math.isfinite(t) or not 1<=t<=100000 or float(np.float32(t))!=t for t in keys) or not 0<keys[-1]-keys[0]<=59:raise Failure('UNSUPPORTED','Native USD cache requires bounded exact float32 frame keys')
    if stage.GetStartTimeCode()!=keys[0] or stage.GetEndTimeCode()!=keys[-1]:raise Failure('UNSUPPORTED','USD cache time range must match point samples')
    if [p.GetName() for p in prims]!=['Clip_%02d'%i for i in range(len(prims))]:raise Failure('UNSUPPORTED','Native cache mesh identifiers must be contiguous')
    first=[];total=0
    for m in meshes:
        if m.GetPointsAttr().GetTimeSamples()!=keys or m.GetSubdivisionSchemeAttr().Get()!=UsdGeom.Tokens.none:raise Failure('UNSUPPORTED','Native cache point times must agree and subdivision must be none')
        if m.GetFaceVertexCountsAttr().GetNumTimeSamples() or m.GetFaceVertexIndicesAttr().GetNumTimeSamples():raise Failure('UNSUPPORTED','Animated USD topology is outside the native cache contract')
        points=np.asarray(m.GetPointsAttr().Get(Usd.TimeCode(keys[0])),dtype=np.float32);counts=np.asarray(m.GetFaceVertexCountsAttr().Get());indices=np.asarray(m.GetFaceVertexIndicesAttr().Get())
        if points.ndim!=2 or points.shape[1]!=3 or not len(points) or counts.ndim!=1 or indices.ndim!=1 or not len(counts):raise Failure('UNSUPPORTED','USD mesh cache needs nonempty valid surface arrays')
        total+=len(points)
        if total>200000 or points.ndim!=2 or points.shape[1]!=3 or not np.isfinite(points).all() or len(indices)>2000000 or np.any(counts<3) or int(counts.sum())!=len(indices) or np.any(indices<0) or np.any(indices>=len(points)):raise Failure('UNSUPPORTED','Invalid or excessive USD mesh cache geometry')
        cuts=np.r_[0,np.cumsum(counts)];faces=[indices[cuts[i]:cuts[i+1]].tolist() for i in range(len(counts))]
        first.append({'name':m.GetPrim().GetName(),'vertices':points,'faces':faces,'loose_edges':[]})
        for t in keys[1:]:
            sample=np.asarray(m.GetPointsAttr().Get(Usd.TimeCode(t)),np.float32)
            if sample.shape!=points.shape or not np.isfinite(sample).all():raise Failure('UNSUPPORTED','USD cache point samples must retain count and finite values')
    return stage,meshes,keys,first


def import_cache_file(path,fps):
    read_cache(path,fps)
    bpy.ops.wm.usd_import(filepath=str(path),import_materials=False,import_textures_mode='IMPORT_NONE',import_volumes=False,import_cameras=False,import_lights=False,import_skeletons=False,set_frame_range=True,apply_unit_conversion_scale=True)


def import_(params,spec,job):
    started=time.monotonic();fps=spec['fps'];path=job/'import-cache.usdc'
    if path.exists():raise Failure('CONFLICT','Import cache output already exists')
    shutil.copyfile(params['file'],path)
    if digest(path)!=params['expected_sha256']:raise Failure('CONFLICT','Copied USD cache hash mismatch')
    stage,meshes,keys,first=read_cache(path,fps)
    class Reference:
        def __init__(self):self.first=first
        def data(self,frame):
            values=[np.asarray(m.GetPointsAttr().Get(Usd.TimeCode(frame)),np.float32) for m in meshes]
            if any(len(v)!=len(r['vertices']) or not np.isfinite(v).all() for v,r in zip(values,first)):raise Failure('VALIDATION_FAILED','USD cache samples have invalid coordinates/counts')
            return np.concatenate(values)
    reference=Reference();times=set(keys)
    for a,b in zip(keys,keys[1:]):times.update(a+(b-a)*f for f in (.25,.5,.75))
    bpy.ops.wm.read_factory_settings(use_empty=True);bpy.context.scene.render.fps=fps;import_cache_file(path,fps)
    def verify(reverse=False):
        checks=[]
        if bpy.context.scene.render.fps!=fps or bpy.context.scene.render.fps_base!=1 or len(bpy.context.scene.objects)!=len(first):raise Failure('VALIDATION_FAILED','Imported cache context changed')
        for requested in sorted(times,reverse=reverse):
            base=math.floor(requested);bpy.context.scene.frame_set(base,subframe=requested-base);actual=float(bpy.context.scene.frame_current_final)
            checks.extend(capture_check(reference,requested,actual,1e-5))
        atomic_json(job/('exchange-cache-'+('reopen' if reverse else 'import')+'-checks.json'),checks)
        if not all(x['ok'] for x in checks):raise Failure('VALIDATION_FAILED','Imported USD cache changed surface geometry')
        return checks
    checks=verify();bpy.context.scene.frame_set(spec['frame']);bpy.context.scene.name=spec['scene'];candidate=exchange.save_candidate(job)
    bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False);verify(True)
    report={'exchange_report_version':'1.0','operation':'import','format':'USD','mode':'ANIMATION','settings':spec,'checks':checks,'outputs':[{'file':str(path),'sha256':digest(path),'bytes':path.stat().st_size}],'candidate':str(candidate),'candidate_sha256':digest(candidate),'source_saved':False,'reopen':'pass','seconds':time.monotonic()-started,'material_profile':[],
            'cache_transfer':{'adapter':ADAPTER,'time_domain':'BLENDER_EVALUATED_FRAME','source_fps':fps,'output_fps':fps,'key_frames':keys,'actual_samples':len({x['actual_frame'] for x in checks}),'requested_times':len(times),'vertex_samples':len(times)*sum(len(x['vertices']) for x in first),'source_replay_max_error':None,'interpolation_max_error':None,'source_uv_max_error':None,'source_uv_tolerance':None,'meshes':[{'source':r['name'],'output':r['name'],'vertices':len(r['vertices'])} for r in first],'cache_dependency':str(path)},
            'losses':['Explicit generated USD geometry cache only; source-format points and reopen validated, not the original rig or continuous-time behavior.','Candidate blend depends on copied USD cache; keep both reported files. UV naming, materials, shading and editable animation controls are not certified.']}
    atomic_json(job/'exchange-report.json',report);return report


def export(params,spec,job,started):
    store=Samples(params,spec,job)
    with ExitStack() as stack:
        hashes=nodes.resource_guards(params,stack);scene=mesh_clip.prepare_source(params,spec,job)
        if scene.render.fps_base!=1 or not 1<=scene.render.fps<=240 or scene.unit_settings.scale_length!=1:raise Failure('UNSUPPORTED','Native cache requires integer fps <=240 and meter scale one')
        fps=scene.render.fps;keys,leaves,witnesses=adaptive(store);limit=spec['sampling']['max_position_error']
        store.progress('source_reopen_reverse',keys=len(keys))
        bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False);mesh_clip.prepare_source(params,spec,job)
        replay=[]
        for row in sorted(store.rows,key=lambda r:r['actual_frame'],reverse=True):
            actual=row['actual_frame'];observed=mesh_clip.capture(spec['objects'],actual,params['driver_profile'])
            matched,uv_error=source_topology(store.first,observed);store.uv_max_error=max(store.uv_max_error,uv_error)
            if not matched:
                atomic_json(job/'exchange-cache-topology-failure.json',{'phase':'source_replay','actual_frame':actual,'completed':len(replay),'differences':topology_differences(store.first,observed)})
                atomic_json(job/'exchange-cache-source-replay.json',replay)
                raise Failure('VALIDATION_FAILED','Native cache source replay topology/UV changed')
            error=mesh_clip.maximum(np.concatenate([r['vertices'] for r in observed]),store.data(actual));replay.append({'frame':actual,'error':error})
            if len(replay)%25==0:store.progress('source_reopen_reverse',completed=len(replay),total=len(store.rows),keys=len(keys))
            if error>min(limit,1e-6):
                atomic_json(job/'exchange-cache-source-replay.json',replay);raise Failure('VALIDATION_FAILED','Native cache source is not repeatable')
        atomic_json(job/'exchange-cache-source-replay.json',replay)
        path=job/'export.usdc';store.progress('write_usd');write_usd(path,store,keys,fps)
        # Every observation made during refinement remains an output witness,
        # including coarse rejected intervals and native-time aliases.
        observations=dict(store.requests);observations.update({r['actual_frame']:r['actual_frame'] for r in store.rows})
        def verify(stage):
            checks=[]
            if bpy.context.scene.render.fps!=fps or bpy.context.scene.render.fps_base!=1:raise Failure('VALIDATION_FAILED','Native cache fps changed')
            if len([o for o in bpy.context.scene.objects if o.type=='MESH'])!=len(store.first):raise Failure('VALIDATION_FAILED','Native cache mesh count changed')
            for requested,actual in sorted(observations.items(),reverse=stage=='reopen'):
                checks.extend(capture_check(store,requested,actual,limit))
                if len(checks)%(100*len(store.first))==0:store.progress(stage,completed=len(checks)//len(store.first),total=len(observations))
            atomic_json(job/('exchange-cache-'+stage+'-checks.json'),checks)
            if not all(x['ok'] for x in checks):raise Failure('VALIDATION_FAILED','USD cache '+stage+' surface exceeds declared budget')
            return checks
        bpy.ops.wm.read_factory_settings(use_empty=True);bpy.context.scene.render.fps=fps;import_cache_file(path,fps)
        store.progress('import');checks=verify('import');candidate=exchange.save_candidate(job)
        bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False);store.progress('reopen');verify('reopen')
        report={'exchange_report_version':'1.0','operation':'export','format':'USD','mode':'ANIMATION','settings':spec,'checks':checks,
                'outputs':[{'file':str(path),'sha256':digest(path),'bytes':path.stat().st_size}],'candidate':str(candidate),'candidate_sha256':digest(candidate),'source_saved':False,'reopen':'pass','resource_hashes':hashes,'seconds':time.monotonic()-started,'material_profile':[],
                'cache_transfer':{'adapter':ADAPTER,'time_domain':'BLENDER_EVALUATED_FRAME','source_fps':fps,'output_fps':fps,'key_frames':keys,'actual_samples':len(store.rows),'requested_times':len(observations),'vertex_samples':store.count*len(store.rows),'source_replay_max_error':max(x['error'] for x in replay),'interpolation_max_error':max(x['max_error'] for x in leaves),'source_uv_max_error':store.uv_max_error,'source_uv_tolerance':UV_TOLERANCE,'meshes':[{'source':r['name'],'output':'Clip_%02d'%i,'vertices':len(r['vertices'])} for i,r in enumerate(store.first)],'cache_dependency':str(path)},
                'losses':['Full evaluated world-space geometry only; editable rig, original drivers, skin and shape semantics remain in source. Materials omitted; UV naming/shading equivalence not certified.',
                          'Time codes/fps preserve source seconds. Geometry gates use Blender actual evaluated frames, including aliases; not a certificate for arbitrary continuous time or other USD readers.',
                          'Candidate blend depends on the reported USD cache; preserve both files and their hashes. Loose edges unsupported; isolated points retained.']}
        atomic_json(job/'exchange-report.json',report);store.progress('verified');return report
