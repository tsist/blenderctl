# SPDX-License-Identifier: GPL-3.0-or-later
"""Native-frame GLB morph geometry with an explicit time origin and NLA clock."""
import json,math,struct,time
from pathlib import Path
import bpy,numpy as np
from pxr import UsdGeom
from protocol import Failure,atomic_json,digest
import native_mesh_cache,fbx_clock,exchange

ADAPTER='GLB_NATIVE_CLOCK_V1';ATTRIBUTE='_SOURCE_VERTEX';LIMIT=1024**3;TOLERANCE=1e-5


def pack(path,doc,binary):
    if path.exists():raise Failure('CONFLICT','GLB clock output already exists')
    payload=json.dumps(doc,separators=(',',':'),allow_nan=False).encode();payload+=b' '*((-len(payload))%4)
    total=28+len(payload)+len(binary)
    if total>LIMIT:raise Failure('UNSUPPORTED','GLB clock exceeds 1 GiB')
    with path.open('xb') as f:
        f.write(struct.pack('<4sII',b'glTF',2,total));f.write(struct.pack('<I4s',len(payload),b'JSON'));f.write(payload);f.write(struct.pack('<I4s',len(binary),b'BIN\0'));f.write(binary)


def unpack(path):
    if path.stat().st_size>LIMIT:raise Failure('UNSUPPORTED','GLB clock exceeds 1 GiB')
    raw=path.read_bytes()
    if len(raw)<28 or struct.unpack_from('<4sII',raw)!=(b'glTF',2,len(raw)):raise Failure('UNSUPPORTED','Invalid GLB container')
    length,kind=struct.unpack_from('<I4s',raw,12);end=20+length
    if kind!=b'JSON' or length>16*1024**2 or end+8>len(raw):raise Failure('UNSUPPORTED','Invalid GLB JSON chunk')
    try:doc=json.loads(raw[20:end])
    except (ValueError,UnicodeError):raise Failure('UNSUPPORTED','Invalid GLB JSON')
    size,kind=struct.unpack_from('<I4s',raw,end)
    if kind!=b'BIN\0' or end+8+size!=len(raw):raise Failure('UNSUPPORTED','Expected one GLB binary chunk')
    return doc,memoryview(raw)[end+8:]


def array(doc,binary,index):
    if type(index)!=int or not 0<=index<len(doc['accessors']):raise Failure('UNSUPPORTED','Invalid GLB accessor index')
    a=doc['accessors'][index]
    if set(a)-{'bufferView','byteOffset','componentType','count','type','min','max'} or a.get('componentType') not in (5125,5126) or a.get('type') not in ('SCALAR','VEC2','VEC3') or type(a.get('count'))!=int or not 0<a['count']<=2000000:raise Failure('UNSUPPORTED','Unsupported GLB accessor layout')
    vi=a.get('bufferView')
    if type(vi)!=int or not 0<=vi<len(doc['bufferViews']):raise Failure('UNSUPPORTED','Invalid GLB buffer view')
    v=doc['bufferViews'][vi]
    if set(v)-{'buffer','byteOffset','byteLength'} or v.get('buffer')!=0 or any(type(v.get(k))!=int or v[k]<0 for k in ('byteOffset','byteLength')):raise Failure('UNSUPPORTED','Unsupported GLB buffer view layout')
    offset=a.get('byteOffset',0);components={'SCALAR':1,'VEC2':2,'VEC3':3}[a['type']];size=a['count']*components*4
    if type(offset)!=int or offset<0 or offset%4 or v['byteOffset']%4 or offset+size>v['byteLength'] or v['byteOffset']+v['byteLength']>len(binary):raise Failure('UNSUPPORTED','GLB accessor out of bounds')
    out=np.frombuffer(binary,dtype='<f4' if a['componentType']==5126 else '<u4',count=a['count']*components,offset=v['byteOffset']+offset).reshape((-1,components))
    if not np.isfinite(out).all():raise Failure('UNSUPPORTED','Nonfinite GLB values')
    return out


def write(path,stage,meshes,keys,first,fps,source_sha):
    scale=fbx_clock.clock_scale(keys);origin=keys[0];seconds=((np.asarray(keys)-origin)/fps).astype(np.float32)
    if np.any(np.diff(seconds)<=0) or not np.array_equal((seconds.astype(np.float64)*fps+origin).astype(np.float32),keys):raise Failure('UNSUPPORTED','GLB relative seconds cannot preserve native frame keys')
    doc={'asset':{'version':'2.0','generator':'Blender CLI native clock'},'scene':0,'scenes':[{'nodes':list(range(len(first)))}],'nodes':[],'meshes':[],'animations':[{'name':'NativeClip','samplers':[],'channels':[]}],'bufferViews':[],'accessors':[],'buffers':[]}
    binary=bytearray()
    def add(values,kind,component=5126,bounds=False):
        a=np.asarray(values,dtype='<f4' if component==5126 else '<u4');a=a.reshape((-1,{'SCALAR':1,'VEC2':2,'VEC3':3}[kind]));view=len(doc['bufferViews']);doc['bufferViews'].append({'buffer':0,'byteOffset':len(binary),'byteLength':a.nbytes});binary.extend(a.tobytes())
        acc={'bufferView':view,'componentType':component,'count':len(a),'type':kind}
        if bounds:acc.update(min=a.min(axis=0).tolist(),max=a.max(axis=0).tolist())
        doc['accessors'].append(acc);return len(doc['accessors'])-1
    time_accessor=add(seconds,'SCALAR',bounds=True);weights=np.zeros((len(keys),len(keys)-1),np.float32);weights[1:]=np.eye(len(keys)-1,dtype=np.float32);weight_accessor=add(weights.ravel(),'SCALAR');metadata=[];total_expanded=0
    def yup(v):return np.asarray(v,np.float32)[:,[0,2,1]]*np.array([1,1,-1],np.float32)
    for i,row in enumerate(first):
        mesh=bpy.data.meshes.new('ClockTopology');mesh.from_pydata(row['vertices'].tolist(),[],row['faces']);mesh.update();mesh.calc_loop_triangles()
        loop_ids=np.array([l.vertex_index for l in mesh.loops],np.int32);tri_loops=np.array([t.loops for t in mesh.loop_triangles],np.int32);uvs=[]
        for pv in sorted(UsdGeom.PrimvarsAPI(meshes[i]).GetPrimvars(),key=lambda p:p.GetPrimvarName()):
            if pv.GetPrimvarName().startswith('st'):
                uv=np.asarray(pv.ComputeFlattened(),np.float32)
                if pv.GetInterpolation()!=UsdGeom.Tokens.faceVarying or pv.GetAttr().GetNumTimeSamples() or uv.shape!=(len(loop_ids),2) or not np.isfinite(uv).all():raise Failure('UNSUPPORTED','GLB clock requires static finite face-varying UVs')
                uvs.append(uv)
        corner=np.column_stack([loop_ids,*uvs]);unique,inverse=np.unique(corner,axis=0,return_inverse=True);ids=unique[:,0].astype(np.int32);isolated=np.setdiff1d(np.arange(len(row['vertices'])),ids)
        primitive_maps=[(ids,inverse[tri_loops].ravel(),4,unique[:,1:])]
        if len(isolated):primitive_maps.append((isolated,np.arange(len(isolated)),0,np.zeros((len(isolated),len(uvs)*2))))
        bpy.data.meshes.remove(mesh);primitives=[]
        for ids,indices,mode,uv in primitive_maps:
            total_expanded+=len(ids)
            if total_expanded*len(keys)>64000000:raise Failure('UNSUPPORTED','GLB expanded vertex-key budget exceeded')
            base=yup(row['vertices'][ids]);attrs={'POSITION':add(base,'VEC3',bounds=True),ATTRIBUTE:add(ids,'SCALAR')}
            for u in range(len(uvs)):
                values=uv[:,u*2:u*2+2].copy();values[:,1]=1-values[:,1];attrs['TEXCOORD_%d'%u]=add(values,'VEC2')
            targets=[]
            for t in keys[1:]:
                value=np.asarray(meshes[i].GetPointsAttr().Get(t),np.float32)[ids];targets.append({'POSITION':add(yup(value)-base,'VEC3')})
            primitives.append({'attributes':attrs,'indices':add(indices,'SCALAR',5125),'mode':mode,'targets':targets})
        name='Clip_%02d'%i;doc['nodes'].append({'name':name,'mesh':i});doc['meshes'].append({'name':name,'primitives':primitives,'weights':[0.]*(len(keys)-1),'extras':{'targetNames':['Sample_%03d'%j for j in range(1,len(keys))]}})
        anim=doc['animations'][0];anim['channels'].append({'sampler':i,'target':{'node':i,'path':'weights'}});anim['samplers'].append({'input':time_accessor,'output':weight_accessor,'interpolation':'LINEAR'})
        metadata.append({'name':name,'vertices':len(row['vertices']),'expanded_vertices':sum(len(x[0]) for x in primitive_maps)})
    meta={'adapter':ADAPTER,'fps':fps,'time_origin_frame':origin,'key_frames':keys,'clock_scale':scale,'source_cache_sha256':source_sha,'meshes':metadata}
    doc['extras']={'blenderctl_native_clock':meta};doc['buffers']=[{'byteLength':len(binary)}];pack(path,doc,binary);return meta


def read(path,fps):
    try:return _read(path,fps)
    except Failure:raise
    except (KeyError,ValueError,TypeError,IndexError,OverflowError) as exc:raise Failure('UNSUPPORTED','Malformed GLB native clock contract: '+type(exc).__name__) from exc


def _read(path,fps):
    doc,binary=unpack(path)
    required={'asset','scene','scenes','nodes','meshes','animations','bufferViews','accessors','buffers','extras'}
    if set(doc)!=required or doc['asset'].get('version')!='2.0' or set(doc['extras'])!={'blenderctl_native_clock'} or doc['buffers']!=[{'byteLength':len(binary)}]:raise Failure('UNSUPPORTED','GLB clock requires a self-contained generated contract')
    meta=doc['extras']['blenderctl_native_clock'];fields={'adapter','fps','time_origin_frame','key_frames','clock_scale','source_cache_sha256','meshes'}
    import re
    if not isinstance(meta,dict) or set(meta)!=fields or meta['adapter']!=ADAPTER or not isinstance(meta['source_cache_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',meta['source_cache_sha256']):raise Failure('UNSUPPORTED','Invalid GLB clock metadata')
    if type(meta['fps'])!=int or meta['fps']!=fps:raise Failure('CONFLICT','GLB clock fps mismatch')
    keys=meta['key_frames']
    if not isinstance(keys,list) or any(type(t) not in (int,float) for t in keys) or type(meta['clock_scale'])!=int or fbx_clock.clock_scale(keys)!=meta['clock_scale'] or meta['time_origin_frame']!=keys[0]:raise Failure('UNSUPPORTED','Invalid GLB native time map')
    rows=meta['meshes'];count=len(rows)
    if not 1<=count<=16 or any(set(r)!={'name','vertices','expanded_vertices'} or r['name']!='Clip_%02d'%i or type(r['vertices'])!=int or not 3<=r['vertices']<=200000 or type(r['expanded_vertices'])!=int or not r['vertices']<=r['expanded_vertices']<=2000000 for i,r in enumerate(rows)):raise Failure('UNSUPPORTED','Invalid GLB mesh inventory')
    if sum(r['vertices'] for r in rows)>200000 or sum(r['expanded_vertices'] for r in rows)*len(keys)>64000000:raise Failure('UNSUPPORTED','GLB vertex-key budget exceeded')
    if doc['scene']!=0 or doc['scenes']!=[{'nodes':list(range(count))}] or doc['nodes']!=[{'name':r['name'],'mesh':i} for i,r in enumerate(rows)] or len(doc['meshes'])!=count or len(doc['animations'])!=1:raise Failure('UNSUPPORTED','GLB clock requires flat identity mesh nodes and one animation')
    animation=doc['animations'][0]
    if set(animation)!={'name','samplers','channels'} or len(animation['samplers'])!=count or animation['channels']!=[{'sampler':i,'target':{'node':i,'path':'weights'}} for i in range(count)]:raise Failure('UNSUPPORTED','Invalid GLB weight animation structure')
    expected_times=((np.asarray(keys)-keys[0])/fps).astype(np.float32);expected_weights=np.zeros((len(keys),len(keys)-1),np.float32);expected_weights[1:]=np.eye(len(keys)-1,dtype=np.float32);time_ids=set()
    if np.any(np.diff(expected_times)<=0) or not np.array_equal((expected_times.astype(np.float64)*fps+keys[0]).astype(np.float32),keys):raise Failure('UNSUPPORTED','GLB seconds collapse native frames')
    for sampler in animation['samplers']:
        if set(sampler)!={'input','output','interpolation'} or sampler['interpolation']!='LINEAR':raise Failure('UNSUPPORTED','GLB clock requires linear weights')
        times=array(doc,binary,sampler['input']);weights=array(doc,binary,sampler['output'])
        if any(doc['accessors'][sampler[k]]['type']!='SCALAR' or doc['accessors'][sampler[k]]['componentType']!=5126 for k in ('input','output')):raise Failure('UNSUPPORTED','GLB clock needs float scalar samples')
        if not np.array_equal(times.ravel(),expected_times) or not np.array_equal(weights.ravel(),expected_weights.ravel()):raise Failure('UNSUPPORTED','GLB file time/weight samples disagree with clock contract')
        time_ids.add(sampler['input'])
    mappings=[]
    for mesh,row in zip(doc['meshes'],rows):
        if set(mesh)!={'name','primitives','weights','extras'} or mesh['name']!=row['name'] or mesh['weights']!=[0.]*(len(keys)-1) or not 1<=len(mesh['primitives'])<=2:raise Failure('UNSUPPORTED','Invalid GLB morph mesh')
        if mesh['extras']!={'targetNames':['Sample_%03d'%j for j in range(1,len(keys))]}:raise Failure('UNSUPPORTED','Invalid GLB morph names')
        parts=[];all_ids=[]
        for p in mesh['primitives']:
            if set(p)!={'attributes','indices','mode','targets'} or p['mode'] not in (0,4) or len(p['targets'])!=len(keys)-1:raise Failure('UNSUPPORTED','Invalid GLB primitive')
            attrs=p['attributes']
            if not {'POSITION',ATTRIBUTE}<=set(attrs) or any(k not in ('POSITION',ATTRIBUTE) and not re.fullmatch('TEXCOORD_[0-7]',k) for k in attrs):raise Failure('UNSUPPORTED','Invalid GLB vertex attributes')
            base=array(doc,binary,attrs['POSITION']);ids=array(doc,binary,attrs[ATTRIBUTE]).ravel();indices=array(doc,binary,p['indices']).ravel()
            for name,index in attrs.items():
                expected='VEC3' if name=='POSITION' else 'SCALAR' if name==ATTRIBUTE else 'VEC2'
                if doc['accessors'][index]['type']!=expected or doc['accessors'][index]['componentType']!=5126:raise Failure('UNSUPPORTED','GLB attribute layout mismatch')
            if doc['accessors'][p['indices']]['type']!='SCALAR':raise Failure('UNSUPPORTED','GLB indices must be scalar')
            if base.shape!=(len(ids),3) or not np.array_equal(ids,np.rint(ids)) or np.any(ids<0) or np.any(ids>=row['vertices']) or indices.dtype.kind!='u' or np.any(indices>=len(base)) or p['mode']==4 and len(indices)%3:raise Failure('UNSUPPORTED','Invalid GLB position/index map')
            ids=ids.astype(np.int32);all_ids.extend(ids.tolist())
            for name,index in attrs.items():
                if name.startswith('TEXCOORD_') and array(doc,binary,index).shape!=(len(ids),2):raise Failure('UNSUPPORTED','Invalid GLB UV shape')
            for t in p['targets']:
                if set(t)!={'POSITION'} or array(doc,binary,t['POSITION']).shape!=base.shape:raise Failure('UNSUPPORTED','Invalid GLB morph target')
                if doc['accessors'][t['POSITION']]['type']!='VEC3' or doc['accessors'][t['POSITION']]['componentType']!=5126:raise Failure('UNSUPPORTED','Invalid GLB morph layout')
            parts.append((p,ids,indices))
        if len(all_ids)!=row['expanded_vertices'] or set(all_ids)!=set(range(row['vertices'])):raise Failure('UNSUPPORTED','GLB vertex map does not cover the full source')
        # UV splits may duplicate vertices, but may never disagree on their motion.
        ids=np.asarray(all_ids);_,unique=np.unique(ids,return_index=True)
        for key in range(len(keys)):
            values=np.concatenate([array(doc,binary,p['attributes']['POSITION'] if key==0 else p['targets'][key-1]['POSITION']) for p,_,_ in parts])
            if not np.array_equal(values,values[unique][ids]):raise Failure('UNSUPPORTED','Inconsistent GLB split-vertex motion')
        mappings.append(parts)
    return doc,binary,meta,time_ids,mappings


def import_scene(path,meta,time_ids):
    from io_scene_gltf2.io.imp.gltf2_io_binary import BinaryData
    bpy.ops.wm.read_factory_settings(use_empty=True);s=bpy.context.scene;s.render.fps=meta['fps'];s.render.fps_base=1
    original=BinaryData.decode_accessor
    def mapped(gltf,accessor_idx,cache=False):
        if accessor_idx in time_ids:return (np.asarray(meta['key_frames'],np.float64)*meta['clock_scale']/meta['fps']).reshape((-1,1))
        return original(gltf,accessor_idx,cache=cache)
    BinaryData.decode_accessor=staticmethod(mapped)
    try:
        from modeling import finished
        finished(bpy.ops.import_scene.gltf(filepath=str(path),import_pack_images=True,import_shading='NORMALS',merge_vertices=False,import_scene_as_collection=False))
    finally:BinaryData.decode_accessor=staticmethod(original)
    s=bpy.context.scene;s.frame_start=int(meta['key_frames'][0]);s.frame_end=int(meta['key_frames'][-1])
    if sorted(o.name for o in s.objects)!=[r['name'] for r in meta['meshes']]:raise Failure('VALIDATION_FAILED','Imported GLB object inventory changed')
    for obj in s.objects:
        data=obj.data.shape_keys
        if obj.type!='MESH' or not data or len(data.key_blocks)!=len(meta['key_frames']):raise Failure('VALIDATION_FAILED','GLB morph count changed')
        anim=data.animation_data
        if not anim or not anim.action or anim.drivers:raise Failure('VALIDATION_FAILED','GLB animation missing or driven')
        for track in list(anim.nla_tracks):
            if not track.mute:raise Failure('VALIDATION_FAILED','Unexpected active imported NLA track')
            anim.nla_tracks.remove(track)
        act=anim.action;slot=anim.action_slot;anim.action=None;track=anim.nla_tracks.new();strip=track.strips.new('NativeFrameClock',s.frame_start,act);strip.action_slot=slot;strip.frame_end=s.frame_end;strip.blend_type='REPLACE';strip.use_animated_time=True;strip.use_animated_time_cyclic=False
        for t in (s.frame_start,s.frame_end):strip.strip_time=t*meta['clock_scale'];strip.keyframe_insert('strip_time',frame=t)
        for fc in strip.fcurves:
            for p in fc.keyframe_points:p.interpolation='LINEAR'
            fc.update()


def observed(meta,t):
    base=math.floor(t);bpy.context.scene.frame_set(base,subframe=t-base);bpy.context.view_layer.update();graph=bpy.context.evaluated_depsgraph_get();out=[]
    for row in meta['meshes']:
        obj=bpy.data.objects[row['name']];ev=obj.evaluated_get(graph);mesh=ev.to_mesh()
        try:
            attr=mesh.attributes.get(ATTRIBUTE)
            if not attr or attr.domain!='POINT' or attr.data_type!='FLOAT':raise Failure('VALIDATION_FAILED','GLB source vertex attribute missing')
            ids=np.empty(len(mesh.vertices),np.float32);attr.data.foreach_get('value',ids)
            if not np.isfinite(ids).all() or np.any(ids!=np.rint(ids)) or set(ids)!=set(range(row['vertices'])):raise Failure('VALIDATION_FAILED','GLB imported source indices changed')
            co=np.empty(len(mesh.vertices)*3,np.float32);mesh.vertices.foreach_get('co',co);matrix=np.asarray(ev.matrix_world);world=co.reshape((-1,3))@matrix[:3,:3].T+matrix[:3,3]
            if not np.isfinite(world).all():raise Failure('VALIDATION_FAILED','Nonfinite GLB output geometry')
            out.append((ids.astype(np.int32),world))
        finally:ev.to_mesh_clear()
    return out,float(bpy.context.scene.frame_current_final)


def topology(meta,mappings):
    def triangles(v):
        v=np.asarray(v,np.int32).reshape((-1,3));v=np.take_along_axis(v,(np.argmin(v,axis=1)[:,None]+np.arange(3))%3,axis=1)
        return v[np.lexsort((v[:,2],v[:,1],v[:,0]))]
    for row,parts in zip(meta['meshes'],mappings):
        o=bpy.data.objects[row['name']];mesh=o.data
        if o.modifiers or o.constraints or len(mesh.vertices)!=row['expanded_vertices'] or any(len(p.vertices)!=3 for p in mesh.polygons):raise Failure('VALIDATION_FAILED','GLB surface structure changed')
        attr=mesh.attributes.get(ATTRIBUTE)
        if not attr or attr.domain!='POINT' or attr.data_type!='FLOAT':raise Failure('VALIDATION_FAILED','GLB vertex map missing')
        ids=np.empty(len(mesh.vertices),np.float32);attr.data.foreach_get('value',ids)
        if not np.array_equal(np.sort(ids),np.sort(np.concatenate([x[1] for x in parts]))):raise Failure('VALIDATION_FAILED','GLB expanded vertex map changed')
        expected=np.concatenate([pids[index].reshape((-1,3)) for p,pids,index in parts if p['mode']==4])
        actual=np.array([ids[list(p.vertices)] for p in mesh.polygons],np.int32)
        if not np.array_equal(triangles(actual),triangles(expected)):raise Failure('VALIDATION_FAILED','GLB triangle connectivity or winding changed')
        used={i for p in mesh.polygons for i in p.vertices};loose=sorted(int(ids[v.index]) for v in mesh.vertices if v.index not in used)
        expected_loose=sorted(int(i) for p,pids,index in parts if p['mode']==0 for i in pids[index])
        if loose!=expected_loose:raise Failure('VALIDATION_FAILED','GLB isolated points changed')


def verify(meta,reference,job,phase,mappings):
    topology(meta,mappings)
    keys=meta['key_frames'];times=set(keys);checks=[]
    for a,b in zip(keys,keys[1:]):times.update(a+(b-a)*f for f in (.25,.5,.75))
    if bpy.context.scene.render.fps!=meta['fps'] or bpy.context.scene.render.fps_base!=1 or bpy.data.cache_files or bpy.data.libraries:raise Failure('VALIDATION_FAILED','GLB scene time/dependencies changed')
    for t in sorted(times,reverse=phase=='reopen'):
        values,actual=observed(meta,t);expected=reference(actual)
        for row,(ids,world),ref in zip(meta['meshes'],values,expected):
            error=float(np.linalg.norm(world-ref[ids],axis=1).max());checks.append({'source':row['name'],'output':row['name'],'requested_frame':t,'actual_frame':actual,'indexed_vertex_error':error,'topology_ok':True,'position_tolerance':TOLERANCE,'ok':error<=TOLERANCE,'scope':'GLB_NATIVE_FRAME_INDEXED_SURFACE'})
    atomic_json(job/('exchange-clock-'+phase+'-checks.json'),checks)
    if not all(r['ok'] for r in checks):raise Failure('VALIDATION_FAILED','GLB native clock geometry exceeds conversion budget')
    return checks


def file_reference(doc,binary,meta,mappings,t):
    keys=meta['key_frames'];i=max(0,min(len(keys)-2,int(np.searchsorted(keys,t,side='right'))-1));alpha=(t-keys[i])/(keys[i+1]-keys[i]);rows=[]
    for row,parts in zip(meta['meshes'],mappings):
        out=np.empty((row['vertices'],3),np.float64)
        for p,ids,indices in parts:
            base=array(doc,binary,p['attributes']['POSITION']).astype(np.float64)
            a=base if i==0 else base+array(doc,binary,p['targets'][i-1]['POSITION']);b=base+array(doc,binary,p['targets'][i]['POSITION']);v=a*(1-alpha)+b*alpha;out[ids]=v[:,[0,2,1]]*np.array([1,-1,1])
        rows.append(out)
    return rows


def finish(path,spec,meta,time_ids,reference,job,started,operation,mappings):
    import_scene(path,meta,time_ids);checks=verify(meta,reference,job,'import',mappings);settings=spec['import'] if operation=='convert' else spec
    bpy.context.scene.name=settings['scene'];bpy.context.scene.frame_set(settings['frame']);candidate=exchange.save_candidate(job)
    bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False);verify(meta,reference,job,'reopen',mappings)
    report={'exchange_report_version':'1.0','operation':operation,'format':'GLB','mode':'ANIMATION','settings':spec,'checks':checks,'outputs':[{'file':str(path),'sha256':digest(path),'bytes':path.stat().st_size}] if operation=='convert' else [],'candidate':str(candidate),'candidate_sha256':digest(candidate),'source_saved':False,'reopen':'pass','seconds':time.monotonic()-started,
            'clock_transfer':{**meta,'conversion_position_tolerance':TOLERANCE,'max_position_error':max(r['indexed_vertex_error'] for r in checks),'self_contained_candidate':True},'losses':['Full evaluated morph geometry; original editable rig/skin/controller semantics require the native Blender workflow.','GLB seconds are relative to the declared time origin. Native-frame fidelity requires the explicit importer and NLA clock; other readers and continuous time are not certified.','Source vertex identity retained across UV splits and isolated points; source polygons triangulated. Materials omitted; UV naming/normals/shading not certified. Conversion error is separate from upstream sampling error.']}
    atomic_json(job/'exchange-report.json',report);return report


def convert(item,job):
    started=time.monotonic();path=Path(item['input']['file']);fps=item['import']['fps']
    if digest(path)!=item['input']['expected_sha256']:raise Failure('CONFLICT','USD cache SHA mismatch')
    stage,meshes,keys,first=native_mesh_cache.read_cache(path,fps)
    if not keys[0]<=item['import']['frame']<=keys[-1]:raise Failure('INVALID_REQUEST','Candidate frame outside clip')
    output=job/'export.glb';write(output,stage,meshes,keys,first,fps,digest(path));doc,binary,meta,time_ids,mapping=read(output,fps)
    reference=lambda t:[np.asarray(m.GetPointsAttr().Get(t),np.float64) for m in meshes]
    return finish(output,item,meta,time_ids,reference,job,started,'convert',mapping)


def import_(params,spec,job):
    started=time.monotonic();path=Path(params['file']);doc,binary,meta,time_ids,mappings=read(path,spec['fps'])
    if not meta['key_frames'][0]<=spec['frame']<=meta['key_frames'][-1]:raise Failure('INVALID_REQUEST','Candidate frame outside clip')
    reference=lambda t:file_reference(doc,binary,meta,mappings,t)
    return finish(path,spec,meta,time_ids,reference,job,started,'import',mappings)
