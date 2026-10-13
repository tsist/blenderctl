"""Editable native Video Sequencer work units. No global state on import."""
bl_info = {
    'name': 'Video Editing Workflow', 'author': 'Blender AI',
    'version': (0, 1, 1), 'blender': (5, 2, 0),
    'location': 'Video Sequencer > Sidebar > Workflow',
    'description': 'Inspect and edit managed video timelines with the public CLI',
    'category': 'Sequencer',
}


def register():
    from . import ui
    ui.register()


def unregister():
    from . import ui
    ui.unregister()
