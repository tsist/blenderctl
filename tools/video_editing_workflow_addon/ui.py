"""视频序列编辑器中的剪辑工作台。"""
import json
from pathlib import Path
import tempfile

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty

from . import artist_ops, core
from ._ui_kit import (
    BackgroundJobRunner, HUDManager, IconPack, SidebarDock, draw_brand_header, get_theme,
    primary_button, progress_card, prop_column, section_panel, status_banner,
)

DOMAIN = "video"
THEME = get_theme(DOMAIN)
_ICONS = IconPack(DOMAIN)
_RUNNER = BackgroundJobRunner(DOMAIN)
_EXPORT_REQUEST = None


class VWE_PG_artist(bpy.types.PropertyGroup):
    shot_id: StringProperty(name="镜头编号", description="留空时使用当前选中条带的镜头编号")
    shot_channel: IntProperty(name="镜头轨道", default=1, min=1, max=128)
    subtitle_channel: IntProperty(name="字幕轨道", default=10, min=1, max=128)
    subtitle_size: IntProperty(name="字幕字号", default=42, min=8, max=512)
    subtitle_font: StringProperty(name="字幕字体", subtype="FILE_PATH")
    subtitles_from_playhead: BoolProperty(name="从播放头起算")
    exposure: FloatProperty(name="曝光补偿", default=0, soft_min=-4, soft_max=4)
    fade_length: IntProperty(name="淡入淡出帧数", default=8, min=1, max=240)
    export_path: StringProperty(name="成片路径", subtype="FILE_PATH")
    export_codec: EnumProperty(name="编码", items=(
        ("H264", "H.264", "MP4 审片及发布"),
        ("PRORES", "ProRes 422 HQ", "MOV 中间母版"),
    ))
    mastering: EnumProperty(name="响度标准", items=(
        ("KEEP", "保留混音", ""), ("-14", "网络 · −14 LUFS", ""), ("-23", "广播 · −23 LUFS", ""),
    ))
    last_report: StringProperty()
    last_report_kind: StringProperty(default="INFO")


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
            unknown = len(result['diagnostics']['unmanaged_strips'])
            message = f'{count} 个受管片段 · {changed} 个有手改 · {unknown} 个普通条带'
            missing, duplicate = artist_ops.inspect_media(context.scene)
            if missing:
                message += "\n缺失素材：" + "、".join(missing[:4])
            if duplicate:
                message += "\n重复镜头：" + "、".join(duplicate[:4])
            warn = bool(missing or duplicate)
            artist_ops.report(context.scene, message, "WARN" if warn else "OK")
            self.report({'WARNING' if warn else 'INFO'}, message)
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}


class VWE_OT_export(bpy.types.Operator):
    bl_idname = "vwe.export_from_timeline"
    bl_label = "后台导出成片"
    bl_description = "在独立快照中渲染视频序列，并将核验后的编码成片写入目标路径"

    def execute(self, context):
        global _EXPORT_REQUEST
        scene = context.scene
        settings = scene.vwe_artist_ui
        if _RUNNER.running:
            return {"CANCELLED"}
        try:
            if not settings.export_path.strip():
                raise ValueError("请先选择成片路径")
            if not scene.sequence_editor or not scene.sequence_editor.strips:
                raise ValueError("请先在时间线添加条带")
            dest = Path(bpy.path.abspath(settings.export_path)).resolve().with_suffix(
                ".mp4" if settings.export_codec == "H264" else ".mov")
            if dest.exists() or not dest.parent.is_dir():
                raise ValueError("请选择现有目录内的新成片路径")
            ffmpeg, ffprobe = artist_ops.media_tools()
            blender = Path(bpy.app.binary_path)
            version = f"{bpy.app.version[0]}.{bpy.app.version[1]}"
            python = blender.with_name(version) / "python" / "bin" / "python.exe"
            if not python.is_file():
                raise ValueError("找不到 Blender 自带的后台 Python")
            work = Path(tempfile.mkdtemp(prefix="blender-vse-export-"))
            snapshot = work / "timeline.blend"
            if bpy.ops.wm.save_as_mainfile(filepath=str(snapshot), copy=True, relative_remap=True) != {"FINISHED"}:
                raise RuntimeError("无法制作剪辑工作快照")
            request_path = work / "request.json"
            status_path, result_path = work / "progress.json", work / "result.json"
            request = {"blend": str(snapshot), "scene": scene.name, "blender": str(blender),
                       "ffmpeg": str(ffmpeg), "ffprobe": str(ffprobe), "output": str(dest),
                       "codec": settings.export_codec, "mastering": settings.mastering}
            request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            scene_name, scene_pointer = scene.name, scene.as_pointer()

            def complete(ok, result):
                target = bpy.data.scenes.get(scene_name)
                if target is None or target.as_pointer() != scene_pointer:
                    return
                if result.get("cancelled"):
                    artist_ops.report(target, "成片导出已取消", "WARN")
                elif ok:
                    measured = result.get("integrated_lufs")
                    loudness = f" · {measured:.1f} LUFS" if measured is not None else ""
                    artist_ops.report(target, f"已导出 {dest.name} · {result['frames']} 帧{loudness}", "OK")
                else:
                    artist_ops.report(target, result.get("error", f"导出失败，日志位于 {work}"), "ERROR")

            _RUNNER.start([python, "-X", "utf8", Path(__file__).with_name("export_worker.py"), request_path],
                          label="成片导出", status_file=status_path, result_file=result_path, on_complete=complete)
            _EXPORT_REQUEST = work / "cancel"
            artist_ops.report(scene, "成片导出已开始", "RUNNING")
            return {"FINISHED"}
        except (OSError, RuntimeError, ValueError) as error:
            return artist_ops.failed(self, context, error)


class VWE_OT_cancel_export(bpy.types.Operator):
    bl_idname = "vwe.cancel_export"
    bl_label = "取消成片导出"

    def execute(self, context):
        if not _RUNNER.running or _EXPORT_REQUEST is None:
            return {"CANCELLED"}
        try:
            _EXPORT_REQUEST.touch(exist_ok=True)
        except OSError as error:
            return artist_ops.failed(self, context, error)
        artist_ops.report(context.scene, "正在取消成片导出", "RUNNING")
        return {"FINISHED"}


class VWE_PT_timeline(bpy.types.Panel):
    bl_label = ''
    bl_idname = 'VWE_PT_timeline'
    bl_space_type = THEME.editor_space
    bl_region_type = 'UI'
    bl_category = THEME.tab_label
    bl_options = {"HEADER_LAYOUT_EXPAND"}

    def draw_header(self, context):
        draw_brand_header(self.layout, DOMAIN, _ICONS, fallback_icon="SEQUENCE")

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        settings = scene.vwe_artist_ui
        if _RUNNER.running:
            progress_card(layout, DOMAIN, _ICONS, text=_RUNNER.detail or _RUNNER.label,
                          factor=_RUNNER.progress, cancel_operator="vwe.cancel_export")
        primary_button(layout, "vwe.inspect", "检查当前时间线",
                       icon_value=_ICONS.action_id("shot_replace"), icon="VIEWZOOM")
        editor = scene.sequence_editor
        strip = editor.active_strip if editor else None
        row = layout.row(align=True)
        row.prop(settings, "shot_id", text="镜头")
        row.operator("vwe.replace_shot_frames", text="原位换片", icon="FILE_REFRESH")
        if strip:
            ident = artist_ops._stable_shot(strip)
            if ident:
                layout.label(text=f"当前镜头 {ident}", icon="RENDERLAYERS")
            row = layout.row(align=True)
            row.prop(strip, "name", text="")
            row.prop(strip, "lock", text="")
            row.prop(strip, "mute", text="")

        _, rough = section_panel(layout, "vwe_rough", "分镜拼装与转场",
                                 icon_value=_ICONS.action_id("strip_comp"), default_closed=True)
        if rough is not None:
            prop_column(rough).prop(settings, "shot_channel")
            rough.operator("vwe.append_shots", text="导入镜头或图像序列", icon="IMPORT")
            row = rough.row(align=True)
            row.prop(settings, "fade_length", text="淡入淡出帧数")
            row.operator("vwe.quick_fade", text="应用")
            rough.operator("vwe.cross_shots", icon="TRANSFORM_ORIGINS")
            if strip and strip.type in {"IMAGE", "MOVIE", "META", "SCENE"}:
                if hasattr(strip, "use_proxy"):
                    rough.prop(strip, "use_proxy", text="使用代理预览")
                    if strip.use_proxy:
                        rough.operator("sequencer.rebuild_proxy", text="生成所选代理", icon="FILE_REFRESH")
                row = rough.row(align=True)
                row.prop(settings, "exposure")
                row.operator("vwe.strip_exposure", text="应用")

        _, subtitles = section_panel(layout, "vwe_subtitles", "字幕轨道与样式",
                                      icon_value=_ICONS.action_id("subtitle_style"), default_closed=True)
        if subtitles is not None:
            props = prop_column(subtitles)
            props.prop(settings, "subtitle_channel")
            props.prop(settings, "subtitle_size")
            props.prop(settings, "subtitle_font")
            subtitles.prop(settings, "subtitles_from_playhead")
            subtitles.operator("vwe.import_srt", icon="IMPORT")
            if strip and strip.type == "TEXT":
                props = prop_column(subtitles)
                props.enabled = not strip.lock
                for field, title in (("text", "字幕正文"), ("font_size", "字号"), ("location", "位置"),
                                     ("color", "颜色"), ("use_box", "背景框"), ("use_outline", "描边")):
                    props.prop(strip, field, text=title)

        _, export = section_panel(layout, "vwe_export", "响度母带与成片导出",
                                   icon_value=_ICONS.action_id("lufs_export"), default_closed=True)
        if export is not None:
            props = prop_column(export)
            props.prop(settings, "export_path")
            props.prop(settings, "export_codec")
            props.prop(settings, "mastering")
            primary_button(export, "vwe.export_from_timeline", "后台导出成片",
                           icon_value=_ICONS.action_id("lufs_export"), enabled=not _RUNNER.running)
        status_banner(layout, context, settings.last_report, kind=settings.last_report_kind, icon_pack=_ICONS)


CLASSES = (VWE_PG_artist, VWE_OT_inspect, *artist_ops.CLASSES,
           VWE_OT_export, VWE_OT_cancel_export, VWE_PT_timeline)


def register():
    _ICONS.register()
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.vwe_inspection = bpy.props.StringProperty(options={'SKIP_SAVE'})
    bpy.types.Scene.vwe_artist_ui = PointerProperty(type=VWE_PG_artist)
    SidebarDock.register_plugin(DOMAIN, _ICONS.icons_dir)


def unregister():
    if _RUNNER.running and _EXPORT_REQUEST is not None:
        _EXPORT_REQUEST.touch(exist_ok=True)
    _RUNNER.cleanup()
    HUDManager.clear(DOMAIN)
    SidebarDock.unregister_plugin(DOMAIN)
    if hasattr(bpy.types.Scene, "vwe_artist_ui"):
        del bpy.types.Scene.vwe_artist_ui
    if hasattr(bpy.types.WindowManager, 'vwe_inspection'):
        del bpy.types.WindowManager.vwe_inspection
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    _ICONS.unregister()
