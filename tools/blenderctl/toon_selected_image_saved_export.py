# SPDX-License-Identifier: GPL-3.0-or-later
"""Selected saved-disk export; trusted host owns implementation authorization."""
import sys
from pathlib import Path
import bpy
import numpy as np
from filesystem import FileGuard
from toon_selected_export_contract import normalize_request
from toon_image_saved_intake import guarded_saved_source, _need, _plain, _info, _write_json
from toon_image_backend.selected_scene_export import export_scene


def _descriptor(path):
    with FileGuard(path) as guard:
        return dict(file=str(path),expected_sha256=guard.sha256(),bytes=_info(guard)['bytes'])


def export_saved(params,output_root,*,checkpoint):
    p=normalize_request(params);checkpoint()
    # Only trusted code maps the export-only context into the mature intake's
    # internal recipe role. Request fields never select a module or callback.
    internal=dict(file=p['file'],expected_sha256=p['expected_sha256'],resources=p['resources'],
        recipe=_plain(p['context']))
    with guarded_saved_source(internal,output_root,checkpoint) as session:
        root,scene,capture=session.root,session.scene,session.context
        geometry=root/'geometry'
        result=export_scene(geometry,object_names=list(p['context']['occluder_object_names']),
            camera_name=p['context']['camera_name'],allowed_roots=[root],scene=scene,
            limits=_plain(p['export_limits']),checkpoint=checkpoint)
        _need(result['schema']=='toon-selected-scene-export.v1' and
              result['object_ids']==list(p['context']['occluder_object_names']),
              'Selected exporter domain differs','CONFLICT')
        export_descriptor=_descriptor(geometry/'EXPORT.json')
        _need(Path(result['input']['file'])==geometry/'toon-image-input.json',
              'Selected input fixed role differs','CONFLICT')
        input_descriptor=_descriptor(geometry/'toon-image-input.json')
        _need(input_descriptor['expected_sha256']==result['input']['sha256'] and
              input_descriptor['bytes']==result['input']['bytes'],'Selected input descriptor differs','CONFLICT')
        _need(len(result['objects'])==len(result['object_ids']),'Selected NPZ descriptor count differs','CONFLICT')
        npz=[]
        for index,row in enumerate(result['objects']):
            checkpoint();path=geometry/f'export-{index:06d}.npz'
            _need(row['object_id']==result['object_ids'][index] and Path(row['file']['file'])==path,
                  'Selected NPZ fixed role differs','CONFLICT')
            actual=_descriptor(path)
            _need(actual['expected_sha256']==row['file']['sha256'] and actual['bytes']==row['file']['bytes'],
                  'Selected NPZ descriptor differs','CONFLICT')
            npz.append(dict(object_id=row['object_id'],file=actual,arrays=row['arrays'],
                evaluated_matrix_world=row['evaluated_matrix_world'],material_culling=row['material_culling']))
        capture.update(schema='toon-selected-image-saved-capture.v1',source_mode='SAVED_DISK_V1',
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
        return dict(schema='toon-selected-image-saved-export.v1',source=session.source,resources=session.resources,
            verified_inputs_after=after,capture=capture_descriptor,dependency_audit=session.dependency_audit,
            scene_export=export_descriptor,input=input_descriptor,npz=npz,camera=result['camera'],
            object_ids=result['object_ids'],member_object_ids=list(p['context']['member_object_names']),
            inputs_unchanged=True,source_saved=False,numerical_producer_run=False,parent_accepted=False,
            native_source_authenticated=False)
