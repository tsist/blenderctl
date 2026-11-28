"""Compositing Workflow；合同可由普通 Python 导入。"""
bl_info = {'name': '合成工作台', 'author': 'Blender AI', 'version': (1, 0, 0),
           'blender': (5, 3, 0), 'description': '交互式深度补光、光组混光、遮罩调色与场景效果',
           'location': '3D 视口 > 侧栏 > F·合成',
           'category': 'Compositing'}


def register():
    from . import relight_preview_ui
    relight_preview_ui.register()


def unregister():
    from . import relight_preview_ui
    relight_preview_ui.unregister()
