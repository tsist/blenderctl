# SPDX-License-Identifier: GPL-3.0-or-later
"""Render a snapshot in Blender, encode it, and verify the delivered stream."""

from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback


class Cancelled(Exception):
    pass


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path, payload):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    deadline = time.monotonic() + 2
    while True:
        try:
            temporary.replace(path)
            return
        except OSError as error:
            if getattr(error, "winerror", None) not in {5, 32, 33} or time.monotonic() >= deadline:
                raise
            time.sleep(0.015)


def _status(root, factor, detail):
    _write(root / "progress.json", {"progress": factor, "detail": detail})


def _start(argv, root, log_name):
    log_path = root / log_name
    with log_path.open("wb") as stream:
        child = subprocess.Popen(
            [str(value) for value in argv], stdin=subprocess.DEVNULL,
            stdout=stream, stderr=subprocess.STDOUT,
        )
        try:
            while child.poll() is None:
                if (root / "cancel").exists():
                    raise Cancelled
                time.sleep(0.2)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
        if child.returncode:
            raise RuntimeError(f"后台程序执行失败，请查看 {log_path}")
    return log_path


def render_snapshot(request_file):
    import bpy

    # Resolve the package by the current installed folder, also in bl_ext deployments.
    from importlib import import_module

    package = Path(__file__).resolve().parent
    sys.path.insert(0, str(package.parent))
    finishing = import_module(f"{package.name}.finishing")
    request = _json(request_file)
    root = request_file.parent
    scene = bpy.data.scenes[request["scene"]]
    bpy.context.window.scene = scene
    editor = scene.sequence_editor
    if not editor or not editor.strips:
        raise ValueError("时间线没有条带")
    sources = finishing.media_snapshot(scene)
    start, end = scene.frame_start, scene.frame_end
    count = end - start + 1
    if count < 1 or start < 0:
        raise ValueError("导出范围需要从第 0 帧或以后开始")
    fps = Fraction(scene.render.fps / scene.render.fps_base).limit_denominator(1001)
    if scene.camera is None:
        data = bpy.data.cameras.new("剪辑后台占位相机")
        scene.camera = bpy.data.objects.new(data.name, data)
        scene.collection.objects.link(scene.camera)

    frames = root / "frames"
    frames.mkdir()
    scene.render.use_sequencer = True
    scene.render.use_compositing = False
    scene.render.use_stamp = False
    settings = scene.render.image_settings
    settings.media_type = "IMAGE"
    settings.file_format = "PNG"
    settings.color_mode = "RGB"
    settings.color_depth = "8"
    scene.render.filepath = str(frames / "frame-######")
    scene.render.use_file_extension = True

    def frame_written(rendered_scene, *_):
        done = rendered_scene.frame_current - start + 1
        _status(root, 0.05 + 0.74 * done / count, f"正在输出画面 {done}/{count}")

    bpy.app.handlers.render_write.append(frame_written)
    try:
        if bpy.ops.render.render(animation=True, scene=scene.name) != {"FINISHED"}:
            raise RuntimeError("序列画面输出失败")
    finally:
        bpy.app.handlers.render_write.remove(frame_written)
    audio_enabled = any(strip.type == "SOUND" and not strip.mute for strip in editor.strips_all)
    if audio_enabled:
        _status(root, 0.80, "正在混合音轨")
        if bpy.ops.sound.mixdown(filepath=str(root / "mix.wav"), check_existing=False, container="WAV",
                                 codec="PCM", format="S16", channels="STEREO", mixrate=48000) != {"FINISHED"}:
            raise RuntimeError("无法输出时间线混音")
    if finishing.media_snapshot(scene) != sources:
        raise RuntimeError("导出期间受管素材发生变化")
    if len(list(frames.glob("frame-*.png"))) != count:
        raise RuntimeError("输出画面帧数与时间线不一致")
    _write(root / "rendered.json", {"start": start, "frames": count, "fps": str(fps), "audio": audio_enabled})


def _ffmpeg(request):
    return [request["ffmpeg"], "-hide_banner", "-nostdin"]


def _normalize(request, root):
    target = int(request["mastering"])
    base = f"loudnorm=I={target}:TP=-1.8:LRA=7"
    source = root / "mix.wav"
    first = _start([*_ffmpeg(request), "-i", source, "-af", base + ":print_format=json", "-f", "null", "-"],
                   root, "loudness-analysis.log")
    report = json.loads(re.findall(r"{[^{}]*}", first.read_text(encoding="utf-8", errors="replace"))[-1])
    fields = {"measured_I": "input_i", "measured_TP": "input_tp", "measured_LRA": "input_lra",
              "measured_thresh": "input_thresh", "offset": "target_offset"}
    if not all(math.isfinite(float(report[key])) for key in fields.values()):
        raise ValueError("无法测量静音素材，选择「保留混音」可照常导出")
    measurement = ":".join(f"{option}={report[key]}" for option, key in fields.items())
    return f"{base}:{measurement}:linear=true,aresample=48000"


def _encoded_file(root, request, rendered):
    output = Path(request["output"])
    pending = output.with_name(f".{output.stem}-{root.name[-8:]}{output.suffix}")
    cmd = [*_ffmpeg(request), "-v", "error", "-n", "-framerate", rendered["fps"],
           "-start_number", rendered["start"], "-i", root / "frames" / "frame-%06d.png"]
    if rendered["audio"]:
        cmd.extend(("-i", root / "mix.wav", "-map", "0:v:0", "-map", "1:a:0"))
        fps = Fraction(rendered["fps"])
        samples = round(Fraction(rendered["frames"] * 48000, 1) / fps)
        chain = f"aresample=48000,apad,atrim=end_sample={samples},asetpts=N/SR/TB"
        if request["mastering"] != "KEEP":
            _status(root, 0.81, "正在计算目标响度")
            chain = _normalize(request, root) + "," + chain
        cmd.extend(("-af", chain, "-ar", 48000))
    elif request["mastering"] != "KEEP":
        raise ValueError("时间线没有可做响度处理的音轨")
    else:
        cmd.append("-an")
    conversion = "colorspace=iall=bt709:itrc=srgb:irange=pc:all=bt709:range=tv:fast=0"
    if request["codec"] == "PRORES":
        cmd.extend(("-vf", conversion + ",format=yuv422p10le", "-c:v", "prores_ks", "-profile:v", "3"))
        if rendered["audio"]:
            cmd.extend(("-c:a", "pcm_s24le"))
    else:
        cmd.extend(("-vf", conversion + ",format=yuv420p", "-c:v", "libx264", "-crf", "17",
                    "-preset", "veryfast", "-movflags", "+faststart"))
        if rendered["audio"]:
            cmd.extend(("-c:a", "aac", "-b:a", "256k"))
    cmd.extend(("-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", pending))
    try:
        _status(root, 0.86, "正在编码成片")
        _start(cmd, root, "encode.log")
        _status(root, 0.94, "正在复核成片帧数与编码")
        probe = _start([request["ffprobe"], "-v", "error", "-count_frames", "-show_streams",
                        "-of", "json", pending], root, "probe.json")
        streams = _json(probe)["streams"]
        video = next(stream for stream in streams if stream["codec_type"] == "video")
        expected_codec = "prores" if request["codec"] == "PRORES" else "h264"
        if video["codec_name"] != expected_codec or int(video["nb_read_frames"]) != rendered["frames"]:
            raise RuntimeError("成片编码或帧数核验失败")
        payload = {"ok": True, "output": str(output), "frames": rendered["frames"], "codec": expected_codec}
        if rendered["audio"] and request["mastering"] != "KEEP":
            audit = _start([*_ffmpeg(request), "-i", pending, "-vn", "-af", "ebur128=peak=true",
                            "-f", "null", "-"], root, "loudness-verify.log")
            summary = audit.read_text(encoding="utf-8", errors="replace").rsplit("Summary:", 1)[-1]
            measured = float(re.search(r"I:\s+([-\d.]+) LUFS", summary)[1])
            peak = float(re.search(r"Peak:\s+([-\d.]+) dBFS", summary)[1])
            if abs(measured - int(request["mastering"])) > 1.0 or peak > -1:
                raise RuntimeError(f"响度或真峰值未通过复核：{measured:.1f} LUFS / {peak:.1f} dBTP")
            payload.update(integrated_lufs=measured, true_peak_dbtp=peak)
        if (root / "cancel").exists():
            raise Cancelled
        if output.exists():
            raise FileExistsError(f"成片路径已有文件：{output}")
        pending.rename(output)
        return payload
    finally:
        if pending.exists():
            pending.unlink()


def run(request_file):
    root = request_file.parent
    request = _json(request_file)
    result = root / "result.json"
    try:
        _status(root, 0.03, "正在加载时间线快照")
        _start([request["blender"], "--background", "--factory-startup", "--disable-autoexec", "--offline-mode",
                request["blend"], "--python-exit-code", 9, "--python", __file__, "--", request_file],
               root, "blender.log")
        _write(result, _encoded_file(root, request, _json(root / "rendered.json")))
        for path in (root / "frames").glob("frame-*.png"):
            path.unlink()
        (root / "frames").rmdir()
    except Cancelled:
        _write(result, {"ok": False, "cancelled": True})
    except Exception as error:
        (root / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
        _write(result, {"ok": False, "error": f"{error}\n日志：{root}"})


if __name__ == "__main__":
    if "--" in sys.argv:
        render_snapshot(Path(sys.argv[sys.argv.index("--") + 1]))
    else:
        run(Path(sys.argv[1]))
