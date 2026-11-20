"""Build local fixture, an animated one-bone armature, and a linked negative."""
from pathlib import Path
import sys
import bpy

root = Path(sys.argv[-1])
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.object.armature_add()
rig = bpy.context.object
rig.name = "TestRig"
bone = rig.pose.bones[0]
for frame, offset in [(1, 0), (11, 3)]:
    bone.location.x = offset
    bone.keyframe_insert(data_path="location", frame=frame)
bpy.context.scene.frame_set(1)
bpy.ops.wm.save_as_mainfile(filepath=str(root / "clean.blend"), compress=False)
bpy.ops.wm.read_factory_settings(use_empty=True)
with bpy.data.libraries.load(str(root / "clean.blend"), link=True) as (_, linked):
    linked.objects = ["TestRig"]
bpy.context.scene.collection.objects.link(linked.objects[0])
bpy.ops.wm.save_as_mainfile(filepath=str(root / "linked.blend"), compress=False)
