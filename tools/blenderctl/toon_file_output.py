"""Saved Toon file-panel overrides, scoped media output, and compositor handoff."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time

from filesystem import FileGuard, PathLocks
from material_draft_handoff import canonical, fingerprint, guard_file, read_document, require
from protocol import Failure, atomic_json, digest
import toon_viewport_lines_contract as contract


READ = "inspect-file-output"
EDIT = "edit-file-output"
RENDER = "render-file-output"
OPERATIONS = (READ, EDIT, RENDER)
MANIFEST = "toon.scoped-output.v1"
HELP = ("inspect-file-output reads saved scene/layer overrides, worker preferences and the last native line output. "
        "edit-file-output accepts output_action SET_SCOPE {kind,overrides}, REMOVE_SCOPE {kind}, "
        "PREFERENCES {values} or ADD_OUTPUT with pass_name and either {manifest,frame} or "
        "{saved_output:{file,sha256}} from the scene, and writes a new .blend. "
        "render-file-output writes a new --output directory; chooses PNG/EXR, prefix, frames or keyed_only, "
        "and saved_elements or explicit elements for multilayer EXR.")


def file_ref(value):
    require(type(value) is dict and set(value) == {"file", "sha256"} and isinstance(value["file"], str)
            and Path(value["file"]).is_absolute() and Path(value["file"]).suffix.lower() == ".json"
            and isinstance(value["sha256"], str) and contract.SHA256.fullmatch(value["sha256"]),
            "INVALID_REQUEST", "Expected an absolute hash-pinned output manifest JSON")
    return dict(value)


def media_ref(value):
    require(isinstance(value, dict) and set(value) == {"file", "sha256"}
            and isinstance(value["file"], str) and Path(value["file"]).is_absolute()
            and Path(value["file"]).suffix.lower() in (".png", ".exr")
            and isinstance(value["sha256"], str) and contract.SHA256.fullmatch(value["sha256"]),
            "INVALID_REQUEST", "Expected saved_output with an absolute PNG/EXR file and its SHA-256")
    return dict(value)


def normalize_request(document, editions):
    extras = {"output_action", "format", "prefix", "keyed_only", "saved_elements"}
    operation = document["operation"]
    payload = {key: val for key, val in document.items() if key not in extras}
    require(not {"scope", "patch"} & payload.keys(), "INVALID_REQUEST", "File output uses native scene or layer scope")
    if operation == READ:
        require(not extras.intersection(document), "INVALID_REQUEST", "Inspection accepts no output edits")
        payload.update(operation="inspect", scope={"groups": ["file-output"]})
    else:
        payload["operation"] = "render-camera"
    result = contract.normalize_request(payload, editions)
    result.pop("scope", None)
    result["operation"] = operation
    if operation == RENDER:
        require("output_action" not in document and not (document.get("keyed_only") is True and "frames" in document),
                "INVALID_REQUEST", "Choose either native keyed frames or an explicit frame list")
        result["format"] = document.get("format", "EXR" if document.get("saved_elements") or "elements" in document else "PNG")
        result["prefix"] = document.get("prefix", "line")
        require(result["format"] in ("PNG", "EXR") and isinstance(result["prefix"], str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", result["prefix"]),
                "INVALID_REQUEST", "Use PNG/EXR and a portable filename prefix")
        for field in ("keyed_only", "saved_elements"):
            if field in document:
                require(type(document[field]) is bool, "INVALID_REQUEST", "Expected Boolean " + field)
                result[field] = document[field]
        require(not (result.get("saved_elements") and "elements" in result)
                and (result["format"] == "EXR" or not (result.get("saved_elements") or "elements" in result)),
                "INVALID_REQUEST", "Multilayer output requires EXR and exactly one element source")
        return result
    require(not {"frames", "elements"} & document.keys(), "INVALID_REQUEST", "Frames and elements belong to rendering")
    if operation == READ:
        return result
    require(not (extras - {"output_action"}) & document.keys(), "INVALID_REQUEST", "Use output_action for an edit")
    action = document.get("output_action")
    from_saved = isinstance(action, dict) and "saved_output" in action
    source_fields = {"saved_output"} if from_saved else {"manifest", "frame"}
    fields = {"SET_SCOPE": {"kind", "overrides"}, "REMOVE_SCOPE": {"kind"}, "PREFERENCES": {"values"},
              "ADD_OUTPUT": source_fields | {"pass_name"}}
    require(type(action) is dict and isinstance(action.get("action"), str) and action["action"] in fields
            and action.keys() == fields[action["action"]] | {"action"}, "INVALID_REQUEST", "Unexpected file output action fields")
    action = dict(action)
    mode = action["action"]
    if mode in ("SET_SCOPE", "REMOVE_SCOPE"):
        require(action["kind"] in ("SCENE", "LAYER"), "INVALID_REQUEST", "Scope kind must be SCENE or LAYER")
        if mode == "SET_SCOPE":
            require(type(action["overrides"]) is dict, "INVALID_REQUEST", "overrides must be a native object")
    elif mode == "PREFERENCES":
        values = action["values"]
        require(type(values) is dict and values and values.keys() <= {"worker_timeout", "abort_on_error", "keyed_only"},
                "INVALID_REQUEST", "Expected supported file-panel worker preferences")
        for key, val in values.items():
            valid = (type(val) in (int, float) and .1 <= val <= 86400) if key == "worker_timeout" else type(val) is bool
            require(valid, "INVALID_REQUEST", "Invalid file-panel " + key)
    else:
        require(contract.name(action["pass_name"], 128), "INVALID_REQUEST", "Choose the exact native pass")
        if from_saved:
            action["saved_output"] = media_ref(action["saved_output"])
        else:
            action["manifest"] = file_ref(action["manifest"])
            require(type(action["frame"]) is int, "INVALID_REQUEST", "Choose the exact frame")
    result["output_action"] = action
    return result


def guard_inputs(request, guards):
    action = request.get("output_action", {})
    if action.get("action") != "ADD_OUTPUT":
        return
    if "saved_output" in action:
        media = action["saved_output"]
        media["file"] = str(guard_file(media["file"], guards, media["sha256"]))
        return
    reference = action["manifest"]
    manifest = read_document(guard_file(reference["file"], guards, reference["sha256"]))
    require(type(manifest) is dict and manifest.get("schema") == MANIFEST and manifest.get("edition") == request["edition"]
            and isinstance(manifest.get("files"), list), "INVALID_REQUEST", "Expected this edition's scoped output manifest")
    matches = [row for row in manifest["files"] if isinstance(row, dict) and row.get("frame") == action["frame"]]
    require(len(matches) == 1, "NOT_FOUND", "The selected frame is absent from the scoped output manifest")
    media = matches[0]
    require(isinstance(media.get("file"), str) and Path(media["file"]).suffix.lower() in (".png", ".exr")
            and isinstance(media.get("sha256"), str) and contract.SHA256.fullmatch(media["sha256"])
            and isinstance(media.get("passes"), list) and len(media["passes"]) <= 128, "INVALID_REQUEST", "Invalid line output frame")
    for row in media["passes"]:
        require(type(row) is dict and set(row) == {"id", "pass_name", "label"}
                and all(isinstance(value, str) for value in row.values()), "INVALID_REQUEST", "Invalid native pass mapping")
    require(action["pass_name"] in {"Combined"} | {row["pass_name"] for row in media["passes"]},
            "NOT_FOUND", "Requested native pass is absent from the manifest")
    path = guard_file(media["file"], guards, media["sha256"])
    action["media"] = {**media, "file": str(path)}


def compositor_tree(scene):
    if not hasattr(scene, "compositor_effects"):
        return scene.compositing_node_group
    from toon_workflow.toon_viewport_preview.compositor_scope import active_binding

    return active_binding(scene)[1]


def captured_tree(scene):
    from inspection import node_content

    tree = compositor_tree(scene)
    if tree is None:
        return None
    images = {}
    for node in tree.nodes:
        if node.get("toon_viewport_export_sha256") and node.bl_idname == "CompositorNodeImage":
            im = node.image
            if im and im.type == "MULTILAYER" and im.packed_file:
                # Blender initializes packed EXR layer/view enums lazily after
                # a cold open. Touch the native image info before reading RNA.
                _ = im.size[:]
                _ = len(im.pixels)
            images[node.name] = {"image": im.name if im else None, "alpha": im.alpha_mode if im else None,
                                 "color": im.colorspace_settings.name if im else None,
                                 "sha256": hashlib.sha256(bytes(im.packed_file.data)).hexdigest()
                                 if im and im.packed_file else None}
    return {"name": tree.name, "nodes": node_content(tree), "packed_line_images": images}


def state(bpy, context, scope):
    scene = context.scene
    panel = scene.toon_author_files
    return {"scopes": scope.read(scene), "active_scope": scope.resolved(scene, context.view_layer.name),
            "preferences": {key: getattr(panel, key) for key in ("worker_timeout", "abort_on_error", "keyed_only")},
            "compositor": captured_tree(scene)}


def saved_output(bpy, scene):
    """Read the native export menu without opening an external file during inspection."""
    settings = scene.toon_viewport_preview
    filename = settings.last_output_path
    sha = settings.last_output_sha256
    elements = settings.last_output_elements
    if not any((filename, sha, elements)):
        return None, None
    if filename.startswith("//"):
        filename = bpy.path.abspath(filename, library=scene.library)
    try:
        ref = media_ref({"file": os.path.normpath(filename), "sha256": sha})
        rows = json.loads(elements) if elements else []
        require(isinstance(rows, list) and len(rows) <= 128, "INVALID_REQUEST", "Invalid saved line pass list")
        ids = set()
        for row in rows:
            require(isinstance(row, dict) and row.keys() == {"id", "pass_name", "label"}
                    and isinstance(row["id"], str) and re.fullmatch(r"[A-Za-z0-9_]{1,32}", row["id"])
                    and row["id"] not in ids and row["pass_name"] == "PCL_E_" + row["id"]
                    and isinstance(row["label"], str) and len(row["label"]) <= 256,
                    "INVALID_REQUEST", "Invalid or duplicate element in the saved line pass list")
            ids.add(row["id"])
        require(not rows or Path(filename).suffix.lower() == ".exr", "INVALID_REQUEST",
                "Saved line element passes require multilayer EXR")
        return {**ref, "passes": rows}, None
    except (Failure, ValueError, RecursionError) as error:
        return None, str(error)


def add_output(bpy, context, action):
    scene = context.scene
    original = compositor_tree(scene)
    require(original is not None, "NOT_FOUND", "Saved scene has no active compositor tree")
    nodes = set(original.nodes.keys())
    if "saved_output" in action:
        media, issue = saved_output(bpy, scene)
        require(not issue, "CONFLICT", issue)
        require(media is not None, "NOT_FOUND", "The scene has no saved line output")
        requested = action["saved_output"]
        require(Path(media["file"]) == Path(requested["file"]) and media["sha256"] == requested["sha256"],
                "CONFLICT", "The requested line output does not match the selected scene's saved export")
        require(action["pass_name"] in {"Combined"} | {row["pass_name"] for row in media["passes"]},
                "NOT_FOUND", "The selected pass is absent from the scene's saved line export")
    else:
        media = action["media"]
    settings = scene.toon_viewport_preview
    patch = {"last_output_path": media["file"], "last_output_sha256": media["sha256"],
             "last_output_elements": json.dumps(media["passes"])}
    before = {key: getattr(settings, key) for key in patch}
    try:
        for key, val in patch.items():
            setattr(settings, key, val)
        require(bpy.ops.toon_viewport.add_output_node(pass_name=action["pass_name"]) == {"FINISHED"},
                "VALIDATION_FAILED", "Native line output attachment failed")
    finally:
        for key, val in before.items():
            setattr(settings, key, val)
    tree = compositor_tree(scene)
    added = [node for node in tree.nodes if node.name not in nodes]
    images = [node for node in added if node.bl_idname == "CompositorNodeImage"]
    require(len(images) == 1 and images[0].get("toon_viewport_export_sha256") == media["sha256"],
            "VALIDATION_FAILED", "Native operator did not add exactly one packed output")
    image = images[0]
    if media["passes"]:
        selected = [node for node in added if node.get("toon_element_pass") == action["pass_name"]]
        require(len(selected) == 1 and len(selected[0].inputs[0].links) == 1
                and selected[0].inputs[0].links[0].from_node == image
                and selected[0].inputs[0].links[0].from_socket.identifier == action["pass_name"],
                "VALIDATION_FAILED", "Native output pass was not connected to its reroute")
    elif action["pass_name"] != "Combined":
        raise Failure("INVALID_REQUEST", "Flat output exposes only Combined")
    return {"added_nodes": [node.name for node in added], "source": media, "pass_name": action["pass_name"]}


def edit_work(bpy, context, request, author, *, save_candidate, author_sha, read_bindings, dependencies):
    from toon_workflow.toon_viewport_preview import output_scope as scope

    scene = context.scene
    before = state(bpy, context, scope)
    binding_rows = read_bindings()
    paths = dependencies()
    response = {"operation": request["operation"], "product": request["product"], "context": request["context"],
                "source": {"file": request["file"], "sha256": request["expected_sha256"], "unchanged": True},
                "runtime": {"blender": bpy.app.version_string, "build": bpy.app.build_hash.decode(), "pid": os.getpid()}}
    scene_sha = fingerprint([binding_rows, paths])
    if request["operation"] == READ:
        state_sha = fingerprint(before)
        require(state_sha == request.get("verify_state_sha256", state_sha)
                and scene_sha == request.get("verify_scene_sha256", scene_sha),
                "VALIDATION_FAILED", "Independent reopen differs from the authored output settings or packed compositor")
        saved, issue = saved_output(bpy, scene)
        return {**response, "file_output": before, "saved_output": saved, "saved_output_issue": issue,
                "author_sha256": author, "state_sha256": state_sha,
                "scene_sha256": scene_sha}

    action = request["output_action"]
    kind = action["action"]
    layer = context.view_layer.name if action.get("kind") == "LAYER" else None
    event = {"action": kind}
    try:
        if kind == "SET_SCOPE":
            scope.set_scope(scene, action["overrides"], view_layer=layer)
        elif kind == "REMOVE_SCOPE":
            scope.remove_scope(scene, view_layer=layer)
        elif kind == "PREFERENCES":
            for key, value in action["values"].items():
                setattr(scene.toon_author_files, key, value)
        else:
            event.update(add_output(bpy, context, action))
        after = state(bpy, context, scope)
    except (KeyError, ValueError, TypeError, RuntimeError) as error:
        raise Failure("CONFLICT", str(error)) from error
    require(after != before and read_bindings() == binding_rows and author_sha(scene) == author,
            "VALIDATION_FAILED", "File-panel action changed line authors or material bindings, or made no native change")
    if kind != "ADD_OUTPUT":
        require(paths == dependencies() and before["compositor"] == after["compositor"],
                "VALIDATION_FAILED", "Output settings changed prior compositor state or dependency paths")
    else:
        new = set(event["added_nodes"])
        original = before["compositor"]["nodes"]
        edited = after["compositor"]["nodes"]
        require(edited["interface"] == original["interface"]
                and [row for row in edited["nodes"] if row["name"] not in new] == original["nodes"]
                and [row for row in edited["links"] if row["from_node"] not in new and row["to_node"] not in new]
                == original["links"], "VALIDATION_FAILED", "Output attachment changed the original compositor")
    if kind in ("SET_SCOPE", "REMOVE_SCOPE"):
        old_layers = before["scopes"]["layers"]
        new_layers = after["scopes"]["layers"]
        require({k: v for k, v in old_layers.items() if k != layer} == {k: v for k, v in new_layers.items() if k != layer}
                and (layer is None or before["scopes"]["scene"] == after["scopes"]["scene"]),
                "VALIDATION_FAILED", "Scope edit changed another layer")
    retained = save_candidate(bpy, request)
    return {**response, "event": event, "file_output": after, "author_before_sha256": author,
            "author_after_sha256": author, "state_sha256": fingerprint(after), "scene_sha256": fingerprint([binding_rows, dependencies()]),
            "candidate_sha256": digest(request["stage"]), "retained_orphans": retained,
            "protected": {"status": "passed", "line_author_and_slots": True}}


def render(bpy, context, request, job, author):
    from toon_workflow.toon_viewport_preview import addon, output_scope, render_output, runtime_preferences, sequence_output

    scene = context.scene
    folder = job / "frames"
    folder.mkdir()
    suffix = "." + request["format"].lower()
    target = folder / (request["prefix"] + suffix)
    panel = scene.toon_author_files
    keyed = request.get("keyed_only", panel.keyed_only)
    frames = request.get("frames")
    require(not (keyed and frames is not None), "INVALID_REQUEST", "Disable saved keyed_only to provide an explicit frame list")
    if keyed:
        require(len(output_scope.keyed_frames(scene)) <= 96, "INVALID_REQUEST", "Scoped output supports up to 96 frames")
    elements = addon.element_configuration(scene.toon_viewport_preview) if request.get("saved_elements") else request.get("elements")
    require(not request.get("saved_elements") or elements, "NOT_FOUND", "The scene has no saved render elements")
    owner = sequence_output if keyed or frames is not None else render_output
    preference = runtime_preferences.configuration(scene)
    unchanged = state(bpy, context, output_scope)
    try:
        submitted = output_scope.request(target, context, elements=elements, frames=frames, keyed_only=keyed, extension=suffix)
    except (ValueError, TypeError, RuntimeError, OSError) as error:
        raise Failure("WORKER_FAILED", "Native setup raised (abort_on_error=true): " + str(error)) from error
    require(submitted.get("id"), "WORKER_FAILED", "Native SETUP_FAILED (abort_on_error=false): " + str(submitted.get("error")))
    token = submitted["id"]
    current = None
    try:
        while True:
            owner.poll()
            current = next((row for row in owner.jobs() if row["id"] == token), None)
            require(current is not None, "WORKER_FAILED", "Scoped output job disappeared")
            if current["status"] != "QUEUED":
                break
            runtime_preferences._tick()
            time.sleep(.1)
    finally:
        if token in runtime_preferences._watched:
            runtime_preferences._watched.pop(token)
            if not current or current["status"] == "QUEUED":
                owner.cancel(token)
    require(current["status"] == "COMPLETE", "TIMEOUT" if current.get("cancel_requested") else "WORKER_FAILED",
            "Scoped output " + current["status"] + " at worker_timeout=" + str(preference["worker_timeout"])
            + "; native receipt " + current["receipt_path"] + "; " + current.get("message", ""))
    require(state(bpy, context, output_scope) == unchanged and digest(request["file"]) == request["expected_sha256"],
            "VALIDATION_FAILED", "Scoped rendering changed the saved author, settings or source")
    rows = current["published_frames"] if owner is sequence_output else [
        {"frame": scene.frame_current, "path": str(target), "result": current["result"]}]
    files = []
    for row in rows:
        result = row["result"]
        path = Path(row["path"])
        require(path.parent == folder and path.suffix == suffix and digest(path) == result["sha256"]
                and result["evaluation"] == "RENDER", "VALIDATION_FAILED", "Native media file identity changed")
        files.append({"file": str(path), "sha256": result["sha256"], "frame": row["frame"], "format": request["format"],
                      "bytes": path.stat().st_size, "size": [result["width"], result["height"]],
                      "alpha_mode": result.get("alpha_mode"), "passes": result.get("element_passes", [])})
    return {"operation": RENDER, "edition": request["edition"], "source": {"file": request["file"],
            "sha256": request["expected_sha256"], "unchanged": True}, "context": request["context"], "author_sha256": author,
            "runtime": {"blender": bpy.app.version_string, "build": bpy.app.build_hash.decode(), "pid": os.getpid()},
            "files": files, "native_job": {"id": token, "receipt": current["receipt_path"],
            "snapshot_sha256": current["snapshot_sha256"]}, "keyed_only": keyed, "scope": unchanged["active_scope"],
            "preferences": unchanged["preferences"]}


def reserve_output(output, guards):
    require(output is not None and output.is_absolute() and output.name not in ("", ".", ".."), "INVALID_REQUEST",
            "Scoped rendering requires a new absolute --output directory")
    parent = canonical(output.parent)
    for folder in reversed((parent, *parent.parents)):
        if folder != Path(folder.anchor):
            guards.enter_context(FileGuard(folder, directory=True))
    destination = parent / output.name
    guards.enter_context(PathLocks([destination]))
    require(not os.path.lexists(destination), "CONFLICT", "Output directory already exists")
    return destination


def publish_output(result, job, destination, checkpoint, guards):
    stage = Path(tempfile.mkdtemp(prefix=".toon-scoped-", dir=destination.parent))
    guards.callback(lambda: stage.rmdir() if stage.is_dir() else None)
    assets = set()
    for row in result["files"]:
        source = Path(row["file"])
        require(source.parent == job / "frames" and source.name not in assets, "VALIDATION_FAILED", "Unexpected scoped output path")
        assets.add(source.name)
        work = stage / source.name
        guards.callback(work.unlink, missing_ok=True)
        with FileGuard(source) as lock:
            require(lock.sha256(checkpoint) == row["sha256"], "CONFLICT", "Native output changed before publication")
            shutil.copyfile(source, work)
        require(digest(work) == row["sha256"], "VALIDATION_FAILED", "Scoped output copy differs")
        row["file"] = str(destination / source.name)
    manifest = stage / "OUTPUT.json"
    guards.callback(manifest.unlink, missing_ok=True)
    atomic_json(manifest, {"schema": MANIFEST, **result})
    manifest_sha = digest(manifest)
    checkpoint()
    require(not os.path.lexists(destination), "CONFLICT", "Output directory appeared")
    stage.rename(destination)
    return {"file": str(destination / manifest.name), "sha256": manifest_sha}
