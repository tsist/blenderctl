"""Create only synthetic saved-file input and refusal fixtures, with no media output."""
import bpy
from pathlib import Path
import sys

root = Path(sys.argv[-1])
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.name = 'Fixture'
scene.view_layers[0].name = 'Primary'
scene.frame_set(3)
collection = bpy.data.collections.new('Fixture Collection')
scene.collection.children.link(collection)
mesh = bpy.data.meshes.new('Triangle Mesh')
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
triangle = bpy.data.objects.new('Triangle', mesh)
collection.objects.link(triangle)
triangle.location = (2, 3, 4)
camera = bpy.data.objects.new('Fixture Camera', bpy.data.cameras.new('Fixture Camera'))
collection.objects.link(camera)
scene.camera = camera
light = bpy.data.objects.new('Fixture Light', bpy.data.lights.new('Fixture Light', 'AREA'))
collection.objects.link(light)
instance = bpy.data.objects.new('Instance', None)
instanced = bpy.data.collections.new('Instanced Source')
instance.instance_type = 'COLLECTION'
instance.instance_collection = instanced
scene.collection.objects.link(instance)
bpy.ops.wm.save_as_mainfile(filepath=str(root / 'clean.blend'), compress=False)
mod = triangle.modifiers.new('subsurf', 'SUBSURF')
bpy.ops.wm.save_as_mainfile(filepath=str(root / 'modifier.blend'), compress=False)
triangle.modifiers.remove(mod)
image = bpy.data.images.new('External Source', 2, 2)
image.source = 'FILE'
image.filepath = str(root / 'does-not-exist.png')
image.use_fake_user = True
bpy.ops.wm.save_as_mainfile(filepath=str(root / 'external.blend'), compress=False)
bpy.data.images.remove(image)
with bpy.data.libraries.load(str(root / 'clean.blend'), link=True) as (_, data):
    data.objects = ['Triangle']
scene.collection.objects.link(data.objects[0])
bpy.ops.wm.save_as_mainfile(filepath=str(root / 'linked.blend'), compress=False)
