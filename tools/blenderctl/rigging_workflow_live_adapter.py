"""Named local adapter for the missing Rigging Workflow 0.23.0 live actions.

This module is both:

* the Blender-side attach/pump host (``attach`` / ``detach``) that must be
  called from the owned Blender window, exactly like the existing
  ``material.handoff-live`` and ``compositing.session-live`` queues; and
* the public CLI (``rigging-workflow.session-live``) that writes one typed
  request directory and waits for its result.

It adds only four narrow live capabilities and never accepts scripts, Python
expressions, modules or arbitrary operation names:

* ``transport-enable``  -> product ``server.start(context, service.live_dispatch, root)``
* ``transport-disable`` -> product ``server.stop()``
* ``play-pause``        -> native ``screen.animation_play`` / ``animation_cancel``
* ``focus-source``      -> select/activate an existing ``record.sources[].object``

The frozen 0.23.0 ``contracts.py``, ``service.py``, ``server.py`` and UI are
not modified.  The product transport is started through its published public
entry, so the ordinary ``rigging-workflow session`` CLI can use the resulting
descriptor immediately.
"""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

sys.dont_write_bytecode = True
TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from host_compat import is_supported_host
from filesystem import FileGuard, PathLocks  # noqa: E402
from material_draft_handoff import Parser, canonical, fingerprint, require  # noqa: E402
from protocol import CODES, DEFAULT_BLENDER, Failure, atomic_json, digest, envelope, read_json

COMMAND = "rigging-workflow.session-live"
SESSION_SCHEMA = "rigging-live-adapter/1"
REQUEST_SCHEMA = "rigging-live-adapter.request/1"
OPERATIONS = {
    "inspect",
    "transport-enable",
    "transport-disable",
    "play-pause",
    "focus-source",
    "stop",
    "result",
}
WRITES = {"transport-enable", "transport-disable", "play-pause", "focus-source", "stop"}
FIELDS = {
    "schema",
    "session_id",
    "request_id",
    "operation",
    "expected_context",
    "queue_root",
    "character_id",
    "object",
    "hair_id",
}
_ID = re.compile(r"[0-9a-f]{32}")
_SHA = re.compile(r"[0-9a-f]{64}")
_attachment = None


def valid_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def valid_sha(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def verify_request(request):
    require(isinstance(request, dict) and set(request) == FIELDS, "INVALID_REQUEST",
            "Unexpected adapter request fields")
    require(request["schema"] == REQUEST_SCHEMA, "INVALID_REQUEST", "Unknown adapter request schema")
    operation = request["operation"]
    require(isinstance(operation, str) and operation in OPERATIONS, "INVALID_REQUEST",
            "Unknown adapter operation")
    require(valid_id(request["session_id"]) and valid_id(request["request_id"]), "INVALID_REQUEST",
            "Expected 32 lowercase hex session/request IDs")
    require(valid_sha(request["expected_context"]) if operation in WRITES
            else request["expected_context"] is None, "INVALID_REQUEST",
            "Only write operations take a 64-hex expected_context")
    queue_root = request["queue_root"]
    require((isinstance(queue_root, str) and queue_root) if operation == "transport-enable"
            else queue_root is None, "INVALID_REQUEST",
            "Only transport-enable takes an explicit queue_root")
    character_id = request["character_id"]
    require(valid_id(character_id) if operation in {"play-pause", "focus-source"}
            else character_id is None, "INVALID_REQUEST",
            "Only play-pause and focus-source take a character_id")
    target = request["object"]
    require((isinstance(target, str) and 0 < len(target) <= 256) if operation == "focus-source"
            else target is None, "INVALID_REQUEST",
            "Only focus-source takes an explicit source object name")
    hair_id = request["hair_id"]
    require(valid_id(hair_id) if operation == "play-pause" else hair_id is None, "INVALID_REQUEST",
            "Only play-pause takes a 32-hex hair_id")
    return request


def _json(raw, default):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, dict) else default


def _product():
    from rigging_workflow.core import service
    from rigging_workflow.core.transport import server
    return service, server


def _window(bpy, pointer):
    return next((window for window in bpy.context.window_manager.windows
                 if window.as_pointer() == pointer), None)


def _context_payload(bpy, info):
    context = bpy.context
    service, server = _product()
    try:
        record = service.current(context)
        record_error = None
    except Exception as exc:
        record = None
        record_error = str(exc)
    window = _window(bpy, info["window"])
    descriptor = server.descriptor_path()
    transport = {"active": bool(server.is_active()),
                 "descriptor": None if descriptor is None else str(descriptor),
                 "session_id": None}
    if descriptor is not None:
        try:
            value = json.loads(descriptor.read_text(encoding="utf-8"))
            transport["session_id"] = value.get("session_id")
            transport["epoch"] = value.get("epoch")
            transport["window_bound"] = value.get("window_bound")
        except (OSError, ValueError):
            transport["descriptor_error"] = "unreadable"
    hair = {}
    for obj in (record.work.all_objects if record is not None and record.work else []):
        if obj.type == "ARMATURE" and obj.get("rw_hair_id"):
            state = _json(obj.get("rw_hair_state"), {})
            runtime = _json(obj.get("rw_hair_runtime"), {})
            hair[obj["rw_hair_id"]] = {
                "object": obj.name,
                "mode": state.get("mode"),
                "enabled": bool(state.get("enabled")),
                "preview_id": runtime.get("preview_id"),
            }
    payload = {
        "file": bpy.data.filepath,
        "scene": info["scene"],
        "view_layer": info["view_layer"],
        "window": info["window"],
        "window_scene": None if window is None else window.scene.name,
        "character_id": None if record is None else record.character_id,
        "revision": None if record is None else record.revision,
        "mode": context.mode,
        "active_object": None if context.view_layer.objects.active is None
        else context.view_layer.objects.active.name,
        "frame": context.scene.frame_current,
        "subframe": context.scene.frame_subframe,
        "playing": bool(context.screen.is_animation_playing) if context.screen else None,
        "transport": transport,
        "hair": hair,
        "record_error": record_error,
    }
    # A playing timeline advances the frame continuously.  The frame is still
    # reported in the snapshot, but it cannot be part of the write fingerprint
    # while playback is running or pause could never be requested reliably.
    if payload["playing"] is True:
        payload["frame"] = None
        payload["subframe"] = None
        # The live hair frame writer advances record.revision on every step.
        # Bind the stable role identity instead; the snapshot still reports it.
        payload["revision"] = None
    return payload


def snapshot(bpy, info):
    context = bpy.context
    service, server = _product()
    try:
        record = service.current(context)
        record_error = None
    except Exception as exc:
        record = None
        record_error = str(exc)
    window = _window(bpy, info["window"])
    descriptor = server.descriptor_path()
    descriptor_value = None
    state_value = None
    if descriptor is not None and descriptor.is_file():
        try:
            raw = json.loads(descriptor.read_text(encoding="utf-8"))
            descriptor_value = {key: raw.get(key) for key in (
                "schema", "transport_version", "session_id", "epoch", "pid", "file_kind",
                "scene", "view_layer", "window_bound", "contract_schema", "contract_sha256",
                "timer_interval_s", "lease_timeout_ns")}
        except (OSError, ValueError):
            descriptor_value = {"error": "unreadable"}
        state_path = descriptor.parent / "state.json"
        if state_path.is_file():
            try:
                state_value = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state_value = {"error": "unreadable"}
    sources = [
        {"object": None if ref.object is None else ref.object.name,
         "work_object": None if ref.work_object is None else ref.work_object.name,
         "hidden": bool(ref.hidden)}
        for ref in (record.sources if record is not None else [])
    ]
    hair = []
    for obj in (record.work.all_objects if record is not None and record.work else []):
        if obj.type == "ARMATURE" and obj.get("rw_hair_id"):
            state = _json(obj.get("rw_hair_state"), {})
            runtime = _json(obj.get("rw_hair_runtime"), {})
            hair.append({
                "object": obj.name,
                "hair_id": obj["rw_hair_id"],
                "mode": state.get("mode"),
                "enabled": bool(state.get("enabled")),
                "display_layer": state.get("display_layer"),
                "preview_id": runtime.get("preview_id"),
                "active_action": (obj.animation_data.action.name
                                  if obj.animation_data and obj.animation_data.action else None),
            })
    current = _context_payload(bpy, info)
    return {
        "schema": SESSION_SCHEMA,
        "session_id": info["session_id"],
        "pid": os.getpid(),
        "blender": bpy.app.version_string,
        "background": bool(bpy.app.background),
        "window_present": window is not None,
        "window_pointer": info["window"],
        "window_scene": None if window is None else window.scene.name,
        "screen": None if context.screen is None else context.screen.name,
        "workspace": None if context.workspace is None else context.workspace.name,
        "file": bpy.data.filepath,
        "dirty": bool(bpy.data.is_dirty),
        "scene": context.scene.name,
        "view_layer": context.view_layer.name,
        "mode": context.mode,
        "active_object": None if context.view_layer.objects.active is None
        else context.view_layer.objects.active.name,
        "selected_objects": sorted(obj.name for obj in context.view_layer.objects if obj.select_get()),
        "frame": context.scene.frame_current,
        "subframe": context.scene.frame_subframe,
        "playing": bool(context.screen.is_animation_playing) if context.screen else None,
        "character_id": None if record is None else record.character_id,
        "revision": None if record is None else record.revision,
        "record_error": record_error,
        "sources": sources,
        "hair": hair,
        "transport_active": bool(server.is_active()),
        "descriptor": None if descriptor is None else str(descriptor),
        "descriptor_value": descriptor_value,
        "transport_state": state_value,
        "context_ok": (window is not None
                       and window.scene.name == info["scene"]
                       and bpy.context.scene.name == info["scene"]
                       and bpy.context.view_layer.name == info["view_layer"]),
        "context": current,
        "expected_context": fingerprint(current),
    }


def attach(directory):
    """Attach this module once from the owned Blender window."""
    import bpy
    import rigging_workflow
    from rigging_workflow_contract import PRODUCT, runtime_documents

    global _attachment
    require(_attachment is None, "CONFLICT", "A rigging live-adapter queue is already attached")
    require(not bpy.app.background and is_supported_host(bpy.app.version)
            and tuple(rigging_workflow.bl_info["version"]) == (0,23,0), "UNSUPPORTED",
            "Attach from Blender 5.3.0 or later with Rigging Workflow 0.23.0 enabled")
    installed = Path(rigging_workflow.__file__).resolve().parent
    for document in runtime_documents():
        path = Path(document["file"])
        if path.is_relative_to(PRODUCT):
            require(digest(installed / path.relative_to(PRODUCT)) == document["expected_sha256"],
                    "CONFLICT", "Active Rigging Workflow differs from the selected public product")
    window = bpy.context.window
    require(window is not None and bpy.context.screen is not None and bpy.context.scene is not None,
            "UNSUPPORTED", "Attach from the owned Blender window after its event loop starts")
    service, _ = _product()
    record = service.current(bpy.context)
    root = Path(directory)
    require(root.is_absolute() and canonical(root.parent) == root.parent and not os.path.lexists(root),
            "CONFLICT", "Choose an unused absolute session directory in an existing parent")
    root.mkdir()
    (root / "requests").mkdir()
    info = {
        "schema": SESSION_SCHEMA,
        "session_id": uuid.uuid4().hex,
        "pid": os.getpid(),
        "directory": str(root),
        "blender": bpy.app.binary_path,
        "build": bpy.app.build_hash.decode(),
        "version": list(bpy.app.version),
        "version_string": bpy.app.version_string,
        "file": bpy.data.filepath,
        "scene": bpy.context.scene.name,
        "view_layer": bpy.context.view_layer.name,
        "window": window.as_pointer(),
        "character_id": record.character_id,
        "adapter_version": "0.1.0",
        "created_ns": time.time_ns(),
    }
    atomic_json(root / "session.json", info)
    _attachment = (root, info)
    bpy.app.handlers.load_pre.append(_on_load)
    if not bpy.app.timers.is_registered(_pump):
        bpy.app.timers.register(_pump, first_interval=0.15)
    return info


def _on_load(*_):
    detach()


def detach():
    """Detach this queue only; leave the Blender window and product transport alone."""
    import bpy

    global _attachment
    if _attachment is None:
        return
    root, info = _attachment
    atomic_json(root / "closed.json", {"session_id": info["session_id"], "detached_ns": time.time_ns()})
    if _on_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_on_load)
    _attachment = None


def _require_bound_context(bpy, info):
    window = _window(bpy, info["window"])
    require(window is not None, "CONFLICT", "The attached Blender window has closed")
    context = bpy.context
    require(window.scene.name == info["scene"] and window.view_layer.name == info["view_layer"] and
            context.scene.name == info["scene"] and context.view_layer.name == info["view_layer"],
            "WINDOW_CONTEXT_CHANGED",
            "The current window scene/view layer differs from the attached session")
    return window


def _transport_enable(bpy, info, request, observed):
    _, server = _product()
    from rigging_workflow.core import service
    require(not server.is_active(), "CONFLICT", "A product transport is already active in this Blender")
    root = Path(request["queue_root"])
    require(root.is_absolute() and root.is_dir() and canonical(root) == root, "INVALID_REQUEST",
            "queue_root must be an existing absolute directory")
    require(not any(root.iterdir()), "CONFLICT", "queue_root must be empty for a new transport session")
    descriptor = server.start(bpy.context, service.live_dispatch, root)
    bpy.context.scene.rw_live_root = str(root)
    bpy.context.scene.rw_live_descriptor = str(descriptor)
    raw = json.loads(descriptor.read_text(encoding="utf-8"))
    value = {key: raw.get(key) for key in (
        "schema", "transport_version", "session_id", "epoch", "pid", "file_kind",
        "scene", "view_layer", "window_bound", "contract_schema", "contract_sha256",
        "timer_interval_s", "lease_timeout_ns")}
    after = snapshot(bpy, info)
    return {
        "state": "enabled",
        "descriptor": str(descriptor),
        "descriptor_value": value,
        "server_active": True,
        "previous": observed,
        "after": after,
    }


def _transport_disable(bpy, info, request, observed):
    _, server = _product()
    require(server.is_active(), "CONFLICT", "No product transport is active in this Blender")
    descriptor = server.descriptor_path()
    require(descriptor is not None, "CONFLICT", "Active transport has no descriptor")
    raw = json.loads(descriptor.read_text(encoding="utf-8"))
    value = {key: raw.get(key) for key in (
        "schema", "transport_version", "session_id", "epoch", "pid", "file_kind",
        "scene", "view_layer", "window_bound", "contract_schema", "contract_sha256")}
    server.stop()
    state_path = descriptor.parent / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    return {
        "state": "disabled",
        "descriptor": str(descriptor),
        "descriptor_value": value,
        "server_active": False,
        "transport_state": state,
        "previous": observed,
        "after": snapshot(bpy, info),
    }


def _find_hair(bpy, record, hair_id):
    matches = [obj for obj in (record.work.all_objects if record.work else [])
               if obj.type == "ARMATURE" and obj.get("rw_hair_id") == hair_id]
    require(len(matches) == 1, "NOT_FOUND", "The requested hair_id is not a unique live hair rig")
    return matches[0]


def _play_pause(bpy, info, request, observed):
    from rigging_workflow.core import service
    context = bpy.context
    record = service.current(context)
    require(record.character_id == request["character_id"], "CONFLICT",
            "The requested character differs from the attached role")
    rig = _find_hair(bpy, record, request["hair_id"])
    state = _json(rig.get("rw_hair_state"), {})
    runtime = _json(rig.get("rw_hair_runtime"), {})
    require(runtime.get("preview_id"), "HAIR_PREVIEW_REQUIRED",
            "Create a live Core-owned hair preview before using timeline playback")
    require(bool(context.scene.rw_hair_enabled) and bool(record.hair_enabled), "HAIR_RUNTIME_DISABLED",
            "Scene or character hair physics is disabled")
    screen = context.screen
    require(screen is not None and hasattr(screen, "is_animation_playing"), "UNSUPPORTED",
            "Native screen playback state is unavailable")
    before = {
        "playing": bool(screen.is_animation_playing),
        "frame": context.scene.frame_current,
        "subframe": context.scene.frame_subframe,
        "hair_mode": state.get("mode"),
        "hair_enabled": bool(state.get("enabled")),
        "preview_id": runtime.get("preview_id"),
    }
    if before["playing"]:
        bpy.ops.screen.animation_cancel(restore_frame=False)
        target = False
        state_name = "paused"
    else:
        bpy.ops.screen.animation_play()
        target = True
        state_name = "playing"
    require(bool(screen.is_animation_playing) == target, "VALIDATION_FAILED",
            "Native playback state did not change as requested")
    after = {
        "playing": bool(screen.is_animation_playing),
        "frame": context.scene.frame_current,
        "subframe": context.scene.frame_subframe,
    }
    return {
        "state": state_name,
        "before": before,
        "after": after,
        "hair": {"object": rig.name, "hair_id": request["hair_id"],
                 "mode": state.get("mode"), "preview_id": runtime.get("preview_id")},
        "window_pointer": info["window"],
        "screen": screen.name,
    }


def _focus_source(bpy, info, request, observed):
    from rigging_workflow.core import service
    context = bpy.context
    require(context.mode == "OBJECT", "OBJECT_MODE_REQUIRED",
            "Source focus requires Object Mode; this adapter never exits another native edit mode")
    record = service.current(context)
    require(record.character_id == request["character_id"], "CONFLICT",
            "The requested character differs from the attached role")
    matches = [ref for ref in record.sources
               if ref.object is not None and ref.object.name == request["object"]]
    require(len(matches) == 1, "SOURCE_NOT_FOUND",
            "The requested name is not an original source object of this role")
    ref = matches[0]
    target = ref.object
    require(target.name in context.view_layer.objects, "SOURCE_UNAVAILABLE",
            "The source object is not linked in the attached view layer")
    require(target.library is None and (target.data is None or target.data.library is None),
            "SOURCE_UNAVAILABLE", "Linked library sources cannot be focused")
    require(not target.hide_select and not target.hide_viewport, "SOURCE_UNAVAILABLE",
            "The source object is disabled for selection or viewport display")
    work_names = {obj.name for obj in (record.work.all_objects if record.work else [])}
    require(target.name not in work_names, "CONFLICT",
            "The requested object is a work object, not an original source")
    for obj in context.selected_objects:
        obj.select_set(False)
    target.hide_set(False)
    target.select_set(True)
    context.view_layer.objects.active = target
    require(context.view_layer.objects.active == target and target.select_get(), "VALIDATION_FAILED",
            "Blender did not retain the named source selection")
    after = snapshot(bpy, info)
    return {
        "state": "focused",
        "object": target.name,
        "type": target.type,
        "mode": context.mode,
        "selected_objects": sorted(obj.name for obj in context.selected_objects),
        "source_of": record.character_id,
        "work_object": None if ref.work_object is None else ref.work_object.name,
        "revision": record.revision,
        "window_pointer": info["window"],
        "previous": observed,
        "after": after,
    }


def operate(bpy, info, request, observed):
    action = request["operation"]
    if action in WRITES:
        require(request["expected_context"] == observed["expected_context"], "STALE_CONTEXT",
                "The attached scene, window, role, frame, mode, hair preview or transport changed since inspection")
        _require_bound_context(bpy, info)
    if action == "inspect":
        return snapshot(bpy, info)
    if action == "transport-enable":
        return _transport_enable(bpy, info, request, observed)
    if action == "transport-disable":
        return _transport_disable(bpy, info, request, observed)
    if action == "play-pause":
        return _play_pause(bpy, info, request, observed)
    if action == "focus-source":
        return _focus_source(bpy, info, request, observed)
    raise Failure("INVALID_REQUEST", "Unsupported adapter operation")


def _pump():
    import bpy

    if _attachment is None:
        return None
    root, info = _attachment
    for path in sorted((root / "requests").glob("*/request.json"), key=lambda item: item.stat().st_mtime_ns):
        directory = path.parent
        ident = directory.name
        if not valid_id(ident) or directory.resolve() != directory or path.resolve() != path:
            continue
        marker, done = directory / "started.json", directory / "result.json"
        if done.exists() or marker.exists():
            continue
        closing = False
        try:
            request = read_json(path)
            verify_request(request)
            require(request["session_id"] == info["session_id"] and request["request_id"] == ident,
                    "CONFLICT", "Queue entry identity differs from its session")
            window = _window(bpy, info["window"])
            require(window is not None, "CONFLICT", "The attached Blender window has closed")
            atomic_json(marker, {"session_id": info["session_id"], "request_id": ident})
            with bpy.context.temp_override(window=window):
                observed = snapshot(bpy, info)
                if request["operation"] == "stop":
                    require(request["expected_context"] == observed["expected_context"], "STALE_CONTEXT",
                            "Inspect this window before detaching its adapter")
                    value = {"state": "detached"}
                    closing = True
                else:
                    value = operate(bpy, info, request, observed)
            response = envelope(COMMAND, ident, data={**value, "session_id": info["session_id"],
                                                       "request_id": ident})
        except Exception as exc:
            response = envelope(COMMAND, ident, error={
                "code": getattr(exc, "code", "WORKER_FAILED"),
                "message": str(exc) + (
                    "; current expected_context=" + snapshot(bpy, info)["expected_context"]
                    if getattr(exc, "code", None) == "STALE_CONTEXT" else ""),
            })
        atomic_json(done, response)
        if closing:
            detach()
            return None
        break
    return 0.2


def _read_result(path):
    try:
        return read_json(path), None
    except Failure as exc:
        if exc.code == "NOT_FOUND":
            return None, None
        raise
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) in (5, 32, 33):
            return None, exc
        raise


def run(args):
    root = canonical(args.session_dir)
    require(valid_id(args.session_id), "INVALID_REQUEST", "Expected a 32-hex session ID")
    require(0 <= args.wait_seconds <= 55, "INVALID_REQUEST", "wait-seconds must be in 0..55")
    writing = args.operation in WRITES
    lookup = args.operation == "result"
    require(not (writing or lookup) or args.request_id, "INVALID_REQUEST",
            "Supply a stable request UUID for write/result operations")
    ident = args.request_id or uuid.uuid4().hex
    require(valid_id(ident), "INVALID_REQUEST", "Expected a 32-hex request ID")
    request = {
        "schema": REQUEST_SCHEMA,
        "session_id": args.session_id,
        "request_id": ident,
        "operation": args.operation,
        "expected_context": args.expected_context,
        "queue_root": args.queue_root,
        "character_id": args.character_id,
        "object": args.object,
        "hair_id": args.hair_id,
    }
    verify_request(request)
    queue = root / "requests"
    directory = queue / ident
    with ExitStack() as guards:
        for parent in reversed((queue, *queue.parents)):
            if parent != Path(parent.anchor):
                guards.enter_context(FileGuard(parent, directory=True))
        guards.enter_context(FileGuard(root / "session.json"))
        info = read_json(root / "session.json")
        require(info["schema"] == SESSION_SCHEMA and info["session_id"] == args.session_id
                and Path(info["directory"]) == root
                and canonical(info["blender"]) == canonical(args.blender),
                "CONFLICT", "Attached session identity or host differs")
        with PathLocks([directory]):
            require(not os.path.lexists(directory) or (directory.is_dir() and canonical(directory) == directory),
                    "CONFLICT", "Request directory is redirected")
            if not lookup:
                if directory.exists():
                    require(read_json(directory / "request.json") == request, "CONFLICT",
                            "This request UUID already belongs to a different operation")
                else:
                    require(not (root / "closed.json").exists(), "CONFLICT", "Session has detached")
                    directory.mkdir()
                    atomic_json(directory / "request.json", request)
            require(directory.is_dir(), "NOT_FOUND", "Unknown request UUID")
        deadline = time.monotonic() + args.wait_seconds
        while True:
            result_path = directory / "result.json"
            result, read_error = _read_result(result_path)
            if result is not None:
                return result
            if (root / "closed.json").is_file() and not result_path.is_file():
                raise Failure("CONFLICT", "Session detached before completing this request")
            if time.monotonic() >= deadline:
                pending = envelope(COMMAND, ident, data={
                    "state": "pending", "session_id": args.session_id, "request_id": ident,
                    "result": str(result_path)})
                if read_error is not None:
                    pending["data"]["read_pending"] = str(read_error)
                return pending
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def main():
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--blender", type=Path, default=DEFAULT_BLENDER)
    for flag in ("--jobs-dir", "--transactions-dir", "--timeout"):
        parser.add_argument(flag, help=argparse.SUPPRESS)
    display = parser.add_mutually_exclusive_group()
    for flag in ("--compact", "--human", "--full"):
        display.add_argument(flag, action="store_true")
    command = parser.add_subparsers(required=True, parser_class=Parser).add_parser(
        "session-live", allow_abbrev=False,
        description="Typed live adapter for transport start/stop, hair playback toggle and source focus.",
        epilog="Attach this module once in the owned Blender window: "
               "rigging_workflow_live_adapter.attach(NEW_ABSOLUTE_DIRECTORY). "
               "Reuse the same request UUID after a pending wait. No scripts or arbitrary operations are accepted.")
    command.add_argument("--session-dir", type=Path, required=True)
    command.add_argument("--session-id", required=True)
    command.add_argument("--operation", choices=sorted(OPERATIONS), required=True)
    command.add_argument("--request-id")
    command.add_argument("--expected-context")
    command.add_argument("--queue-root")
    command.add_argument("--character-id")
    command.add_argument("--object")
    command.add_argument("--hair-id")
    command.add_argument("--wait-seconds", type=float, default=10)
    args = None
    try:
        args = parser.parse_args()
        result = run(args)
    except (Failure, OSError, ValueError, KeyError, TypeError) as exc:
        result = envelope(COMMAND, error={"code": getattr(exc, "code", "INVALID_REQUEST"),
                                          "message": str(exc)})
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["ok"] else CODES.get(result["error"]["code"], 8)


if __name__ == "__main__":
    raise SystemExit(main())
