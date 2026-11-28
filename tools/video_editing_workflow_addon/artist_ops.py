# SPDX-License-Identifier: GPL-3.0-or-later
"""VSE 操作入口，使用受管条带的现有核心格式。"""

from hashlib import file_digest
from pathlib import Path
import re
import shutil
import uuid

import bpy
from bpy.props import CollectionProperty, StringProperty

from . import core, finishing, subtitles
from .contract import WorkflowError, validate_clip
from ._ui_kit import apply_node_theme, apply_strip_theme

_IMAGE = {".png", ".jpeg", ".jpg", ".tif", ".tiff", ".exr", ".webp", ".bmp", ".tga"}
_MOVIE = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".mxf"}
_AUDIO = {".wav", ".flac", ".ogg", ".mp3", ".m4a", ".aac"}


def report(scene, message, kind="INFO"):
    settings = scene.vwe_artist_ui
    settings.last_report = message
    settings.last_report_kind = kind


def failed(operator, context, error):
    message = str(error)
    report(context.scene, message, "ERROR")
    operator.report({"ERROR"}, message)
    return {"CANCELLED"}


def media_tools():
    binary = Path(bpy.app.binary_path).resolve()
    for parent in binary.parents:
        if not (parent / "blenderctl.ps1").is_file():
            continue
        for bin_dir in (parent / "tools" / "ffmpeg").glob("*/bin"):
            ffmpeg, ffprobe = bin_dir / "ffmpeg.exe", bin_dir / "ffprobe.exe"
            if ffmpeg.is_file() and ffprobe.is_file():
                return ffmpeg, ffprobe
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return Path(ffmpeg), Path(ffprobe)
    raise ValueError("找不到工作区的 FFmpeg 与 FFprobe")


def _descriptor(path):
    with path.open("rb") as stream:
        return {"file": str(path), "expected_sha256": file_digest(stream, "sha256").hexdigest()}


def _active(scene):
    return scene.sequence_editor.active_strip if scene.sequence_editor else None


def _stable_shot(strip):
    if strip is None:
        return None
    return strip.get("shot_id") or core._read(strip, "_vwe_spec", {}).get("shot_id")


def _locked(scene, strip):
    channels = scene.sequence_editor.channels
    return strip.lock or (strip.channel <= len(channels) and channels[strip.channel - 1].lock)


def inspect_media(scene):
    editor = scene.sequence_editor
    if editor is None:
        return [], []
    missing, seen, duplicate = [], set(), set()
    for strip in editor.strips_all:
        ident = _stable_shot(strip)
        if ident:
            if ident in seen:
                duplicate.add(ident)
            seen.add(ident)
        paths = []
        if strip.type == "IMAGE":
            base = Path(bpy.path.abspath(strip.directory))
            paths = (base / elem.filename for elem in strip.elements)
        elif strip.type == "MOVIE":
            paths = (Path(bpy.path.abspath(strip.filepath)),)
        elif strip.type == "SOUND" and strip.sound and not strip.sound.packed_file:
            paths = (Path(bpy.path.abspath(strip.sound.filepath, library=strip.sound.library)),)
        if any(not path.is_file() for path in paths):
            missing.append(strip.name)
    return missing, sorted(duplicate)


def _paths(operator):
    base = Path(bpy.path.abspath(operator.directory)) if operator.directory else None
    paths = [base / item.name for item in operator.files] if base and operator.files else []
    if not paths and operator.filepath:
        paths = [Path(bpy.path.abspath(operator.filepath))]
    if not paths:
        raise ValueError("请选择媒体文件")
    paths = [path.resolve(strict=True) for path in paths]
    if any(not path.is_file() for path in paths):
        raise ValueError("所选路径必须是媒体文件")
    return sorted(dict.fromkeys(paths), key=lambda p: [
        (0, int(part)) if part.isdigit() else (1, part.casefold()) for part in re.split(r"([0-9]+)", p.name)
    ])


def _track_validate(editor, specs):
    existing = []
    if editor:
        existing = [{"channel": strip.channel, "start": int(strip.left_handle), "duration": int(strip.duration)}
                    for strip in editor.strips]
        for spec in specs:
            index = spec["channel"] - 1
            if index < len(editor.channels) and editor.channels[index].lock:
                raise ValueError(f"轨道 {index + 1} 已锁定")
    core._preflight_collisions({spec["id"]: spec for spec in specs}, existing)


def _install(scene, specs):
    checked = [validate_clip(spec) for spec in specs]
    if not checked:
        raise ValueError("没有可用条带")
    _track_validate(scene.sequence_editor, checked)
    editor = scene.sequence_editor_create()
    owner = scene.get("_vwe_workflow") or "vse-" + uuid.uuid4().hex[:12]
    before = {strip.as_pointer() for strip in editor.strips}
    created = []
    font_cache = {}
    try:
        for spec in checked:
            strip = core._make(scene, spec, owner, font_cache=font_cache)
            apply_strip_theme(strip, "video")
            core._store(scene, strip, spec)
            created.append(strip)
    except Exception:
        for strip in reversed(list(editor.strips)):
            if strip.as_pointer() not in before:
                editor.strips.remove(strip)
        raise
    scene["_vwe_workflow"] = owner
    scene["_vwe_version"] = "1.0"
    scene.render.use_sequencer = True
    scene.frame_end = max(scene.frame_end, max(int(strip.right_handle) - 1 for strip in created))
    scene["_vwe_timeline"] = core._json(core._timeline(scene))
    editor.active_strip = created[0]
    return created


def _native_duration(scratch, path, kind):
    editor = scratch.sequence_editor_create()
    create = editor.strips.new_movie if kind == "MOVIE" else editor.strips.new_sound
    strip = create(name=path.name, filepath=str(path), channel=1, frame_start=1)
    duration = int(strip.content_duration)
    editor.strips.remove(strip)
    return duration


def _replace_movie(scene, strip, path):
    if path.suffix.lower() not in _MOVIE:
        raise ValueError("请选择正式视频文件")
    scratch = bpy.data.scenes.new("剪辑媒体探针")
    try:
        scratch.render.fps, scratch.render.fps_base = scene.render.fps, scene.render.fps_base
        if _native_duration(scratch, path, "MOVIE") != strip.content_duration:
            raise ValueError("正式视频与原片的源帧数必须一致，包含手柄帧")
    finally:
        bpy.data.scenes.remove(scratch)
    old_path = strip.filepath
    spec = core._read(strip, "_vwe_spec", {})
    prior = {key: {"baseline": core._read(strip, key), "observed": observed}
             for key, observed in (("_vwe_baseline", core._actual(scene, strip)),
                                   ("_vwe_native", core.native_strip_record(strip)),
                                   ("_vwe_curves", core._curve_snapshot(scene, strip)))} if spec else None
    before = (strip.left_handle, strip.right_handle, strip.channel, strip.content_start)
    descriptor = _descriptor(path)
    try:
        strip.filepath = str(path)
        if before != (strip.left_handle, strip.right_handle, strip.channel, strip.content_start):
            raise RuntimeError("换片改变了镜头的切点或媒体入点")
        if spec:
            spec["source"]["files"] = [descriptor]
            core._store(scene, strip, spec, prior)
    except Exception:
        strip.filepath = old_path
        raise


def _file_dialog(operator, context):
    context.window_manager.fileselect_add(operator)
    return {"RUNNING_MODAL"}


class VWE_OT_append_shots(bpy.types.Operator):
    bl_idname = "vwe.append_shots"
    bl_label = "导入分镜素材"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")
    directory: StringProperty(subtype="DIR_PATH")
    files: CollectionProperty(type=bpy.types.OperatorFileListElement)
    filter_glob: StringProperty(default="*.png;*.jpg;*.jpeg;*.exr;*.tif;*.mp4;*.mov;*.mkv;*.wav;*.flac;*.mp3",
                                options={"HIDDEN"})

    def invoke(self, context, event):
        return _file_dialog(self, context)

    def execute(self, context):
        scene = context.scene
        try:
            paths = _paths(self)
            image_sequence = all(path.suffix.lower() in _IMAGE for path in paths)
            groups = [paths] if image_sequence else [[path] for path in paths]
            channel = scene.vwe_artist_ui.shot_channel
            frame = scene.frame_current
            specs = []
            scratch = None
            try:
                for group in groups:
                    path = group[0]
                    suffix = path.suffix.lower()
                    if suffix in _IMAGE:
                        kind, duration = "IMAGE", len(group)
                    elif suffix in _MOVIE or suffix in _AUDIO:
                        kind = "MOVIE" if suffix in _MOVIE else "SOUND"
                        if scratch is None:
                            scratch = bpy.data.scenes.new("剪辑媒体探针")
                            scratch.render.fps, scratch.render.fps_base = scene.render.fps, scene.render.fps_base
                        duration = _native_duration(scratch, path, kind)
                    else:
                        raise ValueError(f"不支持的媒体格式：{suffix}")
                    ident = "shot_" + uuid.uuid4().hex[:10]
                    spec = {"id": ident, "kind": kind, "name": path.stem, "channel": channel,
                            "start": frame, "duration": duration,
                            "source": {"files": [_descriptor(item) for item in group]}}
                    if kind != "SOUND":
                        safe_stem = re.sub(r"[^A-Za-z0-9_-]", "_", path.stem)
                        explicit = scene.vwe_artist_ui.shot_id.strip() if len(groups) == 1 else ""
                        shot = explicit or (safe_stem if safe_stem[:1].isalpha() else "shot_" + safe_stem)
                        used = {_stable_shot(strip) for strip in scene.sequence_editor.strips_all} if scene.sequence_editor else set()
                        used.update(item.get("shot_id") for item in specs)
                        if explicit and explicit in used:
                            raise ValueError(f"镜头编号已在时间线中：{explicit}")
                        fallback = ident if shot in used or len(shot) > 64 else shot
                        spec["shot_id"] = fallback
                    specs.append(spec)
                    frame += duration
            finally:
                if scratch is not None:
                    bpy.data.scenes.remove(scratch)
            _install(scene, specs)
            report(scene, f"已从第 {specs[0]['start']} 帧拼接 {len(specs)} 个条带", "OK")
            return {"FINISHED"}
        except (OSError, RuntimeError, ValueError, WorkflowError) as error:
            return failed(self, context, error)


class VWE_OT_replace_shot(bpy.types.Operator):
    bl_idname = "vwe.replace_shot_frames"
    bl_label = "按镜头编号原位换片"
    bl_description = "选择与原镜头匹配的正式图像序列或正式视频，保留切点、手柄和修饰符"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")
    directory: StringProperty(subtype="DIR_PATH")
    files: CollectionProperty(type=bpy.types.OperatorFileListElement)
    filter_glob: StringProperty(default="*.png;*.jpg;*.jpeg;*.exr;*.tif;*.tiff;*.webp;*.mp4;*.mov;*.mkv;*.avi;*.mxf",
                                options={"HIDDEN"})

    def invoke(self, context, event):
        return _file_dialog(self, context)

    def execute(self, context):
        scene = context.scene
        try:
            ident = scene.vwe_artist_ui.shot_id.strip() or _stable_shot(_active(scene))
            if not scene.sequence_editor or not ident:
                raise ValueError("请填写镜头编号或选中受管镜头")
            matches = [strip for strip in scene.sequence_editor.strips_all if _stable_shot(strip) == ident]
            if len(matches) != 1 or matches[0].type not in {"IMAGE", "MOVIE"} or _locked(scene, matches[0]):
                raise ValueError("请选择唯一、未锁定的图像序列或视频镜头")
            strip = matches[0]
            paths = _paths(self)
            if strip.type == "MOVIE":
                if len(paths) != 1:
                    raise ValueError("请选择一个正式视频文件")
                _replace_movie(scene, strip, paths[0])
            else:
                if len(paths) != len(strip.elements) or any(path.suffix.lower() not in _IMAGE for path in paths):
                    raise ValueError(f"请选择 {len(strip.elements)} 张正式帧，包括原有手柄帧")
                rows = [{"frame": int(strip.content_start) + index, "path": str(path),
                         "sha256": _descriptor(path)["expected_sha256"]} for index, path in enumerate(paths)]
                finishing.replace_shots(scene, [{"shot_id": ident, "frames": rows,
                                                "frame_start": int(strip.left_handle),
                                                "frame_end": int(strip.right_handle) - 1}])
            apply_strip_theme(strip, "video")
            report(scene, f"镜头 {ident} 已原位换片", "OK")
            return {"FINISHED"}
        except (OSError, RuntimeError, ValueError, WorkflowError) as error:
            return failed(self, context, error)


class VWE_OT_import_srt(bpy.types.Operator):
    bl_idname = "vwe.import_srt"
    bl_label = "导入 SRT 字幕"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.srt", options={"HIDDEN"})

    def invoke(self, context, event):
        return _file_dialog(self, context)

    def execute(self, context):
        scene = context.scene
        settings = scene.vwe_artist_ui
        try:
            path = Path(bpy.path.abspath(self.filepath)).resolve(strict=True)
            font = Path(bpy.path.abspath(settings.subtitle_font)).resolve(strict=True) if settings.subtitle_font else (
                Path(r"C:\Windows\Fonts\msyh.ttc"))
            descriptor = _descriptor(font) if font.is_file() else None
            parsed = subtitles.import_srt(path.read_text(encoding="utf-8-sig"), fps=core._timeline(scene)["fps"],
                                         timeline_start=scene.frame_current if settings.subtitles_from_playhead else scene.frame_start,
                                         channel=settings.subtitle_channel, prefix="sub_" + uuid.uuid4().hex[:8],
                                         font=descriptor, font_size=settings.subtitle_size)
            strips = _install(scene, parsed["clips"])
            for strip in strips:
                strip.location = (0.5, 0.09)
                core._store(scene, strip, core._read(strip, "_vwe_spec"))
            report(scene, f"已导入 {len(strips)} 条字幕", "OK")
            return {"FINISHED"}
        except (OSError, RuntimeError, ValueError, WorkflowError) as error:
            return failed(self, context, error)


class VWE_OT_cross_shots(bpy.types.Operator):
    bl_idname = "vwe.cross_shots"
    bl_label = "叠化两段相叠镜头"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        editor = scene.sequence_editor
        selected = [strip for strip in editor.strips if strip.select and strip.type != "SOUND"] if editor else []
        try:
            if len(selected) != 2 or any(_locked(scene, strip) for strip in selected):
                raise ValueError("请选择两段未锁定且时间相叠的画面条带")
            left, right = sorted(selected, key=lambda strip: (strip.left_handle, strip.channel))
            first, last = max(left.left_handle, right.left_handle), min(left.right_handle, right.right_handle)
            channel = max(left.channel, right.channel) + 1
            while channel <= 128:
                occupied = any(strip.channel == channel and strip.left_handle < last and strip.right_handle > first
                               for strip in editor.strips)
                locked = channel <= len(editor.channels) and editor.channels[channel - 1].lock
                if not occupied and not locked:
                    break
                channel += 1
            if channel > 128:
                raise ValueError("叠化区没有可用轨道")
            effect = finishing.cross(scene, left, right, "CROSS", channel)
            apply_strip_theme(effect, "video")
            report(scene, "已插入叠化转场", "OK")
            return {"FINISHED"}
        except (RuntimeError, ValueError) as error:
            return failed(self, context, error)


class VWE_OT_quick_fade(bpy.types.Operator):
    bl_idname = "vwe.quick_fade"
    bl_label = "淡入淡出"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        strip = _active(scene)
        try:
            if strip is None or _locked(scene, strip):
                raise ValueError("请选中未锁定的条带")
            sound = strip.type == "SOUND"
            field = "volume" if sound else "blend_alpha"
            path = strip.path_from_id(field)
            if any(curve.data_path == path for curve in core._curves(scene)):
                raise ValueError("条带已有手调淡入淡出曲线")
            duration = int(strip.duration)
            if duration < 3:
                raise ValueError("淡入淡出至少需要三帧")
            fade = min(scene.vwe_artist_ui.fade_length, (duration - 1) // 2)
            spec = {"kind": "SOUND" if sound else strip.type, "start": int(strip.left_handle), "duration": duration,
                    "fade_in": fade, "fade_out": fade, "volume" if sound else "opacity": getattr(strip, field)}
            core._fade(scene, strip, spec)
            report(scene, f"已应用 {fade} 帧淡入淡出", "OK")
            return {"FINISHED"}
        except (RuntimeError, ValueError) as error:
            return failed(self, context, error)


class VWE_OT_exposure(bpy.types.Operator):
    bl_idname = "vwe.strip_exposure"
    bl_label = "添加条带曝光修饰符"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        strip = _active(scene)
        try:
            if strip is None or strip.type == "SOUND" or _locked(scene, strip):
                raise ValueError("请选中未锁定的画面条带")
            modifier = finishing.compositor_modifier(strip, exposure=scene.vwe_artist_ui.exposure)
            modifier.name = "曝光补偿"
            for node in modifier.node_group.nodes:
                apply_node_theme(node, "video")
            report(scene, "已添加曝光修饰符", "OK")
            return {"FINISHED"}
        except (RuntimeError, ValueError) as error:
            return failed(self, context, error)


CLASSES = (VWE_OT_append_shots, VWE_OT_replace_shot, VWE_OT_import_srt,
           VWE_OT_cross_shots, VWE_OT_quick_fade, VWE_OT_exposure)
