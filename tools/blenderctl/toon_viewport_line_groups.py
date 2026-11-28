"""Replay Toon viewport parameter tracks through the native Scene Action API."""

import os

from material_draft_handoff import fingerprint, require
from protocol import Failure, digest
import toon_viewport_lines_contract as contract


READ = "inspect-parameter-tracks"
WRITE = "manage-parameter-tracks"
SCOPES = ("BRUSH", "LINE", "MERGE", "LF", "MASK", "SELF")
FIELDS = {"scope", "owner", "path", "component"}


def checked_target(value):
    require(isinstance(value, dict) and FIELDS - {"component"} <= value.keys() <= FIELDS,
            "INVALID_REQUEST", "Parameter target needs scope, owner, path and optional component")
    require(value["scope"] in SCOPES and contract.name(value["owner"], 256),
            "INVALID_REQUEST", "Invalid parameter scope or owner")
    path = value["path"]
    require(isinstance(path, list) and 1 <= len(path) <= 5 and all(contract.name(part, 128) for part in path),
            "INVALID_REQUEST", "Invalid parameter field path")
    component = value.get("component", -1)
    require(type(component) is int and -1 <= component <= 2, "INVALID_REQUEST", "Invalid parameter component")
    return {**value, "component": component}


def checked_frame(value):
    require(type(value) in (int, float) and -1048574 <= value <= 1048574,
            "INVALID_REQUEST", "Parameter frame is outside Blender's supported frame range")
    return value


def checked_action(value):
    require(isinstance(value, dict) and {"action", "target"} <= value.keys(),
            "INVALID_REQUEST", "Parameter edit needs action and target")
    action = value["action"]
    fields = ("action", "target")
    if action == "KEY":
        fields += ("value",)
        require(type(value.get("value")) is bool or type(value.get("value")) in (int, float)
                and abs(value["value"]) <= 1e12, "INVALID_REQUEST", "Invalid parameter key value")
    elif action == "TOGGLE":
        fields += ("enabled",)
        require(type(value.get("enabled")) is bool, "INVALID_REQUEST", "Parameter enabled must be boolean")
    else:
        require(action == "REMOVE", "INVALID_REQUEST", "Unknown parameter action")
    require(set(fields) <= value.keys() <= {*fields, "frame"}, "INVALID_REQUEST", "Unexpected parameter action fields")
    result = {**value, "target": checked_target(value["target"])}
    if "frame" in value:
        result["frame"] = checked_frame(value["frame"])
    return result


def normalize_request(document, editions):
    operation = document["operation"]
    additional = {"parameter_action", "parameter_scope", "owner", "sample", "expected_animation_sha256"}
    payload = {k: v for k, v in document.items() if k not in additional}
    require(not {"scope", "frames", "elements", "patch"} & payload.keys(), "INVALID_REQUEST",
            "Parameter tracks use saved PRESET identities")
    if operation == READ:
        require(not {"parameter_action", "expected_animation_sha256"} & document.keys(), "INVALID_REQUEST",
                "Read accepts no parameter edit or animation guard")
        payload.update(operation="inspect", scope={"groups": ["parameter-tracks"]})
    else:
        require(not {"parameter_scope", "owner", "sample"} & document.keys(), "INVALID_REQUEST",
                "Parameter edit accepts only one explicit parameter_action")
        payload["operation"] = "render-camera"
    request = contract.normalize_request(payload, editions)
    request.pop("scope", None)
    request["operation"] = operation
    if operation == WRITE:
        request["parameter_action"] = checked_action(document.get("parameter_action"))
        expected = document.get("expected_animation_sha256")
        require(isinstance(expected, str) and contract.SHA256.fullmatch(expected), "INVALID_REQUEST",
                "A parameter edit requires expected_animation_sha256 from inspection")
        request["expected_animation_sha256"] = expected
    else:
        if "parameter_scope" in document:
            require(document["parameter_scope"] in SCOPES, "INVALID_REQUEST", "Unknown parameter scope")
            request["parameter_scope"] = document["parameter_scope"]
        if "owner" in document:
            require("parameter_scope" in request and contract.name(document["owner"], 256), "INVALID_REQUEST",
                    "Owner filter requires a named parameter scope")
            request["owner"] = document["owner"]
        if "sample" in document:
            sample = document["sample"]
            require(isinstance(sample, dict) and {"target"} <= sample.keys() <= {"target", "frame"},
                    "INVALID_REQUEST", "Sample requires an exact target and optional frame")
            request["sample"] = {"target": checked_target(sample["target"])}
            if "frame" in sample:
                request["sample"]["frame"] = checked_frame(sample["frame"])
    return request


def curve_state(curve, key_state):
    return {
        "path": curve.data_path, "index": curve.array_index,
        "group": curve.group.name if curve.group else None,
        "extrapolation": curve.extrapolation, "mute": curve.mute, "lock": curve.lock,
        "hide": curve.hide, "select": curve.select, "color": list(curve.color), "color_mode": curve.color_mode,
        "keys": [key_state(point) for point in curve.keyframe_points],
        "samples": [list(point.co) for point in curve.sampled_points],
        "modifiers": [modifier.type for modifier in curve.modifiers],
    }


def native_state(scene, animation, amount, *, exclude_path=None):
    action, slot, _bag, tracked = animation._graph(scene)
    keys = {token: [amount._key_state(point) for point in curve.keyframe_points] for token, curve in tracked.items()}
    tracks = {
        token: {"target": target, "kind": item.kind, "enabled": item.enabled, "keys": keys.get(token, [])}
        for token, (item, target) in animation.rows(scene).items()
    }
    # When a new track has no curve, the typed value is its manual/static input.
    for token, (item, _target) in animation.rows(scene).items():
        if token not in tracked:
            tracks[token]["value"] = getattr(item, animation.SUFFIX[item.kind])
    channels = {}
    if action is not None:
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    rows = [
                        curve_state(curve, amount._key_state) for curve in bag.fcurves
                        if not (slot is not None and slot.handle == bag.slot_handle and curve.data_path == exclude_path)
                    ]
                    if rows:
                        channels[str(bag.slot_handle)] = sorted(rows, key=lambda row: (row["path"], row["index"]))
    return tracks, channels


def set_frame(scene, frame):
    whole = int(frame // 1)
    scene.frame_set(whole, subframe=frame - whole)


def current_frame(scene):
    return scene.frame_current + scene.frame_subframe


def sample_value(context, animation, spec, frame):
    scene = context.scene
    saved_frame = current_frame(scene)
    try:
        set_frame(scene, frame)
        context.view_layer.update()
        token = spec["token"]
        original_rows = animation.rows(scene)
        track = original_rows.get(token)
        if track is None or not track[0].enabled:
            return {"frame": current_frame(scene), "origin": "STATIC", "value": spec["static"]}
        evaluated = scene.evaluated_get(context.evaluated_depsgraph_get())
        observed = animation.rows(evaluated)[token][0]
        return {"frame": current_frame(scene), "origin": "ACTION",
                "value": getattr(observed, animation.SUFFIX[spec["kind"]])}
    finally:
        set_frame(scene, saved_frame)
        context.view_layer.update()


def protected_state(scene, dependencies, author_sha):
    from toon_workflow.toon_workflow_addon import joint_workflow

    return fingerprint({
        "author": author_sha(scene), "geometry": joint_workflow.non_author_signature(scene),
        "dependencies": dependencies(),
    })


def work(bpy, context, request, wrapped, author, *, dependencies, save_candidate, author_sha):
    from toon_workflow.toon_viewport_preview import amount_animation as amount
    from toon_workflow.toon_viewport_preview import parameter_animation as animation
    from toon_workflow.toon_viewport_preview import parameter_overlay as overlay

    scene = context.scene
    operation = request["operation"]
    document = wrapped["document"]
    animation.validate_registry(scene, document)
    tracks, channels = native_state(scene, animation, amount)
    animation_sha = fingerprint([tracks, channels])
    protected = protected_state(scene, dependencies, author_sha)
    response = {
        "operation": operation, "product": request["product"], "context": request["context"],
        "source": {"file": request["file"], "sha256": request["expected_sha256"], "unchanged": True},
        "runtime": {"blender": bpy.app.version_string, "build": bpy.app.build_hash.decode(), "pid": os.getpid()},
    }
    if operation == READ:
        if "verify_state_sha256" in request:
            require(animation_sha == request["verify_state_sha256"] and protected == request["verify_scene_sha256"],
                    "VALIDATION_FAILED", "Cold reopen changed parameter tracks or protected state")
        report = {**response, "scopes": list(overlay.SCOPES), "tracks": tracks, "animation_sha256": animation_sha,
                  "state_sha256": animation_sha, "scene_sha256": protected, "author_sha256": author}
        if "parameter_scope" in request:
            report["targets"] = list(overlay.targets(document, scope=request["parameter_scope"], owner=request.get("owner")))
        if "sample" in request:
            sample = request["sample"]
            spec = overlay.describe(document, sample["target"])
            report["sample"] = {**spec, **sample_value(context, animation, spec, sample.get("frame", current_frame(scene)))}
        require(digest(request["file"]) == request["expected_sha256"], "CONFLICT", "Source changed during inspection")
        return report

    require(scene.library is None and scene.override_library is None and scene.is_editable, "CONFLICT",
            "Parameter editing requires a local editable Scene")
    require(animation_sha == request["expected_animation_sha256"], "CONFLICT", "Saved animation SHA differs")
    step = request["parameter_action"]
    target = step["target"]
    spec = overlay.describe(document, target)
    token = spec["token"]
    path = animation.key_path(target, spec["kind"])
    frame = step.get("frame", current_frame(scene))
    saved_frame = current_frame(scene)
    excluded = native_state(scene, animation, amount, exclude_path=path)[1]
    try:
        set_frame(scene, frame)
        context.view_layer.update()
        if step["action"] == "KEY":
            animation.set_key(scene, target, step["value"], frame=frame)
        elif step["action"] == "REMOVE":
            animation.remove_key(scene, target, frame=frame)
        else:
            animation.set_enabled(scene, target, step["enabled"])
    except ValueError as error:
        raise Failure("CONFLICT", str(error)) from error
    finally:
        set_frame(scene, saved_frame)
        context.view_layer.update()
    after, channels = native_state(scene, animation, amount)
    retained = native_state(scene, animation, amount, exclude_path=path)[1]
    require(retained == excluded and all(after.get(key) == row for key, row in tracks.items() if key != token),
            "VALIDATION_FAILED", "Parameter edit changed a non-target curve or track")
    require(protected_state(scene, dependencies, author_sha) == protected, "VALIDATION_FAILED",
            "Parameter edit changed the saved static author, scene or dependencies")
    after_sha = fingerprint([after, channels])
    require(after_sha != animation_sha, "CONFLICT", "Parameter action is a no-op")
    entry = after[token]
    command = step["action"]
    if command in ("KEY", "REMOVE"):
        exact = amount._frame(scene, frame)
        old_keys = [key for key in tracks.get(token, {}).get("keys", []) if key["co"][0] != exact]
        new_keys = [key for key in entry["keys"] if key["co"][0] != exact]
        require(old_keys == new_keys, "VALIDATION_FAILED", "A non-target key on the same curve changed")
    else:
        require(entry["keys"] == tracks.get(token, {}).get("keys", []) and entry["enabled"] == step["enabled"],
                "VALIDATION_FAILED", "Track switch altered native keyframes")
    observed = sample_value(context, animation, spec, frame)
    orphans = save_candidate(bpy, request)
    return {
        **response, "event": {"action": command, "target": target, "token": token, "sample": observed},
        "tracks": after, "animation_before_sha256": animation_sha, "animation_sha256": after_sha,
        "author_before_sha256": author, "author_after_sha256": author,
        "state_sha256": after_sha, "scene_sha256": protected, "candidate_sha256": digest(request["stage"]),
        "retained_orphans": orphans,
        "protected": {"status": "passed", "scene_sha256": protected, "other_channel_count": sum(map(len, retained.values()))},
    }
