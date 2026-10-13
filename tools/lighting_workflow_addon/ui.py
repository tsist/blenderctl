"""Small, scene-backed lighting manager; artistic orchestration stays in the CLI."""
import json
from datetime import datetime, timezone

import bpy
from bpy.props import BoolProperty, StringProperty

from . import core


UI_PROPERTY = "lighting_workflow_ui_v1"


def _settings(scene):
    return getattr(scene, UI_PROPERTY)


def _lights(scene, rig_id):
    return [obj for obj in scene.objects
            if obj.type == 'LIGHT' and obj.get("lw_owner") == rig_id]


def _selected_light(scene):
    settings = _settings(scene)
    obj = scene.objects.get(settings.active_light)
    if obj and obj.type == 'LIGHT' and obj.get("lw_owner") == settings.active_rig:
        return obj
    return None


def _require_rig(scene):
    rig_id = _settings(scene).active_rig
    rigs = core.inspect(scene.name, rig_id).get("rigs", [])
    if not rig_id or not any(rig.get("rig_id") == rig_id for rig in rigs):
        raise ValueError("请先选择当前场景中存在的受管灯光方案")
    return rig_id


def _select_objects(context, objects):
    if context.mode != 'OBJECT':
        raise ValueError("请先切换到物体模式，再选择灯光")
    available = [obj for obj in objects if context.view_layer.objects.get(obj.name) is obj]
    if not available:
        raise ValueError("当前 View Layer 中没有可选的受管灯光")
    if any(obj.hide_select or obj.hide_get(view_layer=context.view_layer) for obj in available):
        raise ValueError("部分灯光当前隐藏或禁止选择；请先在 Outliner 中恢复可选状态")
    for obj in list(context.selected_objects):
        obj.select_set(False)
    for obj in available:
        obj.select_set(True)
    context.view_layer.objects.active = available[0]


def _failed(operator, context, error):
    message = f"{type(error).__name__}: {error}"
    _settings(context.scene).last_report = message
    operator.report({'ERROR'}, message)
    return {'CANCELLED'}


class LWUISettings(bpy.types.PropertyGroup):
    active_rig: StringProperty(name="方案 ID")
    active_light: StringProperty(name="当前灯光")
    last_report: StringProperty(name="最近操作")


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
            settings.last_report = f"当前方案：{self.rig_id}"
            return {'FINISHED'}
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
            if obj is None or obj.type != 'LIGHT' or obj.get("lw_owner") != rig_id:
                raise ValueError("灯光已不存在或不属于当前受管方案")
            _select_objects(context, [obj])
            _settings(context.scene).active_light = obj.name
            _settings(context.scene).last_report = f"选中灯光：{obj.name}"
            return {'FINISHED'}
        except Exception as error:
            return _failed(self, context, error)


class LWUI_OT_select_group(bpy.types.Operator):
    bl_idname = "lighting_workflow.select_group"
    bl_label = "选中整组灯光"

    def execute(self, context):
        try:
            rig_id = _require_rig(context.scene)
            _select_objects(context, _lights(context.scene, rig_id))
            _settings(context.scene).last_report = f"选中灯组：{rig_id}"
            return {'FINISHED'}
        except Exception as error:
            return _failed(self, context, error)


class LWUI_OT_save_snapshot(bpy.types.Operator):
    bl_idname = "lighting_workflow.save_snapshot"
    bl_label = "保存当前方案快照"
    bl_description = "将当前真实灯光参数保存到新的 Text JSON 数据块；保存 blend 后持久保留"
    bl_options = {'REGISTER', 'UNDO'}

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
            # New Text names are allocated by Blender; previous snapshots stay intact.
            serialized = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
            text = bpy.data.texts.new(f"LW_Snapshot_{rig_id}.json")
            text.write(serialized)
            _settings(context.scene).last_report = f"已保存 {text.name}；请保存 blend"
            self.report({'INFO'}, _settings(context.scene).last_report)
            return {'FINISHED'}
        except Exception as error:
            return _failed(self, context, error)


class LWUI_OT_restore_original(bpy.types.Operator):
    bl_idname = "lighting_workflow.restore_original"
    bl_label = "恢复原始方案"
    bl_description = "通过受管核心恢复创建方案前记录的原灯光、World 与曝光"
    bl_options = {'REGISTER', 'UNDO'}
    overwrite_manual: BoolProperty(
        name="允许覆盖本方案中的手动修改", default=False,
        description="明确选择后，恢复操作使用 overwrite 冲突策略；默认保留错误检查",
    )

    def invoke(self, context, event):
        self.overwrite_manual = False
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        self.layout.label(text="恢复创建受管方案前记录的灯光、World 和曝光。")
        self.layout.label(text="若存在手改，默认报告冲突并停止。")
        self.layout.prop(self, "overwrite_manual")

    def execute(self, context):
        try:
            rig_id = _require_rig(context.scene)
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
            _settings(context.scene).last_report = f"已恢复原始方案：{rig_id}"
            self.report({'INFO'}, _settings(context.scene).last_report)
            return {'FINISHED'}
        except Exception as error:
            return _failed(self, context, error)


class LWUI_PT_manager(bpy.types.Panel):
    bl_label = "受管灯光"
    bl_idname = "LWUI_PT_manager"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Lighting"

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        settings = _settings(scene)
        try:
            actual = core.inspect(scene.name)
        except Exception as error:
            layout.label(text=f"场景读取失败：{type(error).__name__}", icon='ERROR')
            layout.label(text=str(error)[:100])
            return
        rigs = actual.get("rigs", [])
        if not rigs:
            layout.label(text="当前场景暂无受管灯光方案")
            layout.label(text="通过 CLI 创建后，方案会显示于此")
        for rig in rigs:
            rig_id = rig.get("rig_id")
            if not isinstance(rig_id, str):
                layout.label(text="方案返回值缺少 rig_id", icon='ERROR')
                continue
            button = layout.operator("lighting_workflow.choose_rig", text=rig_id,
                                     depress=settings.active_rig == rig_id)
            button.rig_id = rig_id
        current = next((rig for rig in rigs if rig.get("rig_id") == settings.active_rig), None)
        if current:
            box = layout.box()
            box.label(text=f"方案：{settings.active_rig}")
            box.operator("lighting_workflow.select_group", icon='LIGHT')
            for obj in _lights(scene, settings.active_rig):
                button = box.operator("lighting_workflow.choose_light", text=obj.name,
                                      depress=settings.active_light == obj.name)
                button.object_name = obj.name
            obj = _selected_light(scene)
            if obj:
                controls = box.column()
                local = obj.library is None and obj.override_library is None
                unique = (obj.data.library is None and obj.data.override_library is None
                          and obj.data.users == 1)
                controls.enabled = local and unique
                if not (local and unique):
                    box.label(text="共享或链接灯具只读；请通过 CLI 隔离后编辑", icon='ERROR')
                controls.prop(obj.data, "energy", text="能量")
                controls.prop(obj.data, "color", text="颜色")
                if obj.data.type == 'AREA':
                    controls.prop(obj.data, "shape", text="形状")
                    controls.prop(obj.data, "size", text="尺寸 X")
                    if obj.data.shape in {'RECTANGLE', 'ELLIPSE'}:
                        controls.prop(obj.data, "size_y", text="尺寸 Y")
                elif obj.data.type == 'SUN':
                    controls.prop(obj.data, "angle", text="太阳角直径")
                elif hasattr(obj.data, "shadow_soft_size"):
                    controls.prop(obj.data, "shadow_soft_size", text="半径")
                controls.prop(obj, "hide_render", text="禁用渲染")
                controls.prop(obj, "hide_viewport", text="禁用视口")
                if hasattr(obj.data, "use_shadow"):
                    controls.prop(obj.data, "use_shadow", text="投射阴影")
            box.operator("lighting_workflow.save_snapshot", icon='TEXT')
            box.operator("lighting_workflow.restore_original", icon='LOOP_BACK')
        if settings.last_report:
            box = layout.box()
            box.label(text="最近操作")
            for line in settings.last_report.splitlines():
                box.label(text=line[:120])


_CLASSES = (
    LWUISettings, LWUI_OT_choose_rig, LWUI_OT_choose_light,
    LWUI_OT_select_group, LWUI_OT_save_snapshot,
    LWUI_OT_restore_original, LWUI_PT_manager,
)


def unregister():
    """Remove only classes and Scene properties owned by this module."""
    if hasattr(bpy.types.Scene, UI_PROPERTY):
        delattr(bpy.types.Scene, UI_PROPERTY)
    for cls in reversed(_CLASSES):
        registered = getattr(bpy.types, cls.__name__, None)
        if registered is not None:
            bpy.utils.unregister_class(registered)


def register():
    """Allow development reloads without duplicate registered classes."""
    unregister()
    completed = []
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            completed.append(cls)
        setattr(bpy.types.Scene, UI_PROPERTY, bpy.props.PointerProperty(type=LWUISettings))
    except Exception:
        if hasattr(bpy.types.Scene, UI_PROPERTY):
            delattr(bpy.types.Scene, UI_PROPERTY)
        for cls in reversed(completed):
            bpy.utils.unregister_class(cls)
        raise
