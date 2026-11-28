"""Editable native Video Sequencer work units. No global state on import."""
bl_info = {
    'name': '视频剪辑工作台', 'author': 'Blender AI',
    'version': (1, 0, 0), 'blender': (5, 3, 0),
    'location': '视频序列编辑器侧栏',
    'description': '拼装镜头、原位换片、编排字幕和导出成片',
    'category': 'Sequencer',
}


def register():
    from . import ui
    ui.register()


def unregister():
    from . import ui
    ui.unregister()
