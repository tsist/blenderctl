"""Lighting Workflow: deterministic lighting core and optional local panel."""
bl_info = {'name': 'Lighting Workflow', 'author': 'Blender AI', 'version': (0, 2, 1),
           'blender': (5, 2, 0), 'location': 'View3D > Sidebar > Lighting Workflow',
           'description': 'Managed lighting rigs, explicit edits and recoverable candidates', 'category': 'Lighting'}


def register():
    from . import ui
    ui.register()


def unregister():
    from . import ui
    ui.unregister()
