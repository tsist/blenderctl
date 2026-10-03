# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit generated USD cache -> FBX 7400 morph clip with native-frame NLA time.

File time stays in seconds (64-bit KTIME). Only the imported Action clock is
expanded by an exact power of two. No installed importer files are modified.
"""
import json,math,re,time
from pathlib import Path
from contextlib import contextmanager
import bpy,numpy as np
from protocol import Failure,atomic_json,digest
import exchange,rig_exchange,native_mesh_cache

ADAPTER='FBX_NATIVE_CLOCK_V1'
MARKER=b'BlenderCtlNativeClock'
TOLERANCE=1e-5
MAX_BYTES=1024**3
MAX_VERTEX_KEYS=64000000


def clock_scale(keys):
    values=np.asarray(keys,dtype=np.float64)
    if not 2<=len(values)<=1024 or not np.isfinite(values).all() or np.any(values<1) or np.any(values>100000) or np.any(values.astype(np.float32)!=values) or np.any(np.diff(values)<=0) or values[-1]-values[0]>59 or not values[0].is_integer() or not values[-1].is_integer():
        raise Failure('UNSUPPORTED','FBX clock requires 2..1024 ordered exact native-frame keys, integer endpoints, range <=59')
    scale=1
    while float(np.diff(values).min())*scale<1/64:scale*=2
    # RNA NLA action endpoints are bounded even though FCurve co is float32.
    if values[-1]*scale>1048574:raise Failure('UNSUPPORTED','Required internal clock exceeds NLA action range')
    return scale


def child(node,name):
    rows=[x for x in node.elems if x.id==name]
    if len(rows)!=1:raise Failure('UNSUPPORTED','Expected one FBX element: '+name.decode())
    return rows[0]


def clone_tree(tree,transform=lambda node,values,ancestors:values):
    from io_scene_fbx import encode_bin,data_types
    methods={getattr(data_types,n):'add_'+n.lower() for n in ('BOOL','CHAR','INT8','INT16','INT32','INT64','FLOAT32','FLOAT64','BYTES','STRING','INT32_ARRAY','INT64_ARRAY','FLOAT32_ARRAY','FLOAT64_ARRAY','BOOL_ARRAY','BYTE_ARRAY')}
    def visit(node,ancestors=()):
        values=transform(node,list(node.props),ancestors);out=encode_bin.FBXElem(node.id)
        for typ,value in zip(node.props_type,values):
            if typ not in methods:raise Failure('UNSUPPORTED','Unsupported FBX property type')
            getattr(out,methods[typ])(value)
        out.elems=[visit(c,ancestors+(node.id,)) for c in node.elems];return out
    with encode_bin.FBXElem.enable_multithreading_cm():return visit(tree)


def write_fbx(template,path,metadata):
    from io_scene_fbx import parse_fbx,encode_bin,fbx_utils
    if path.exists():raise Failure('CONFLICT','FBX clock output already exists')
    tree,version=parse_fbx.parse(str(template))
    if version!=7400:raise Failure('UNSUPPORTED','FBX clock writer is pinned to FBX 7400')
    keys=np.asarray(metadata['key_frames']);fps=metadata['fps'];ticks=fbx_utils.FBX_KTIME_V7
    mapped=np.rint(keys/fps*ticks).astype(np.int64)
    if np.any(np.diff(mapped)<=0) or not np.array_equal((mapped/ticks*fps).astype(np.float32),keys):raise Failure('UNSUPPORTED','FBX KTIME cannot round-trip native frame keys')
    stats={'curves':0,'time_values':0,'max_time_error_frames':float(np.max(np.abs(mapped/ticks*fps-keys)))}
    def remap(node,values,ancestors):
        if node.id==b'KeyTime':
            times=np.asarray(values[0])*fps/ticks;indices=np.rint(times).astype(np.int64)
            if np.any(np.abs(times-indices)>1e-4) or np.any(indices<1) or np.any(indices>len(keys)):raise Failure('VALIDATION_FAILED','Unexpected FBX template key time')
            values=[mapped[indices-1]];stats['curves']+=1;stats['time_values']+=len(indices)
        elif node.id==b'P' and values:
            name=values[0]
            if b'GlobalSettings' in ancestors and name in (b'TimeSpanStart',b'TimeSpanStop'):values[-1]=int(mapped[-1 if name.endswith(b'Stop') else 0])
            if b'AnimationStack' in ancestors and name in (b'LocalStart',b'LocalStop',b'ReferenceStart',b'ReferenceStop'):values[-1]=int(mapped[-1 if name.endswith(b'Stop') else 0])
        elif node.id in (b'LocalTime',b'ReferenceTime') and b'Takes' in ancestors:values=[int(mapped[0]),int(mapped[-1])]
        return values
    root=clone_tree(tree,remap);marker=encode_bin.FBXElem(MARKER);marker.add_string(json.dumps(metadata,separators=(',',':'),allow_nan=False).encode());root.elems.append(marker)
    temp=path.with_suffix('.fbx.tmp');encode_bin.write(str(temp),root,version);temp.replace(path)
    if path.stat().st_size>MAX_BYTES:raise Failure('UNSUPPORTED','FBX clock exceeds 1 GiB')
    return stats


def preflight(path,fps):
    from io_scene_fbx import parse_fbx,fbx_utils
    if path.stat().st_size>MAX_BYTES:raise Failure('UNSUPPORTED','FBX clock exceeds 1 GiB')
    tree,version=parse_fbx.parse(str(path))
    if version!=7400:raise Failure('UNSUPPORTED','FBX clock requires FBX 7400')
    marker=child(tree,MARKER)
    if len(marker.props)!=1 or not isinstance(marker.props[0],bytes) or len(marker.props[0])>65536:raise Failure('UNSUPPORTED','Invalid FBX clock marker')
    try:meta=json.loads(marker.props[0])
    except (ValueError,UnicodeError):raise Failure('UNSUPPORTED','Invalid FBX clock metadata')
    fields={'adapter','fps','key_frames','clock_scale','source_cache_sha256','meshes'}
    if not isinstance(meta,dict) or set(meta)!=fields or meta['adapter']!=ADAPTER or not isinstance(meta['source_cache_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',meta['source_cache_sha256']):raise Failure('UNSUPPORTED','Unexpected FBX clock contract')
    if type(meta['fps'])!=int or meta['fps']!=fps:raise Failure('CONFLICT','FBX clock fps must match the declared source fps')
    keys=meta['key_frames']
    if not isinstance(keys,list) or any(type(x) not in (int,float) for x in keys) or type(meta['clock_scale'])!=int or clock_scale(keys)!=meta['clock_scale']:raise Failure('UNSUPPORTED','Invalid FBX clock time map')
    rows=meta['meshes']
    if not isinstance(rows,list) or not 1<=len(rows)<=16 or any(not isinstance(r,dict) or set(r)!={'name','vertices'} or r['name']!='Clip_%02d'%i or type(r['vertices'])!=int or not 3<=r['vertices']<=200000 for i,r in enumerate(rows)):raise Failure('UNSUPPORTED','Invalid FBX mesh inventory')
    total=sum(r['vertices'] for r in rows)
    if total>200000 or total*len(keys)>MAX_VERTEX_KEYS:raise Failure('UNSUPPORTED','FBX morph vertex-key budget exceeded')
    ticks=np.rint(np.asarray(keys)/fps*fbx_utils.FBX_KTIME_V7).astype(np.int64)
    if not np.array_equal((ticks/fbx_utils.FBX_KTIME_V7*fps).astype(np.float32),keys) or np.any(np.diff(ticks)<=0):raise Failure('UNSUPPORTED','Unrepresentable native FBX key times')
    objects=child(tree,b'Objects');connections=child(tree,b'Connections')
    allowed={b'Geometry',b'Model',b'Deformer',b'Pose',b'AnimationStack',b'AnimationLayer',b'AnimationCurveNode',b'AnimationCurve'}
    if any(len(e.props)!=3 or type(e.props[0])!=int for e in objects.elems):raise Failure('UNSUPPORTED','Unexpected FBX object header')
    ids={e.props[0]:e for e in objects.elems}
    if len(ids)!=len(objects.elems) or any(e.id not in allowed for e in objects.elems):raise Failure('UNSUPPORTED','FBX clock allows only mesh morph animation without external resources or rigs')
    for kind in (b'AnimationStack',b'AnimationLayer'):
        if sum(e.id==kind for e in objects.elems)!=1:raise Failure('UNSUPPORTED','FBX clock requires a single animation stack/layer')
    parents={}
    for c in connections.elems:
        if c.id!=b'C' or len(c.props) not in (3,4) or c.props[0] not in (b'OO',b'OP') or c.props[1] not in ids or c.props[2] not in {0,*ids}:raise Failure('UNSUPPORTED','Invalid FBX clock connection')
        parents.setdefault(c.props[1],[]).append(c.props)
    models=[e for e in objects.elems if e.id==b'Model']
    names=sorted(e.props[1].split(b'\x00\x01')[0].decode() for e in models)
    if names!=[r['name'] for r in rows] or any(e.props[2]!=b'Mesh' for e in models):raise Failure('UNSUPPORTED','FBX clock mesh names/types changed')
    if any(not any(c[2]==0 for c in parents.get(e.props[0],[])) for e in models):raise Failure('UNSUPPORTED','FBX clock meshes must be unparented')
    mesh_nodes=[e for e in objects.elems if e.id==b'Geometry' and e.props[2]==b'Mesh']
    shape_nodes=[e for e in objects.elems if e.id==b'Geometry' and e.props[2]==b'Shape']
    if len(mesh_nodes)!=len(rows) or len(shape_nodes)!=len(rows)*(len(keys)-1):raise Failure('UNSUPPORTED','FBX geometry count disagrees with clock inventory')
    def object_parent(e,kind,subtype):
        links=[c for c in parents.get(e.props[0],[]) if c[0]==b'OO' and c[2] in ids and ids[c[2]].id==kind and ids[c[2]].props[2]==subtype]
        if len(links)!=1:raise Failure('UNSUPPORTED','Ambiguous FBX morph geometry binding')
        return ids[links[0][2]]
    mesh_counts={}
    for e in mesh_nodes:
        model=object_parent(e,b'Model',b'Mesh');name=model.props[1].split(b'\x00\x01')[0].decode();expected=next(r['vertices'] for r in rows if r['name']==name)
        if len(child(e,b'Vertices').props[0])!=expected*3:raise Failure('VALIDATION_FAILED','FBX base vertex count disagrees with clock inventory')
        mesh_counts[e.props[0]]=expected;raw=np.asarray(child(e,b'PolygonVertexIndex').props[0]);indices=np.where(raw<0,-raw-1,raw)
        if raw.ndim!=1 or not len(raw) or len(raw)>2000000 or raw[-1]>=0 or np.any(indices<0) or np.any(indices>=expected) or np.any(np.diff(np.r_[-1,np.flatnonzero(raw<0)])<3):raise Failure('UNSUPPORTED','Invalid FBX polygon indices')
    for e in shape_nodes:
        channel=object_parent(e,b'Deformer',b'BlendShapeChannel');deform=object_parent(channel,b'Deformer',b'BlendShape');mesh=object_parent(deform,b'Geometry',b'Mesh')
        indices=np.asarray(child(e,b'Indexes').props[0]);vertices=child(e,b'Vertices').props[0]
        if indices.ndim!=1 or len(vertices)!=3*len(indices) or len(np.unique(indices))!=len(indices) or np.any(indices<0) or np.any(indices>=mesh_counts[mesh.props[0]]):raise Failure('UNSUPPORTED','Invalid FBX shape indices')
    for e in objects.elems:
        if e.id==b'Pose' and e.props[2]!=b'BindPose':raise Failure('UNSUPPORTED','Unsupported FBX pose')
        if e.id==b'Deformer' and e.props[2] not in (b'BlendShape',b'BlendShapeChannel'):raise Failure('UNSUPPORTED','Unsupported FBX deformer')
        if e.id==b'Geometry':
            if e.props[2] not in (b'Mesh',b'Shape'):raise Failure('UNSUPPORTED','Unsupported FBX geometry')
            v=np.asarray(child(e,b'Vertices').props[0])
            if v.ndim!=1 or len(v)%3 or len(v)>600000 or not np.isfinite(v).all():raise Failure('UNSUPPORTED','Invalid FBX vertices')
        if e.id==b'AnimationCurve':
            times=np.asarray(child(e,b'KeyTime').props[0]);values=np.asarray(child(e,b'KeyValueFloat').props[0]);flags=np.asarray(child(e,b'KeyAttrFlags').props[0])
            if not np.array_equal(times,ticks) or values.shape!=times.shape or not np.isfinite(values).all() or not len(flags) or np.any(flags!=24836):raise Failure('UNSUPPORTED','FBX clock requires shared complete linear finite curve samples')
            links=parents.get(e.props[0],[])
            if len(links)!=1 or links[0][2] not in ids or ids[links[0][2]].id!=b'AnimationCurveNode':raise Failure('UNSUPPORTED','Unexpected FBX curve binding')
            node=ids[links[0][2]];targets=[c for c in parents.get(node.props[0],[]) if c[0]==b'OP']
            if len(targets)!=1 or len(targets[0])!=4 or targets[0][2] not in ids:raise Failure('UNSUPPORTED','Ambiguous FBX curve node target')
            target=ids[targets[0][2]]
            if target.id==b'Model':
                if targets[0][3] not in (b'Lcl Translation',b'Lcl Rotation',b'Lcl Scaling') or np.any(values!=values[0]):raise Failure('UNSUPPORTED','FBX native clock requires constant object transforms')
            elif target.id not in (b'Deformer',b'Geometry'):raise Failure('UNSUPPORTED','Unsupported FBX animation target')
    return meta


@contextmanager
def import_time_map(meta,index_domain=False):
    from io_scene_fbx import import_fbx,fbx_utils
    original=import_fbx._convert_fbx_time_to_blender_time;keys=np.asarray(meta['key_frames']);ticks=np.rint(keys/meta['fps']*fbx_utils.FBX_KTIME_V7).astype(np.int64)
    def convert(values,blen_start_offset,fbx_start_offset,fps,fbx_ktime):
        indices=np.searchsorted(ticks,values)
        if blen_start_offset!=0 or fbx_start_offset!=0 or fps!=meta['fps'] or fbx_ktime!=fbx_utils.FBX_KTIME_V7 or np.any(indices>=len(ticks)) or not np.array_equal(ticks[indices],values):raise Failure('UNSUPPORTED','Importer received times outside explicit FBX clock map')
        return (indices+1).astype(np.float64) if index_domain else keys[indices]*meta['clock_scale']
    import_fbx._convert_fbx_time_to_blender_time=convert
    try:yield
    finally:import_fbx._convert_fbx_time_to_blender_time=original


def import_scene(path,meta,index_domain=False):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    with import_time_map(meta,index_domain):
        from modeling import finished
        finished(bpy.ops.import_scene.fbx(filepath=str(path),use_image_search=False,use_anim=True,anim_offset=0,use_custom_props=False,automatic_bone_orientation=False))
    s=bpy.context.scene;keys=meta['key_frames'];scale=meta['clock_scale']
    if s.render.fps!=meta['fps'] or s.render.fps_base!=1:raise Failure('VALIDATION_FAILED','FBX header fps disagrees with native clock')
    s.frame_start=1 if index_domain else int(keys[0]);s.frame_end=len(keys) if index_domain else int(keys[-1])
    names=[r['name'] for r in meta['meshes']]
    if sorted(o.name for o in s.objects)!=names or bpy.data.cache_files or bpy.data.libraries:raise Failure('VALIDATION_FAILED','Unexpected FBX objects or external dependencies')
    for o,r in zip([bpy.data.objects[n] for n in names],meta['meshes']):
        if o.type!='MESH' or len(o.data.vertices)!=r['vertices'] or o.modifiers or o.constraints or not o.data.shape_keys or len(o.data.shape_keys.key_blocks)!=len(keys):raise Failure('VALIDATION_FAILED','FBX morph topology/count changed')
    for data in [*bpy.data.objects,*bpy.data.shape_keys]:
        anim=data.animation_data
        if not anim or not anim.action:continue
        if anim.drivers or anim.nla_tracks:raise Failure('VALIDATION_FAILED','Unexpected imported animation mixing')
        if index_domain:continue
        action=anim.action;slot=anim.action_slot;anim.action=None
        track=anim.nla_tracks.new();strip=track.strips.new('NativeFrameClock',int(keys[0]),action);strip.action_slot=slot
        strip.frame_end=keys[-1];strip.blend_type='REPLACE';strip.extrapolation='HOLD';strip.use_animated_time=True;strip.use_animated_time_cyclic=False
        for t in (keys[0],keys[-1]):strip.strip_time=t*scale;strip.keyframe_insert('strip_time',frame=t)
        for fc in strip.fcurves:
            for p in fc.keyframe_points:p.interpolation='LINEAR'
            fc.update()


def points(meta,requested):
    s=bpy.context.scene;base=math.floor(requested);s.frame_set(base,subframe=requested-base);bpy.context.view_layer.update();graph=bpy.context.evaluated_depsgraph_get();out=[]
    for r in meta['meshes']:
        obj=bpy.data.objects[r['name']];ev=obj.evaluated_get(graph);mesh=ev.to_mesh()
        try:
            if len(mesh.vertices)!=r['vertices']:raise Failure('VALIDATION_FAILED','FBX evaluated vertex count changed')
            co=np.empty(len(mesh.vertices)*3,np.float32);mesh.vertices.foreach_get('co',co);matrix=np.asarray(ev.matrix_world,dtype=np.float64)
            v=co.reshape((-1,3))@matrix[:3,:3].T+matrix[:3,3]
            if not np.isfinite(v).all():raise Failure('VALIDATION_FAILED','Nonfinite FBX surface')
            out.append(v)
        finally:ev.to_mesh_clear()
    return np.concatenate(out),float(s.frame_current_final)


def topology(meta):
    return [[list(p.vertices) for p in bpy.data.objects[r['name']].data.polygons] for r in meta['meshes']]


def verify(meta,reference,job,phase,expected_topology):
    keys=meta['key_frames'];times=set(keys)
    for a,b in zip(keys,keys[1:]):times.update(a+(b-a)*f for f in (.25,.5,.75))
    checks=[]
    if topology(meta)!=expected_topology or bpy.data.cache_files or bpy.data.libraries or any(o.modifiers or o.constraints for o in bpy.context.scene.objects):raise Failure('VALIDATION_FAILED','FBX native clock topology or dependency structure changed')
    if bpy.context.scene.render.fps!=meta['fps'] or bpy.context.scene.render.fps_base!=1:raise Failure('VALIDATION_FAILED','FBX clock output fps changed')
    for requested in sorted(times,reverse=phase=='reopen'):
        observed,actual=points(meta,requested);expected=reference(actual);offset=0
        for r in meta['meshes']:
            n=r['vertices'];error=float(np.linalg.norm(observed[offset:offset+n]-expected[offset:offset+n],axis=1).max());offset+=n
            checks.append({'source':r['name'],'output':r['name'],'requested_frame':requested,'actual_frame':actual,'indexed_vertex_error':error,'topology_ok':True,'position_tolerance':TOLERANCE,'ok':error<=TOLERANCE,'scope':'FBX_NATIVE_FRAME_INDEXED_SURFACE'})
    atomic_json(job/('exchange-clock-'+phase+'-checks.json'),checks)
    if not all(r['ok'] for r in checks):raise Failure('VALIDATION_FAILED','FBX native clock '+phase+' geometry exceeds conversion budget')
    return checks


def report(spec,meta,checks,path,candidate,started,operation):
    return {'exchange_report_version':'1.0','operation':operation,'format':'FBX','mode':'ANIMATION','settings':spec,'checks':checks,'outputs':[] if operation=='import' else [{'file':str(path),'sha256':digest(path),'bytes':path.stat().st_size}],'candidate':str(candidate),'candidate_sha256':digest(candidate),'source_saved':False,'reopen':'pass','seconds':time.monotonic()-started,
            'clock_transfer':{**meta,'conversion_position_tolerance':TOLERANCE,'max_position_error':max(r['indexed_vertex_error'] for r in checks),'self_contained_candidate':True},
            'losses':['Generated full evaluated morph geometry; original editable rig, skin, control and source shape semantics are not retained. Materials omitted; UV naming/shading not certified.',
                      'File stores seconds as FBX 64-bit KTIME. Blender native-frame fidelity requires this explicit importer and its NLA clock; ordinary/other FBX importers are not certified.',
                      'Conversion budget is relative to serialized input, separate from prior source sampling error. Gates sample native frames, not arbitrary continuous time.']}


def convert(item,job):
    started=time.monotonic();spec=item['import'];source=Path(item['input']['file']);fps=spec['fps']
    if digest(source)!=item['input']['expected_sha256']:raise Failure('CONFLICT','USD input hash changed')
    stage,meshes,keys,first=native_mesh_cache.read_cache(source,fps);scale=clock_scale(keys)
    if not keys[0]<=spec['frame']<=keys[-1]:raise Failure('INVALID_REQUEST','Candidate frame outside native clip range')
    count=sum(len(r['vertices']) for r in first)
    if count*len(keys)>MAX_VERTEX_KEYS:raise Failure('UNSUPPORTED','FBX morph vertex-key budget exceeded')
    meta={'adapter':ADAPTER,'fps':fps,'key_frames':keys,'clock_scale':scale,'source_cache_sha256':digest(source),'meshes':[{'name':r['name'],'vertices':len(r['vertices'])} for r in first]}
    bpy.ops.wm.read_factory_settings(use_empty=True);s=bpy.context.scene;s.render.fps=fps;s.frame_start=1;s.frame_end=len(keys)
    for i,row in enumerate(first):
        mesh=bpy.data.meshes.new('ClipMesh_%02d'%i);mesh.from_pydata(row['vertices'].tolist(),[],row['faces']);mesh.update()
        # Preserve generated USD face-varying UV coordinates where present.
        from pxr import UsdGeom
        for pv in UsdGeom.PrimvarsAPI(meshes[i]).GetPrimvars():
            if pv.GetPrimvarName().startswith('st'):
                if pv.GetInterpolation()!=UsdGeom.Tokens.faceVarying or pv.GetAttr().GetNumTimeSamples():raise Failure('UNSUPPORTED','FBX clock requires static face-varying UVs')
                uv=np.asarray(pv.ComputeFlattened(),np.float32)
                if uv.shape!=(len(mesh.loops),2) or not np.isfinite(uv).all():raise Failure('UNSUPPORTED','Invalid USD cache UV array')
                mesh.uv_layers.new(name=pv.GetPrimvarName()).data.foreach_set('uv',uv.ravel())
        obj=bpy.data.objects.new(row['name'],mesh);s.collection.objects.link(obj);obj.select_set(True);obj.shape_key_add(name='Basis')
        bag=rig_exchange.action(obj.data.shape_keys,'Morph_%02d'%i,'KEY')
        for j,t in enumerate(keys[1:],1):
            key=obj.shape_key_add(name='Sample_%03d'%j);key.data.foreach_set('co',np.asarray(meshes[i].GetPointsAttr().Get(t),np.float32).ravel())
            indices=sorted({1,j,j+1,min(j+2,len(keys)),len(keys)})
            rig_exchange.curve(bag,key.path_from_id('value'),0,indices,[float(k==j+1) for k in indices])
    template=job/'clock-template.fbx';exchange.export_file(template,{'format':'FBX','mode':'ANIMATION','frame_start':1,'frame_end':len(keys),'materials':'NONE'})
    path=job/'export.fbx';stats=write_fbx(template,path,meta);atomic_json(job/'exchange-clock-time-map.json',stats);preflight(path,fps)
    def reference(t):return np.concatenate([np.asarray(m.GetPointsAttr().Get(t),np.float64) for m in meshes])
    expected_topology=[r['faces'] for r in first]
    import_scene(path,meta);checks=verify(meta,reference,job,'import',expected_topology);bpy.context.scene.name=spec['scene'];bpy.context.scene.frame_set(spec['frame']);candidate=exchange.save_candidate(job)
    bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False);verify(meta,reference,job,'reopen',expected_topology)
    if digest(source)!=item['input']['expected_sha256']:raise Failure('CONFLICT','USD source changed during conversion')
    result=report(item,meta,checks,path,candidate,started,'convert');atomic_json(job/'exchange-report.json',result);return result


def import_(params,spec,job):
    started=time.monotonic();path=Path(params['file']);meta=preflight(path,spec['fps']);keys=meta['key_frames']
    if not keys[0]<=spec['frame']<=keys[-1]:raise Failure('INVALID_REQUEST','Candidate frame outside native clip range')
    # Independently map file samples to well-separated integer times for reference.
    # This path never uses the NLA clock or fractional FCurve evaluation.
    import_scene(path,meta,index_domain=True);samples=[];expected_topology=topology(meta)
    for i in range(len(keys)):
        v,_=points(meta,i+1);file=job/('clock-reference-%04d.npy'%i);np.save(file,v,allow_pickle=False);samples.append(file)
    from functools import lru_cache
    @lru_cache(maxsize=4)
    def load(i):return np.load(samples[i],allow_pickle=False)
    def reference(t):
        i=max(0,min(len(keys)-2,int(np.searchsorted(keys,t,side='right'))-1));alpha=(t-keys[i])/(keys[i+1]-keys[i]);return load(i)*(1-alpha)+load(i+1)*alpha
    import_scene(path,meta);checks=verify(meta,reference,job,'import',expected_topology);bpy.context.scene.name=spec['scene'];bpy.context.scene.frame_set(spec['frame']);candidate=exchange.save_candidate(job)
    bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False);verify(meta,reference,job,'reopen',expected_topology)
    result=report(spec,meta,checks,path,candidate,started,'import');atomic_json(job/'exchange-report.json',result);return result
