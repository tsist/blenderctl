"""Run with Blender --background --factory-startup --disable-autoexec --threads 1 --python FILE -- OUTPUT."""
from pathlib import Path
import sys
import bpy

target = Path(sys.argv[sys.argv.index('--') + 1]).resolve()
if target.exists():
    raise RuntimeError('Choose a new fixture path')
target.parent.mkdir(parents=True, exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.name = 'FixtureSource'
scene.render.threads_mode = 'FIXED'
scene.render.threads = 1
scene.render.resolution_x = 160
scene.render.resolution_y = 90
scene.render.resolution_percentage = 100
bpy.ops.wm.save_as_mainfile(filepath=str(target))
