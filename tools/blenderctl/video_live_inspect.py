"""Inspect the currently attached Blender VSE timeline through its native panel."""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import time
import uuid

from filesystem import FileGuard, PathLocks
from material_draft_handoff import Parser, canonical, require
from material_live_session import valid_id
from protocol import CODES, DEFAULT_BLENDER, Failure, atomic_json, digest, envelope, read_json


COMMAND = "video.inspect-live"
SESSION_SCHEMA = "video-inspect-live/1"
from host_compat import is_supported_host
RELEASE = Path(__file__).resolve().parents[2] / "runtime/video-editing/0.1.5/SOURCE-MANIFEST.json"
RELEASE_SHA = "df264f3166569f4ad7f79fc0f20042cedfdc35469b9ef3c2a7c582320b5ff78d"
_attachment = None


def native_panel(bpy):
    require(sys.platform == "win32" and not bpy.app.background and is_supported_host(bpy.app.version), "UNSUPPORTED",
            "Attach to a window of Windows Blender 5.3.0 or later")
    operator = bpy.types.Operator.bl_rna_get_subclass_py("VWE_OT_inspect")
    require(operator is not None, "UNSUPPORTED", "Enable Video Editing Workflow first")
    module = sys.modules[operator.__module__]
    addon = sys.modules[module.__package__]
    require(tuple(addon.bl_info.get("version", ())) == (0, 1, 2), "CONFLICT",
            "The enabled Video Editing panel must be 0.1.2 from distribution 0.1.5")
    require(digest(RELEASE) == RELEASE_SHA, "CONFLICT", "The qualified VSE release manifest changed")
    release = read_json(RELEASE)
    prefix = "tools/video_editing_workflow_addon/"
    directory = Path(module.__file__).resolve().parent
    with ExitStack() as guards:
        for row in release["files"]:
            if not row["file"].startswith(prefix):
                continue
            path = directory / row["file"].removeprefix(prefix)
            require(path.resolve() == path and path.is_file(), "CONFLICT", "Missing or redirected native VSE file")
            handle = guards.enter_context(FileGuard(path))
            require(handle.sha256() == row["sha256"], "CONFLICT", "The enabled VSE files differ from distribution 0.1.5")
    return operator, release


def attach(directory):
    """Attach a read-only queue from the target Blender window."""
    import bpy

    global _attachment
    require(_attachment is None, "CONFLICT", "A video inspection queue is already attached")
    root = Path(directory)
    require(root.is_absolute() and canonical(root.parent) == root.parent and not os.path.lexists(root),
            "CONFLICT", "Choose an unused absolute queue directory in an existing parent")
    operator, release = native_panel(bpy)
    window = bpy.context.window
    require(window is not None, "UNSUPPORTED", "Attach from the owning Blender window")
    scene = window.scene
    info = {
        "schema": SESSION_SCHEMA, "session_id": str(uuid.uuid4()), "directory": str(root),
        "pid": os.getpid(), "blender": bpy.app.binary_path, "build": bpy.app.build_hash.decode(), "version": list(bpy.app.version),
        "scene": scene.name, "workflow_id": scene.get("_vwe_workflow"), "saved_file": bpy.data.filepath,
        "addon_version": release["addon_version"], "distribution_version": release["distribution_version"],
        "implementation_sha256": release["implementation_sha256"], "release_manifest_sha256": RELEASE_SHA,
    }
    root.mkdir()
    (root / "requests").mkdir()
    atomic_json(root / "session.json", info)
    _attachment = (root, info, window.as_pointer(), scene.as_pointer(), operator)
    bpy.app.handlers.load_pre.append(detach)
    if not bpy.app.timers.is_registered(_pump):
        bpy.app.timers.register(_pump, first_interval=.2)
    return info


def detach(*_):
    """Close only the queue, preserving the Blender window and current project."""
    import bpy

    global _attachment
    if _attachment is None:
        return
    root, info, *_ = _attachment
    atomic_json(root / "closed.json", {"session_id": info["session_id"]})
    if detach in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(detach)
    _attachment = None


def scoped_request(info, ident):
    return {"session_id": info["session_id"], "request_id": ident,
            "scene": info["scene"], "workflow_id": info["workflow_id"]}


def inspect_native(bpy, info, window_pointer, scene_pointer, operator):
    window = next((window for window in bpy.context.window_manager.windows
                   if window.as_pointer() == window_pointer), None)
    require(window is not None, "CONFLICT", "The attached Blender window has closed")
    scene = window.scene
    require(scene.as_pointer() == scene_pointer and scene.name == info["scene"]
            and scene.get("_vwe_workflow") == info["workflow_id"] and bpy.data.filepath == info["saved_file"],
            "CONFLICT", "The attached scene, workflow or saved file identity changed")
    require(bpy.types.Operator.bl_rna_get_subclass_py("VWE_OT_inspect") is operator,
            "CONFLICT", "The attached Video Editing panel changed")
    with bpy.context.temp_override(window=window):
        require(bpy.ops.vwe.inspect() == {"FINISHED"}, "WORKER_FAILED", "Native timeline inspection did not finish")
        value = json.loads(bpy.context.window_manager.vwe_inspection)
    require(value["scene"] == info["scene"] and value["workflow_id"] == info["workflow_id"],
            "VALIDATION_FAILED", "Native report belongs to a different timeline")
    return {"state": "inspected", "scope": "LIVE_WINDOW", "session": info, "inspection": value}


def _pump():
    import bpy

    if _attachment is None:
        return None
    root, info, *scope = _attachment
    for path in sorted((root / "requests").glob("*/request.json")):
        directory = path.parent
        ident = directory.name
        if not valid_id(ident) or directory.resolve() != directory or path.resolve() != path:
            continue
        started, result = directory / "started.json", directory / "result.json"
        if started.exists() or result.exists():
            continue
        try:
            require(read_json(path) == scoped_request(info, ident), "CONFLICT", "Video inspection request identity differs")
            atomic_json(started, {"session_id": info["session_id"], "request_id": ident})
            value = inspect_native(bpy, info, *scope)
            response = envelope(COMMAND, ident, data={**value, "request_id": ident}, artifacts={"result": str(result)})
        except Exception as exc:
            response = envelope(COMMAND, ident, error={"code": getattr(exc, "code", "WORKER_FAILED"),
                                                      "message": str(exc)})
        atomic_json(result, response)
        break
    return .2


def run(args):
    require(valid_id(args.session_id), "INVALID_REQUEST", "Supply the attached session UUID")
    ident = args.request_id or str(uuid.uuid4())
    require(valid_id(ident) and 0 <= args.wait_seconds <= 55, "INVALID_REQUEST",
            "Supply a valid request UUID and a wait of 0 to 55 seconds")
    root = canonical(args.session_dir)
    queue = root / "requests"
    directory = queue / ident
    with ExitStack() as guards:
        for parent in reversed((queue, *queue.parents)):
            if parent != Path(parent.anchor):
                guards.enter_context(FileGuard(parent, directory=True))
        session = root / "session.json"
        guards.enter_context(FileGuard(session))
        info = read_json(session)
        require(info["schema"] == SESSION_SCHEMA and info["session_id"] == args.session_id
                and Path(info["directory"]) == root and is_supported_host(info.get("version"))
                and canonical(info["blender"]) == canonical(args.blender)
                and info["scene"] == args.scene and info["workflow_id"] == args.workflow_id,
                "CONFLICT", "Attached session, host, scene or workflow differs")
        request = scoped_request(info, ident)
        with PathLocks([directory]):
            if os.path.lexists(directory):
                require(directory.is_dir() and canonical(directory) == directory
                        and canonical(directory / "request.json") == directory / "request.json"
                        and read_json(directory / "request.json") == request,
                        "CONFLICT", "This request UUID belongs to a different inspection")
            else:
                require(not (root / "closed.json").exists(), "CONFLICT", "Video inspection queue has detached")
                directory.mkdir()
                atomic_json(directory / "request.json", request)
        result = directory / "result.json"
        deadline = time.monotonic() + args.wait_seconds
        while not result.is_file():
            require(not (root / "closed.json").exists(), "CONFLICT", "Video inspection queue detached")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return envelope(COMMAND, ident, data={"state": "pending", "session_id": args.session_id,
                                                      "request_id": ident, "result": str(result)})
            time.sleep(min(.1, remaining))
        require(canonical(result) == result, "CONFLICT", "Redirected live inspection result")
        return read_json(result)


def main():
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--blender", type=Path, default=DEFAULT_BLENDER)
    for flag in ("--jobs-dir", "--transactions-dir", "--timeout"):
        parser.add_argument(flag, help=argparse.SUPPRESS)
    mode = parser.add_mutually_exclusive_group()
    for flag in ("--compact", "--human", "--full"):
        mode.add_argument(flag, action="store_true")
    sub = parser.add_subparsers(required=True, parser_class=Parser).add_parser(
        "inspect-live", epilog="Call attach(NEW_ABSOLUTE_DIRECTORY) in the owning Blender window. "
                              "Pass its session and scene identities. Supply the managed workflow ID if present. "
                              "Repeat the same request UUID to recover a pending inspection.", allow_abbrev=False)
    sub.add_argument("--session-dir", type=Path, required=True)
    sub.add_argument("--session-id", required=True)
    sub.add_argument("--scene", required=True)
    sub.add_argument("--workflow-id")
    sub.add_argument("--request-id")
    sub.add_argument("--wait-seconds", type=float, default=10)
    try:
        result = run(parser.parse_args())
    except (Failure, OSError, ValueError, KeyError, TypeError) as exc:
        result = envelope(COMMAND, error={"code": getattr(exc, "code", "INVALID_REQUEST"), "message": str(exc)})
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["ok"] else CODES.get(result["error"]["code"], 8)


if __name__ == "__main__":
    raise SystemExit(main())
