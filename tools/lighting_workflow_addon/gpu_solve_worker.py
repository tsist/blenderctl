# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless worker invoked asynchronously by L·灯光 UI for RTX GPU goal solving."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import traceback


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _detect_best_device():
    import bpy

    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
    except Exception:
        return {"backend": "CPU"}

    for backend in ("OPTIX", "CUDA"):
        try:
            prefs.compute_device_type = backend
            prefs.get_devices()
            for dev in prefs.devices:
                if dev.type == backend:
                    return {"backend": backend, "id": dev.id}
        except Exception:
            continue
    return {"backend": "CPU"}


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if len(argv) < 4:
        raise SystemExit("Usage: gpu_solve_worker.py -- <addon_parent> <req.json> <status.json> <result.json>")
    addon_parent, req_path, status_path, result_path = (
        Path(argv[0]),
        Path(argv[1]),
        Path(argv[2]),
        Path(argv[3]),
    )
    pkg_name = Path(__file__).resolve().parent.name
    if str(addon_parent) not in sys.path:
        sys.path.insert(0, str(addon_parent))

    try:
        _write_json(status_path, {"progress": 0.18, "detail": "正在检测 RTX GPU (OPTIX/CUDA) 渲染设备…"})
        request = json.loads(req_path.read_text(encoding="utf-8"))
        device_pref = request.pop("_device_mode", "AUTO")

        if device_pref == "CPU":
            device = {"backend": "CPU"}
        else:
            device = _detect_best_device()
            if device_pref in ("OPTIX", "CUDA") and device["backend"] != device_pref:
                device = _detect_best_device()

        request.setdefault("sampling", {})["device"] = device
        backend_label = device["backend"]
        _write_json(
            status_path,
            {"progress": 0.42, "detail": f"正在通过 Cycles {backend_label} Light Groups 单次测光求解…"},
        )

        import importlib
        import bpy

        core = importlib.import_module(f"{pkg_name}.core")
        goals = importlib.import_module(f"{pkg_name}.goals")

        # On this isolated temporary snapshot, align managed rig baselines to current human slider edits
        scene = bpy.data.scenes.get(request["scene"])
        if scene is not None:
            states = core._states(scene)
            changed = False
            for state in states.values():
                for lid, row in state.get("lights", {}).items():
                    obj = bpy.data.objects.get(row["object"])
                    if obj is not None:
                        row["baseline"] = core._light_values(obj, lid in state.get("links", {}))
                        row["structure"] = core._light_structure(obj)
                        changed = True
            if changed:
                core._save_states(scene, states)

        result = goals.tune(request)
        _write_json(status_path, {"progress": 0.92, "detail": "求解收敛，正在封装功率回写数据…"})
        _write_json(result_path, {"ok": True, "device_used": device, "result": result})
        return 0
    except Exception as exc:
        _write_json(
            result_path,
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=8),
            },
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
