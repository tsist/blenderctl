"""Control an explicitly attached Relight preview from the public CLI."""

import argparse
from contextlib import ExitStack, contextmanager
import copy
import importlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import uuid
from zipfile import ZipFile

sys.dont_write_bytecode = True
CORE = Path(__file__).resolve().parent
sys.path.insert(0, str(CORE))

from filesystem import FileGuard, PathLocks
from host_compat import is_supported_host
from material_draft_handoff import Parser, canonical, fingerprint, require
from material_live_session import valid_id
from protocol import CODES, DEFAULT_BLENDER, Failure, atomic_json, digest, envelope, read_json
from relight_saved_handoff import EDITION, RELEASE, RELEASE_SHA256, SAVE_FIELDS, read_preview, save_candidate, scene_owner

COMMAND = "compositing.session-live"
SCHEMA = "relight-blender-session/1"
SCENE_OPERATIONS = {"inspect", "preflight", "bind", "save-copy", "display", "final", "stop", "anime", "cli-root", "aim-distance"}
OPTIONAL_OWNER = {"inspect", "cli-root"}
WRITES = {"bind", "save-copy", "display", "final", "start", "stop", "anime", "cli-root", "aim-distance"}
READY_WRITES = {"bind", "start"}
OPERATIONS = SCENE_OPERATIONS | {"start", "result", "detach"}
CLI_ROOT_CHOICES = {"inherit", "qualified-host"}
CLIENT_OPERATIONS = {"start", "bind", "preflight", "anime", "aim-distance", "final"}
PROJECT_ROOT = CORE.parents[1]
_attachment = None


def hex_string(value, length):
    return isinstance(value, str) and re.fullmatch(f"[0-9a-f]{{{length}}}", value) is not None


def native_panel(bpy):
    require(is_supported_host(bpy.app.version), "UNSUPPORTED",
            "Expected the qualified Blender 5.2.1 or 5.3.0 host")
    operators = [bpy.types.Operator.bl_rna_get_subclass_py("RELIGHT_PREVIEW_OT_" + action)
                 for action in ("bind", "save", "display_mode", "start", "stop", "anime_mode")]
    require(all(operators) and len({item.__module__ for item in operators}) == 1,
            "UNSUPPORTED", "Enable the matching native Relight preview panel first")
    module = importlib.import_module(operators[0].__module__)
    require(module._save_pre in bpy.app.handlers.save_pre, "CONFLICT", "Native Relight save callback is unregistered")
    require(digest(RELEASE) == RELEASE_SHA256, "CONFLICT", "The qualified Relight release archive changed")
    path = Path(module.__file__)
    actual = path.read_bytes() if path.is_file() else module.__spec__.loader.get_data(module.__file__)
    with ZipFile(RELEASE) as archive:
        require(actual == archive.read("relight_preview_ui.py"), "CONFLICT", "Enabled Relight panel differs from 0.9.0")
    return module


def attach(directory, owner, jobs_dir, output_root):
    """Attach from the Blender window that owns a saved or live preview scene."""
    import bpy

    global _attachment
    require(_attachment is None, "CONFLICT", "A Relight queue is already attached")
    require(owner is None or hex_string(owner, 32), "INVALID_REQUEST", "Expected the native 32-character owner ID or None")
    window = bpy.context.window
    require(window is not None, "UNSUPPORTED", "Attach from the owned Blender window")
    ui = native_panel(bpy)
    if owner is None:
        bootstrap_snapshot(bpy, ui, bpy.context)
    else:
        scene_owner(bpy, ui, bpy.context, {"expected_owner": owner})
    root = Path(directory)
    require(root.is_absolute() and canonical(root.parent) == root.parent and not os.path.lexists(root),
            "CONFLICT", "Choose an unused absolute session directory in an existing parent")
    jobs, destination = canonical(jobs_dir), canonical(output_root)
    require(jobs.is_dir() and destination.is_dir(), "INVALID_REQUEST", "Select existing job and output directories")
    root.mkdir()
    (root / "requests").mkdir()
    info = dict(schema=SCHEMA, session_id=str(uuid.uuid4()), owner=owner, pid=os.getpid(), directory=str(root),
                blender=bpy.app.binary_path, build=bpy.app.build_hash.decode(), edition=EDITION,
                jobs_dir=str(jobs), output_root=str(destination))
    atomic_json(root / "session.json", info)
    _attachment = (root, info, ui, {"pointer": window.as_pointer(), "owner": owner, "cli_root": "inherit"})
    bpy.app.handlers.load_pre.append(_on_load)
    if not bpy.app.timers.is_registered(_pump):
        bpy.app.timers.register(_pump, first_interval=.15)
    return info


def _on_load(*_):
    detach()


def detach():
    """Detach CLI admission and leave the Blender window and its GPU session alone."""
    import bpy

    global _attachment
    if _attachment is None:
        return
    root, info, _, _ = _attachment
    atomic_json(root / "closed.json", {"session_id": info["session_id"]})
    if _on_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_on_load)
    _attachment = None


def cli_root_state(ui):
    profile = _attachment[3]["cli_root"] if _attachment else "inherit"
    root, selected_by = ui._cli_selection() if profile == "inherit" else (str(PROJECT_ROOT), "附接队列")
    return dict(profile=profile, root=root, selected_by=selected_by, scope="ATTACHED_QUEUE")


@contextmanager
def qualified_cli(ui, profile):
    """Resolve the pinned client closure; a queue request never supplies a path."""
    addon = ui.bpy.context.preferences.addons.get(ui._ADDON_ID)
    preferences = getattr(addon, "preferences", None)
    previous = None
    with ExitStack() as guards:
        if profile == "qualified-host":
            require(preferences is not None and hasattr(preferences, "cli_root"), "UNSUPPORTED",
                    "Enable the qualified Relight AddonPreferences to set the attached CLI root")
            previous = preferences.cli_root
            preferences.cli_root = str(PROJECT_ROOT)
            guards.callback(setattr, preferences, "cli_root", previous)
        raw, selected_by = ui._cli_selection()
        requested = Path(raw)
        require(requested.is_absolute() and Path(os.path.normpath(requested)) == PROJECT_ROOT,
                "CONFLICT", "Native CLI root differs from the attached qualified host")
        require(canonical(requested) == PROJECT_ROOT, "CONFLICT", "The CLI root is redirected")
        folder, modules, contract = ui._cli_files()
        expected = {name: CORE / (name + ".py") for name in ui._CLIENT_MODULES}
        expected_contract = PROJECT_ROOT / "tools/compositing_workflow_addon/depth_contract.py"
        require(folder == CORE and modules == expected and contract == expected_contract,
                "CONFLICT", "The native client closure does not come from the qualified host")
        for path in (*modules.values(), contract):
            guards.enter_context(FileGuard(path))
        ui._check_client_sources(modules, contract)
        yield {"root": str(PROJECT_ROOT), "directory": str(folder), "selected_by": selected_by,
               "closure": {**{name: str(path) for name, path in modules.items()}, "depth_contract": str(contract)}}


def choose_cli_root(bpy, ui, context, request, attachment):
    owner = attachment["owner"]
    panel = bootstrap_snapshot(bpy, ui, context) if owner is None else snapshot(bpy, ui, context, owner)[0]
    require(request["expected_context"] == panel["expected_context"], "CONFLICT",
            "The scene or attached CLI root changed since inspection")
    profile = request["root_source"]
    with qualified_cli(ui, profile) as verified:
        resolution = verified
    attachment["cli_root"] = profile
    panel = bootstrap_snapshot(bpy, ui, context) if owner is None else snapshot(bpy, ui, context, owner)[0]
    return dict(state="client_source_selected", profile=profile, panel=panel, resolution=resolution,
                persisted_preferences=False)


def bootstrap_snapshot(bpy, ui, context):
    scene = context.scene
    require(scene is not None and scene.get(ui.OWNER) is None and context.window is not None,
            "CONFLICT", "Attach for start from a window without a Relight preview owner")
    state = dict(state="ready_to_start", owner=None, file=bpy.data.filepath, scene=scene.name,
                 window=context.window.as_pointer(), scene_pointer=scene.as_pointer(), frame=scene.frame_current,
                 subframe=scene.frame_subframe, view_layer=context.view_layer.name, cli_root=cli_root_state(ui))
    state["expected_context"] = fingerprint(state)
    return state


def snapshot(bpy, ui, context, owner):
    scene = context.scene
    text = scene_owner(bpy, ui, context, {"expected_owner": owner})
    require(scene.frame_subframe == 0, "UNSUPPORTED", "Live Relight handoff uses a whole frame")
    preview = read_preview(ui, scene, text)
    runtime = ui._sessions.get(owner)
    client = runtime.get("client") if runtime else None
    shown = preview["shown_manifest"] or {}
    state = dict(owner=owner, file=bpy.data.filepath, scene=scene.name, view_layer=context.view_layer.name,
                 frame=scene.frame_current, binding=client.ready["session_id"] if client else None,
                 displayed_job=(shown.get("request") or {}).get("session_id", shown.get("session_id")),
                 ready_file=preview["ready_file"], ready_snapshot_sha256=preview["ready_snapshot_sha256"],
                 current_config=preview["current_config"], pending_local_edits=preview["pending_local_edits"],
                 applied_local_edits=preview["applied_local_edits"], revisions=preview["revisions"],
                 shown_config=preview["shown_config"], shown_parameters_sha256=shown.get("parameters_sha256"),
                 display=preview["display"], cli_root=cli_root_state(ui))
    state["expected_context"] = fingerprint(state)
    return state, preview


def ready_reference(info, job_id, guards):
    jobs = canonical(info["jobs_dir"])
    folder = jobs / job_id
    require(folder.is_dir() and canonical(folder) == folder, "NOT_FOUND", "Requested GPU job is not in the attached jobs directory")
    path = folder / "session-ready.json"
    require(path.is_file() and canonical(path) == path, "NOT_FOUND", "Requested GPU job has no ready file")
    with_guard = guards.enter_context(FileGuard(path))
    return {"file": str(path), "sha256": with_guard.sha256()}


def live_ready(ui, job_id, identity):
    try:
        client = ui._client(identity["file"])
    except (RuntimeError, ValueError, OSError) as error:
        raise Failure("CONFLICT", "The native client rejected the requested GPU job: " + str(error)) from error
    ready = client.ready
    require(ready["session_id"] == job_id, "CONFLICT", "Ready identity differs from the requested job")
    return ready


def validate_live_ready(ui, scene, job_id, identity):
    ready = live_ready(ui, job_id, identity)
    require(ready["initial_config"]["frame"] == ui.extract_config(scene)["frame"]
            and ui.frame_geometry(ready) == json.loads(scene["rp_geometry"]), "CONFLICT",
            "Ready frame or image domain differs from the attached preview")


def fresh_output(info, raw):
    output = Path(raw)
    require(output.is_absolute() and output.suffix.lower() == ".blend", "INVALID_REQUEST",
            "Expected a new absolute .blend output")
    parent = canonical(output.parent)
    require(parent == output.parent and parent.is_relative_to(canonical(info["output_root"])), "CONFLICT",
            "Output must stay in the attached output directory")
    require(not os.path.lexists(output), "CONFLICT", "Output already exists")
    return output


def save_copy(bpy, ui, context, info, request, guards):
    scene = context.scene
    text = scene_owner(bpy, ui, context, {"expected_owner": request["owner"]})
    output = fresh_output(info, request["output"])
    for parent in reversed((output.parent, *output.parent.parents)):
        if parent != Path(parent.anchor):
            guards.enter_context(FileGuard(parent, directory=True))
    guards.enter_context(PathLocks([output]))
    require(not os.path.lexists(output), "CONFLICT", "Output already exists")
    stage = output.with_name(".relight-live-" + uuid.uuid4().hex + ".blend")
    require(not os.path.lexists(stage), "CONFLICT", "Private stage already exists")
    guards.callback(stage.unlink, missing_ok=True)
    current = {key: scene[key] for key in SAVE_FIELDS if key in scene}
    original_text = text.as_string() if text else None
    source_file = bpy.data.filepath
    try:
        save_candidate(bpy, ui, scene, str(stage), str(output))
    finally:
        for key in SAVE_FIELDS:
            if key in current:
                if scene.get(key) != current[key]:
                    scene[key] = current[key]
            elif key in scene:
                del scene[key]
        if text and text.as_string() != original_text:
            text.clear()
            text.write(original_text)
    require(bpy.data.filepath == source_file, "VALIDATION_FAILED", "Native save did not keep the current project open")
    with FileGuard(stage, renameable=True) as artifact:
        fingerprint = artifact.sha256()
        artifact.rename_new(output)
    return {"file": str(output), "sha256": fingerprint}


def start_preview(bpy, ui, context, request, info, attachment):
    observed = bootstrap_snapshot(bpy, ui, context)
    if request["operation"] == "inspect":
        return observed
    require(request["operation"] == "start", "CONFLICT", "Create and identify a Relight preview first")
    require(request["expected_context"] == observed["expected_context"], "CONFLICT",
            "The attached source scene or window changed since inspection")
    job_id = request["job_id"]
    require(all(runtime["client"].ready["session_id"] != job_id for runtime in ui._sessions.values()), "CONFLICT",
            "The selected GPU job is already owned by a Blender preview in this process")
    original_scene = context.scene
    window = context.window
    manager = context.window_manager
    original_windows = {item.as_pointer() for item in manager.windows}
    with ExitStack() as guards:
        ready = ready_reference(info, job_id, guards)
        require(request["ready_sha256"] == ready["sha256"], "CONFLICT", "Ready file SHA differs from the supplied identity")
        live_ready(ui, job_id, ready)
        # A Blender child window shares scene switches with its source. Keep the
        # original scene active by using a separate main window for native setup.
        require(bpy.ops.wm.window_new_main() == {"FINISHED"}, "WORKER_FAILED", "Could not open a Relight main window")
        previews = [item for item in manager.windows if item.as_pointer() not in original_windows]
        require(len(previews) == 1 and window.scene == original_scene and bpy.data.filepath == observed["file"],
                "VALIDATION_FAILED", "Relight main window did not preserve the attached source")
        new = previews[0]
        scene = ui.setup(ready["file"], open_window=False)
        owner = scene[ui.OWNER]
        new.scene = scene
        ui._configure_preview_window(new, scene)
        scene["rp_window_created"] = True
        ui._sessions[owner]["window_pointer"] = new.as_pointer()
        require(window.scene == original_scene, "VALIDATION_FAILED", "The Relight window changed the source scene")
        with bpy.context.temp_override(window=new):
            state, _ = snapshot(bpy, ui, bpy.context, owner)
        require(state["binding"] == job_id, "VALIDATION_FAILED", "Fresh preview did not own the requested job")
        attachment.update(pointer=new.as_pointer(), owner=owner)
        return dict(state="started", panel=state, job_id=job_id, ready=ready,
                    source=observed, preview_window=new.as_pointer())


def require_named_preview(observed, ready):
    require(canonical(observed["ready_file"]) == Path(ready["file"])
            and observed["ready_snapshot_sha256"] == fingerprint(read_json(Path(ready["file"]))), "CONFLICT",
            "The ready identity differs from the attached preview")


def anime(bpy, ui, context, request, observed):
    field, value = request["field"], request["value"]
    config = observed["current_config"]
    require(config["version"] == ui.ANIME_VERSION, "UNSUPPORTED", "Anime controls require an Anime 0.4 preview")
    values = {"view": ui.ANIME_VIEWS, "subject_source": ("LIGHT_MASK", "DEPTH_VALID", "ALPHA"),
              "solo_light_id": ("", *(item["id"] for item in config["lights"]))}
    require(value in values[field], "INVALID_REQUEST", "Unknown Anime choice or light ID")
    require(bpy.ops.relight_preview.anime_mode(field=field, value=value) == {"FINISHED"}, "WORKER_FAILED",
            "Native Anime control did not finish")
    current, _ = snapshot(bpy, ui, context, request["owner"])
    require(current["current_config"]["anime"][field] == value, "VALIDATION_FAILED", "Native Anime value differs")
    return dict(state="control_set", job_id=request["job_id"], panel=current, field=field, value=value,
                previous_shown_revision=observed["revisions"]["shown"])


def aim_distance(bpy, ui, context, request, observed):
    light_id, value = request["light_id"], request["distance"]
    matches = [obj for obj in context.scene.objects if obj.get(ui.OWNER) == request["owner"]
               and obj.get("relight_id") == light_id]
    require(len(matches) == 1, "NOT_FOUND", "The named owned light is absent or ambiguous")
    obj = matches[0]
    require(obj.type == "LIGHT" and obj.data.type in {"SPOT", "AREA"} and "rp_aim_distance" in obj,
            "UNSUPPORTED", "Aim distance is a native SPOT or AREA control")
    require(not obj.library and not obj.override_library and not obj.data.library and not obj.data.override_library,
            "CONFLICT", "The named light and its data must be local")
    direction = ui._direction(obj)
    location, rotation = tuple(obj.location), tuple(obj.rotation_quaternion)
    expected_point = [origin + component * value for origin, component in zip(location, direction, strict=True)]
    require(all(-10000 <= coordinate <= 10000 for coordinate in expected_point)
            and math.dist(location, expected_point) > 1e-6, "INVALID_REQUEST", "Aim point is outside the Relight domain")
    previous = obj["rp_aim_distance"]
    expected_config = copy.deepcopy(observed["current_config"])
    target = next(row for row in expected_config["lights"] if row["id"] == light_id)
    old_aim = target["aim"]
    try:
        # The panel binds straight to this ID property; the native tick reads
        # the point from extract_config and submits the resulting revision.
        obj["rp_aim_distance"] = value
        obj.update_tag()
        current, _ = snapshot(bpy, ui, context, request["owner"])
        row = next(row for row in current["current_config"]["lights"] if row["id"] == light_id)
        target["aim"] = row["aim"]
        require(current["current_config"] == expected_config and tuple(obj.location) == location
                and tuple(obj.rotation_quaternion) == rotation and current["pending_local_edits"] == observed["pending_local_edits"]
                and all(math.isclose(actual, expected, rel_tol=2e-6, abs_tol=2e-6)
                        for actual, expected in zip(row["aim"], expected_point, strict=True)),
                "VALIDATION_FAILED", "Native aim-distance edit changed an unrelated light property or the aim point differed")
    except BaseException:
        obj["rp_aim_distance"] = previous
        obj.update_tag()
        raise
    return dict(state="control_set" if row["aim"] != old_aim else "within_preview_tolerance",
                panel=current, job_id=request["job_id"], light_id=light_id, light_type=obj.data.type, distance=value,
                previous_distance=previous, aim_before=old_aim, aim=row["aim"], direction=direction,
                previous_shown_revision=observed["revisions"]["shown"])


def stop_preview(bpy, ui, context, request, observed):
    owner = request["owner"]
    require(observed["binding"] == request["job_id"], "CONFLICT", "The requested GPU job is not actively bound")
    timer = ui._sessions[owner]["timer"]
    require(bpy.ops.relight_preview.stop() == {"FINISHED"}, "WORKER_FAILED", "Native Relight stop did not finish")
    current, _ = snapshot(bpy, ui, context, owner)
    require(not current["binding"] and (not timer or not bpy.app.timers.is_registered(timer))
            and all(current[key] == observed[key] for key in ("file", "scene", "display", "pending_local_edits", "revisions")),
            "VALIDATION_FAILED", "Native stop did not retain the displayed frame and editable proxies")
    journal = json.loads(context.scene.get("rp_event_log", "[]"))
    event = journal[-1] if journal else {}
    require(event.get("event") == "stop_requested", "WORKER_FAILED",
            "Local preview stopped; GPU stop receipt: " + str(event.get("message") or event.get("event")))
    return dict(state="stop_requested", job_id=request["job_id"], panel=current, native_event=event)


def submit_final(bpy, ui, context, request, observed):
    require(is_supported_host(bpy.app.version), "UNSUPPORTED",
            "Live Relight final submission requires Blender 5.3.0")
    job_id = request["job_id"]
    require(observed["binding"] == job_id, "CONFLICT", "Final requires the actively bound GPU job")
    require(observed["pending_local_edits"] == observed["applied_local_edits"], "CONFLICT",
            "Apply the pending local regions in the owning Blender preview before submitting final")
    config = copy.deepcopy(observed["current_config"])
    if config["version"] == ui.ANIME_VERSION:
        config["anime"]["view"] = "RESULT"
    ticket = ui.request_final(context.scene)
    current, _ = snapshot(bpy, ui, context, request["owner"])
    require(ticket["revision"] == current["revisions"]["latest"] and current["binding"] == job_id
            and current["current_config"] == config and current["display"]["mode"] == "RESULT"
            and current["applied_local_edits"] == observed["applied_local_edits"],
            "VALIDATION_FAILED", "Native final submission differed from the observed controls")
    return dict(state="submitted_final", job_id=job_id, panel=current, **ticket)


def operate(bpy, ui, context, request, info):
    action = request["operation"]
    owner = request["owner"]
    observed, preview = snapshot(bpy, ui, context, owner)
    if action == "inspect":
        return observed
    if action in WRITES:
        require(request["expected_context"] == observed["expected_context"], "CONFLICT",
                "The preview or its draft changed since inspection")
    job_id = request["job_id"]
    require(observed["binding"] in (None, job_id), "CONFLICT", "Another GPU job is already bound to this preview")
    scene = context.scene
    with ExitStack() as guards:
        ready = ready_reference(info, job_id, guards)
        if action in {"preflight", "bind"}:
            if action == "bind":
                require(request["ready_sha256"] == ready["sha256"], "CONFLICT", "Ready file SHA differs from preflight")
            validate_live_ready(ui, scene, job_id, ready)
        if action == "preflight":
            destination = fresh_output(info, request["output"]) if request["output"] else None
            if destination:
                require(observed["binding"] == observed["displayed_job"] == job_id, "CONFLICT",
                        "Wait for the named GPU job to display a revision before saving")
            return dict(state="ready", panel=observed, job_id=job_id, ready=ready,
                        output=str(destination) if destination else None)
        if action == "bind":
            if observed["binding"] is None:
                require(bpy.ops.relight_preview.bind(ready_file=ready["file"]) == {"FINISHED"}, "WORKER_FAILED",
                        "Native Relight bind did not finish")
            else:
                return dict(state="already_bound", panel=observed, job_id=job_id, ready=ready)
            current, _ = snapshot(bpy, ui, context, owner)
            require(current["binding"] == job_id
                    and current["pending_local_edits"] == observed["pending_local_edits"],
                    "VALIDATION_FAILED", "Native bind did not retain the pending local draft")
            return dict(state="bound", panel=current, job_id=job_id, ready=ready)
        require_named_preview(observed, ready)
        if action == "stop":
            return stop_preview(bpy, ui, context, request, observed)
        if action == "final":
            validate_live_ready(ui, scene, job_id, ready)
            return submit_final(bpy, ui, context, request, observed)
        if action in {"anime", "aim-distance"}:
            require(observed["binding"] == job_id, "CONFLICT", "This control requires the actively bound job")
            validate_live_ready(ui, scene, job_id, ready)
            control = anime if action == "anime" else aim_distance
            return control(bpy, ui, context, request, observed)
        require(observed["displayed_job"] == job_id, "CONFLICT", "Wait for the named GPU job to display a revision")
        if action == "display":
            require(bpy.ops.relight_preview.display_mode(mode=request["view"]) == {"FINISHED"}, "WORKER_FAILED",
                    "Native Relight display switch did not finish")
            current, _ = snapshot(bpy, ui, context, owner)
            require(current["display"]["mode"] == request["view"], "VALIDATION_FAILED",
                    "Native Relight display differs")
            return dict(state="displayed", panel=current, job_id=job_id)
        require(action == "save-copy", "INVALID_REQUEST", "Unsupported Relight operation")
        candidate = save_copy(bpy, ui, context, info, request, guards)
        return dict(state="saved", panel=observed, job_id=job_id, candidate=candidate,
                    displayed_parameters_sha256=(preview["shown_manifest"] or {}).get("parameters_sha256"))


def verify_request(request):
    fields = {"operation", "owner", "job_id", "ready_sha256", "output", "view", "expected_context",
              "session_id", "request_id"}
    require(isinstance(request, dict), "INVALID_REQUEST", "Expected a Relight queue object")
    action = request.get("operation")
    added = {"anime": {"field", "value"}, "cli-root": {"root_source"}, "aim-distance": {"light_id", "distance"}}
    require(isinstance(action, str) and request.keys() == fields | added.get(action, set()),
            "INVALID_REQUEST", "Unexpected Relight queue fields")
    require(isinstance(action, str) and action in OPERATIONS and valid_id(request["session_id"])
            and valid_id(request["request_id"]), "INVALID_REQUEST", "Invalid Relight operation or UUID")
    owner = request["owner"]
    require((hex_string(owner, 32) if action in SCENE_OPERATIONS - OPTIONAL_OWNER else
             (owner is None or hex_string(owner, 32)) if action in OPTIONAL_OWNER else owner is None),
            "INVALID_REQUEST", "Supply the attached --owner for preview actions")
    require((hex_string(request["job_id"], 32) if action in (SCENE_OPERATIONS - OPTIONAL_OWNER) | {"start"}
             else request["job_id"] is None), "INVALID_REQUEST", "This operation requires an explicit --job-id")
    for field, needed in (("expected_context", action in WRITES), ("ready_sha256", action in READY_WRITES)):
        require(hex_string(request[field], 64) if needed else request[field] is None,
                "INVALID_REQUEST", f"Invalid {field} for this operation")
    output = request["output"]
    require((action == "preflight" and (output is None or isinstance(output, str))) or
            (action == "save-copy" and isinstance(output, str)) or
            (action not in {"preflight", "save-copy"} and output is None),
            "INVALID_REQUEST", "Expected --output for save-copy or optional preflight output")
    require(request["view"] in {"ORIGINAL", "RESULT"} if action == "display" else request["view"] is None,
            "INVALID_REQUEST", "Only display uses --view ORIGINAL or RESULT")
    if action == "anime":
        require(request["field"] in {"view", "subject_source", "solo_light_id"}
                and type(request["value"]) is str and len(request["value"]) <= 128,
                "INVALID_REQUEST", "Anime needs --field and --value; use --value= to clear Solo")
    if action == "cli-root":
        require(isinstance(request["root_source"], str) and request["root_source"] in CLI_ROOT_CHOICES,
                "INVALID_REQUEST", "Select a trusted root source")
    if action == "aim-distance":
        distance = request["distance"]
        light = request["light_id"]
        require(isinstance(light, str) and 0 < len(light) <= 64 and all(ord(char) >= 32 for char in light)
                and type(distance) in (int, float) and 0 < distance <= 35000 and math.isfinite(distance),
                "INVALID_REQUEST", "Supply the named light and a positive bounded aim distance")


def _pump():
    import bpy

    if _attachment is None:
        return None
    root, info, ui, attachment = _attachment
    for path in sorted((root / "requests").glob("*/request.json")):
        directory = path.parent
        ident = directory.name
        if not valid_id(ident) or directory.resolve() != directory or path.resolve() != path:
            continue
        marker, done = directory / "started.json", directory / "result.json"
        if marker.exists() or done.exists():
            continue
        closing = False
        try:
            request = read_json(path)
            verify_request(request)
            require(request["session_id"] == info["session_id"] and request["request_id"] == ident,
                    "CONFLICT", "Queue entry identity differs")
            atomic_json(marker, {"session_id": info["session_id"], "request_id": ident})
            if request["operation"] == "detach":
                data = {"state": "detached"}
                closing = True
            else:
                require(request["owner"] == attachment["owner"], "CONFLICT", "Request owner differs from the attached preview")
                window = next((item for item in bpy.context.window_manager.windows
                               if item.as_pointer() == attachment["pointer"]), None)
                require(window is not None, "CONFLICT", "The attached Blender window has closed")
                with bpy.context.temp_override(window=window), ExitStack() as guards:
                    action = request["operation"]
                    if action in CLIENT_OPERATIONS:
                        guards.enter_context(qualified_cli(ui, attachment["cli_root"]))
                    if action == "cli-root":
                        data = choose_cli_root(bpy, ui, bpy.context, request, attachment)
                    elif attachment["owner"] is None:
                        data = start_preview(bpy, ui, bpy.context, request, info, attachment)
                    else:
                        data = operate(bpy, ui, bpy.context, request, info)
            result = envelope(COMMAND, ident, data={**data, "session_id": info["session_id"], "request_id": ident})
        except Exception as error:
            code = getattr(error, "code", "WORKER_FAILED")
            result = envelope(COMMAND, ident, error={"code": code, "message": str(error)})
        atomic_json(done, result)
        if closing:
            detach()
            return None
        break
    return .2


def run(args):
    root = canonical(args.session_dir)
    require(0 <= args.wait_seconds <= 55, "INVALID_REQUEST", "wait-seconds must be in 0..55")
    writing = args.operation in WRITES | {"result"}
    require(not writing or valid_id(args.request_id), "INVALID_REQUEST", "Supply a stable request UUID")
    ident = args.request_id or str(uuid.uuid4())
    request = {key: getattr(args, key) for key in ("operation", "owner", "job_id", "ready_sha256",
                                                  "output", "view", "expected_context", "session_id")}
    request["request_id"] = ident
    operation_fields = {"anime": ("field", "value"), "cli-root": ("root_source",), "aim-distance": ("light_id", "distance")}
    chosen = operation_fields.get(args.operation, ())
    request.update({field: getattr(args, field) for field in chosen})
    require(all(getattr(args, field) is None for fields in operation_fields.values() for field in fields if field not in chosen),
            "INVALID_REQUEST", "Unexpected operation-specific arguments")
    verify_request(request)
    queue = root / "requests"
    directory = queue / ident
    with ExitStack() as guards:
        for parent in reversed((queue, *queue.parents)):
            if parent != Path(parent.anchor):
                guards.enter_context(FileGuard(parent, directory=True))
        guards.enter_context(FileGuard(root / "session.json"))
        info = read_json(root / "session.json")
        require(info["schema"] == SCHEMA and info["session_id"] == args.session_id and Path(info["directory"]) == root
                and canonical(info["blender"]) == canonical(args.blender), "CONFLICT", "Attached session identity or host differs")
        require(args.owner is None or info["owner"] is None or args.owner == info["owner"],
                "CONFLICT", "Requested owner is not attached")
        with PathLocks([directory]):
            require(not os.path.lexists(directory) or (directory.is_dir() and canonical(directory) == directory),
                    "CONFLICT", "Request directory is redirected")
            if args.operation != "result":
                if directory.exists():
                    require(read_json(directory / "request.json") == request, "CONFLICT",
                            "This UUID belongs to a different operation")
                else:
                    require(not (root / "closed.json").exists(), "CONFLICT", "Session detached")
                    directory.mkdir()
                    atomic_json(directory / "request.json", request)
            require(directory.is_dir(), "NOT_FOUND", "Unknown request UUID")
        result = directory / "result.json"
        deadline = time.monotonic() + args.wait_seconds
        while not result.is_file():
            require(not (root / "closed.json").exists(), "CONFLICT", "Session detached before completing this request")
            if time.monotonic() >= deadline:
                return envelope(COMMAND, ident, data=dict(state="pending", session_id=args.session_id,
                                                         request_id=ident, result=str(result)))
            time.sleep(min(.1, max(0, deadline - time.monotonic())))
        return read_json(result)


def main():
    cli = Parser(description=__doc__, allow_abbrev=False)
    cli.add_argument("--blender", type=Path, default=DEFAULT_BLENDER)
    for flag in ("--jobs-dir", "--transactions-dir", "--timeout"):
        cli.add_argument(flag, help=argparse.SUPPRESS)
    mode = cli.add_mutually_exclusive_group()
    for flag in ("--compact", "--human", "--full"):
        mode.add_argument(flag, action="store_true")
    command = cli.add_subparsers(required=True, parser_class=Parser).add_parser(
        "session-live", allow_abbrev=False,
        description="Start, inspect, bind, display, edit, submit final, save or stop a preview in the attached Blender process.",
        epilog="Explicitly attach(session_dir, owner, jobs_dir, output_root) in the owner window. "
               "For start use owner=None in a non-preview window and pass the ready SHA from the named job. "
               "Inspect gives the expected context SHA. Preflight gives the ready SHA for bind. "
               "Anime writes set a native control; inspect shown_config and revisions after the GPU publishes. "
               "cli-root selects inherit or the qualified host for this attached queue without saving preferences. "
               "aim-distance takes a named SPOT/AREA light and a positive --distance along its current direction. "
               "final submits the current controls and applied regions of the bound job at source resolution. "
               "Read its revision and parameters SHA with compositing depth-control read, then stop and read the job result. "
               "Use result with the same request UUID to recover a pending operation. "
               "Detach leaves the Blender and GPU processes running.",
    )
    command.add_argument("--session-dir", type=Path, required=True)
    command.add_argument("--session-id", required=True)
    command.add_argument("--operation", choices=sorted(OPERATIONS), required=True)
    command.add_argument("--owner")
    command.add_argument("--job-id")
    command.add_argument("--ready-sha256")
    command.add_argument("--output")
    command.add_argument("--view", choices=("ORIGINAL", "RESULT"))
    command.add_argument("--field", choices=("view", "subject_source", "solo_light_id"))
    command.add_argument("--value")
    command.add_argument("--root-source", choices=sorted(CLI_ROOT_CHOICES))
    command.add_argument("--light-id")
    command.add_argument("--distance", type=float)
    command.add_argument("--request-id")
    command.add_argument("--expected-context")
    command.add_argument("--wait-seconds", type=float, default=10)
    try:
        result = run(cli.parse_args())
    except (Failure, OSError, ValueError, KeyError, TypeError) as error:
        result = envelope(COMMAND, error={"code": getattr(error, "code", "INVALID_REQUEST"), "message": str(error)})
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["ok"] else CODES.get(result["error"]["code"], 8)


if __name__ == "__main__":
    raise SystemExit(main())
