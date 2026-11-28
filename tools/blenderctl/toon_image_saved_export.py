# SPDX-License-Identifier: GPL-3.0-or-later
"""Saved-disk intersection export over the common trusted intake context."""
import sys
import bpy
import numpy as np
from toon_image_contract import normalize_params
from toon_image_backend.intersection_scene_export import export_scene
from toon_image_saved_intake import (guarded_saved_source, _need, _local_path,
    _key, _info, _write_json, _plain, _tree_is_viewport, _capture_context)

def export_saved(params,output_root,*,checkpoint):
    """Guard explicit saved inputs, audit finite closure, reopen source and export.

    Guards are held until the return boundary, then released. This function does
    not transfer leases, validate implementation_sha256, or accept a parent job.
    """
    p=normalize_params(params);checkpoint()
    _need(not p['recipe']['budgets'].get('source'),
          'Existing exporter has no source-budget override API; nonempty overrides are unsupported')
    with guarded_saved_source(p,output_root,checkpoint) as session:
        root,scene,capture=session.root,session.scene,session.context
        before=[session.source,*session.resources]
        audit_descriptor=session.dependency_audit
        result=export_scene(root/'snapshot',object_names=list(p['recipe']['occluder_object_names']),
            camera_name=p['recipe']['camera_name'],allowed_roots=[root],scene=scene)
        capture.update(schema='toon-image-saved-capture.v1',source_mode='SAVED_DISK_V1',
            snapshot=result['snapshot'],camera_exact=result['camera_exact'],object_ids=result['object_ids'],
            material_observations=result['material_observations'],
            runtime=dict(python=sys.version,numpy=np.__version__,blender=bpy.app.version_string,
                         blender_build=bpy.app.build_hash.decode('ascii')),
            declared_recipe=_plain(p['recipe']),
            source_saved=False,numerical_producer_run=False,parent_accepted=False,
            implementation_pin_validation='host_responsibility_not_performed',
            dependency_scope='finite registered static files; audit may read undeclared paths before rejection; not a preload sandbox',
            guard_scope='explicit source/resources deny write/delete; all local lexical ancestors guarded against delete/reparse; held until return boundary',
            guard_security_unverified=['adversarial Windows races/volume aliases','arbitrary native/plugin IO','future lease transfer'],
            source_budget_mode='existing_writer_hard_caps_no_nonempty_overrides',
            pipeline_budgets='declared_for_later_numerical_stage_not_executed')
        after=session.recheck_inputs()
        capture['inputs_unchanged']=True
        capture.update(source=before[0],resources=before[1:],verified_inputs_after=after)
        capture_descriptor=_write_json(root/'capture.json',capture)
        checkpoint()
        return dict(schema='toon-image-saved-export.v1',source=before[0],resources=before[1:],
            verified_inputs_after=after,capture=capture_descriptor,dependency_audit=audit_descriptor,
            snapshot=result['snapshot'],camera_exact=result['camera_exact'],
            object_ids=result['object_ids'],member_object_ids=list(p['recipe']['member_object_names']),
            inputs_unchanged=True,parent_accepted=False,source_saved=False)
