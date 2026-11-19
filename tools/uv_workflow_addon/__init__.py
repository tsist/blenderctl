"""UV Workflow: shared native Blender core and a small editing workbench."""
bl_info = {"name": "UV Workflow", "author": "Blender AI", "version": (0, 8, 9),
           "blender": (5, 2, 0), "location": "View3D > Sidebar > UV Workflow",
           "description": "Protected UV candidates, independent checks and native editing", "category": "UV"}

def register():
    from . import ui
    ui.register()

def unregister():
    from . import ui
    ui.unregister()
