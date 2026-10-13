"""Compositing Workflow；合同可由普通 Python 导入。"""
bl_info = {'name': 'Compositing Workflow', 'author': 'Blender AI', 'version': (0, 4, 0),
           'blender': (5, 2, 1), 'description': 'Managed native compositor groups and explicit conflict adoption',
           'category': 'Compositing'}


def register():
    # M1 由公共 CLI 调用 core；原生节点编辑器接管，不另设作业系统或偏好。
    pass


def unregister():
    pass
