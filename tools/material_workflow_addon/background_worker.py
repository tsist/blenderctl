"""Execute Material Workflow jobs against the supplied private snapshot."""
import importlib
import json
from pathlib import Path
import sys


def _save(path, payload):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _device(preference):
    if preference == "CPU":
        return "CPU", "CPU"
    import bpy

    cycles = bpy.context.preferences.addons.get("cycles")
    if cycles is not None:
        for backend in ("OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"):
            try:
                cycles.preferences.compute_device_type = backend
                cycles.preferences.get_devices()
            except (RuntimeError, TypeError, ValueError):
                continue
            devices = cycles.preferences.devices
            if any(device.type == backend for device in devices):
                for device in devices:
                    device.use = device.type == backend
                return "GPU", backend
    return "CPU", "CPU"


def main():
    import bpy

    request_path, status_path, result_path = map(Path, sys.argv[sys.argv.index("--") + 1:])
    package = Path(__file__).resolve().parent
    sys.path.insert(0, str(package.parent))

    def progress(value, detail):
        _save(status_path, {"progress": value, "detail": detail})

    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        scene = bpy.data.scenes[request["scene"]]
        window = bpy.context.window
        window.scene = scene
        window.view_layer = scene.view_layers[request["view_layer"]]
        if request["job"] == "projection":
            core = importlib.import_module(f"{package.name}.projection_bake")
            objects = window.view_layer.objects
            preference, device = _device(request["device"])
            progress(.12, f"正在准备投射副本（{device}）")
            manifest = core.bake_projection(
                bpy.context, objects[request["target"]], [objects[name] for name in request["sources"]],
                request["uv_layer"], request["output"],
                cage=objects[request["cage"]] if request["cage"] else None,
                channels=request["channels"], device=preference, progress=progress, **request["settings"],
            )
            report = json.loads(manifest.read_text(encoding="utf-8"))
            payload = {
                "ok": True, "manifest": str(manifest), "status": report["schema"],
                "coverage": report["witness"]["coverage_fraction"], "device": device,
            }
        elif request["job"] == "procedural_export":
            workbench = importlib.import_module(f"{package.name}.procedural_ui")
            workbench.register()
            try:
                scene.mwp_ui_work_dir = request["output"]
                progress(.2, "正在烘焙程序化 PBR 通道")
                if bpy.ops.material_workflow.procedural_export() != {"FINISHED"}:
                    raise RuntimeError(scene.mwp_ui_error or "程序化贴图导出未完成")
                payload = {"ok": True, "directory": scene.mwp_ui_result_dir, "receipt": scene.mwp_ui_last_report}
            finally:
                workbench.unregister()
        else:
            raise ValueError("未知材质后台任务")
        _save(result_path, payload)
    except Exception as error:
        _save(result_path, {"ok": False, "error": str(error)})
        raise


if __name__ == "__main__":
    main()
