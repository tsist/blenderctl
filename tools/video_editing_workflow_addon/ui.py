"""Small native UI: inspect managed state and expose editable native controls."""
import json
import bpy
from . import core


class VWE_OT_inspect(bpy.types.Operator):
    bl_idname = 'vwe.inspect'
    bl_label = '检查当前剪辑'
    bl_description = '读取当前工程，包括手改差异和未知条带；不保存或重建时间线'

    def execute(self, context):
        try:
            result = core.inspect(context.scene.name)
            context.window_manager.vwe_inspection = json.dumps(result, ensure_ascii=False)
            count = len(result['clips'])
            changed = len(result['diagnostics']['manual_fields'])
            self.report({'INFO'}, f'{count} 个受管片段，{changed} 个有手改；详细 JSON 可从 WindowManager.vwe_inspection 读取')
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class VWE_PT_timeline(bpy.types.Panel):
    bl_label = 'Video Editing Workflow'
    bl_idname = 'VWE_PT_timeline'
    bl_space_type = 'SEQUENCE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'Workflow'

    def draw(self, context):
        layout = self.layout
        owner = context.scene.get('_vwe_workflow')
        layout.label(text='技术开发版 0.1.1')
        layout.label(text='工作流：' + (owner or '未受管'))
        layout.operator('vwe.inspect')
        editor = context.scene.sequence_editor
        strip = editor.active_strip if editor else None
        if strip and strip.get('_vwe_clip_id'):
            layout.label(text='片段：' + strip['_vwe_clip_id'])
            layout.prop(strip, 'name', text='名称')
            layout.prop(strip, 'lock', text='锁定')
            layout.prop(strip, 'mute', text='静音/隐藏')
            layout.label(text='其他修改使用 Blender 原生剪辑控件')
        layout.label(text='批量与输出使用公共 video CLI')


CLASSES = (VWE_OT_inspect, VWE_PT_timeline)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.vwe_inspection = bpy.props.StringProperty(options={'SKIP_SAVE'})


def unregister():
    if hasattr(bpy.types.WindowManager, 'vwe_inspection'):
        del bpy.types.WindowManager.vwe_inspection
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
