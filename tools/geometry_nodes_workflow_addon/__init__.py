"""Geometry Nodes Workflow. Importing the JSON contract is safe outside Blender."""
bl_info = {'name': 'Geometry Nodes Workflow', 'author': 'Blender AI',
           'version': (0, 2, 0), 'blender': (5, 2, 0),
           'location': 'View3D > Sidebar > Geometry Workflow',
           'description': 'Managed geometry cores, CLI jobs and explicit live-session handoff',
           'category': 'Node'}


def register():
    from . import ui
    ui.register()


def unregister():
    from . import session, ui
    session.stop()
    ui.unregister()
