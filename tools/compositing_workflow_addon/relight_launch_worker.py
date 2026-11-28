"""Prepare a depth bundle and start the existing guarded GPU session."""

import hashlib
import json
import math
from pathlib import Path
import struct
import sys
import time


def _write(path, value):
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _descriptor(raw):
    path = Path(raw).resolve(strict=True)
    with path.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"file": str(path), "expected_sha256": sha}


def _png_size(path):
    with Path(path).open("rb") as stream:
        header = stream.read(25)
    if len(header) != 25 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR" or header[24] != 8:
        raise ValueError("原图需要为 8 位 PNG")
    width, height = struct.unpack(">II", header[16:24])
    if not (4 <= width <= 2048 and 4 <= height <= 2048):
        raise ValueError("补光原图宽高均需在 4 至 2048 像素之间")
    return width, height


def _prepare_params(request):
    rgb = _descriptor(request["rgb"])
    raw = _descriptor(request["depth"])
    suffix = Path(raw["file"]).suffix.lower()
    if suffix not in {".npy", ".exr"}:
        raise ValueError("深度图需要为 NPY 或单通道 EXR")
    scalar = {"format": suffix[1:].upper(), "channel": (request["depth_channel"] or None) if suffix == ".exr" else None}
    width, height = _png_size(rgb["file"])
    return {
        "depth_prepare_version": "1.0", "rgb": rgb, "rgb_encoding": "display_sRGB_PNG8",
        "depth": raw | scalar,
        "domain": {
            "width": width, "height": height, "pixel_aspect": [1, 1], "origin": "top-left",
            "pixel_center": [0.5, 0.5], "row_direction": "down", "column_direction": "right",
        },
        "semantics": {"measure": "relative_score", "near_order": request["near_order"], "units": "unitless"},
        "provenance": {"kind": "declared_external", "statement": "创作者提供与原图对齐的相对深度"},
        "validity": {"kind": "all_frame_artistic_declared", "statement": "创作者将整幅画面作为补光范围"},
        "normalization": {"method": "percentile_near_one", "low": 2, "high": 98},
        "edits": [],
        "normal": {
            "method": "central_heightfield", "height_scale": request["depth_strength"],
            "max_height_jump": 0.35, "boundary_exclusion": None,
        },
        "budget": {"max_pixels": width * height, "timeout_seconds": 60}, "resources": [rgb, raw],
    }


def _config(frame, average_depth, mode):
    metric = frame["kind"] == "metric_perspective"
    if metric and mode == "ANIME":
        raise ValueError("动漫补光使用相对深度，请选择相对深度包或输入相对深度图")
    if not math.isfinite(average_depth) or average_depth <= 0:
        average_depth = 2
    point_z = -0.75 * average_depth if metric else 1.0
    point_scale = min(10, average_depth * 0.25) if metric else 1.0
    config = {
        "version": "relight-kernel-draft-0.3", "frame": frame, "mix": 1.0, "bypass": False,
        "material": {"roughness": 0.4, "f0": [0.04, 0.04, 0.04]}, "line_protect": 1.0,
        "quality": {"shadow_steps": 8, "shadow_bias": 0.01, "sun_reach": 5.0, "max_work": 2147483647},
        "lights": [
            {"id": "主光", "type": "POINT", "intensity": 0.2, "color": [1.0, 0.89, 0.77],
             "diffuse": 1.0, "specular": 0.35, "samples": 1, "position": [-0.3 * point_scale, 0.4 * point_scale, point_z],
             "radius": 0.0, "softening": 0.05, "reference_distance": point_scale},
            {"id": "辅光", "type": "POINT", "intensity": 0.08, "color": [0.74, 0.87, 1.0],
             "diffuse": 1.0, "specular": 0.15, "samples": 1, "position": [0.4 * point_scale, 0.1 * point_scale, point_z],
             "radius": 0.0, "softening": 0.05, "reference_distance": point_scale},
        ],
        "projection": {"enabled": False, "point": [0.0, 0.0, 0.0], "normal": [0.0, 0.0, 1.0],
                       "strength": 1.0, "transmission": [0.0, 0.0, 0.0], "light_ids": ["主光", "辅光"]},
        "base": {"exposure": 0.0, "contrast": 1.0, "saturation": 1.0, "mask": "ALL"},
    }
    if mode == "ANIME":
        config["version"] = "relight-kernel-draft-0.4"
        config["anime"] = {
            "form": 0.5, "wrap": 0.85, "para_spread": 1.1, "back_dark": 0.0,
            "back_span": 0.25, "dark_side_reach": 1.0, "shadow_lo": 0.15, "shadow_hi": 0.48,
            "shadow_tint": 0.0, "shadow_color": [0.25, 0.28, 0.38], "cel": 0.35,
            "cel_steps": 3, "cel_soft": 0.2, "rim_width": 3.0, "rim_edge0": 0.08,
            "rim_edge1": 0.22, "rim_gain": 0.0, "rim_white_mix": 0.0, "rim_light_ids": ["主光"],
            "line_core_lo": 0.08, "line_core_hi": 0.35, "solo_light_id": "",
            "subject_source": "ALPHA", "view": "RESULT", "delta_gain": 3.0,
        }
        config["environment"] = {
            "subject_tint": [1.0, 1.0, 1.0], "subject_exposure": 0.0,
            "background_tint": [1.0, 1.0, 1.0], "background_exposure": 0.0,
        }
    return config


def _session_params(bundle_file, profile_file, mode, frame_for):
    bundle_file = Path(bundle_file)
    if bundle_file.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("深度包描述超出 16 MiB")
    bundle = _read(bundle_file)
    if bundle.get("schema") != "compositing-depth-bundle-v1" or bundle.get("status") != "complete":
        raise ValueError("请选择完整的深度包")
    frame = frame_for(bundle)
    domain = bundle["request"]["params"]["domain"]
    pixels = int(domain["width"]) * int(domain["height"])
    profile = _descriptor(profile_file)
    depth = _descriptor(bundle_file)
    average = float(bundle.get("stats", {}).get("scalar_mean", 2))
    divisor = next(d for d in (4, 2, 1)
                   if all(domain[k] % d == 0 and domain[k] // d >= 4 for k in ("width", "height")))
    return {
        "depth_session_version": "1.0", "preview_divisor": divisor,
        "lifetime_seconds": 1200, "idle_seconds": 360, "max_revisions": 320,
        "revision_timeout_seconds": 45, "max_data_generations": 32,
        "relight": {
            "depth_relight_version": "1.0", "bundle": depth, "source": {"kind": "bundle_RGB"},
            "masks": {"light": None, "highlight": None, "line": None},
            "config": _config(frame, average, mode),
            "compute": {"device": "CUDA0_FP32", "runtime_profile": profile},
            "budget": {"max_pixels": pixels, "timeout_seconds": 90},
            "resources": [depth, profile],
        },
    }


def main(request_file):
    work = request_file.parent
    request = _read(request_file)
    result, status = work / "result.json", work / "status.json"
    cancel = work / "cancel"
    job = None
    delivered = False
    try:
        tools = Path(request["tools"]).resolve(strict=True)
        sys.path.insert(0, str(tools))
        import runner
        from compositing_relight_contract import frame_for

        def begin(command, params, timeout):
            if cancel.exists():
                raise InterruptedError("已取消补光任务")
            path = runner.prepare(
                {"schema_version": "1.0", "command": command, "params": params},
                blender=request["blender"], root=work / "j", timeout=timeout,
            )
            _write(work / "active.json", {"directory": str(path)})
            if cancel.exists():
                runner.cancel(path)
            runner.submit(path)
            return path

        def wait(path, *, artifact=None, seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if cancel.exists():
                    raise InterruptedError("已取消补光任务")
                if artifact is not None and (path / artifact).is_file():
                    return path / artifact
                outcome = path / "result.json"
                if outcome.is_file():
                    envelope = _read(outcome)
                    if not envelope.get("ok"):
                        raise RuntimeError(envelope.get("error", {}).get("message") or "补光后台作业失败")
                    if artifact is None:
                        return outcome
                    raise RuntimeError("补光后台作业结束，未发布预览会话")
                time.sleep(0.20)
            raise TimeoutError("等待补光结果超时")

        bundle = request["bundle"]
        if not bundle:
            _write(status, {"progress": 0.20, "detail": "正在准备相对深度与法线"})
            job = begin("compositing.depth-prepare", _prepare_params(request), 100)
            wait(job, seconds=100)
            bundle = job / "depth-bundle.json"
        _write(status, {"progress": 0.57, "detail": "正在校验并启动本机 GPU 补光会话"})
        params = _session_params(bundle, request["runtime_profile"], request["mode"], frame_for)
        job = begin("compositing.depth-session", params, 1400)
        ready = wait(job, artifact="session-ready.json", seconds=130)
        _write(result, {"ok": True, "ready": str(ready), "bundle": str(bundle)})
        delivered = True
        return 0
    except Exception as error:
        _write(result, {"ok": False, "error": str(error), "job": str(job) if job else None})
        return 1
    finally:
        if job is not None and not delivered:
            runner.cancel(job)


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
