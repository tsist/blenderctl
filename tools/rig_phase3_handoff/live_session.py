# SPDX-License-Identifier: GPL-3.0-or-later
"""Capture keyed manual edits from an owned Win53 rigging window."""
from contextlib import ExitStack
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_pin import SCENE_FIELDS, activate, execution_inputs, open_scene, scene_input, user_environment
from filesystem import FileGuard
from handoff_io import admission_lock, canonical, error_payload, fingerprint, guard_file, read_document, read_result, require
from processes import ProcessTree
from protocol import atomic_json, envelope, read_json
from resource_leases import process_identity


COMMAND = "rigging-workflow.phase3.live"
SCHEMA = "rigging-win53-live/1"
ENTRY = Path(__file__).resolve()
IDENT = re.compile(r"[0-9a-f]{32}")
SHA = re.compile(r"[0-9a-f]{64}")


def open_host(args):
    require(args.session_dir.is_absolute(), "INVALID_REQUEST", "Use a new absolute session directory")
    root = canonical(args.session_dir.parent) / args.session_dir.name
    require(root == args.session_dir and not os.path.lexists(root), "CONFLICT", "Use a new canonical session directory")
    with ExitStack() as held:
        request = read_document(guard_file(args.params.absolute(), held))
        require(isinstance(request, dict) and request.keys() == SCENE_FIELDS | {"rig"}
                and isinstance(request["rig"], str) and request["rig"], "INVALID_REQUEST",
                "Live-open needs file, expected_sha256, scene, view_layer and rig")
        launch = scene_input(request, held)
        blender, package = execution_inputs(args, held)
    root.mkdir()
    for folder in ("requests", "started", "results", "snapshots"):
        (root / folder).mkdir()
    session_id = uuid.uuid4().hex
    atomic_json(root / "host-launch.json", {**launch, "session_id": session_id, "schema": SCHEMA, "package": package})
    env = user_environment(root / "profile")
    argv = [str(blender), "--factory-startup", "--disable-autoexec", "--offline-mode", "--threads", "2",
            "--python-exit-code", "23", "--python", str(ENTRY), "--", str(root)]
    with (root / "host.log").open("xb") as log:
        tree = ProcessTree(argv, cwd=str(root), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        try:
            code = tree.process.wait()
        finally:
            tree.close()
    atomic_json(root / "host-ended.json", {"session_id": session_id, "exit_code": code})
    error, _ = read_result(root / "host-error.json")
    if error is not None:
        return envelope(COMMAND, error=error["error"])
    closed, _ = read_result(root / "closed.json")
    require(code == 0, "WORKER_FAILED", "Owned Blender exited unexpectedly; read " + str(root / "host.log"))
    return envelope(COMMAND, data=closed or {"session_id": session_id, "status": "ended"})


def read_session(root, session_id):
    info = read_json(root / "session.json")
    require(isinstance(session_id, str) and IDENT.fullmatch(session_id) and info["schema"] == SCHEMA
            and info["session_id"] == session_id and info["session_dir"] == str(root),
            "CONFLICT", "Owned session identity differs")
    return info


def transact(args):
    root = canonical(args.session_dir)
    info = read_session(root, args.session_id)
    action = args.live_action
    request_id = args.request_id or uuid.uuid4().hex
    require(IDENT.fullmatch(request_id) is not None, "INVALID_REQUEST", "Request ID must be 32 lowercase hex digits")
    seconds = args.wait_seconds
    require(math.isfinite(seconds) and 0 <= seconds <= 55, "INVALID_REQUEST", "Wait time must be 0–55 seconds")
    result_path = root / "results" / (request_id + ".json")
    request_path = root / "requests" / (request_id + ".json")
    if action != "result":
        expected = getattr(args, "expected_context", None)
        require(action == "status" or SHA.fullmatch(expected) is not None,
                "INVALID_REQUEST", "Use the fresh expected_context from live-status")
        payload = {"schema": SCHEMA, "session_id": info["session_id"], "request_id": request_id,
                   "operation": action, "expected_context": expected}
        with admission_lock(root / "admission.json"):
            if request_path.exists():
                require(read_json(request_path) == payload, "CONFLICT", "Request ID was already used with other inputs")
            else:
                require(not (root / "closed.json").exists() and not (root / "closing.json").exists()
                        and not (root / "host-ended.json").exists()
                        and process_identity(info["process"]["pid"]) == info["process"],
                        "CONFLICT", "Owned Blender session is closed, closing or no longer running")
                atomic_json(request_path, payload)
                if action == "close":
                    atomic_json(root / "closing.json", {"request_id": request_id})
    else:
        require(request_path.is_file(), "NOT_FOUND", "No request with that ID in this session")
    deadline = time.monotonic() + seconds
    while True:
        result, _ = read_result(result_path)
        if result is not None:
            return result
        if (root / "host-ended.json").exists() or process_identity(info["process"]["pid"]) != info["process"]:
            result, _ = read_result(result_path)
            if result is not None:
                return result
            return envelope(COMMAND, error={"code": "WORKER_FAILED", "message":
                            f"Owned Blender ended without a result for {request_id}"})
        if time.monotonic() >= deadline:
            return envelope(COMMAND, data={"status": "pending", "session_id": info["session_id"],
                                            "request_id": request_id, "result": str(result_path)})
        time.sleep(.08)


def observe(bpy, launch, workfile):
    from rigging_workflow.retarget_contact_correction import action_fingerprint

    ctx = bpy.context
    require(Path(bpy.data.filepath).samefile(workfile) and ctx.scene.name == launch["scene"]
            and ctx.view_layer.name == launch["view_layer"], "CONFLICT", "The owned file or scene context changed")
    rig = ctx.view_layer.objects.get(launch["rig"])
    require(rig is not None and rig.type == "ARMATURE" and not rig.library
            and not rig.override_library and not rig.data.library and ctx.mode in {"OBJECT", "POSE"},
            "CONFLICT", "The named local rig must be in an Object or Pose context")
    ad = rig.animation_data
    action = ad.action if ad else None
    slot = ad.action_slot if ad else None
    payload = {"file": str(workfile), "scene": ctx.scene.name, "view_layer": ctx.view_layer.name,
               "frame": ctx.scene.frame_current, "subframe": ctx.scene.frame_subframe, "mode": ctx.mode,
               "active_object": ctx.object.name if ctx.object else None, "rig": rig.name,
               "rig_session_uid": int(rig.session_uid), "action": action.name if action else None,
               "slot": slot.identifier if slot else None, "use_nla": bool(ad and ad.use_nla),
               "action_sha256": action_fingerprint(action) if action else None,
               "pose_sha256": fingerprint({bone.name: [list(row) for row in bone.matrix_basis]
                                           for bone in rig.pose.bones}),
               "dirty": bpy.data.is_dirty}
    return {**payload, "expected_context": fingerprint(payload)}


def native_open(root):
    import bpy

    launch = read_json(root / "host-launch.json")
    workfile = root / "live-work.blend"
    with ExitStack() as held:
        source = guard_file(launch["file"], held, launch["expected_sha256"])
        window = open_scene(bpy, source, launch, ui=True)
        runtime = activate(bpy, root / "native-package", launch["package"], background=False)
        with bpy.context.temp_override(window=window):
            require(bpy.ops.wm.save_as_mainfile(filepath=str(workfile), copy=False, relative_remap=True,
                                                check_existing=False) == {"FINISHED"},
                    "VALIDATION_FAILED", "Could not create the initial owned working copy")
    owner = window.as_pointer()
    info = {"session_id": launch["session_id"], "schema": SCHEMA, "session_dir": str(root),
            "process": process_identity(os.getpid()), "runtime": runtime, "workfile": str(workfile),
            "source": {"file": launch["file"], "sha256": launch["expected_sha256"]}}
    atomic_json(root / "session.json", info)

    def pump():
        paths = sorted((root / "requests").glob("*.json"), key=lambda path: path.stat().st_mtime_ns)
        for path in paths:
            result_path = root / "results" / path.name
            started = root / "started" / path.name
            if result_path.exists() or started.exists():
                continue
            atomic_json(started, {"request_id": path.stem})
            operation = None
            try:
                request = read_json(path)
                require(isinstance(request, dict), "INVALID_REQUEST", "Invalid live request")
                operation = request.get("operation")
                require(request.keys() == {"schema", "session_id", "request_id", "operation", "expected_context"}
                        and request["schema"] == SCHEMA and request["session_id"] == launch["session_id"]
                        and request["request_id"] == path.stem and IDENT.fullmatch(path.stem)
                        and operation in {"status", "capture", "close"}, "INVALID_REQUEST", "Invalid live request")
                window = next((item for item in bpy.context.window_manager.windows if item.as_pointer() == owner), None)
                require(window is not None and not window.screen.is_animation_playing,
                        "CONFLICT", "Pause playback in the owned window before capturing")
                with bpy.context.temp_override(window=window):
                    current = observe(bpy, launch, workfile)
                    result = {**info, "status": "observed", "request_id": path.stem, "context": current}
                    if operation != "status":
                        require(request["expected_context"] == current["expected_context"],
                                "CONFLICT", "Live context changed; read it again before saving a copy")
                        snapshot = root / "snapshots" / path.with_suffix(".blend").name
                        require(not os.path.lexists(snapshot), "CONFLICT", "Snapshot already exists")
                        require(bpy.ops.wm.save_as_mainfile(filepath=str(snapshot), copy=True, relative_remap=True,
                                                            check_existing=False) == {"FINISHED"},
                                "VALIDATION_FAILED", "Could not save the live working snapshot")
                        with FileGuard(snapshot) as handle:
                            result.update(status="captured", snapshot={"file": str(snapshot), "sha256": handle.sha256()})
                atomic_json(result_path, envelope(COMMAND, data=result))
                if operation == "close":
                    atomic_json(root / "closed.json", {**result, "status": "closed"})
                    bpy.app.timers.register(lambda: bpy.ops.wm.quit_blender() and None, first_interval=.1)
                    return None
            except Exception as exc:
                atomic_json(result_path, envelope(COMMAND, error=error_payload(exc, "WORKER_FAILED"),
                                                  artifacts={"request_id": path.stem}))
                if operation == "close":
                    with admission_lock(root / "admission.json"):
                        (root / "closing.json").unlink(missing_ok=True)
        return .2

    bpy.app.timers.register(pump, first_interval=.1, persistent=True)


def add_commands(actions):
    start = actions.add_parser("live-open", help="Open a separately owned Blender window; run in a second terminal")
    start.add_argument("--params", type=Path, required=True, help="Saved source fields plus rig")
    start.add_argument("--session-dir", type=Path, required=True)
    start.set_defaults(handler=open_host)
    for action in ("status", "capture", "result", "close"):
        parser = actions.add_parser("live-" + action, help={
            "status": "Observe the current Action, Slot and pose fingerprint",
            "capture": "Save a SHA-bound copy if the observed context still matches",
            "result": "Collect the original request without resubmitting",
            "close": "Save a final recovery copy and close this owned window",
        }[action])
        parser.add_argument("--session-dir", type=Path, required=True)
        parser.add_argument("--session-id", required=True)
        parser.add_argument("--request-id", required=action == "result")
        parser.add_argument("--wait-seconds", type=float, default=10)
        if action in {"capture", "close"}:
            parser.add_argument("--expected-context", required=True)
        parser.set_defaults(handler=transact, live_action=action)


if __name__ == "__main__":
    import bpy

    directory = Path(sys.argv[sys.argv.index("--") + 1]).resolve(strict=True)

    def start():
        try:
            native_open(directory)
        except Exception as exc:
            atomic_json(directory / "host-error.json", {"error": error_payload(exc, "WORKER_FAILED")})
            bpy.ops.wm.quit_blender()
        return None

    bpy.app.timers.register(start, first_interval=.1)
