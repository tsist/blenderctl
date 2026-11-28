"""Launch material jobs from a disposable copy of the current Blender project."""
import json
from pathlib import Path
import tempfile

import bpy
from bpy.app.handlers import persistent

from ._ui_kit import BackgroundJobRunner, ensure_object_mode

RUNNER = BackgroundJobRunner("material")
_STAGING = None
_REQUEST_SCENE = None


def start(context, label, request, callback):
    global _STAGING, _REQUEST_SCENE
    if RUNNER.running:
        raise ValueError("材质工作台已有后台任务")
    scene = context.scene
    scene_pointer = scene.as_pointer()
    staging = tempfile.TemporaryDirectory(prefix="blender-ai-material-")
    try:
        folder = Path(staging.name)
        snapshot = folder / "snapshot.blend"
        request_file = folder / "request.json"
        status = folder / "status.json"
        result = folder / "result.json"
        with ensure_object_mode(context):
            saved = bpy.ops.wm.save_as_mainfile(filepath=str(snapshot), copy=True)
            if saved != {"FINISHED"}:
                raise ValueError("无法保存当前场景的后台临时副本")
        request_file.write_text(json.dumps({
            **request, "scene": scene.name, "view_layer": context.view_layer.name,
        }, ensure_ascii=False), encoding="utf-8")

        def complete(ok, payload):
            global _STAGING, _REQUEST_SCENE
            _STAGING = None
            _REQUEST_SCENE = None
            try:
                destination = next((item for item in bpy.data.scenes
                                    if item.as_pointer() == scene_pointer), None)
                if destination is not None:
                    callback(destination, ok, payload)
            finally:
                staging.cleanup()

        RUNNER.start(
            [bpy.app.binary_path, "--background", "--quiet", "--factory-startup", "--offline-mode",
             str(snapshot), "--python-exit-code", "1", "--python",
             str(Path(__file__).with_name("background_worker.py")), "--",
             str(request_file), str(status), str(result)],
            label=label, status_file=status, result_file=result, on_complete=complete,
        )
    except Exception:
        staging.cleanup()
        raise
    _STAGING = staging
    _REQUEST_SCENE = scene
    scene.mw_last_report = "已启动后台任务"
    scene.mw_last_report_kind = "RUNNING"


@persistent
def reset(_):
    global _STAGING, _REQUEST_SCENE
    RUNNER.cleanup()
    staging, _STAGING = _STAGING, None
    _REQUEST_SCENE = None
    if staging is not None:
        try:
            staging.cleanup()
        except OSError:
            pass


class MW_OT_cancel_job(bpy.types.Operator):
    bl_idname = "material_workflow.cancel_workbench_job"
    bl_label = "取消材质后台任务"

    def execute(self, context):
        scene = _REQUEST_SCENE or context.scene
        reset(None)
        scene.mw_last_report = "已取消材质后台任务"
        scene.mw_last_report_kind = "WARN"
        return {"FINISHED"}


def finish(scene, ok, payload, success):
    if ok:
        scene.mw_last_report = success
        scene.mw_last_report_kind = "OK"
    else:
        detail = payload.get("error") or payload.get("stderr_tail")
        if detail:
            print(f"[S·材质] {detail}")
        scene.mw_last_report = "后台任务未完成，详情见系统控制台"
        scene.mw_last_report_kind = "ERROR"
