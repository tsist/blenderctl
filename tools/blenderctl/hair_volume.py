# SPDX-License-Identifier: GPL-3.0-or-later
"""Owned native OpenVDB density sequences with exact file and source witnesses."""
import bpy,numpy as np,json,atexit
from contextlib import ExitStack
from pathlib import Path
from mathutils import Matrix,Vector
import hair_density,hair_shape,hair_dynamics,hair_voxels,scenes
from hair_volume_contract import normalize
from protocol import Failure,digest,atomic_json
from filesystem import FileGuard
KEY='blenderctl_curve_density_grid_v1'
_stack=ExitStack();atexit.register(_stack.close);_guards={};_grids={}
def fail(msg):raise Failure('UNSUPPORTED',msg)
def backend():
    if not bpy.app.build_options.openvdb:fail('This Blender build lacks OpenVDB')
    try:import openvdb
    except ImportError as e:raise Failure('UNSUPPORTED','Bundled Python OpenVDB backend is missing') from e
    return openvdb
def metadata(o):
    try:return json.loads(o[KEY])
    except Exception as e:raise Failure('UNSUPPORTED','Malformed volume metadata') from e
def source_check(source,op):
    if hair_density.KEY in source:spec=hair_density.metadata(source)['op']
    else:_,spec=hair_density.source_info(source)
    if (spec['frame_start'],spec['frame_end'])!=(op['frame_start'],op['frame_end']):fail('Volume requires the complete source witness window')
def guard_files(r):
    files=r['files'];op=r['op']
    if [x['frame'] for x in files]!=list(range(op['frame_start'],op['frame_end']+1)):fail('Volume file frame inventory changed')
    if len({x['file'] for x in files})!=len(files) or sum(x['bytes'] for x in files)>134217728:fail('Volume file inventory exceeds its bounds')
    for doc in files:
        p=Path(doc['file'])
        if not p.is_absolute() or p.suffix.lower()!='.vdb':fail('Volume files require absolute VDB paths')
        key=str(p).casefold()
        if key not in _guards:
            guard=_stack.enter_context(FileGuard(p));sha=guard.sha256();_guards[key]=(guard,sha)
        if _guards[key][1]!=doc['expected_sha256'] or p.stat().st_size!=doc['bytes']:raise Failure('CONFLICT','Managed volume file SHA/size changed')
    parent=Path(files[0]['file']).parent
    if any(Path(d['file']).parent!=parent for d in files) or {str(p) for p in parent.rglob('*') if p.is_file()}!={d['file'] for d in files}:raise Failure('CONFLICT','Managed volume directory has missing or extra files')
def read_field(doc,op):
    key=(doc['file'],doc['expected_sha256'])
    if key in _grids:return _grids[key]
    vdb=backend();grids,_=vdb.readAll(doc['file'])
    if len(grids)!=1 or not isinstance(grids[0],vdb.FloatGrid) or grids[0].name!='density' or grids[0].gridClass!=vdb.GridClass.FOG_VOLUME or grids[0].background!=0:fail('Volume requires exactly one FLOAT fog density grid with zero background')
    g=grids[0];lo,hi=g.evalActiveVoxelBoundingBox();shape=np.array(hi,dtype=np.int64)-lo+1
    if g.activeVoxelCount()<1 or int(np.prod(shape,dtype=np.float64))>op['max_voxels']:fail('VDB active bounds exceed voxel budget')
    # Check the actual affine map, not just a voxel-size label.
    for index in [(0,0,0),(1,0,0),(0,1,0),(0,0,1)]:
        if max(abs(a-b*op['voxel_size']) for a,b in zip(g.transform.indexToWorld(index),index))>1e-9:fail('VDB world transform changed')
    data=np.zeros(tuple(shape),np.float32);g.copyToArray(data,ijk=lo)
    if not np.isfinite(data).all() or float(data.min())<0 or float(data.max())>op['density']+1e-5:fail('Invalid VDB scalar values')
    result={'active_voxels':g.activeVoxelCount(),'index_min':list(lo),'index_max':list(hi),'sum':float(data.sum(dtype=np.float64)),'maximum':float(data.max()),'values_sha256':hair_shape.hashed(data),'voxel_size':op['voxel_size']};_grids[key]=result;return result
def create(op,ctx,job):
    normalize(op);vdb=backend();source=scenes.find(bpy.data.objects,op['source'],True);source_check(source,op);s=bpy.context.scene
    if (ctx['frame'],s.frame_start,s.frame_end)!=(op['frame_start'],op['frame_start'],op['frame_end']):fail('Volume context/scene must match the source frame window')
    for data in [bpy.data.objects,bpy.data.volumes,bpy.data.materials]:scenes.fresh(data,op['name'])
    collection=scenes.find(bpy.data.collections,op['collection'],True);folder=job/('volume-data-'+hair_dynamics.digest(op['name'])[:16]);folder.mkdir();files=[];before=s.frame_current;total_voxels=0
    try:
        for f in range(op['frame_start'],op['frame_end']+1):
            s.frame_set(f);e,pts,radii,sizes,ids,witness=hair_density.coordinates(source,bpy.context.evaluated_depsgraph_get());world=np.array([list(e.matrix_world@Vector(p)) for p in pts],np.float64);array,low,work=hair_voxels.rasterize(world,sizes,op['voxel_size'],op['kernel_radius'],op['density'],op['max_voxels']);total_voxels+=array.size
            if total_voxels>8000000:fail('Volume sequence exceeds eight million bounding voxel-frames')
            g=vdb.FloatGrid();g.name='density';g.gridClass=vdb.GridClass.FOG_VOLUME;g.transform=vdb.createLinearTransform(voxelSize=op['voxel_size']);g.copyFromArray(array,ijk=tuple(map(int,low)));file=folder/f'density_{f:04}.vdb';vdb.write(str(file),grids=[g])
            doc={'frame':f,'file':str(file),'expected_sha256':digest(file),'bytes':file.stat().st_size,'source_sha256':witness,'bounding_voxels':int(array.size),'voxel_segment_evaluations':work};doc['grid']=read_field(doc,op);files.append(doc)
            atomic_json(job/'hair-volume-progress.json',{'state':'generating','completed_frame':f,'frame_end':op['frame_end'],'files':len(files)})
    finally:s.frame_set(before)
    d=bpy.data.volumes.new(op['name']);d.filepath=files[0]['file'];d.is_sequence=True;d.frame_start=op['frame_start'];d.frame_duration=len(files);d.frame_offset=op['frame_start']-1;d.sequence_mode='CLIP';o=bpy.data.objects.new(op['name'],d);collection.objects.link(o)
    material=bpy.data.materials.new(op['name']);material.use_nodes=True;t=material.node_tree;t.nodes.clear();attr=t.nodes.new('ShaderNodeAttribute');attr.attribute_name='density';scatter=t.nodes.new('ShaderNodeVolumeScatter');scatter.inputs['Color'].default_value=(*op['color'],1);scatter.inputs['Anisotropy'].default_value=0;output=t.nodes.new('ShaderNodeOutputMaterial');t.links.new(attr.outputs['Fac'],scatter.inputs['Density']);t.links.new(scatter.outputs['Volume'],output.inputs['Volume']);d.materials.append(material)
    record={'op':op,'object':o.name,'source':source.name,'files':files,'field':'WORLD max over segments: density * max(0,1-distance/kernel_radius)^2; voxel centers at integer index * voxel_size','backend':'native OpenVDB FloatGrid + Blender Volume sequence; no fluid advection solver'};o[KEY]=json.dumps(record,sort_keys=True,separators=(',',':'))
    if op['hide_source']:source.hide_render=True
    bpy.context.view_layer.update();sample([before]);return record
def validate():
    rows=[];all_files=[]
    for o in bpy.context.scene.objects:
        if KEY not in o:continue
        r=metadata(o);op=normalize(r['op']);source=scenes.find(bpy.data.objects,r['source'],True);source_check(source,op);guard_files(r)
        if o.type!='VOLUME' or o.name!=r['object'] or o.library or o.override_library or o.data.library or o.data.users!=1 or o.data.animation_data or o.modifiers:fail('Managed volume ownership or modifier stack changed')
        hair_dynamics.identity(o,None);d=o.data
        if not d.is_sequence or (d.frame_start,d.frame_duration,d.frame_offset,d.sequence_mode)!=(op['frame_start'],len(r['files']),op['frame_start']-1,'CLIP') or str(Path(bpy.path.abspath(d.filepath)))!=r['files'][0]['file']:fail('Volume sequence path or frame mapping changed')
        rows.append({'object':o.name,'source':source.name,'adapter':op['adapter'],'frames':len(r['files']),'files_sha256':[f['expected_sha256'] for f in r['files']]})
        all_files.extend(r['files'])
    if len(rows)>8 or sum(d['bytes'] for d in all_files)>134217728 or sum(d['bounding_voxels'] for d in all_files)>8000000:fail('Scene volumes exceed eight objects / 128 MiB / eight million voxel-frames')
    return rows
def managed_objects():return {r['object'] for r in validate()}
def documents():
    validate();return [{'file':d['file'],'expected_sha256':d['expected_sha256'],'bytes':d['bytes']} for o in bpy.context.scene.objects if KEY in o for d in metadata(o)['files']]
def sample(frames):
    validate();s=bpy.context.scene;before=s.frame_current;rows=[]
    try:
        for f in frames:
            s.frame_set(f);dg=bpy.context.evaluated_depsgraph_get();objects=[]
            for o in s.objects:
                if KEY not in o:continue
                r=metadata(o);op=r['op']
                if not op['frame_start']<=f<=op['frame_end']:fail('Volume sample outside sequence witness window')
                doc=r['files'][f-op['frame_start']];*_,sha=hair_density.coordinates(bpy.data.objects[r['source']],dg)
                if sha!=doc['source_sha256']:raise Failure('CONFLICT','Volume source geometry changed; regenerate density sequence')
                grid=read_field(doc,op)
                if grid!=doc['grid']:raise Failure('VALIDATION_FAILED','VDB field changed after reopen')
                evaluated=o.evaluated_get(dg);evaluated.data.grids.load()
                if str(Path(evaluated.data.grids.frame_filepath))!=doc['file'] or evaluated.data.grids.error_message or [(g.name,g.data_type) for g in evaluated.data.grids]!=[('density','FLOAT')]:raise Failure('VALIDATION_FAILED','Blender Volume loaded a different/missing frame or grid')
                objects.append({'object':o.name,'file':doc['file'],'file_sha256':doc['expected_sha256'],'source_sha256':sha,**grid})
            rows.append({'frame':f,'volumes':objects})
    finally:s.frame_set(before)
    return rows
def verify_all():
    frames=sorted({d['frame'] for o in bpy.context.scene.objects if KEY in o for d in metadata(o)['files']})
    return sample(list(reversed(frames))) if frames else []
