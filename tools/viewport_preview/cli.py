# SPDX-License-Identifier: GPL-3.0-or-later
"""Public CLI for `blenderctl preview`: one-shot isolated-desktop & hot-session real viewport preview."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid
from typing import Any

sys.dont_write_bytecode = True
THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from __init__ import VERSION, VALID_MODES
import desktop_runner
import framing
import session_manager


ROOT = THIS_DIR.parents[1]
DEFAULT_JOBS = Path(os.environ.get("BLENDERCTL_JOBS_DIR") or (ROOT / "runtime" / "blenderctl" / "preview-jobs"))
WORKER_SCRIPT = THIS_DIR / "viewport_worker.py"


def _resolve_blender_binary(explicit: str | None, version: str | None = None) -> str:
    """Resolve Blender executable from --blender, --blender-version, BLENDER_PATH, portable tree or PATH."""
    if explicit:
        return str(Path(explicit).resolve())
    env_blender = os.environ.get("BLENDER_PATH")
    if env_blender and Path(env_blender).is_file():
        return str(Path(env_blender).resolve())
    ver = version or "5.3.0"
    roots = []
    if os.environ.get("BLENDERCTL_BLENDER_ROOT"):
        roots.append(Path(os.environ["BLENDERCTL_BLENDER_ROOT"]))
    roots.append(ROOT / "runtime" / "blender" / "releases")
    for base in roots:
        candidate = base / ver / f"blender-{ver}-windows-x64" / "blender.exe"
        if candidate.is_file():
            return str(candidate.resolve())
    found = shutil.which("blender")
    if found:
        return str(Path(found).resolve())
    raise FileNotFoundError(
        "Blender executable not found; pass --blender <path>, set BLENDER_PATH, "
        "or place Blender 5.3.0 under BLENDERCTL_BLENDER_ROOT."
    )


def _split_csv(items: list[str] | None) -> list[str] | None:
    if not items:
        return None
    out: list[str] = []
    for item in items:
        for part in item.split(","):
            s = part.strip()
            if s:
                out.append(s)
    return out or None


def _build_session_parser(subparsers) -> None:
    sp = subparsers.add_parser(
        "session",
        help="manage optional isolated-desktop hot preview sessions (start/status/stop/list)",
    )
    ss = sp.add_subparsers(dest="session_action", required=True)

    p_start = ss.add_parser("start", help="start a hidden isolated-desktop hot Blender session")
    p_start.add_argument("file", nargs="?", help="optional .blend hint")
    p_start.add_argument("--idle-timeout", type=float, default=180.0, help="auto-exit seconds when idle (default 180)")
    p_start.add_argument("--window-size", type=int, default=640, help="viewport window pixel size (default 640)")
    p_start.add_argument("--sessions-dir", type=Path, default=session_manager.DEFAULT_SESSIONS_ROOT)

    p_status = ss.add_parser("status", help="check hot preview session status")
    p_status.add_argument("session_id")
    p_status.add_argument("--sessions-dir", type=Path, default=session_manager.DEFAULT_SESSIONS_ROOT)

    p_stop = ss.add_parser("stop", help="stop a hot preview session and destroy its isolated desktop")
    p_stop.add_argument("session_id")
    p_stop.add_argument("--sessions-dir", type=Path, default=session_manager.DEFAULT_SESSIONS_ROOT)

    p_list = ss.add_parser("list", help="list recorded hot preview sessions")
    p_list.add_argument("--sessions-dir", type=Path, default=session_manager.DEFAULT_SESSIONS_ROOT)


def _add_capture_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("file", nargs="?", help="path to source .blend file (read-only; never modified)")
    p.add_argument("--request", type=Path, help="optional UTF-8 JSON request file (CLI flags override JSON fields)")
    p.add_argument("--expected-sha256", help="optional strict SHA256 check on source .blend")
    p.add_argument("--scene", help="scene name (defaults to active saved scene)")
    p.add_argument("--view-layer", help="view layer name (defaults to active view layer)")
    p.add_argument("--frame", type=int, help="timeline frame to evaluate before capture")

    # Focus & isolation
    p.add_argument("--objects", nargs="+", action="extend", help="object names to focus (comma or space separated)")
    p.add_argument("--collection", help="collection name to focus")
    p.add_argument("--isolate", action="store_true", help="hide non-target geometry while keeping lights/world")

    # Layout & framing
    p.add_argument("--layout", default=None, choices=["quad", "2x2", "orbit", "3x3", "single"],
                   help="view grid preset (default: quad = TOP/FRONT/RIGHT ortho + FRONT_3Q persp)")
    p.add_argument("--views", nargs="+", action="extend",
                   help="explicit view presets (comma or space separated: front,right,top,front_3q,back,left,bottom,rear_3q,top_3q,camera:<Name>)")
    p.add_argument("--view", help="single view preset or camera:<Name> (used with single layout or --compare-modes)")
    p.add_argument("--projection", choices=["ortho", "persp"], help="force ORTHO or PERSP across preset views")
    p.add_argument("--margin", type=float, default=None, help="framing margin 0.03..0.40 (default 0.10)")
    p.add_argument("--lens", type=float, default=None, help="perspective focal length in mm (default 50)")
    p.add_argument("--framing-from", type=Path, help="lock identical framing center/scale from a prior preview.json")

    # Display mode & overlays
    p.add_argument("--mode", default=None, choices=list(VALID_MODES),
                   help="display mode (default: solid_wire)")
    p.add_argument("--compare-modes", nargs="+", action="extend",
                   help="same-view multi-mode comparison grid, e.g. solid,solid_wire,material,rendered")
    p.add_argument("--wire-opacity", type=float, default=None,
                   help="semi-transparent wireframe opacity 0..1 for solid_wire (default 0.45)")
    p.add_argument("--wire-overlay", type=float, default=None,
                   help="overlay semi-transparent wireframe 0..1 onto ANY display mode (solid/matcap/material/rendered)")
    p.add_argument("--wire-threshold", type=float, default=None,
                   help="wireframe edge angle threshold 0..1 (default 1.0 shows all edges)")
    p.add_argument("--xray", dest="xray", action="store_true", default=None, help="enable X-Ray in wireframe mode (default on)")
    p.add_argument("--no-xray", dest="xray", action="store_false", help="disable X-Ray in wireframe mode")
    p.add_argument("--xray-alpha", type=float, default=None, help="wireframe X-Ray alpha 0..1 (default 0.5)")
    p.add_argument("--wire-color", choices=["theme", "object", "random"], help="wireframe color type")
    p.add_argument("--solid-color", choices=["single", "material", "object", "random", "vertex", "texture"],
                   help="solid shading color type (default single; use random to color-code parts)")
    p.add_argument("--matcap", help="matcap filename or keyword for matcap mode (default metal_carpaint.exr)")
    p.add_argument("--use-scene-lights", action="store_true", default=None, help="use scene lights in material mode")
    p.add_argument("--use-scene-world", action="store_true", default=None, help="use scene world in material mode")
    p.add_argument("--studio-world", help="built-in HDRI for material preview (studio.exr/forest.exr/city.exr/...)")

    # Rendered preview (DLSS / OptiX / EEVEE)
    p.add_argument("--render-engine", choices=["cycles", "eevee"], help="engine for rendered mode (default cycles)")
    p.add_argument("--samples", type=int, default=None, help="viewport samples for rendered mode (default 8)")
    p.add_argument("--denoiser", choices=["auto", "dlss", "optix", "oidn", "none"],
                   help="Cycles viewport denoiser (default auto: DLSS if driver>=620 else explicit OPTIX fallback)")
    p.add_argument("--strict-denoiser", action="store_true", default=None,
                   help="fail instead of falling back if requested denoiser (e.g. DLSS) is unavailable")
    p.add_argument("--dlss-quality", choices=["none", "quality", "balanced", "performance", "ultra_performance"],
                   help="DLSS upscale quality mode (default balanced)")
    p.add_argument("--strict-scene-lights", action="store_true", default=None,
                   help="do not add temporary studio fill lights when scene has no lights")
    p.add_argument("--settle-timeout", type=float, default=None,
                   help="max seconds to wait per rendered tile convergence (default 15.0)")

    # Output & session reuse
    p.add_argument("--cell-size", type=int, default=None, help="tile pixel size 256..1024 (default 640, auto-capped <=2048px total)")
    p.add_argument("--output", "-o", type=Path, help="output PNG path (default: <jobs-dir>/<job_id>/preview.png)")
    p.add_argument("--save-tiles", action="store_true", default=None, help="also write individual per-view PNG files")
    p.add_argument("--session", help="reuse an active hot preview session ID instead of spawning one-shot Blender")
    p.add_argument("--sessions-dir", type=Path, default=session_manager.DEFAULT_SESSIONS_ROOT)
    p.add_argument("--force-reload", action="store_true", default=None, help="force reload .blend inside hot session")


def _parse_args(argv: list[str]) -> tuple[str, argparse.Namespace]:
    # Extract global flags injected by blenderctl.ps1 or supplied before `preview`
    g = argparse.ArgumentParser(add_help=False)
    g.add_argument("--blender", default=None)
    g.add_argument("--blender-version", default=None)
    g.add_argument("--jobs-dir", type=Path, default=DEFAULT_JOBS)
    g.add_argument("--transactions-dir", help=argparse.SUPPRESS)
    g.add_argument("--timeout", type=float, default=90.0)
    g.add_argument("--human", action="store_true")
    g.add_argument("--compact", action="store_true")
    g.add_argument("--full", action="store_true")
    global_ns, rest = g.parse_known_args(argv)

    # Support both `cli.py preview ...` (portable launcher) and `cli.py ...` (direct handoff)
    if rest and rest[0] == "preview":
        rest = rest[1:]

    # Dispatch `preview session ...` vs `preview [shot] <file.blend> ...`
    if rest and rest[0] == "session":
        root_p = argparse.ArgumentParser(prog="blenderctl preview", parents=[g])
        sub = root_p.add_subparsers(dest="subcmd", required=True)
        _build_session_parser(sub)
        ns = root_p.parse_args(rest)
        for k, v in vars(global_ns).items():
            if getattr(ns, k, None) is None or k in ("human", "compact", "full"):
                setattr(ns, k, getattr(ns, k, False) or v if k in ("human", "compact", "full") else v)
        return "session", ns

    if rest and rest[0] == "shot":
        rest = rest[1:]

    cap_p = argparse.ArgumentParser(
        prog="blenderctl preview",
        description=(
            "Fast isolated-desktop real-viewport 4-view preview for Blender 5.3.0.\n"
            "Focuses objects/collections/scenes with zero desktop disruption and supports "
            "wireframe, solid, solid_wire (semi-transparent wireframe), matcap, "
            "face_orientation, material (LookDev), and rendered (Cycles DLSS / OptiX / EEVEE Next).\n\n"
            "Subcommand: `blenderctl preview session {start,status,stop,list}` for hot session reuse."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_capture_args(cap_p)
    cap_ns = cap_p.parse_args(rest)
    for k, v in vars(global_ns).items():
        setattr(cap_ns, k, v)
    return "capture", cap_ns


def _build_spec(ns: argparse.Namespace, job_dir: Path) -> dict[str, Any]:
    spec: dict[str, Any] = {}
    if ns.request:
        req_path = ns.request.resolve()
        if not req_path.is_file():
            raise FileNotFoundError(f"Request JSON not found: {req_path}")
        raw = json.loads(req_path.read_text(encoding="utf-8-sig"))
        if isinstance(raw, dict) and "params" in raw and isinstance(raw["params"], dict):
            spec.update(raw["params"])
        elif isinstance(raw, dict):
            spec.update(raw)
        else:
            raise ValueError("Request JSON must be an object")

    if ns.file:
        spec["file"] = str(Path(ns.file).resolve())
    elif "file" in spec:
        spec["file"] = str(Path(spec["file"]).resolve())
    else:
        raise ValueError("Provide a .blend file path or --request JSON containing 'file'")

    if ns.expected_sha256:
        spec["expected_sha256"] = ns.expected_sha256
    if ns.scene:
        spec["scene"] = ns.scene
    if ns.view_layer:
        spec["view_layer"] = ns.view_layer
    if ns.frame is not None:
        spec["frame"] = ns.frame

    objs = _split_csv(ns.objects)
    if objs:
        spec["objects"] = objs
    if ns.collection:
        spec["collection"] = ns.collection
    if ns.isolate:
        spec["isolate"] = True

    if ns.layout:
        spec["layout"] = ns.layout
    views = _split_csv(ns.views)
    if views:
        spec["views"] = views
    if ns.view:
        spec["view"] = ns.view
    if ns.projection:
        spec["projection"] = ns.projection
    if ns.margin is not None:
        spec["margin"] = ns.margin
    if ns.lens is not None:
        spec["lens"] = ns.lens
    if ns.framing_from:
        prior = json.loads(ns.framing_from.resolve().read_text(encoding="utf-8-sig"))
        if isinstance(prior, dict) and isinstance(prior.get("framing"), dict):
            spec["framing"] = prior["framing"]
        elif isinstance(prior, dict) and isinstance(prior.get("data"), dict) and isinstance(prior["data"].get("framing"), dict):
            spec["framing"] = prior["data"]["framing"]
        else:
            raise ValueError(f"No valid 'framing' object found in {ns.framing_from}")

    if ns.mode:
        spec["mode"] = ns.mode
    cmodes = _split_csv(ns.compare_modes)
    if cmodes:
        spec["compare_modes"] = cmodes
    if ns.wire_opacity is not None:
        spec["wire_opacity"] = ns.wire_opacity
    if ns.wire_overlay is not None:
        spec["wire_overlay"] = ns.wire_overlay
    if ns.wire_threshold is not None:
        spec["wire_threshold"] = ns.wire_threshold
    if ns.xray is not None:
        spec["xray"] = ns.xray
    if ns.xray_alpha is not None:
        spec["xray_alpha"] = ns.xray_alpha
    if ns.wire_color:
        spec["wire_color"] = ns.wire_color
    if ns.solid_color:
        spec["solid_color"] = ns.solid_color
    if ns.matcap:
        spec["matcap"] = ns.matcap
    if ns.use_scene_lights is not None:
        spec["use_scene_lights"] = ns.use_scene_lights
    if ns.use_scene_world is not None:
        spec["use_scene_world"] = ns.use_scene_world
    if ns.studio_world:
        spec["studio_world"] = ns.studio_world

    if ns.render_engine:
        spec["render_engine"] = ns.render_engine
    if ns.samples is not None:
        spec["samples"] = ns.samples
    if ns.denoiser:
        spec["denoiser"] = ns.denoiser
    if ns.strict_denoiser is not None:
        spec["strict_denoiser"] = ns.strict_denoiser
    if ns.dlss_quality:
        spec["dlss_quality"] = ns.dlss_quality
    if ns.strict_scene_lights is not None:
        spec["strict_scene_lights"] = ns.strict_scene_lights
    if ns.settle_timeout is not None:
        spec["settle_timeout"] = ns.settle_timeout

    if ns.cell_size is not None:
        spec["cell_size"] = max(256, min(1024, int(ns.cell_size)))
    if ns.save_tiles is not None:
        spec["save_tiles"] = ns.save_tiles
    if ns.force_reload is not None:
        spec["force_reload"] = ns.force_reload

    # Defaults
    spec.setdefault("mode", "solid_wire")
    spec.setdefault("layout", "quad")
    spec.setdefault("cell_size", 640)
    spec.setdefault("margin", 0.10)
    spec.setdefault("lens", 50.0)
    spec.setdefault("denoiser", "auto")
    spec.setdefault("dlss_quality", "balanced")
    spec.setdefault("samples", 8)

    if ns.output:
        out_png = ns.output.resolve()
    elif "output" in spec:
        out_png = Path(spec["output"]).resolve()
    else:
        out_png = (job_dir / "preview.png").resolve()
    if out_png.suffix.lower() != ".png":
        raise ValueError("Output path must end with .png")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    spec["output"] = str(out_png)
    spec["report_path"] = str(out_png.with_suffix(".json"))

    # Pre-validate views & modes early before launching Blender
    if spec.get("compare_modes"):
        for m in spec["compare_modes"]:
            if m.lower() not in VALID_MODES:
                raise ValueError(f"Unknown mode in compare_modes: {m!r}")
        if spec.get("view"):
            framing.parse_view_token(spec["view"], spec.get("projection"))
    else:
        if spec["mode"].lower() not in VALID_MODES:
            raise ValueError(f"Unknown mode: {spec['mode']!r}")
        layout = spec["layout"]
        views_arg = spec.get("views")
        if spec.get("view") and not views_arg:
            layout = "single"
            views_arg = [spec["view"]]
        framing.resolve_views(layout, views_arg, spec.get("projection"))

    return spec


def _emit(payload: dict[str, Any], ns: argparse.Namespace) -> int:
    if getattr(ns, "human", False):
        if payload.get("ok"):
            data = payload.get("data", {})
            if "output_png" in data:
                png = data["output_png"]
                print(f"OK {png['file']} ({png['width']}x{png['height']}, {data.get('elapsed_seconds')}s)")
            else:
                print(json.dumps(data, ensure_ascii=False))
        else:
            err = payload.get("error", {})
            print(f"ERROR [{err.get('code', 'ERROR')}]: {err.get('message')}")
    elif getattr(ns, "compact", False) and payload.get("ok"):
        data = payload.get("data", {})
        if "output_png" in data:
            summary = {
                "ok": True,
                "command": payload["command"],
                "output_png": data["output_png"]["file"],
                "image_size": [data["output_png"]["width"], data["output_png"]["height"]],
                "report_file": data.get("report_file"),
                "source_sha256": data.get("source_sha256"),
                "source_unchanged": data.get("source_unchanged"),
                "focus_count": len(data.get("focus_objects", [])),
                "isolate": data.get("isolate"),
                "tiles_summary": [
                    {
                        "label": t.get("label"),
                        "mode": t.get("mode"),
                        "projection": t.get("projection"),
                        "actual_denoiser": t.get("actual_denoiser"),
                        "fallback_reason": t.get("fallback_reason"),
                    }
                    for t in data.get("tiles", [])
                ],
                "elapsed_seconds": data.get("elapsed_seconds"),
            }
            print(json.dumps(summary, ensure_ascii=False))
        else:
            print(json.dumps(payload, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


def _handle_session(ns: argparse.Namespace) -> int:
    action = ns.session_action
    cmd = f"preview.session.{action}"
    try:
        if action == "start":
            blender_bin = _resolve_blender_binary(ns.blender, getattr(ns, "blender_version", None))
            data = session_manager.start_session(
                blender_bin,
                sessions_root=ns.sessions_dir,
                idle_timeout=ns.idle_timeout,
                window_size=ns.window_size,
                preload_file=ns.file,
            )
        elif action == "status":
            data = session_manager.get_session_status(ns.session_id, sessions_root=ns.sessions_dir)
        elif action == "stop":
            data = session_manager.stop_session(ns.session_id, sessions_root=ns.sessions_dir)
        elif action == "list":
            data = {"sessions": session_manager.list_sessions(sessions_root=ns.sessions_dir)}
        else:
            raise ValueError(f"Unknown session action: {action}")
        return _emit({"ok": True, "command": cmd, "version": VERSION, "data": data}, ns)
    except Exception as exc:
        return _emit(
            {"ok": False, "command": cmd, "version": VERSION, "error": {"code": type(exc).__name__, "message": str(exc)}},
            ns,
        )


def _handle_capture(ns: argparse.Namespace) -> int:
    t0 = time.monotonic()
    job_id = f"preview-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    job_dir = ns.jobs_dir.resolve() / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        spec = _build_spec(ns, job_dir)
        if ns.session:
            worker_res = session_manager.submit_to_session(
                ns.session, spec, sessions_root=ns.sessions_dir, timeout=ns.timeout
            )
            runner_info = {"mode": "hot_session", "session_id": ns.session}
        else:
            spec_path = job_dir / "job-spec.json"
            result_path = job_dir / "worker-result.json"
            log_path = job_dir / "blender.log"
            spec["worker_result_path"] = str(result_path)
            spec_path.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")

            blender_bin = _resolve_blender_binary(ns.blender, getattr(ns, "blender_version", None))
            win_sz = max(512, min(1024, int(spec["cell_size"])))
            argv = [
                blender_bin,
                "--factory-startup",
                "-noaudio",
                "-p",
                "0",
                "0",
                str(win_sz),
                str(win_sz),
                "--python",
                str(WORKER_SCRIPT),
                "--",
                "--job-spec",
                str(spec_path),
            ]
            run_info = desktop_runner.run_on_isolated_desktop(
                argv,
                timeout_seconds=ns.timeout,
                log_path=str(log_path),
            )
            runner_info = {"mode": "isolated_desktop_oneshot", **run_info, "log_file": str(log_path)}
            if not result_path.is_file():
                tail = ""
                if log_path.is_file():
                    tail = log_path.read_text(encoding="utf-8", errors="replace")[-1200:]
                raise RuntimeError(
                    f"Preview worker exited (code={run_info['exit_code']}, timed_out={run_info['timed_out']}) "
                    f"without result JSON. Log tail: {tail}"
                )
            worker_res = json.loads(result_path.read_text(encoding="utf-8-sig"))

        if not worker_res.get("ok"):
            err = worker_res.get("error", {})
            return _emit(
                {
                    "ok": False,
                    "command": "preview",
                    "version": VERSION,
                    "job_id": job_id,
                    "runner": runner_info,
                    "error": {
                        "code": err.get("type", "WORKER_ERROR"),
                        "message": err.get("message", "Unknown worker error"),
                    },
                },
                ns,
            )

        worker_res["wall_seconds"] = round(time.monotonic() - t0, 3)
        worker_res["runner"] = runner_info
        payload = {
            "ok": True,
            "command": "preview",
            "version": VERSION,
            "job_id": job_id,
            "data": worker_res,
            "artifacts": {
                "job_directory": str(job_dir),
                "preview_png": worker_res["output_png"]["file"],
                "preview_report": worker_res["report_file"],
            },
        }
        (job_dir / "result.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return _emit(payload, ns)
    except Exception as exc:
        return _emit(
            {
                "ok": False,
                "command": "preview",
                "version": VERSION,
                "job_id": job_id,
                "error": {"code": type(exc).__name__, "message": str(exc)},
            },
            ns,
        )


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    kind, ns = _parse_args(sys.argv[1:])
    if kind == "session":
        return _handle_session(ns)
    return _handle_capture(ns)


if __name__ == "__main__":
    raise SystemExit(main())
