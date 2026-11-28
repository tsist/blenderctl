"""Geometry Nodes Workflow. Importing the JSON contract is safe outside Blender."""
bl_info = {'name': '几何节点工作台', 'author': 'Blender AI',
           'version': (0, 2, 0), 'blender': (5, 2, 0),
           'location': '三维视口 > 侧栏 > G·几何',
           'description': '创建受管几何节点配方、编辑参数、烘焙缓存与现场交接',
           'category': 'Node'}


def register():
    from . import ui
    ui.register()


def unregister():
    from . import session, ui
    session.stop()
    ui.unregister()
