# SPDX-License-Identifier: GPL-3.0-or-later
"""Atomic assignment boundary shared by batch CLI and the add-on.

This module does not schedule dependencies, save files or create preview rigs.
"""
import copy
from .contract import WorkflowError
from .batch_contract import assignment_manifest
from . import core, core_v2

def _face_checkpoint(index):
    """Private fault-injection seam, inaccessible through manifest parameters."""
    pass

def _assign_faces(obj, faces, slot):
    changed=0
    for index in faces:
        polygon=obj.data.polygons[index]
        if polygon.material_index!=slot:
            polygon.material_index=slot; changed+=1
        _face_checkpoint(index)
    obj.data.update()
    return changed

def apply_assignment(batch, assignment, resource_paths=None):
    """Apply one shader and its explicitly named face indices atomically.

    faces=None preserves all polygon indices; a list writes only those entries.
    Mesh sharing is allowed for OBJECT slot overrides but forbids face edits.
    """
    import bpy
    manifest=assignment_manifest(batch,assignment)
    scene=bpy.data.scenes.get(manifest['context']['scene'])
    obj=scene.objects.get(manifest['target']['object']) if scene else None
    faces=assignment.get('faces')
    if faces is not None:
        if not isinstance(faces,list) or not faces or any(isinstance(x,bool) or not isinstance(x,int) for x in faces) or len(set(faces))!=len(faces):
            raise WorkflowError('invalid_faces','Faces must be a nonempty list of unique integer indices')
        if obj is None or obj.type!='MESH': raise WorkflowError('invalid_target','Face assignment needs an explicit mesh')
        if obj.mode!='OBJECT' or obj.library or obj.override_library or obj.data.library or obj.data.override_library:
            raise WorkflowError('protected_target','Face assignment requires a local non-override Object Mode mesh')
        if obj.data.users>1: raise WorkflowError('shared_faces','Face edits cannot change shared mesh data')
        if any(index<0 or index>=len(obj.data.polygons) for index in faces):
            raise WorkflowError('invalid_faces','Face index lies outside the mesh polygon range')
    if obj is None or obj.type!='MESH':
        # Reuse the core's full context and target diagnostics.
        return core.apply_material(manifest,resource_paths)
    before_slots=[(slot.link,slot.material) for slot in obj.material_slots]
    # Slot pop can normalize even unnamed invalid polygon slot indices; a failed
    # assignment must restore the entire mesh's original indices, not just faces.
    before_faces={p.index:p.material_index for p in obj.data.polygons}
    before_images=set(bpy.data.images);before_materials=set(bpy.data.materials)
    mid=manifest['material']['id']
    matches=[m for m in bpy.data.materials if m.get(core.KEY)==mid]
    original=matches[0] if len(matches)==1 else None
    backup=original.copy() if original else None
    if backup and core.KEY in backup: del backup[core.KEY]
    core_done=False
    try:
        result=core.apply_material(manifest,resource_paths); core_done=True
        result['faces_changed']=_assign_faces(obj,faces,manifest['target']['material_slot']) if faces is not None else 0
        result['assignment_id']=assignment['id']
        return result
    except Exception as error:
        if core_done or not isinstance(error,WorkflowError):
            try:
                if original and backup:
                    backup[core.KEY]=mid;core_v2._restore_managed(original,backup)
                while len(obj.material_slots)>len(before_slots): obj.data.materials.pop(index=len(obj.material_slots)-1)
                for index,(link,material) in enumerate(before_slots):
                    obj.material_slots[index].link=link;obj.material_slots[index].material=material
                for index,slot in before_faces.items():
                    if obj.data.polygons[index].material_index!=slot: obj.data.polygons[index].material_index=slot
                obj.data.update()
                for material in list(bpy.data.materials):
                    if material not in before_materials and material!=backup and material.get(core.KEY)==mid:
                        consumers=[other.name for other in bpy.data.objects if any(s.material==material for s in other.material_slots)]
                        if consumers: raise RuntimeError('Rollback candidate gained external consumers: '+str(consumers))
                        # Popping an OBJECT slot can retain a hidden user count;
                        # this is exclusively our new candidate, with no slots.
                        bpy.data.materials.remove(material)
                if before_slots!=[(s.link,s.material) for s in obj.material_slots] or any(obj.data.polygons[index].material_index!=value for index,value in before_faces.items()):
                    raise RuntimeError('Rollback verification differs from the original assignment')
            except Exception as rollback_error:
                failure=WorkflowError('batch_rollback_failed','Assignment '+assignment['id']+' rollback failed: '+str(rollback_error))
                failure.rollback_ok=False
                raise failure from error
        failure=error if isinstance(error,WorkflowError) else WorkflowError('assignment_apply_failed','Assignment '+assignment['id']+' failed: '+str(error))
        failure.rollback_ok=True
        if failure is error: raise failure
        raise failure from error
    finally:
        if backup: bpy.data.materials.remove(backup)
        for image in list(bpy.data.images):
            if image not in before_images and image.users==0 and image.get(core.KEY): bpy.data.images.remove(image)
