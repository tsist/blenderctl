# SPDX-License-Identifier: GPL-3.0-or-later
"""Seal paint-v2 expectations from parent-held exports before numerical work.

Reuse the mature selected source/context/dependency guards. No numerical output
or receipt supplies Expected fields. Caller retains HeldFiles to parent terminal.
"""
from dataclasses import dataclass
import copy
import hashlib
import math
import os
from pathlib import Path
import re
import zipfile

import toon_image_prepared_run as legacy
from toon_selected_export_contract import normalize_request, EXPORT_HARD_LIMITS
from toon_selected_image_prepared_run import (_need, _fields, _uint, _digest,
    _native, _canonical, _same, _sha, _descriptor, _export_members,
    _source_inputs, _audit, _COMPATIBILITY, _ARRAYS)

_BACKEND = Path(__file__).resolve().parent.parent/'toon_image_backend'
_METADATA_BYTES = 8*1024**2
_FILE_BYTES = 512*1024**2
_INPUT_BYTES = 4*1024**2
AUTHORED = 'toon-painted-authored-image-request.v1'
INTERSECTION = 'toon-painted-intersection-image-request.v1'


@dataclass(frozen=True, slots=True)
class PreparedPaintedRun:
    request_path: str
    request_sha256: str
    expected_path: str
    expected_sha256: str
    expected_run: object
    host_context_sha256: str
    bundle_path: str


def _numeric_recipe(recipe, image, paint, mesh, intersection):
    """Validate exact current producer declarations without producing sources."""
    _need(type(recipe) is dict,'Native numeric recipe dictionary required','INVALID_REQUEST')
    authored=recipe.get('schema')==AUTHORED
    _need(authored or recipe.get('schema')==INTERSECTION,'Only authored or painted-intersection producer accepted','UNSUPPORTED')
    image.fields(recipe,('schema','groups','settings'),('budgets',) if authored else ('limits','pipeline_budgets'))
    paint.checked_groups(recipe['groups'])
    settings=recipe['settings']
    image.fields(settings,('include_open','feather_px','samples_per_axis','coverage_profile','post_filter_sigma',
        'source_types','force_marked_visible') if authored else
        ('feather_px','samples_per_axis','coverage_profile','postfilter_sigma','point_policy','self_intersection'))
    paint.scalar(settings['feather_px'],0,256,'Feather range')
    image.need(type(settings['samples_per_axis']) is int and settings['samples_per_axis'] in (1,2,4),'Sample rate')
    image.need(settings['coverage_profile'] in ('round_capsule.v1','extent_falloff.v1'),'Coverage profile')
    paint.scalar(settings['post_filter_sigma' if authored else 'postfilter_sigma'],0,4,'Postfilter range')
    if authored:
        image.need(type(settings['include_open']) is bool and type(settings['force_marked_visible']) is bool,'Literal authored booleans')
        kinds=settings['source_types']
        image.need(type(kinds) is list and 1<=len(kinds)<=3 and
            all(type(k) is str and k in ('selected','sharp','marked') for k in kinds) and len(set(kinds))==len(kinds),'Unique authored source types')
        budgets=recipe.get('budgets',{});image.fields(budgets,(),('binding','render'))
        binding=mesh.limits_checked(budgets.get('binding'))
        render=dict(image.RENDER_LIMITS)
        image.need(type(budgets.get('render',{})) is dict,'Render budget dictionary')
        for key,value in budgets.get('render',{}).items():
            image.need(key in render,'Render budget name')
            if key=='max_seconds':paint.scalar(value,0,render[key],'Render time budget')
            else:image.need(type(value) is int and 0<=value<=render[key],'Render budget tightens')
            render[key]=value
        return True,dict(binding=binding,render=render)
    for group in recipe['groups']:
        image.fields(group,('id','priority','object_ids','material_names','visible_enabled','hidden_enabled','brush'))
    image.need(type(settings['self_intersection']) is bool,'Literal self-intersection flag')
    image.need(settings['point_policy'] in ('disk','suppress'),'Point policy')
    limits=intersection.checked_limits(recipe.get('limits'))
    pipeline=intersection.image_pipeline.Budget(recipe.get('pipeline_budgets'))
    return False,dict(limits=limits,pipeline=pipeline.limits)


def prepare_exported_painted_run(*, run_root, export_request, numeric_recipe,
        capture_sha256, dependency_audit, host_context, expected_runtime,
        expected_backend_sources, expected_dependency_implementation, held):
    """Create held request/context/Expected only while bundle is absent."""
    _need(isinstance(held,legacy.HeldFiles),'Existing host HeldFiles required','INVALID_REQUEST')
    for value in (export_request,numeric_recipe,host_context,expected_runtime,expected_backend_sources):_native(value)
    export_request,numeric_recipe,host_context,expected_runtime,expected_backend_sources=(
        copy.deepcopy(v) for v in (export_request,numeric_recipe,host_context,expected_runtime,expected_backend_sources))
    normalized=legacy.plain(normalize_request(export_request));context=normalized['context']
    _same(context['member_object_names'],context['occluder_object_names'],'Paint backend requires all occluders as members; groups filter strokes')
    root=legacy.local_path(str(run_root));bundle=root/'bundle'
    _need(not os.path.lexists(bundle),'Numeric bundle exists before preparation')
    held.directory(root)
    _fields(host_context,('schema','run_root','nonce','export_request_sha256','numeric_recipe_sha256'),'Exact painted host context required')
    _same(host_context['schema'],'toon-painted-image-host-context.v1','Host context schema differs')
    _same(host_context['run_root'],str(root),'Host context root differs')
    _need(type(host_context['nonce']) is str and re.fullmatch('[0-9a-f]{32}',host_context['nonce']) is not None,'Host nonce differs')
    _same(_digest(host_context['export_request_sha256']),_sha(_canonical(export_request)),'Export request context SHA differs')
    _same(_digest(host_context['numeric_recipe_sha256']),_sha(_canonical(numeric_recipe)),'Numeric recipe context SHA differs')
    _fields(expected_runtime,('python','numpy','blender','blender_build'),'Complete independent target runtime required')
    _need(all(type(v) is str and 0<len(v)<=4096 for v in expected_runtime.values()),'Native runtime strings required')

    export=root/'export';geometry=export/'geometry'
    _export_members(export,held)
    capture=held.document(export/'capture.json',_digest(capture_sha256),_METADATA_BYTES)
    _need(type(capture) is dict,'Capture object required')
    for key,value in dict(schema='toon-painted-image-saved-capture.v1',source_mode='SAVED_DISK_V1',
            declared_request=normalized,inputs_unchanged=True,source_saved=False,numerical_producer_run=False,
            parent_accepted=False,native_source_authenticated=False,runtime=expected_runtime).items():
        _same(capture.get(key),value,'Capture '+key+' differs')
    for key in ('scene','view_layer','frame','camera_name','occluder_object_names','member_object_names'):
        _same(capture.get('object_names' if key=='occluder_object_names' else key),context[key],'Capture context '+key+' differs')
    _same(capture.get('object_ids'),context['occluder_object_names'],'Capture object order differs')
    _same(capture.get('member_object_ids'),context['member_object_names'],'Capture members differ')
    _same(capture.get('camera_type'),'ORTHO','Capture camera type differs')
    wanted,total=_source_inputs(capture,normalized,held)
    audit_row=_descriptor(dependency_audit,export/'resources.json',held)
    audit=held.document(export/'resources.json',audit_row['sha256'],_METADATA_BYTES)
    _audit(audit,wanted,total,context,expected_runtime,expected_dependency_implementation)

    _descriptor(capture.get('scene_export'),geometry/'EXPORT.json',held)
    exported=held.document(geometry/'EXPORT.json',capture['scene_export']['expected_sha256'],_METADATA_BYTES)
    _same(exported,capture.get('scene_export_record'),'EXPORT differs from capture')
    for key,value in dict(schema='toon-selected-scene-export.v2',scene=context['scene'],view_layer=context['view_layer'],
            frame=context['frame'],object_ids=context['occluder_object_names'],source_saved=False,numerical_producer_run=False,
            parent_accepted=False,native_source_authenticated=False,concurrent_writer_safe=False,
            runtime={k:expected_runtime[k] for k in ('blender','blender_build')}).items():
        _same(exported.get(key),value,'EXPORT '+key+' differs')
    _same(exported.get('depsgraph_mode'),capture.get('graph_mode'),'Evaluated graph mode differs')
    limits=dict(EXPORT_HARD_LIMITS);limits.update(normalized['export_limits'])
    _same(exported.get('limits'),limits,'Export limits differ')
    _fields(exported.get('counts'),('max_vertices','max_edges','max_triangles','max_loops','max_polygons','max_array_bytes'),'Complete exporter counts required')
    for key,value in exported['counts'].items():
        _uint(value,'Native exporter count');_need(value<=limits[key],'Export declared count budget','RESOURCE_LIMIT')
    _uint(exported.get('charged_input_bytes'),'Native input byte charge')
    _need(exported['charged_input_bytes']<=limits['max_input_bytes'],'Export declared input byte budget','RESOURCE_LIMIT')
    input_path=geometry/'toon-image-input.json'
    input_row=_descriptor(capture.get('input'),input_path,held,maximum=_INPUT_BYTES)
    _descriptor(exported.get('input'),input_path,held,low=True,maximum=_INPUT_BYTES)
    _same(capture['input']['expected_sha256'],exported['input']['sha256'],'Input SHA declarations differ')
    document=held.document(input_path,input_row['sha256'],_INPUT_BYTES)
    _fields(document,('schema','frame','objects','camera','settings'),'Exact paint input fields required')
    _same(document['schema'],'toon-image-input.v2','Paint input v2 required')
    _same(document['frame'],context['frame'],'Input frame differs')
    _same(document['settings'],_COMPATIBILITY,'Fixed loader compatibility differs')
    _same(document['camera'],exported.get('camera'),'Input/EXPORT camera differs')
    _same(document['camera'],capture.get('camera'),'Input/capture camera differs')
    camera=document['camera'];_fields(camera,('kind','width','height','world','projection','clip_start','clip_end'),'Exact camera fields required')
    for key in ('width','height'):
        _uint(camera[key],'Native image dimension');_same(camera[key],capture.get(key),'Capture dimension differs')
        _need(0<camera[key]<=limits['max_dimension'],'Export dimension budget','RESOURCE_LIMIT')
    _need(camera['width']*camera['height']<=limits['max_pixels'],'Export pixel budget','RESOURCE_LIMIT')
    objects,captures,entries=exported.get('objects'),capture.get('npz'),document['objects']
    count=len(context['occluder_object_names'])
    _need(type(objects) is list and type(captures) is list and type(entries) is list and len(objects)==len(captures)==len(entries)==count,'Complete ordered object records required')
    members={'EXPORT.json','toon-image-input.json'};npz_rows=[]
    for ordinal,name in enumerate(context['occluder_object_names']):
        low,high,entry=objects[ordinal],captures[ordinal],entries[ordinal]
        for row in (low,high):_fields(row,('object_id','file','arrays','evaluated_matrix_world','material_culling','paint'),'Exact paint capture object required')
        _fields(entry,('id','name','file','sha256','matrix_world','material_culling','paint'),'Exact paint input object required')
        _same([low['object_id'],high['object_id'],entry['id'],entry['name']],[name]*4,'Object identity/order differs')
        path=geometry/('export-%06d.npz'%ordinal);members.add(path.name)
        row=_descriptor(low['file'],path,held,low=True,maximum=_FILE_BYTES)
        _descriptor(high['file'],path,held,maximum=_FILE_BYTES)
        _same(entry['file'],str(path),'NPZ path escaped fixed role');_same(entry['sha256'],row['sha256'],'NPZ digest differs')
        for key,entry_key in (('arrays',None),('evaluated_matrix_world','matrix_world'),('material_culling','material_culling'),('paint','paint')):
            _same(low[key],high[key],'Capture/EXPORT '+key+' differs')
            if entry_key is not None:_same(entry[entry_key],low[key],'Input '+key+' differs')
        _need(type(low['arrays']) is dict,'Array declaration object required')
        for declaration in low['arrays'].values():
            _fields(declaration,('dtype','shape','payload_sha256'),'Exact array declaration required');_digest(declaration['payload_sha256'])
            _need(type(declaration['dtype']) is str and type(declaration['shape']) is list and len(declaration['shape']) in (1,2),'Native array dtype/shape required')
            for dimension in declaration['shape']:_uint(dimension,'Array extent')
        npz_rows.append(row)
    _same(held.flat_names(geometry),members,'Exact geometry members required')

    _need(type(expected_backend_sources) is dict and 0<len(expected_backend_sources)<=4096,'Independent backend source inventory required')
    for name,pin in expected_backend_sources.items():
        _need(type(name) is str and name.endswith('.py') and '\\' not in name and ':' not in name and
            all(p not in ('','.','..') for p in name.split('/')),'Canonical relative backend source required')
        _digest(pin)
    product=_BACKEND;names=held.source_names(product)
    _same(names,sorted(expected_backend_sources),'Backend inventory differs')
    code_pins={}
    for name in names:
        row=held.file(product/name,expected_backend_sources[name],maximum=_METADATA_BYTES)
        code_pins[str(product/name)]=dict(bytes=row['bytes'],sha256=row['sha256'])
    # No numerical module or array loader is imported before all approved code
    # and every referenced input/NPZ are held under fixed paths above.
    from toon_image_backend import painted_image_runner as authored, painted_intersection_runner as intersection
    from toon_image_backend import chain_image_core as image, paint_style as paint, mesh_source_core as mesh
    from toon_image_backend import contract, paint_attributes, selected_scene_export, painted_image_receipt_reader as reader
    import numpy as np
    for module,name in ((authored,'painted_image_runner.py'),(intersection,'painted_intersection_runner.py'),
            (image,'chain_image_core.py'),(paint,'paint_style.py'),(mesh,'mesh_source_core.py'),
            (contract,'contract.py'),(paint_attributes,'paint_attributes.py'),(selected_scene_export,'selected_scene_export.py'),
            (reader,'painted_image_receipt_reader.py')):
        _need(Path(module.__file__).resolve()==product/name,'Imported backend origin differs')
    is_authored,effective=_numeric_recipe(numeric_recipe,image,paint,mesh,intersection)
    code_identity=code_pins if is_authored else {k:v['sha256'] for k,v in code_pins.items()}
    _same(image.code_pins(authored.__file__) if is_authored else intersection.source_code_pins(),code_identity,'Actual producer code closure differs')
    request=copy.deepcopy(numeric_recipe)
    request['input']=dict(input_path=str(input_path),input_sha256=input_row['sha256'])
    data=image.canonical(request)
    _need(len(data)<=image.MAX_REQUEST_BYTES,'Request byte budget','RESOURCE_LIMIT')
    _same(image.parse_request(data),request,'Serialized request differs')
    held.checkpoint();loaded=contract.load_input(input_path);held.checkpoint()
    _same(loaded['document'],document,'Actual loaded input document differs')
    hashes={str(input_path):input_row['sha256'],**{r['file']:r['sha256'] for r in npz_rows}}
    _same(loaded['input_hashes'],hashes,'Complete held input hashes differ')
    _need(all(set(g['object_ids'])<=set(context['occluder_object_names']) for g in request['groups']),'Unknown group object ID')
    counts=dict(max_vertices=0,max_edges=0,max_triangles=0,max_loops=0,max_polygons=0,max_array_bytes=0)
    charge=input_row['bytes'];largest_members=0
    dtypes={key:np.dtype(dtype).str for key,_,_,_,dtype in selected_scene_export.ARRAY_SPECS+selected_scene_export.PAINT_ARRAY_SPECS}
    dtypes['matrix_world']='<f8'
    for obj,captured,row,entry in zip(loaded['objects'],objects,npz_rows,document['objects']):
        held.checkpoint();meta,paint_keys=paint_attributes.metadata(entry)
        _same(obj['paint'],captured['paint'],'Actual paint metadata differs')
        wanted_arrays=_ARRAYS|paint_keys
        largest_members=max(largest_members,len(wanted_arrays))
        _same(set(captured['arrays']),wanted_arrays,'Complete geometry and paint array descriptors required')
        arrays=dict(obj['arrays'])
        with np.load(row['file'],allow_pickle=False) as archive:
            _same(set(archive.files),wanted_arrays,'Actual NPZ member inventory differs')
            arrays['matrix_world']=np.array(archive['matrix_world'],copy=True)
        _need(np.array_equal(arrays['matrix_world'],obj['matrix_world']),'Actual NPZ matrix differs from input')
        with zipfile.ZipFile(row['file']) as archive:
            charge+=row['bytes']+sum(info.file_size for info in archive.infolist())
        for key,array in arrays.items():
            declaration=captured['arrays'][key]
            _same(list(array.shape),declaration['shape'],'Actual array shape differs')
            _same(array.dtype.str,declaration['dtype'],'Actual array dtype differs')
            _same(array.dtype.str,dtypes.get(key,'<f4'),'Actual dtype differs from installed exporter profile')
            _need(array.flags.c_contiguous,'Exported array must be C-contiguous')
            _same(hashlib.sha256(memoryview(array).cast('B')).hexdigest(),declaration['payload_sha256'],'Actual array payload differs')
            counts['max_array_bytes']+=array.nbytes
        for key,target in (('co','max_vertices'),('edges','max_edges'),('triangles','max_triangles'),('loop_vertex','max_loops'),('face_start','max_polygons')):
            counts[target]+=len(arrays[key])
    _same(counts,exported['counts'],'Actual export count/array bytes differ')
    _same(charge,exported['charged_input_bytes'],'Actual export byte charge differs')
    _need(charge<=limits['max_input_bytes'] and all(v<=limits[k] for k,v in counts.items()),'Actual export budget','RESOURCE_LIMIT')
    pixels=camera['width']*camera['height']
    if is_authored:
        binding=effective['binding']
        _need(count<=binding['max_objects'] and charge<=binding['max_input_bytes'] and
            counts['max_vertices']<=binding['max_vertices'] and counts['max_edges']<=binding['max_edges'] and
            counts['max_triangles']<=binding['max_triangles'] and largest_members<=binding['max_npz_members'] and
            pixels<=effective['render']['max_pixels'],'Authored input/pixel budget','RESOURCE_LIMIT')
    else:
        bound=effective['limits']
        _need(count<=bound['max_objects'] and counts['max_vertices']<=min(bound['max_vertices'],intersection.paged_snapshot.HARD_LIMITS['max_vertices']) and
            counts['max_triangles']<=bound['max_triangles'] and pixels<=min(bound['max_pixels'],effective['pipeline']['max_pixels']),
            'Intersection input/pixel budget','RESOURCE_LIMIT')
    request_path=root/'backend-request.json'
    sealed=dict(schema='toon-painted-image-numeric-context.v1',parent=host_context,
        export_request_sha256=host_context['export_request_sha256'],numeric_recipe_sha256=host_context['numeric_recipe_sha256'],
        capture_sha256=capture_sha256,dependency_audit_sha256=audit_row['sha256'],
        dependency_implementation_sha256=expected_dependency_implementation,
        scene_export_sha256=capture['scene_export']['expected_sha256'],input_sha256=input_row['sha256'],
        backend_request_sha256=_sha(data),runtime=expected_runtime,backend_sources=code_identity)
    sealed_data=_canonical(sealed);context_sha=_sha(sealed_data)
    expected=reader.normalize_expected_painted_run(host_context_sha256=context_sha,
        receipt_schema=reader.SELECTED if is_authored else reader.INTERSECTION,bundle_path=str(bundle),
        request_path=str(request_path),request_utf8=data,source_input_path=str(input_path),source_input_sha256=input_row['sha256'],
        input_hashes=hashes,code_identity=code_identity,image_shape=(camera['height'],camera['width'],4),checkpoint=held.checkpoint)
    from toon_painted_image_evidence import expected_document
    serialized=expected_document(expected,held.checkpoint);expected_data=_canonical(serialized)
    _need(len(sealed_data)<=_METADATA_BYTES and len(expected_data)<=_METADATA_BYTES,'Prepared metadata budget','RESOURCE_LIMIT')
    del loaded
    for path,raw in ((root/'NUMERIC-CONTEXT.json',sealed_data),(request_path,data),(root/'EXPECTED.json',expected_data)):
        held.checkpoint()
        with path.open('xb') as stream:stream.write(raw)
        held.file(path,_sha(raw),maximum=_METADATA_BYTES,size=len(raw))
    _same(held.document(root/'EXPECTED.json',_sha(expected_data),_METADATA_BYTES),serialized,'Held Expected encoding differs')
    held.recheck();_export_members(export,held)
    _same(held.flat_names(geometry),members,'Geometry inventory changed during prepare')
    _same(held.source_names(product),names,'Backend inventory changed during prepare')
    _need(not os.path.lexists(bundle),'Numeric bundle appeared during prepare')
    return PreparedPaintedRun(str(request_path),_sha(data),str(root/'EXPECTED.json'),_sha(expected_data),expected,context_sha,str(bundle))
