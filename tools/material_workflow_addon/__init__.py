# SPDX-License-Identifier: GPL-3.0-or-later
"""Material Workflow: host-safe contract imports, lazy Blender registration."""
bl_info = {"name": "Material Workflow", "author": "Blender AI", "version": (0, 7, 0), "blender": (5, 2, 0), "location": "View3D > Sidebar > Material Workflow", "category": "Material"}

def register():
    from . import ui, ui_batch, ui_handoff
    ui.register()
    ui_batch.register()
    ui_handoff.register()

def unregister():
    from . import ui, ui_batch, ui_handoff
    ui_handoff.unregister()
    ui_batch.unregister()
    ui.unregister()
