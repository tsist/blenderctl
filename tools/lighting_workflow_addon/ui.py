# SPDX-License-Identifier: GPL-3.0-or-later
"""L·灯光 — Commercial Lighting Workbench GUI (Benchmark #1 for Blender AI 1.0).

Compact direct-access skeleton backed by tools/ui_kit (_ui_kit):
- Independent 3D Viewport sidebar tab: "L·灯光"
- Monogrammed Solar Gold (#FACC15) SVG brand & action icons via bpy.utils.previews
- Outliner Collection color-tag sync (COLOR_03) & Light shader node tinting
- Full 1.1.0 artist-closed-loop controls: 1-click studio presets, Kelvin/IES/Gobo,
  Light Groups, bone-following rigs, World/Sky/HDRI alignment, Volume fog,
  non-blocking RTX GPU goal solver with layout.progress() + top-right GPU HUD,
  and template/snapshot/restore management.
- Preserves 100% backward compatibility with lighting_panel_handoff CLI symbols.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
import uuid

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

from . import commercial, core, presets, templates
from ._ui_kit import (
    BackgroundJobRunner,
    HUDManager,
    IconPack,
    SidebarDock,
    apply_collection_theme,
    apply_node_theme,
    draw_brand_header,
    ensure_object_mode,
    get_theme,
    primary_button,
    progress_card,
    prop_column,
    section_panel,
    status_banner,
)

DOMAIN = "lighting"
THEME = get_theme(DOMAIN)
UI_PROPERTY = "lighting_workflow_ui_v1"

_ICONS = IconPack(DOMAIN)
_RUNNER = BackgroundJobRunner(DOMAIN)
_TEMP_SOLVE_DIR: Path | None = None

_ROLE_LABELS = {
    "key": "主光",
    "fill": "辅光",
    "rim": "轮廓光",
    "accent": "点缀光",
    "sun": "太阳光",
    "ambient": "环境补光",
}

_PRESET_ITEMS = (
    ("natural", "自然三点", "平衡自然的主光、冷调辅光与轮廓光组合"),
    ("dramatic", "戏剧反差", "高反差暖主光与冷轮廓光，强化立体层次"),
    ("soft", "柔光影棚", "大面积柔和面光均匀照亮产品与角色主体"),
)

_MODE_ITEMS = (
    ("relight", "接管原灯", "隐藏场景原有灯光并启用全新受管布光（可一键恢复）"),
    ("adapt", "叠加补光", "保留场景现有灯光，叠加一层独立的受管补光方案"),
)

_ANCHOR_ITEMS = (
    ("world", "世界空间", "灯具固定于世界坐标系，支持角色骨骼追随"),
    ("target", "跟随主体", "灯具随目标主体的变换同步移动"),
    ("camera", "跟随相机", "灯具绑定至当前活动相机随镜头移动"),
)

_WORLD_KIND_ITEMS = (
    ("constant", "纯色影棚", "干净可控的均匀背景环境色"),
    ("sky", "物理天空", "Blender 5.3 多重散射物理天空与太阳方位"),
    ("hdri", "HDRI 环境", "加载外部全景 HDR/EXR 贴图并支持旋转与主光对齐"),
)

_SOLVE_GOAL_ITEMS = (
    ("exposure", "目标曝光 (EV)", "将主体表面平均亮度精准调整至指定曝光档位"),
    ("key_fill", "主辅光比 (Ratio)", "在主体区域实现指定的主光与辅光光照比值"),
)

_SOLVE_QUALITY_ITEMS = (
    ("fast", "极速 (16spp)", "192px · 16 采样，适合快速迭代测光"),
    ("standard", "标准 (32spp)", "256px · 32 采样，速度与精度均衡"),
    ("high", "精算 (64spp)", "384px · 64 采样，适合最终精调"),
)

_SOLVE_DEVICE_ITEMS = (
    ("AUTO", "GPU 自动 (OPTIX)", "自动优先使用 RTX 显卡 OPTIX/CUDA 单次渲染求解，不可用时回退 CPU"),
    ("CPU", "强制 CPU", "兼容模式，逐灯 CPU 测量求解"),
)


# ---------------------------------------------------------------------------
# Internal Helpers (Preserving CLI handoff compatibility)
# ---------------------------------------------------------------------------

def _settings(scene):
    return getattr(scene, UI_PROPERTY)


def _lights(scene, rig_id):
    return [
        obj for obj in scene.objects
        if obj.type == "LIGHT" and obj.get("lw_owner") == rig_id
    ]


def _selected_light(scene):
    settings = _settings(scene)
    obj = scene.objects.get(settings.active_light)
    if obj and obj.type == "LIGHT" and obj.get("lw_owner") == settings.active_rig:
        return obj
    return None


def _require_rig(scene):
    rig_id = _settings(scene).active_rig
    rigs = core.inspect(scene.name, rig_id).get("rigs", [])
    if not rig_id or not any(rig.get("rig_id") == rig_id for rig in rigs):
        raise ValueError("请先创建或选择一个受管灯光方案")
    return rig_id


def _select_objects(context, objects):
    if context.mode != "OBJECT":
        raise ValueError("请先切换到物体模式，再选择灯光")
    available = [obj for obj in objects if context.view_layer.objects.get(obj.name) is obj]
    if not available:
        raise ValueError("当前视图层中没有可选的受管灯光")
    if any(obj.hide_select or obj.hide_get(view_layer=context.view_layer) for obj in available):
        raise ValueError("部分灯光当前隐藏或禁止选择；请先在大纲视图中恢复可选状态")
    for obj in list(context.selected_objects):
        obj.select_set(False)
    for obj in available:
        obj.select_set(True)
    context.view_layer.objects.active = available[0]


def _set_report(scene, message: str, kind: str = "INFO") -> None:
    settings = _settings(scene)
    settings.last_report = message
    settings.last_report_kind = kind


def _failed(operator, context, error):
    message = f"{type(error).__name__}: {error}"
    _set_report(context.scene, message, "ERROR")
    operator.report({"ERROR"}, message)
    return {"CANCELLED"}


def _default_targets(context):
    geom_types = {"MESH", "CURVE", "SURFACE", "META", "FONT", "VOLUME"}
    selected = [
        o.name for o in context.selected_objects
        if o.type in geom_types and not o.get("lw_owner")
    ]
    if selected:
        return selected
    active = context.active_object
    if active and active.type in geom_types and not active.get("lw_owner"):
        return [active.name]
    scene_objs = [
        o.name for o in context.scene.objects
        if o.type in geom_types and not o.get("lw_owner") and not o.hide_viewport
    ]
    return scene_objs[:8]


def _next_rig_id(scene, base: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]+", "_", (base or "studio").strip())
    if not clean or not clean[0].isalpha():
        clean = "rig_" + clean
    clean = clean[:48]
    existing = set(core._states(scene).keys())
    if clean not in existing:
        return clean
    idx = 2
    while f"{clean}_{idx:02d}" in existing:
        idx += 1
    return f"{clean}_{idx:02d}"


def _sync_collection_color(scene, rig_id: str) -> None:
    col = bpy.data.collections.get(f"LW.{rig_id}.Lights")
    if col is not None:
        apply_collection_theme(col, DOMAIN)


def _sync_light_node_tint(obj) -> None:
    tree = getattr(obj.data, "node_tree", None) if obj and obj.data else None
    if tree is None:
        return
    for node in tree.nodes:
        if node.bl_idname in {"ShaderNodeEmission", "ShaderNodeTexIES", "ShaderNodeTexImage"}:
            apply_node_theme(node, DOMAIN, accent=False)


def _sync_managed_light_state(scene, rig_id: str, obj) -> None:
    """Keep managed rig structure/baseline synchronized after intentional GUI edits."""
    if not rig_id or obj is None:
        return
    try:
        states = core._states(scene)
        state = states.get(rig_id)
        if not state:
            return
        lid = obj.get("lw_id")
        row = state.get("lights", {}).get(lid) if lid else None
        if row is None:
            return
        row["baseline"] = core._light_values(obj, lid in state.get("links", {}))
        row["structure"] = core._light_structure(obj)
        row["visibility"] = core._visibility(obj)
        core._save_states(scene, states)
    except Exception:
        pass


def _sync_all_rig_state_for_overwrite(scene, rig_id: str) -> None:
    """Refresh structure/world witnesses when user explicitly confirms overwrite restore."""
    states = core._states(scene)
    state = states.get(rig_id)
    if not state:
        return
    for lid, row in state.get("lights", {}).items():
        obj = bpy.data.objects.get(row["object"])
        if obj is not None:
            row["structure"] = core._light_structure(obj)
            row["visibility"] = core._visibility(obj)
    state["extras"] = core._extras_witness(state)
    core._save_states(scene, states)


def _refresh_hud(scene) -> None:
    settings = _settings(scene)
    if _RUNNER.running:
        return
    if not settings.show_viewport_hud:
        HUDManager.clear(DOMAIN)
        return
    rig_id = settings.active_rig
    if not rig_id:
        HUDManager.clear(DOMAIN)
        return
    lights = _lights(scene, rig_id)
    active_obj = _selected_light(scene)
    sub = f"方案 {rig_id} · {len(lights)} 盏灯"
    if active_obj:
        temp_info = f" · {int(round(active_obj.data.temperature))}K" if getattr(active_obj.data, "use_temperature", False) else ""
        sub = f"{active_obj.name} · {active_obj.data.energy:.0f}W{temp_info}"
    HUDManager.show(DOMAIN, f"{THEME.tab_label} · 影棚控光就绪", sub, kind="INFO")


def _on_hud_toggle(self, context):
    if context and context.scene:
        _refresh_hud(context.scene)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _light_chip_label(obj) -> str:
    role = str(obj.get("lw_role") or "")
    role_zh = _ROLE_LABELS.get(role, role)
    lid = str(obj.get("lw_id") or obj.name.rsplit(".", 1)[-1])
    if role_zh and role_zh != lid:
        return f"{role_zh} ({lid})"
    return lid


def _poll_armature(self, obj):
    return obj.type == "ARMATURE"


def _poll_subject(self, obj):
    return obj.type in {"MESH", "CURVE", "SURFACE", "META", "FONT", "VOLUME"} and not obj.get("lw_owner")


# ---------------------------------------------------------------------------
# PropertyGroup
# ---------------------------------------------------------------------------

class LWUISettings(bpy.types.PropertyGroup):
    # Preserved CLI handoff properties
    active_rig: StringProperty(name="方案 ID")
    active_light: StringProperty(name="当前灯光")
    last_report: StringProperty(name="最近操作")
    last_report_kind: StringProperty(name="状态级别", default="INFO")

    # Quick Rig Builder
    preset_style: EnumProperty(name="风格预设", items=_PRESET_ITEMS, default="natural")
    create_mode: EnumProperty(name="布光模式", items=_MODE_ITEMS, default="relight")
    create_anchor: EnumProperty(name="锚定空间", items=_ANCHOR_ITEMS, default="world")
    rig_name_hint: StringProperty(name="方案名称", default="studio_main", maxlen=48)
    include_background: BoolProperty(name="附带影棚底板", default=False)
    include_studio_world: BoolProperty(name="配置受管暗棚环境", default=True)
    auto_light_groups: BoolProperty(name="自动分配灯光分组", default=True)
    show_viewport_hud: BoolProperty(
        name="面板打开时显示视口 HUD",
        default=False,
        description="默认关闭以保持 3D 视口零遮挡；开启后仅在 L·灯光 面板展开时显示右上角状态胶囊（后台 GPU 测光期间始终自动显示实时进度并在完成后自动消失）",
        update=_on_hud_toggle,
    )

    # Single-light Commercial Controls
    light_group_input: StringProperty(name="分组名", default="KeyLight", maxlen=55)
    ies_filepath: StringProperty(name="IES 文件", subtype="FILE_PATH", default="")
    gobo_filepath: StringProperty(name="Gobo 贴图", subtype="FILE_PATH", default="")
    follow_armature: PointerProperty(name="目标骨架", type=bpy.types.Object, poll=_poll_armature)
    follow_bone_name: StringProperty(name="追踪骨骼", default="")

    # World / HDRI / Volume Controls
    world_kind: EnumProperty(name="环境类型", items=_WORLD_KIND_ITEMS, default="constant")
    world_color: FloatVectorProperty(
        name="环境颜色", subtype="COLOR", size=3, min=0.0, max=1.0, default=(0.06, 0.07, 0.09)
    )
    world_strength: FloatProperty(name="环境强度", min=0.0, max=50.0, default=0.35)
    sky_sun_elevation: FloatProperty(name="太阳高度角", subtype="ANGLE", min=0.0, max=math.pi / 2.0, default=math.radians(32.0))
    sky_sun_rotation: FloatProperty(name="太阳方位角", subtype="ANGLE", min=-math.tau, max=math.tau, default=math.radians(45.0))
    hdri_filepath: StringProperty(name="HDRI 路径", subtype="FILE_PATH", default="")
    hdri_rotation_deg: FloatProperty(name="HDRI 旋转 (°)", min=-360.0, max=360.0, default=0.0)
    fog_density: FloatProperty(name="体积雾密度", min=0.0001, max=2.0, default=0.02, precision=4)
    fog_anisotropy: FloatProperty(name="散射各向异性", min=-0.95, max=0.95, default=0.35)
    fog_color: FloatVectorProperty(name="雾气颜色", subtype="COLOR", size=3, min=0.0, max=1.0, default=(0.88, 0.92, 1.0))

    # RTX GPU Auto-Lighting Solver
    solve_subject: PointerProperty(name="测光主体", type=bpy.types.Object, poll=_poll_subject)
    solve_goal: EnumProperty(name="优化目标", items=_SOLVE_GOAL_ITEMS, default="exposure")
    solve_exposure_stops: FloatProperty(
        name="曝光偏移 (EV)", min=-4.0, max=4.0, default=0.5,
        description="目标主体相对当前基准的曝光档位增量（+1.0 表示亮度翻倍）",
    )
    solve_key_fill_ratio: FloatProperty(
        name="主辅光比", min=1.0, max=32.0, default=4.0,
        description="主光与辅光在主体上的目标照度比值（例如 4.0 表示 4:1）",
    )
    solve_quality: EnumProperty(name="采样档位", items=_SOLVE_QUALITY_ITEMS, default="standard")
    solve_device: EnumProperty(name="求解设备", items=_SOLVE_DEVICE_ITEMS, default="AUTO")

    # Templates
    template_filepath: StringProperty(name="模板文件", subtype="FILE_PATH", default="")


# ---------------------------------------------------------------------------
# Operators — Preserved Core + New 1.1.0 Artist Closed-Loop Controls
# ---------------------------------------------------------------------------

class LWUI_OT_create_rig(bpy.types.Operator):
    bl_idname = "lighting_workflow.create_rig"
    bl_label = "一键创建受管灯光方案"
    bl_description = "按所选商业风格围绕目标主体自动搭建受管灯光方案，并联动 Outliner 黄色分组"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            scene = context.scene
            settings = _settings(scene)
            with ensure_object_mode(context):
                targets = _default_targets(context)
                if not targets:
                    raise ValueError("场景中没有可布光的几何物体，请先选中或创建一个网格主体")
                existing_rigs = core.inspect(scene.name).get("rigs", [])
                mode = "adapt" if existing_rigs else settings.create_mode
                rig_id = _next_rig_id(scene, settings.rig_name_hint or settings.preset_style)

                light_rows = presets.lights(settings.preset_style)
                if settings.auto_light_groups:
                    for row in light_rows:
                        role_name = str(row.get("role", row["id"])).capitalize()
                        row["light_group"] = f"LG_{role_name}"

                manifest = {
                    "version": "1.0",
                    "scene": scene.name,
                    "view_layer": context.view_layer.name,
                    "rig_id": rig_id,
                    "action": "create",
                    "targets": targets,
                    "mode": mode,
                    "anchor": settings.create_anchor,
                    "lights": light_rows,
                }
                if settings.include_background:
                    manifest["background"] = {"color": [0.16, 0.17, 0.19], "roughness": 0.78, "size": 10.0}
                if not existing_rigs and settings.include_studio_world:
                    manifest["world"] = {
                        "kind": "constant",
                        "color": list(settings.world_color),
                        "strength": float(settings.world_strength),
                    }

                core.apply(manifest)
                _sync_collection_color(scene, rig_id)
                settings.active_rig = rig_id
                lights = _lights(scene, rig_id)
                settings.active_light = lights[0].name if lights else ""
                if settings.solve_subject is None and targets:
                    settings.solve_subject = scene.objects.get(targets[0])

                style_label = dict((k, v) for k, v, _ in _PRESET_ITEMS).get(settings.preset_style, settings.preset_style)
                msg = f"已创建「{style_label}」方案 {rig_id}（{len(lights)} 盏灯 · 目标 {targets[0]}）"
                _set_report(scene, msg, "OK")
                _refresh_hud(scene)
                self.report({"INFO"}, msg)
                return {"FINISHED"}
        except Exception as error:
            return _failed(self, context, error)


class LWUI_OT_choose_rig(bpy.types.Operator):
    bl_idname = "lighting_workflow.choose_rig"
    bl_label = "选择受管方案"
    rig_id: StringProperty()

    def execute(self, context):
        try:
            rigs = core.inspect(context.scene.name, self.rig_id).get("rigs", [])
            if not any(rig.get("rig_id") == self.rig_id for rig in rigs):
                raise ValueError("此方案已不存在；请重新读取场景")
            settings = _settings(context.scene)
            settings.active_rig = self.rig_id
            lights = _lights(context.scene, self.rig_id)
            settings.active_light = lights[0].name if lights else ""
            _sync_collection_color(context.scene, self.rig_id)
            _set_report(context.scene, f"当前方案：{self.rig_id}", "INFO")
            _refresh_hud(context.scene)
            return {"FINISHED"}
        except Exception as error:
            return _failed(self, context, error)


class LWUI_OT_choose_light(bpy.types.Operator):
    bl_idname = "lighting_workflow.choose_light"
    bl_label = "选择受管灯光"
    object_name: StringProperty()

    def execute(self, context):
        try:
            rig_id = _require_rig(context.scene)
            obj = context.scene.objects.get(self.object_name)
            if obj is None or obj.type != "LIGHT" or obj.get("lw_owner") != rig_id:
                raise ValueError("灯光已不存在或不属于当前受管方案")
            with ensure_object_mode(context):
                _select_objects(context, [obj])
            settings = _settings(context.scene)
            settings.active_light = obj.name
            if obj.lightgroup:
                settings.light_group_input = obj.lightgroup
            _set_report(context.scene, f"已选中灯光：{obj.name}", "INFO")
            _refresh_hud(context.scene)
            return {"FINISHED"}
        except Exception as error:
            return _failed(self, context, error)


class LWUI_OT_select_group(bpy.types.Operator):
    bl_idname = "lighting_workflow.select_group"
    bl_label = "选中整组灯光"
    bl_description = "在 3D 视口中选中当前受管方案的全部灯具以便整体移动或旋转"

    def execute(self, context):
        try:
            rig_id = _require_rig(context.scene)
            with ensure_object_mode(context):
                _select_objects(context, _lights(context.scene, rig_id))
            _set_report(context.scene, f"已选中整组灯具：{rig_id}", "INFO")
            return {"FINISHED"}
        except Exception as error:
            return _failed(self, context, error)


class LWUI_OT_set_kelvin_preset(bpy.types.Operator):
    bl_idname = "lighting_workflow.set_kelvin_preset"
    bl_label = "应用色温预设"
    bl_description = "为当前灯具快速应用标准物理开尔文色温"
    bl_options = {"REGISTER", "UNDO"}
    kelvin: FloatProperty(default=5600.0, min=1000.0, max=20000.0)

    def execute(self, context):
        try:
            obj = _selected_light(context.scene)
            if obj is None:
                raise ValueError("请先选中一盏受管灯具")
            obj.data.use_temperature = True
            obj.data.temperature = float(self.kelvin)
            _sync_managed_light_state(context.scene, _settings(context.scene).active_rig, obj)
            _set_report(context.scene, f"{obj.name} 已设为 {int(round(self.kelvin))}K 物理色温", "OK")
            _refresh_hud(context.scene)
            return {"FINISHED"}
        except Exception as error:
            return _failed(self, context, error)


class LWUI_OT_assign_light_group(bpy.types.Operator):
    bl_idname = "lighting_workflow.assign_light_group"
    bl_label = "指派灯光分组"
    bl_description = "在当前视图层创建（如不存在）并绑定 Cycles Light Group，供批渲染与合成混光使用"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            obj = _selected_light(context.scene)
            if obj is None:
                raise ValueError("请先选中一盏受管灯具")
            name = _settings(context.scene).light_group_input.strip()
            if not name or "." in name or len(name.encode("utf-8")) > 55:
                raise ValueError("分组名不能为空、不能含英文句点且不超过 55 字节")
            groups = context.view_layer.lightgroups
            if groups.get(name) is None:
                groups.add(name=name)
            obj.lightgroup = name
            _sync_managed_light_state(context.scene, _settings(context.scene).active_rig, obj)
            _set_report(context.scene, f"{obj.name} 已加入灯光分组：{name}", "OK")
            return {"FINISHED"}
        except Exception as error:
            return _failed(self, context, error)


class LWUI_OT_apply_ies_gobo(bpy.types.Operator):
    bl_idname = "lighting_workflow.apply_ies_gobo"
    bl_label = "应用 IES / Gobo 配光"
    bl_description = "为当前点光/聚光灯挂载物理 IES 光度文件或 Gobo 投影贴图节点"
    bl_options = {"REGISTER", "UNDO"}
    clear_nodes: BoolProperty(default=False)

    def execute(self, context):
        try:
            rig_id = _require_rig(context.scene)
            obj = _selected_light(context.scene)
            if obj is None:
                raise ValueError("请先选中一盏受管灯具")
            settings = _settings(context.scene)
            if self.clear_nodes:
                obj.data.use_nodes = False
                _sync_managed_light_state(context.scene, rig_id, obj)
                _set_report(context.scene, f"已清除 {obj.name} 的 IES / Gobo 节点", "INFO")
                return {"FINISHED"}

            row = {}
            if settings.ies_filepath.strip():
                if obj.data.type not in {"POINT", "SPOT"}:
                    raise ValueError("IES 配光仅支持点光 (POINT) 或聚光 (SPOT)，请先切换灯具类型")
                p = Path(bpy.path.abspath(settings.ies_filepath)).resolve()
                if not p.is_file():
                    raise ValueError(f"找不到 IES 文件：{p}")
                row["ies"] = {"file": str(p), "expected_sha256": _file_sha256(p)}
            if settings.gobo_filepath.strip():
                if obj.data.type != "SPOT":
                    raise ValueError("Gobo 图像投影仅支持聚光灯 (SPOT)，请先将灯具类型改为聚光灯")
                p = Path(bpy.path.abspath(settings.gobo_filepath)).resolve()
                if not p.is_file():
                    raise ValueError(f"找不到 Gobo 贴图：{p}")
                row["gobo"] = {"file": str(p), "expected_sha256": _file_sha256(p)}
            if not row:
                raise ValueError("请先选择有效的 IES 文件或 Gobo 贴图路径")

            state_stub = {"rig_id": rig_id, "texts": [], "images": []}
            commercial.light_graph(obj, row, state_stub)
            _sync_light_node_tint(obj)
            _sync_managed_light_state(context.scene, rig_id, obj)
            tags = " + ".join(k.upper() for k in row.keys())
            _set_report(context.scene, f"已为 {obj.name} 挂载 {tags} 光学节点", "OK")
            return {"FINISHED"}
        except Exception as error:
            return _failed(self, context, error)


class LWUI_OT_bind_bone_follow(bpy.types.Operator):
    bl_idname = "lighting_workflow.bind_bone_follow"
    bl_label = "绑定骨骼追光"
    bl_description = "让当前灯具自动跟随指定角色的骨骼移动（保留相对偏移）"
    bl_options = {"REGISTER", "UNDO"}
    clear_constraint: BoolProperty(default=False)

    def execute(self, context):
        try:
            obj = _selected_light(context.scene)
            if obj is None:
                raise ValueError("请先选中一盏受管灯具")
            existing = [c for c in obj.constraints if c.name == "Lighting Workflow Bone Follow"]
            for c in existing:
                obj.constraints.remove(c)
            if self.clear_constraint:
                _sync_managed_light_state(context.scene, _settings(context.scene).active_rig, obj)
                _set_report(context.scene, f"已解除 {obj.name} 的骨骼追光绑定", "INFO")
                return {"FINISHED"}

            settings = _settings(context.scene)
            arm = settings.follow_armature
            bone = settings.follow_bone_name.strip()
            if arm is None or arm.type != "ARMATURE":
                raise ValueError("请先拾取一个角色骨架对象")
            if not bone or bone not in arm.pose.bones:
                raise ValueError(f"骨架 {arm.name} 中不存在骨骼：{bone or '(空)'}")
            commercial.bone_follow(context.scene, obj, {"armature": arm.name, "bone": bone})
            _sync_managed_light_state(context.scene, settings.active_rig, obj)
            _set_report(context.scene, f"{obj.name} 已绑定追随 {arm.name}::{bone}", "OK")
            return {"FINISHED"}
        except Exception as error:
            return _failed(self, context, error)


class LWUI_OT_apply_world(bpy.types.Operator):
    bl_idname = "lighting_workflow.apply_world"
    bl_label = "应用环境世界设置"
    bl_description = "按纯色影棚、物理天空或 HDRI 全景贴图重建当前场景环境节点"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            scene = context.scene
            settings = _settings(scene)
            rig_id = settings.active_rig
            states = core._states(scene)
            state = states.get(rig_id) if rig_id else None
            managed_owner = rig_id if (state and state.get("world")) else ""

            spec = {"kind": settings.world_kind, "strength": float(settings.world_strength)}
            if settings.world_kind == "constant":
                spec["color"] = list(settings.world_color)
            elif settings.world_kind == "sky":
                spec["sky_type"] = "MULTIPLE_SCATTERING"
                spec["sun_elevation_deg"] = math.degrees(settings.sky_sun_elevation)
                spec["sun_rotation_deg"] = math.degrees(settings.sky_sun_rotation)
                spec["sun_disc"] = True
            elif settings.world_kind == "hdri":
                p = Path(bpy.path.abspath(settings.hdri_filepath)).resolve()
                if not p.is_file():
                    raise ValueError(f"找不到 HDRI 环境贴图：{p}")
                spec["file"] = str(p)
                spec["rotation_deg"] = float(settings.hdri_rotation_deg)

            old_managed_world = bpy.data.worlds.get(state["world"]) if managed_owner else None
            world_name = f"LW.{rig_id}.World.{uuid.uuid4().hex[:4]}" if managed_owner else f"Studio_World_{uuid.uuid4().hex[:4]}"
            world = core._world(spec, world_name, managed_owner or "studio_world")
            if world.node_tree:
                for node in world.node_tree.nodes:
                    apply_node_theme(node, DOMAIN, accent=False)
            scene.world = world
            if managed_owner and state is not None:
                if old_managed_world is not None and old_managed_world != world and old_managed_world.users == 0:
                    bpy.data.worlds.remove(old_managed_world)
                state["world"] = world.name
                state["applied_world"] = world.name
                state["world_witness"] = core._world_witness(world)
                state["images"] = [img.name for img in bpy.data.images if img.get(core.OWNER) == rig_id]
                state["manifest"]["world"] = spec
                states[rig_id] = state
                core._save_states(scene, states)
            kind_zh = dict((k, v) for k, v, _ in _WORLD_KIND_ITEMS).get(settings.world_kind, settings.world_kind)
            _set_report(scene, f"已应用「{kind_zh}」世界环境（强度 {settings.world_strength:.2f}）", "OK")
            return {"FINISHED"}
        except Exception as error:
            return _failed(self, context, error)


class LWUI_OT_align_hdri_sun(bpy.types.Operator):
    bl_idname = "lighting_workflow.align_hdri_sun"
    bl_label = "对齐 HDRI 主光方位"
    bl_description = "自动提取当前 HDRI 最亮光斑质心，将所选灯具（推荐太阳光）旋转对准 HDRI 主光方向"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            scene = context.scene
            world = scene.world
            if world is None or not world.use_nodes or world.node_tree is None:
                raise ValueError("当前场景没有启用节点式世界环境")
            if not any(n.bl_idname == "ShaderNodeTexEnvironment" and n.image for n in world.node_tree.nodes):
                raise ValueError("当前世界环境未加载 HDRI 环境贴图")
            obj = _selected_light(scene) or (
                context.active_object if context.active_object and context.active_object.type == "LIGHT" else None
            )
            if obj is None:
                raise ValueError("请先选中一盏待对齐的灯具（推荐太阳光 SUN）")

            rot_deg = float(_settings(scene).hdri_rotation_deg)
            for node in world.node_tree.nodes:
                if node.bl_idname == "ShaderNodeMapping":
                    rot_deg = math.degrees(node.inputs["Rotation"].default_value[2])
                    break
            direction = commercial.hdri_direction(world, rot_deg)
            pos = obj.matrix_world.translation.copy()
            core._transform(obj, pos, pos - direction)
            _set_report(scene, f"已将 {obj.name} 精准对齐至 HDRI 最亮主光方向", "OK")
            return {"FINISHED"}
        except Exception as error:
            return _failed(self, context, error)


class LWUI_OT_toggle_volume_fog(bpy.types.Operator):
    bl_idname = "lighting_workflow.toggle_volume_fog"
    bl_label = "添加/更新局部体积雾"
    bl_description = "围绕布光目标创建或更新可控局部体积雾盒（Volume Scatter），避免全局雾气吞噬远景光"
    bl_options = {"REGISTER", "UNDO"}
    remove_fog: BoolProperty(default=False)

    def execute(self, context):
        try:
            scene = context.scene
            settings = _settings(scene)
            rig_id = _require_rig(scene)
            fog_name = f"LW.{rig_id}.Fog"
            existing = bpy.data.objects.get(fog_name)
            states = core._states(scene)
            state = states[rig_id]
            if existing is not None:
                mesh = existing.data
                bpy.data.objects.remove(existing, do_unlink=True)
                if mesh and mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
                mat = bpy.data.materials.get(fog_name + ".Material")
                if mat and mat.users == 0:
                    bpy.data.materials.remove(mat)
                if fog_name in state.get("objects", []):
                    state["objects"].remove(fog_name)
                if (fog_name + ".Material") in state.get("materials", []):
                    state["materials"].remove(fog_name + ".Material")
            if self.remove_fog:
                state["extras"] = core._extras_witness(state)
                states[rig_id] = state
                core._save_states(scene, states)
                _set_report(scene, f"已移除方案 {rig_id} 的局部体积雾盒", "INFO")
                return {"FINISHED"}

            box = core._box(scene, state["targets"])
            col = bpy.data.collections.get(state["collection"]) or scene.collection
            fog_spec = {
                "density": float(settings.fog_density),
                "anisotropy": float(settings.fog_anisotropy),
                "color": list(settings.fog_color),
                "location": [0.0, 0.0, 0.0],
                "size": [4.5, 4.5, 3.2],
            }
            commercial.local_fog(fog_spec, box, col, state)
            fog_obj = bpy.data.objects.get(fog_name)
            if fog_obj:
                fog_obj.display_type = "WIRE"
                fog_obj.hide_select = False
            state["extras"] = core._extras_witness(state)
            states[rig_id] = state
            core._save_states(scene, states)
            _set_report(scene, f"已更新局部体积雾（密度 {settings.fog_density:.4f}）", "OK")
            return {"FINISHED"}
        except Exception as error:
            return _failed(self, context, error)


class LWUI_OT_gpu_solve(bpy.types.Operator):
    bl_idname = "lighting_workflow.gpu_solve"
    bl_label = "RTX GPU 自动测光求解"
    bl_description = "在后台非阻塞进程中调用 Cycles GPU Light Groups 单次渲染，自动求解各灯精准功率并回写当前场景"

    def execute(self, context):
        global _TEMP_SOLVE_DIR
        try:
            scene = context.scene
            settings = _settings(scene)
            rig_id = _require_rig(scene)
            lights = _lights(scene, rig_id)
            if not lights:
                raise ValueError("当前方案没有可求解的受管灯具")
            if scene.camera is None:
                raise ValueError("测光求解需要当前场景存在活动相机（Scene Camera）")

            subject = settings.solve_subject
            if subject is None:
                state = core._states(scene).get(rig_id, {})
                t_names = state.get("targets", [])
                subject = scene.objects.get(t_names[0]) if t_names else None
            if subject is None:
                raise ValueError("请先指定一个测光主体网格对象")

            quality_map = {
                "fast": (192, 16),
                "standard": (256, 32),
                "high": (384, 64),
            }
            width, samples = quality_map.get(settings.solve_quality, (256, 32))

            light_specs = [{"name": obj.name, "min_energy": 0.0} for obj in lights]
            req = {
                "scene": scene.name,
                "view_layer": context.view_layer.name,
                "apply": True,
                "lights": light_specs,
                "sampling": {"width": width, "samples": samples, "threads": 4},
                "_device_mode": settings.solve_device,
            }

            if settings.solve_goal == "exposure":
                req["exposure"] = [{
                    "subject": subject.name,
                    "stops": float(settings.solve_exposure_stops),
                    "tolerance_stops": 0.12,
                }]
            else:
                keys = [o.name for o in lights if o.get("lw_role") == "key"] or [lights[0].name]
                fills = [o.name for o in lights if o.get("lw_role") == "fill"] or (
                    [lights[1].name] if len(lights) > 1 else []
                )
                if not fills:
                    raise ValueError("主辅光比求解至少需要 2 盏灯（主光与辅光）")
                req["key_fill"] = [{
                    "key": keys,
                    "fill": fills,
                    "region": {"object": subject.name},
                    "ratio": float(settings.solve_key_fill_ratio),
                    "tolerance_stops": 0.15,
                }]

            temp_dir = Path(tempfile.mkdtemp(prefix="lw_gpu_solve_"))
            _TEMP_SOLVE_DIR = temp_dir
            snapshot_blend = temp_dir / "solve_snapshot.blend"
            req_path = temp_dir / "request.json"
            status_path = temp_dir / "status.json"
            result_path = temp_dir / "result.json"

            with ensure_object_mode(context):
                bpy.ops.wm.save_as_mainfile(filepath=str(snapshot_blend), copy=True)

            req_path.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
            worker_script = Path(__file__).resolve().with_name("gpu_solve_worker.py")
            addon_parent = Path(__file__).resolve().parent.parent

            scene_name = scene.name

            def _apply_result(ok: bool, payload: dict) -> None:
                target_scene = bpy.data.scenes.get(scene_name)
                if target_scene is None:
                    return
                if not ok:
                    err = payload.get("error") or payload.get("stderr_tail") or "后台求解失败"
                    _set_report(target_scene, f"测光求解未完成：{err}", "ERROR")
                    _refresh_hud(target_scene)
                    return
                res = payload.get("result", {})
                solved_lights = res.get("lights", [])
                dev = payload.get("device_used", {}).get("backend", "GPU")
                elapsed = res.get("solve_seconds")
                time_str = f"{elapsed:.2f}s" if isinstance(elapsed, (int, float)) else "完成"
                if res.get("applied"):
                    summary_parts = []
                    for row in solved_lights:
                        lobj = target_scene.objects.get(row["object"])
                        if lobj and lobj.type == "LIGHT":
                            lobj.data.energy = float(row["final_energy"])
                            summary_parts.append(f"{lobj.get('lw_id', lobj.name)}={row['final_energy']:.0f}W")
                    try:
                        bpy.ops.ed.undo_push(message="L·灯光 GPU 自动测光求解")
                    except Exception:
                        pass
                    _set_report(
                        target_scene,
                        f"求解达标（{dev} · {time_str}）：" + " / ".join(summary_parts[:4]),
                        "OK",
                    )
                else:
                    _set_report(
                        target_scene,
                        f"已测光但当前灯位无法满足约束（{dev} · {time_str}），建议放宽目标或调整灯位",
                        "WARN",
                    )
                _refresh_hud(target_scene)

            argv = [
                bpy.app.binary_path,
                "--background",
                "--factory-startup",
                "--offline-mode",
                str(snapshot_blend),
                "--python",
                str(worker_script),
                "--",
                str(addon_parent),
                str(req_path),
                str(status_path),
                str(result_path),
            ]
            _RUNNER.start(
                argv,
                label="RTX GPU 自动测光求解",
                status_file=status_path,
                result_file=result_path,
                on_complete=_apply_result,
            )
            _set_report(scene, "已启动后台非阻塞测光求解，界面可继续操作…", "RUNNING")
            self.report({"INFO"}, "后台测光求解已启动")
            return {"FINISHED"}
        except Exception as error:
            return _failed(self, context, error)


class LWUI_OT_cancel_job(bpy.types.Operator):
    bl_idname = "lighting_workflow.cancel_job"
    bl_label = "取消后台测光"
    bl_description = "立即终止正在进行的后台 GPU 测光求解进程"

    def execute(self, context):
        if _RUNNER.cancel():
            _set_report(context.scene, "已取消后台测光求解", "WARN")
            _refresh_hud(context.scene)
        return {"FINISHED"}


class LWUI_OT_save_snapshot(bpy.types.Operator):
    bl_idname = "lighting_workflow.save_snapshot"
    bl_label = "保存方案快照"
    bl_description = "将当前真实灯光参数保存到新的 Text JSON 数据块；保存工程后持久保留"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            rig_id = _require_rig(context.scene)
            snapshot = core.inspect(context.scene.name, rig_id)
            payload = {
                "schema": "lighting_workflow.snapshot.v1",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "scene": context.scene.name,
                "view_layer": context.view_layer.name,
                "rig_id": rig_id,
                "snapshot": snapshot,
            }
            serialized = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
            text = bpy.data.texts.new(f"LW_Snapshot_{rig_id}.json")
            text.write(serialized)
            msg = f"已保存快照 {text.name}"
            _set_report(context.scene, msg, "OK")
            self.report({"INFO"}, msg)
            return {"FINISHED"}
        except Exception as error:
            return _failed(self, context, error)


class LWUI_OT_export_template(bpy.types.Operator):
    bl_idname = "lighting_workflow.export_template"
    bl_label = "导出跨镜模板"
    bl_description = "将当前受管方案导出为可跨场景/跨镜头复用的标准灯光模板 JSON"

    def execute(self, context):
        try:
            rig_id = _require_rig(context.scene)
            raw_path = _settings(context.scene).template_filepath.strip()
            if not raw_path:
                raise ValueError("请先选择模板输出 JSON 路径")
            out_path = Path(bpy.path.abspath(raw_path)).resolve()
            if out_path.suffix.lower() != ".json":
                out_path = out_path.with_suffix(".json")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            res_dir = out_path.parent / (out_path.stem + "_resources")
            tpl = templates.export(context.scene.name, rig_id, resource_directory=res_dir)
            out_path.write_text(json.dumps(tpl, ensure_ascii=False, indent=2), encoding="utf-8")
            msg = f"已导出灯光模板：{out_path.name}"
            _set_report(context.scene, msg, "OK")
            self.report({"INFO"}, msg)
            return {"FINISHED"}
        except Exception as error:
            return _failed(self, context, error)


class LWUI_OT_apply_template(bpy.types.Operator):
    bl_idname = "lighting_workflow.apply_template"
    bl_label = "套用跨镜模板"
    bl_description = "读取灯光模板 JSON 并自适应缩放尺寸与功率应用到当前目标主体"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            scene = context.scene
            settings = _settings(scene)
            raw_path = settings.template_filepath.strip()
            if not raw_path:
                raise ValueError("请先选择待套用的灯光模板 JSON 文件")
            in_path = Path(bpy.path.abspath(raw_path)).resolve()
            if not in_path.is_file():
                raise ValueError(f"模板文件不存在：{in_path}")
            tpl = json.loads(in_path.read_text(encoding="utf-8"))
            with ensure_object_mode(context):
                targets = _default_targets(context)
                ref_count = len(tpl["reference"]["targets"])
                if len(targets) < ref_count:
                    raise ValueError(f"该模板需要至少 {ref_count} 个几何目标，当前仅选中 {len(targets)} 个")
                existing = core.inspect(scene.name).get("rigs", [])
                rig_id = _next_rig_id(scene, tpl.get("template_id", "tpl_rig"))
                manifest = templates.instantiate(tpl, {
                    "scene": scene.name,
                    "view_layer": context.view_layer.name,
                    "rig_id": rig_id,
                    "targets": targets[:ref_count],
                    "mode": "adapt" if existing else "relight",
                    "anchor": settings.create_anchor,
                    "power_policy": "scale_squared",
                    "bindings": {},
                    "keep_lights": [],
                })
                core.apply(manifest)
                _sync_collection_color(scene, rig_id)
                settings.active_rig = rig_id
                lights = _lights(scene, rig_id)
                settings.active_light = lights[0].name if lights else ""
                msg = f"已套用模板创建方案 {rig_id}（{len(lights)} 盏灯）"
                _set_report(scene, msg, "OK")
                _refresh_hud(scene)
                self.report({"INFO"}, msg)
                return {"FINISHED"}
        except Exception as error:
            return _failed(self, context, error)


class LWUI_OT_restore_original(bpy.types.Operator):
    bl_idname = "lighting_workflow.restore_original"
    bl_label = "恢复原始方案"
    bl_description = "移除当前受管灯组并无损恢复创建前记录的原灯光、世界环境与曝光"
    bl_options = {"REGISTER", "UNDO"}
    overwrite_manual: BoolProperty(
        name="允许覆盖手动修改",
        default=False,
        description="勾选后强制清除当前方案的手改项并还原；默认保留冲突保护检查",
    )

    def invoke(self, context, event):
        self.overwrite_manual = False
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        layout = self.layout
        layout.label(text="将移除当前受管方案，并恢复创建前的原灯光、World 与曝光。", icon="INFO")
        layout.prop(self, "overwrite_manual")

    def execute(self, context):
        try:
            rig_id = _require_rig(context.scene)
            if self.overwrite_manual:
                _sync_all_rig_state_for_overwrite(context.scene, rig_id)
            result = core.apply({
                "version": "1.0",
                "scene": context.scene.name,
                "view_layer": context.view_layer.name,
                "rig_id": rig_id,
                "action": "restore",
                "targets": [],
                "conflict": "overwrite" if self.overwrite_manual else "error",
            })
            if result.get("ok") is False:
                raise RuntimeError(json.dumps(result.get("error", result), ensure_ascii=False))
            settings = _settings(context.scene)
            remaining = core.inspect(context.scene.name).get("rigs", [])
            settings.active_rig = remaining[0]["rig_id"] if remaining else ""
            next_lights = _lights(context.scene, settings.active_rig) if settings.active_rig else []
            settings.active_light = next_lights[0].name if next_lights else ""
            msg = f"已恢复原始方案：{rig_id}"
            _set_report(context.scene, msg, "OK")
            _refresh_hud(context.scene)
            self.report({"INFO"}, msg)
            return {"FINISHED"}
        except Exception as error:
            return _failed(self, context, error)


# ---------------------------------------------------------------------------
# Main Panel — Compact Direct Skeleton (L·灯光)
# ---------------------------------------------------------------------------

class LWUI_PT_manager(bpy.types.Panel):
    bl_label = ""
    bl_idname = "LWUI_PT_manager"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = THEME.tab_label  # "L·灯光"
    bl_options = {"HEADER_LAYOUT_EXPAND"}

    def draw_header(self, context):
        layout = self.layout
        draw_brand_header(layout, DOMAIN, _ICONS, title=THEME.title_zh, fallback_icon="LIGHT_SUN")
        settings = _settings(context.scene)
        right = layout.row(align=True)
        right.alignment = "RIGHT"
        right.prop(settings, "show_viewport_hud", text="", icon="OVERLAY", emboss=False)

    def _draw_quick_create_form(self, layout, settings, *, has_existing: bool):
        row = layout.row(align=True)
        row.scale_y = 1.1
        row.prop(settings, "preset_style", expand=True)

        col = prop_column(layout, split_factor=0.38)
        col.prop(settings, "rig_name_hint", text="方案名")
        if not has_existing:
            col.prop(settings, "create_mode", text="接管方式")
        col.prop(settings, "create_anchor", text="空间锚定")

        checks = layout.row(align=True)
        checks.prop(settings, "auto_light_groups", text="自动灯光分组", toggle=True)
        checks.prop(settings, "include_background", text="影棚底板", toggle=True)

        cta_label = "叠加创建新补光层" if has_existing else "一键创建受管灯光方案"
        primary_button(
            layout,
            "lighting_workflow.create_rig",
            cta_label,
            icon_value=_ICONS.action_id("create_rig"),
            icon="LIGHT_AREA",
            scale_y=1.32,
        )

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        settings = _settings(scene)

        # Live non-blocking background progress bar (always visible at top when running)
        if _RUNNER.running:
            progress_card(
                layout,
                DOMAIN,
                _ICONS,
                text=_RUNNER.detail or _RUNNER.label,
                factor=_RUNNER.progress,
                cancel_operator="lighting_workflow.cancel_job",
            )

        try:
            actual = core.inspect(scene.name)
        except Exception as error:
            status_banner(layout, context, f"场景状态读取异常：{error}", kind="ERROR", icon_pack=_ICONS)
            return

        rigs = actual.get("rigs", [])
        if rigs and not any(r.get("rig_id") == settings.active_rig for r in rigs):
            current = rigs[0]
        else:
            current = next((r for r in rigs if r.get("rig_id") == settings.active_rig), None)
        active_rig_id = current["rig_id"] if current else ""

        # --- 1. 快速布光 / 方案切换（紧凑首屏直达） ---
        if not rigs:
            box = layout.box()
            self._draw_quick_create_form(box, settings, has_existing=False)
        else:
            rig_bar = layout.row(align=True)
            rig_bar.scale_y = 1.12
            for rig in rigs:
                rid = rig.get("rig_id")
                if not isinstance(rid, str):
                    continue
                op = rig_bar.operator(
                    "lighting_workflow.choose_rig",
                    text=rid,
                    icon="OUTLINER_COLLECTION",
                    depress=(active_rig_id == rid),
                )
                op.rig_id = rid
            rig_bar.operator("lighting_workflow.select_group", text="", icon="RESTRICT_SELECT_OFF")
            rig_bar.operator("lighting_workflow.save_snapshot", text="", icon="BOOKMARKS")
            rig_bar.operator("lighting_workflow.restore_original", text="", icon="LOOP_BACK")

            _, new_body = section_panel(
                layout,
                "LW_SEC_add_rig",
                "新建 / 叠加布光方案",
                icon_value=_ICONS.action_id("create_rig"),
                icon="ADD",
                default_closed=True,
            )
            if new_body is not None:
                self._draw_quick_create_form(new_body, settings, has_existing=True)

        # --- 2. 单灯物理控光（色温 · 形态 · Light Groups · IES/Gobo） ---
        if current:
            _, light_body = section_panel(
                layout,
                "LW_SEC_light_ctrl",
                "单灯物理控光",
                icon_value=_ICONS.action_id("kelvin_ies"),
                icon="LIGHT",
                default_closed=False,
            )
            if light_body is not None:
                rig_lights = _lights(scene, active_rig_id)
                lrow = light_body.row(align=True)
                lrow.scale_y = 1.12
                for lobj in rig_lights:
                    op = lrow.operator(
                        "lighting_workflow.choose_light",
                        text=_light_chip_label(lobj),
                        depress=(settings.active_light == lobj.name),
                    )
                    op.object_name = lobj.name

                obj = _selected_light(scene) or (rig_lights[0] if rig_lights else None)
                if obj is not None:
                    local = obj.library is None and obj.override_library is None
                    unique = (
                        obj.data.library is None
                        and obj.data.override_library is None
                        and obj.data.users == 1
                    )
                    if not (local and unique):
                        status_banner(
                            light_body, context,
                            "当前灯具为共享或链接数据块（只读），请先本地化后再编辑。",
                            kind="WARN", icon_pack=_ICONS,
                        )

                    ctrl = prop_column(light_body, split_factor=0.38)
                    ctrl.enabled = local and unique
                    ctrl.prop(obj.data, "type", text="灯具类型")
                    ctrl.prop(obj.data, "energy", text="功率 (W)")

                    if hasattr(obj.data, "use_temperature"):
                        ctrl.prop(obj.data, "use_temperature", text="物理色温 (K)")
                        if obj.data.use_temperature:
                            ctrl.prop(obj.data, "temperature", text="开尔文")
                            krow = light_body.row(align=True)
                            krow.enabled = local and unique
                            for k_val, k_txt in ((2700, "2700K"), (3200, "3200K"), (5600, "5600K"), (7500, "7500K")):
                                kop = krow.operator("lighting_workflow.set_kelvin_preset", text=k_txt)
                                kop.kelvin = float(k_val)

                    ctrl2 = prop_column(light_body, split_factor=0.38)
                    ctrl2.enabled = local and unique
                    ctrl2.prop(obj.data, "color", text="过滤颜色")

                    if obj.data.type == "AREA":
                        ctrl2.prop(obj.data, "shape", text="光源形状")
                        ctrl2.prop(obj.data, "size", text="宽度 X")
                        if obj.data.shape in {"RECTANGLE", "ELLIPSE"}:
                            ctrl2.prop(obj.data, "size_y", text="高度 Y")
                        if hasattr(obj.data, "spread"):
                            ctrl2.prop(obj.data, "spread", text="光束扩散角")
                    elif obj.data.type == "SPOT":
                        ctrl2.prop(obj.data, "spot_size", text="聚光锥角")
                        ctrl2.prop(obj.data, "spot_blend", text="边缘羽化")
                        ctrl2.prop(obj.data, "shadow_soft_size", text="光源半径")
                    elif obj.data.type == "SUN":
                        ctrl2.prop(obj.data, "angle", text="太阳角直径")
                    elif hasattr(obj.data, "shadow_soft_size"):
                        ctrl2.prop(obj.data, "shadow_soft_size", text="光源半径")

                    toggles = light_body.row(align=True)
                    toggles.enabled = local and unique
                    if hasattr(obj.data, "use_shadow"):
                        toggles.prop(obj.data, "use_shadow", text="投射阴影", toggle=True)
                    if hasattr(obj.data, "normalize"):
                        toggles.prop(obj.data, "normalize", text="面积归一", toggle=True)
                    toggles.prop(obj, "hide_viewport", text="", icon="HIDE_OFF")
                    toggles.prop(obj, "hide_render", text="", icon="RESTRICT_RENDER_OFF")

                    # Light Groups quick strip
                    lg_row = light_body.row(align=True)
                    lg_row.prop(settings, "light_group_input", text="光组")
                    lg_op_icon = _ICONS.action_id("light_groups")
                    if lg_op_icon:
                        lg_row.operator("lighting_workflow.assign_light_group", text="指派", icon_value=lg_op_icon)
                    else:
                        lg_row.operator("lighting_workflow.assign_light_group", text="指派", icon="GROUP")

                    # Collapsible IES / Gobo / Bone Follow sub-section
                    _, adv_body = section_panel(
                        light_body,
                        "LW_SEC_ies_gobo",
                        "IES 配光 · Gobo 投影 · 骨骼追光",
                        icon="NODETREE",
                        default_closed=True,
                    )
                    if adv_body is not None:
                        acol = prop_column(adv_body, split_factor=0.34)
                        acol.prop(settings, "ies_filepath", text="IES 文件")
                        acol.prop(settings, "gobo_filepath", text="Gobo 图")
                        arow = adv_body.row(align=True)
                        arow.operator("lighting_workflow.apply_ies_gobo", text="挂载光学节点", icon="CHECKMARK").clear_nodes = False
                        arow.operator("lighting_workflow.apply_ies_gobo", text="清除", icon="X").clear_nodes = True

                        bcol = prop_column(adv_body, split_factor=0.34)
                        bcol.prop(settings, "follow_armature", text="目标骨架")
                        if settings.follow_armature and settings.follow_armature.type == "ARMATURE":
                            bcol.prop_search(settings, "follow_bone_name", settings.follow_armature.data, "bones", text="追随骨骼")
                        brow = adv_body.row(align=True)
                        brow.operator("lighting_workflow.bind_bone_follow", text="绑定追光", icon="CONSTRAINT_BONE").clear_constraint = False
                        brow.operator("lighting_workflow.bind_bone_follow", text="解绑", icon="UNLINKED").clear_constraint = True

        # --- 3. 环境世界、体积雾与 HDRI 对齐 ---
        _, env_body = section_panel(
            layout,
            "LW_SEC_world_fog",
            "环境世界 · 体积雾 · HDRI 对齐",
            icon_value=_ICONS.action_id("volume_hdri"),
            icon="WORLD",
            default_closed=True,
        )
        if env_body is not None:
            ecol = prop_column(env_body, split_factor=0.38)
            ecol.prop(scene.view_settings, "exposure", text="场景曝光 (EV)")
            ecol.separator(factor=0.4)
            ecol.prop(settings, "world_kind", text="环境模式")
            ecol.prop(settings, "world_strength", text="环境强度")
            if settings.world_kind == "constant":
                ecol.prop(settings, "world_color", text="背景颜色")
            elif settings.world_kind == "sky":
                ecol.prop(settings, "sky_sun_elevation", text="太阳高度")
                ecol.prop(settings, "sky_sun_rotation", text="太阳方位")
            elif settings.world_kind == "hdri":
                ecol.prop(settings, "hdri_filepath", text="HDRI 路径")
                ecol.prop(settings, "hdri_rotation_deg", text="旋转 (°)")

            wrow = env_body.row(align=True)
            wrow.operator("lighting_workflow.apply_world", text="应用世界环境", icon="WORLD_DATA")
            wrow.operator("lighting_workflow.align_hdri_sun", text="对齐 HDRI 主光", icon="LIGHT_SUN")

            if current:
                env_body.separator(factor=0.4)
                fcol = prop_column(env_body, split_factor=0.38)
                fcol.prop(settings, "fog_density", text="体积雾密度")
                fcol.prop(settings, "fog_anisotropy", text="散射方向")
                fcol.prop(settings, "fog_color", text="雾气色调")
                frow = env_body.row(align=True)
                frow.operator("lighting_workflow.toggle_volume_fog", text="创建/更新体积雾", icon="VOLUME_DATA").remove_fog = False
                frow.operator("lighting_workflow.toggle_volume_fog", text="移除雾盒", icon="TRASH").remove_fog = True

        # --- 4. RTX GPU 自动测光求解（非阻塞异步） ---
        if current:
            _, solve_body = section_panel(
                layout,
                "LW_SEC_gpu_solve",
                "RTX GPU 自动测光求解",
                icon_value=_ICONS.action_id("gpu_solve"),
                icon="RENDER_STILL",
                default_closed=False,
            )
            if solve_body is not None:
                scol = prop_column(solve_body, split_factor=0.38)
                scol.prop(settings, "solve_subject", text="测光主体")
                scol.prop(settings, "solve_goal", text="目标类型")
                if settings.solve_goal == "exposure":
                    scol.prop(settings, "solve_exposure_stops", text="曝光增量 (EV)")
                else:
                    scol.prop(settings, "solve_key_fill_ratio", text="主辅光比")
                scol.prop(settings, "solve_quality", text="采样档位")
                scol.prop(settings, "solve_device", text="计算设备")

                primary_button(
                    solve_body,
                    "lighting_workflow.gpu_solve",
                    "后台运行正在求解…" if _RUNNER.running else "启动 GPU 自动测光求解",
                    icon_value=_ICONS.action_id("gpu_solve"),
                    icon="PLAY",
                    scale_y=1.30,
                    enabled=not _RUNNER.running,
                )

        # --- 5. 快照与跨场景模板 ---
        _, tpl_body = section_panel(
            layout,
            "LW_SEC_templates",
            "快照与跨镜模板复用",
            icon="FILE_TICK",
            default_closed=True,
        )
        if tpl_body is not None:
            tcol = prop_column(tpl_body, split_factor=0.32)
            tcol.prop(settings, "template_filepath", text="模板路径")
            trow = tpl_body.row(align=True)
            trow.operator("lighting_workflow.export_template", text="导出模板", icon="EXPORT")
            trow.operator("lighting_workflow.apply_template", text="套用模板", icon="IMPORT")
            if current:
                rrow = tpl_body.row(align=True)
                rrow.operator("lighting_workflow.save_snapshot", text="保存 JSON 快照", icon="TEXT")
                rrow.operator("lighting_workflow.restore_original", text="恢复原始方案", icon="LOOP_BACK")

        # --- 底部清爽回执横幅 ---
        status_banner(layout, context, settings.last_report, kind=settings.last_report_kind, icon_pack=_ICONS)


_CLASSES = (
    LWUISettings,
    LWUI_OT_create_rig,
    LWUI_OT_choose_rig,
    LWUI_OT_choose_light,
    LWUI_OT_select_group,
    LWUI_OT_set_kelvin_preset,
    LWUI_OT_assign_light_group,
    LWUI_OT_apply_ies_gobo,
    LWUI_OT_bind_bone_follow,
    LWUI_OT_apply_world,
    LWUI_OT_align_hdri_sun,
    LWUI_OT_toggle_volume_fog,
    LWUI_OT_gpu_solve,
    LWUI_OT_cancel_job,
    LWUI_OT_save_snapshot,
    LWUI_OT_export_template,
    LWUI_OT_apply_template,
    LWUI_OT_restore_original,
    LWUI_PT_manager,
)


def unregister():
    """Cleanly remove classes, Scene properties, icon previews, dock, HUD, and background timer."""
    _RUNNER.cleanup()
    HUDManager.clear(DOMAIN)
    SidebarDock.unregister_plugin(DOMAIN)
    _ICONS.unregister()
    if hasattr(bpy.types.Scene, UI_PROPERTY):
        delattr(bpy.types.Scene, UI_PROPERTY)
    for cls in reversed(_CLASSES):
        registered = getattr(bpy.types, cls.__name__, None)
        if registered is not None:
            bpy.utils.unregister_class(registered)


def register():
    """Allow safe reloads without duplicate class/preview registrations."""
    unregister()
    completed = []
    try:
        _ICONS.register()
        SidebarDock.register_plugin(DOMAIN, _ICONS.icons_dir)
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            completed.append(cls)
        setattr(bpy.types.Scene, UI_PROPERTY, bpy.props.PointerProperty(type=LWUISettings))
    except Exception:
        SidebarDock.unregister_plugin(DOMAIN)
        _ICONS.unregister()
        if hasattr(bpy.types.Scene, UI_PROPERTY):
            delattr(bpy.types.Scene, UI_PROPERTY)
        for cls in reversed(completed):
            bpy.utils.unregister_class(cls)
        raise
