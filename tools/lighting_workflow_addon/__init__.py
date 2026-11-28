# SPDX-License-Identifier: GPL-3.0-or-later
"""L·灯光 — Commercial Lighting Workbench for Blender 5.3+."""

bl_info = {
    "name": "L·灯光 · 商业灯光工作台",
    "author": "Blender AI",
    "version": (1, 1, 0),
    "blender": (5, 3, 0),
    "location": "3D 视口 > 侧栏 > L·灯光",
    "description": "受管影棚布光、物理色温/IES/Gobo、Light Groups、体积雾与 RTX GPU 自动测光求解",
    "category": "Lighting",
}


def register():
    from . import ui
    ui.register()


def unregister():
    from . import ui
    ui.unregister()
