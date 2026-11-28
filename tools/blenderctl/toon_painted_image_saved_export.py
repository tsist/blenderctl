# SPDX-License-Identifier: GPL-3.0-or-later
"""Painted saved-disk export; trusted host owns implementation authorization.

The selected export-only request is a pure source/context declaration, not a
geometry-profile authorization. Its mature guarded_saved_source intake is kept
unchanged; only installed code selects paint.v1. No numerical producer runs.
"""
import hashlib
import json
import math
import sys
from pathlib import Path
import zipfile
import bpy
import numpy as np
from filesystem import FileGuard
from toon_selected_export_contract import normalize_request
from toon_image_saved_intake import guarded_saved_source, _need, _plain, _info, _write_json
from toon_image_backend.selected_scene_export import export_scene
from toon_image_backend.paint_attributes import metadata as paint_metadata


def _descriptor(path):
    with FileGuard(path) as guard:
        return dict(file=str(path),expected_sha256=guard.sha256(),bytes=_info(guard)['bytes'])


def _input_document(path, descriptor, checkpoint):
    with FileGuard(path) as guard:
        _need(_info(guard)['bytes']==descriptor['bytes'] and guard.sha256(checkpoint)==descriptor['expected_sha256'],
              'Paint input descriptor changed','CONFLICT')
        _need(descriptor['bytes']<=4*1024**2,'Paint input document budget','RESOURCE_LIMIT')
        data=b''.join(guard.chunks(checkpoint))
    document=json.loads(data.decode('utf-8'))
    _need(document.get('schema')=='toon-image-input.v2','Paint input v2 required','CONFLICT')
    return document


def _paint_npz(path,row,entry,checkpoint):
    """Verify all paint payloads without allocating another mesh/color array."""
    paint,keys=paint_metadata(entry)
    _need(row.get('paint')==paint,'Paint metadata differs between input and capture','CONFLICT')
    arrays=row['arrays']
    _need(keys<=set(arrays),'Paint payload descriptors missing','CONFLICT')
    with FileGuard(path) as guard:
        actual=dict(file=str(path),expected_sha256=guard.sha256(checkpoint),bytes=_info(guard)['bytes'])
        _need(actual['expected_sha256']==row['file']['sha256'] and actual['bytes']==row['file']['bytes'],
              'Paint NPZ descriptor differs','CONFLICT')
        with zipfile.ZipFile(path,'r') as archive:
            infos=archive.infolist()
            _need(len(infos)<=64 and len({m.filename for m in infos})==len(infos) and
                  {m.filename for m in infos}=={name+'.npy' for name in arrays},
                  'Paint NPZ member inventory differs','CONFLICT')
            _need(sum(m.file_size for m in infos)<=512*1024**2,'Paint NPZ decompressed budget','RESOURCE_LIMIT')
            for key in sorted(keys):
                checkpoint();declared=arrays[key]
                with archive.open(key+'.npy') as stream:
                    version=np.lib.format.read_magic(stream)
                    _need(version in ((1,0),(2,0)),'Paint NPY version differs','CONFLICT')
                    reader=np.lib.format.read_array_header_1_0 if version==(1,0) else np.lib.format.read_array_header_2_0
                    shape,fortran,dtype=reader(stream,max_header_size=65536)
                    _need(list(shape)==declared['shape'] and dtype.str==declared['dtype'] and
                          not dtype.hasobject and not fortran,'Paint NPY header differs','CONFLICT')
                    if key in ('edge_freestyle','face_freestyle'):
                        _need(dtype==np.dtype('?'),'Literal Boolean Freestyle payload required','CONFLICT')
                    elif key=='triangle_loop':
                        _need(len(shape)==2 and shape[1]==3 and dtype.kind in 'iu','Triangle-loop payload differs','CONFLICT')
                    else:
                        _need(len(shape)==2 and shape[1]==4 and dtype.str=='<f4','Color RGBA float32 payload required','CONFLICT')
                    remaining=math.prod(shape)*dtype.itemsize
                    _need(stream.tell()+remaining==archive.getinfo(key+'.npy').file_size,
                          'Paint NPY extent differs','CONFLICT')
                    payload=hashlib.sha256()
                    while remaining:
                        checkpoint();chunk=stream.read(min(1024**2,remaining))
                        _need(bool(chunk),'Truncated paint payload','CONFLICT')
                        payload.update(chunk);remaining-=len(chunk)
                    _need(payload.hexdigest()==declared['payload_sha256'],'Paint NPY payload SHA differs','CONFLICT')
    return actual,paint


def export_saved(params,output_root,*,checkpoint):
    p=normalize_request(params);checkpoint()
    internal=dict(file=p['file'],expected_sha256=p['expected_sha256'],resources=p['resources'],
        recipe=_plain(p['context']))
    with guarded_saved_source(internal,output_root,checkpoint) as session:
        root,scene,capture=session.root,session.scene,session.context
        geometry=root/'geometry'
        result=export_scene(geometry,object_names=list(p['context']['occluder_object_names']),
            camera_name=p['context']['camera_name'],allowed_roots=[root],scene=scene,
            limits=_plain(p['export_limits']),checkpoint=checkpoint,profile='paint.v1')
        _need(result['schema']=='toon-selected-scene-export.v2' and
              result['object_ids']==list(p['context']['occluder_object_names']),
              'Paint exporter domain differs','CONFLICT')
        export_descriptor=_descriptor(geometry/'EXPORT.json')
        _need(Path(result['input']['file'])==geometry/'toon-image-input.json',
              'Paint input fixed role differs','CONFLICT')
        input_descriptor=_descriptor(geometry/'toon-image-input.json')
        _need(input_descriptor['expected_sha256']==result['input']['sha256'] and
              input_descriptor['bytes']==result['input']['bytes'],'Paint input descriptor differs','CONFLICT')
        document=_input_document(geometry/'toon-image-input.json',input_descriptor,checkpoint)
        _need([r['id'] for r in document['objects']]==result['object_ids'] and
              document['camera']==result['camera'] and document['frame']==scene.frame_current,
              'Paint input context/order differs','CONFLICT')
        _need(len(result['objects'])==len(result['object_ids']),'Paint NPZ descriptor count differs','CONFLICT')
        npz=[]
        for index,(row,entry) in enumerate(zip(result['objects'],document['objects'])):
            checkpoint();path=geometry/f'export-{index:06d}.npz'
            _need(row['object_id']==result['object_ids'][index] and Path(row['file']['file'])==path and
                  Path(entry['file'])==path and entry['sha256']==row['file']['sha256'] and
                  entry['material_culling']==row['material_culling'] and
                  entry['matrix_world']==row['evaluated_matrix_world'],
                  'Paint NPZ fixed role/context differs','CONFLICT')
            actual,paint=_paint_npz(path,row,entry,checkpoint)
            npz.append(dict(object_id=row['object_id'],file=actual,arrays=row['arrays'],
                evaluated_matrix_world=row['evaluated_matrix_world'],material_culling=row['material_culling'],paint=paint))
        capture.update(schema='toon-painted-image-saved-capture.v1',source_mode='SAVED_DISK_V1',
            declared_request=_plain(p),scene_export=export_descriptor,scene_export_record=result,
            input=input_descriptor,npz=npz,object_ids=result['object_ids'],
            member_object_ids=list(p['context']['member_object_names']),camera=result['camera'],
            runtime=dict(python=sys.version,numpy=np.__version__,blender=bpy.app.version_string,
                blender_build=bpy.app.build_hash.decode('ascii')),
            source_saved=False,numerical_producer_run=False,parent_accepted=False,
            native_source_authenticated=False,implementation_pin_validation='host_responsibility_not_performed',
            dependency_scope='finite static files; audit may read undeclared paths before rejection; not preload sandbox',
            guard_scope='Explicit source/resources and ancestors held through capture and return boundary; no lease transfer',
            guard_security_unverified=['adversarial Windows races/volume aliases','arbitrary native/plugin IO','future lease transfer'])
        after=session.recheck_inputs()
        capture.update(source=session.source,resources=session.resources,verified_inputs_after=after,inputs_unchanged=True)
        capture_descriptor=_write_json(root/'capture.json',capture)
        checkpoint()
        return dict(schema='toon-painted-image-saved-export.v1',source=session.source,resources=session.resources,
            verified_inputs_after=after,capture=capture_descriptor,dependency_audit=session.dependency_audit,
            scene_export=export_descriptor,input=input_descriptor,npz=npz,camera=result['camera'],
            object_ids=result['object_ids'],member_object_ids=list(p['context']['member_object_names']),
            inputs_unchanged=True,source_saved=False,numerical_producer_run=False,parent_accepted=False,
            native_source_authenticated=False)
