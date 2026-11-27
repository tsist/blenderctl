# SPDX-License-Identifier: GPL-3.0-or-later
"""Saved-scene FBX inspection, Profile export, and body retargeting."""
import json
import math
from pathlib import Path
import re

from handoff_io import fingerprint, guard_file, require


OPERATIONS = {"retarget-fbx-inspect", "retarget-profile-build", "retarget-body"}
READ_ONLY = OPERATIONS - {"retarget-body"}
_SOURCE_REQUIRED = {"source_file", "source_sha256"}
_SOURCE_OPTIONAL = {"take", "slot"}
_TRANSFORMS = {"location", "rotation_quaternion", "rotation_euler", "rotation_axis_angle", "scale"}
_FBX_DATA = ("objects", "collections", "armatures", "actions", "meshes", "materials", "images",
             "textures", "node_groups", "cameras", "lights")


def _text(value, limit=256):
    return isinstance(value, str) and 0 < len(value) <= limit and "\0" not in value and bool(value.strip())


def _file(value, sha, suffix):
    require(_text(value, 4096) and Path(value).is_absolute() and Path(value).suffix.lower() == suffix,
            "INVALID_REQUEST", f"Expected an absolute {suffix} input path")
    require(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha) is not None,
            "INVALID_REQUEST", f"Expected lowercase SHA-256 for {suffix}")


def _mapping(value, field):
    require(isinstance(value, dict) and 0 < len(value) <= 128
            and all(_text(key, 64) and _text(bone) for key, bone in value.items()),
            "INVALID_REQUEST", f"{field} must map semantic names to exact bone names")


def _frame(value):
    return type(value) in (int, float) and math.isfinite(value) and abs(value) <= 1048574


def validate(operation, params):
    require(operation in OPERATIONS and isinstance(params, dict), "INVALID_REQUEST", "Invalid retarget parameters")
    required, optional = set(_SOURCE_REQUIRED), set(_SOURCE_OPTIONAL)
    if operation == "retarget-profile-build":
        optional.update(("source_mapping", "profile_name"))
    if operation == "retarget-body":
        required.update(("rig", "target_kind"))
        optional.update(("profile", "profile_file", "profile_sha256", "motion", "root_rotation",
                         "target_mapping", "acknowledged", "start", "end", "output_start", "action_name"))
    require(required <= params.keys() <= required | optional, "INVALID_REQUEST", "Unknown or missing retarget parameters")
    _file(params["source_file"], params["source_sha256"], ".fbx")
    for key in ("take", "slot", "profile_name", "rig", "action_name"):
        if key in params:
            require(_text(params[key]), "INVALID_REQUEST", f"Expected a nonempty name for {key}")
    for key in ("source_mapping", "target_mapping"):
        if key in params:
            _mapping(params[key], key)
    if operation != "retarget-body":
        return
    if "profile" in params:
        require(isinstance(params["profile"], dict) and not ({"profile_file", "profile_sha256"} & params.keys()),
                "INVALID_REQUEST", "Supply one inline or one SHA-bound file Profile")
    else:
        _file(params.get("profile_file"), params.get("profile_sha256"), ".json")
    require(params.get("motion", "PRESERVE") in ("PRESERVE", "IN_PLACE"), "INVALID_REQUEST", "Unknown motion mode")
    require(("start" in params) == ("end" in params), "INVALID_REQUEST", "Provide start and end together")
    if "start" in params:
        require(_frame(params["start"]) and _frame(params["end"]) and params["start"] < params["end"],
                "INVALID_REQUEST", "Expected a strictly increasing source frame range")
    begin = params.get("output_start", 1)
    require(type(begin) is int and 1 <= begin <= 1048574, "INVALID_REQUEST", "Invalid output_start")
    if params["target_kind"] == "C94":
        require(params.get("root_rotation", "HEADING") in ("HEADING", "FULL")
                and not ({"target_mapping", "acknowledged"} & params.keys()),
                "INVALID_REQUEST", "C94 accepts a native root-rotation mode")
    else:
        require(params["target_kind"] == "GENERIC_FK" and isinstance(params.get("target_mapping"), dict)
                and params.get("acknowledged") is True and "root_rotation" not in params,
                "INVALID_REQUEST", "GENERIC_FK requires an explicit target map and acknowledged=true")


def guard_inputs(operation, params, guards):
    validate(operation, params)
    normalized = dict(params)
    for file_key, sha_key in (("source_file", "source_sha256"), ("profile_file", "profile_sha256")):
        if file_key in params:
            normalized[file_key] = str(guard_file(params[file_key], guards, params[sha_key]))
    return normalized


def _data_ids(bpy):
    return {item for kind in _FBX_DATA for item in getattr(bpy.data, kind)}


def _import(bpy, params, source_api):
    old_actions = set(bpy.data.actions)
    result = source_api.import_fbx(params["source_file"], provenance={"sha256": params["source_sha256"]})
    rig, clip = result["rig"], result["clip"]
    candidates = []
    for action in bpy.data.actions:
        if action in old_actions:
            continue
        timing = json.loads(action.get(source_api.TIME_KEY, "{}"))
        for slot in action.slots:
            if slot.identifier in timing.get("slots", ()):
                candidates.append((action, slot, timing["fps"]))
    if "take" in params or "slot" in params:
        take, slot_id = params.get("take"), params.get("slot")
        matches = [(a, s) for a, s, _ in candidates if (take is None or a.name == take)
                   and (slot_id is None or s.identifier == slot_id)]
        if len(matches) != 1:
            available = sorted((a.name, s.identifier) for a, s, _ in candidates)
            require(False, "NOT_FOUND",
                    f"Take/Slot does not identify one exact imported native Action. "
                    f"Available (Take, Slot), up to 8 of {len(available)}: {available[:8]}")
        rig.animation_data.action, rig.animation_data.action_slot = matches[0]
        clip = source_api.active_clip(rig)
    metadata = {
        "file": params["source_file"], "sha256": params["source_sha256"], "rig": rig.name,
        "bones": len(rig.data.bones), "meshes": sum(obj.type == "MESH" for obj in result["objects"]),
        "selected": {"take": clip["action"].name, "slot": clip["slot"].identifier,
                     "fps": clip["fps"], "frame_range": list(clip["range"])},
        "takes": [{"take": action.name, "slot": slot.identifier, "fps": fps}
                  for action, slot, fps in candidates],
    }
    return result, clip, metadata


def _profile_build(profile, imported, params, proposal):
    source = imported["rig"]
    mapping = params.get("source_mapping", proposal["bones"])
    require(not proposal["missing"] or "source_mapping" in params, "VALIDATION_FAILED",
            "Resolve the missing semantic mapping before building a Profile")
    validation = profile.validate_mapping(source, {"bones": mapping}, include_fingers=False)
    require(validation["ok"], "VALIDATION_FAILED", "; ".join(validation["errors"]))
    options = {"import_record": imported["record"], "name": params.get("profile_name"), "include_fingers": False}
    if proposal["preset"] == "mixamo_names_candidate" and mapping == proposal["bones"]:
        document = profile.from_mixamo(source, **options, translation_origin=profile.TRANSLATION_ORIGIN_CLIP_START,
                                       motion_scale_basis=profile.MOTION_SCALE_LEG_LENGTH)
    else:
        document = profile.from_fbx(source, mapping, **options)
    # Keep the reference SHA and physical identity. Validation should never
    # follow an embedded reference path in a later project or job.
    document["source"]["file"] = None
    return json.loads(profile.profile_to_json(document))


def _load_profile(profile, params):
    if "profile" in params:
        document = profile.profile_from_json(json.dumps(params["profile"], ensure_ascii=False, allow_nan=False))
    else:
        document = profile.load_profile(params["profile_file"])
    original_sha = document["profile_sha256"]
    if document["source"].get("file"):
        document["source"]["file"] = None
        document = json.loads(profile.profile_to_json(document))
    return document, original_sha


def _target_state(bpy, target, cal, correction, rt, excluded=None):
    binding = rt.binding(target)
    tracks = target.animation_data.nla_tracks if target.animation_data else ()
    return {
        "rest": cal.bone_rest_signature(target),
        "parent": target.parent.name if target.parent else None,
        "world": [list(row) for row in target.matrix_world],
        "action": binding["action"].name if binding["action"] else None,
        "slot": binding["slot"].identifier if binding["slot"] else None,
        "nla": [(track.name, track.mute, [(strip.action.name if strip.action else None, strip.frame_start,
                strip.frame_end, strip.action_slot.identifier if strip.action_slot else None) for strip in track.strips])
                for track in tracks],
        "actions": {action.name: correction.action_fingerprint(action) for action in bpy.data.actions if action != excluded},
        "objects": sorted(obj.name for obj in bpy.data.objects),
    }


def _sample(bpy, target, controls, frame_range):
    scene = bpy.context.scene
    first, last = frame_range
    frames = (first, (first + last) * .5, last)
    result = []
    for frame in frames:
        integer = math.floor(frame)
        scene.frame_set(integer, subframe=frame - integer)
        view = target.evaluated_get(bpy.context.evaluated_depsgraph_get())
        result.append({"frame": frame, "points": {name: list(view.matrix_world @ view.pose.bones[name].head)
                                                  for name in controls}})
    return result


def _preview(bpy, target, action, slot, controls, rt, *, managed):
    if managed:
        rt.begin_preview(target, action, slot=slot)
        try:
            return _sample(bpy, target, controls, action.frame_range)
        finally:
            rt.stop_preview(target)
    scene = bpy.context.scene
    old = scene.frame_current, scene.frame_subframe
    binding = rt.binding(target)
    ad = target.animation_data_create()
    ad.action, ad.action_slot = action, slot
    ad.use_nla, ad.action_blend_type, ad.action_influence = False, "REPLACE", 1.0
    try:
        return _sample(bpy, target, controls, action.frame_range)
    finally:
        rt.restore_binding(target, binding)
        scene.frame_set(old[0], subframe=old[1])


def _bag(action, slot):
    require(len(action.layers) == 1 and len(action.layers[0].strips) == 1
            and action.layers[0].strips[0].type == "KEYFRAME", "VALIDATION_FAILED",
            "Expected a single-layer native FK Action")
    bag = action.layers[0].strips[0].channelbag(slot)
    require(bag is not None, "VALIDATION_FAILED", "Native FK slot has no curves")
    return bag


def _write_curves(bpy, target, result, name, fixed_action, fixed_slot):
    action = bpy.data.actions.new(name)
    slot = action.slots.new(id_type="OBJECT", name=fixed_slot.name_display if fixed_slot else target.name)
    if fixed_slot:
        require(slot.identifier == fixed_slot.identifier, "CONFLICT", "New FK Action changed the native Slot identity")
    strip = action.layers.new("Native body retarget").strips.new(type="KEYFRAME")
    bag = strip.channelbag(slot, ensure=True)
    for curve in _bag(result["action"], result["slot"]).fcurves:
        exported = bag.fcurves.new(curve.data_path, index=curve.array_index)
        for key in curve.keyframe_points:
            exported.keyframe_points.insert(*key.co, options={"FAST"}).interpolation = key.interpolation
        exported.update()
    if fixed_action:
        first, last = result["action"].frame_range
        known_paths = {bone.path_from_id(prop) for bone in target.pose.bones for prop in _TRANSFORMS}
        for curve in _bag(fixed_action, fixed_slot).fcurves:
            if bag.fcurves.find(curve.data_path, index=curve.array_index) is not None:
                continue
            keys = list(curve.keyframe_points)
            require(curve.data_path in known_paths and keys and not curve.modifiers, "VALIDATION_FAILED",
                    f"Unsupported native FK template curve: {curve.data_path}")
            values = [key.co.y for key in keys]
            for key, after in zip(keys, keys[1:]):
                if key.interpolation == "BEZIER":
                    values.extend((key.handle_right.y, after.handle_left.y))
            require(max(values) - min(values) < 1e-7, "VALIDATION_FAILED",
                    f"Unmapped template curve is animated: {curve.data_path}")
            exported = bag.fcurves.new(curve.data_path, index=curve.array_index)
            for frame in (first, last):
                exported.keyframe_points.insert(frame, values[0], options={"FAST"}).interpolation = "LINEAR"
            exported.update()
    action.use_fake_user = True
    return action, slot


def _fk_convert(bpy, target, source, document, params, options, fk, profile, rt):
    validation = profile.validate_profile(document, source, check_files=False)
    require(validation["applies"], "VALIDATION_FAILED", "; ".join(validation["errors"]))
    found = [item for item in fk.conflicts(target) if item["kind"] != "object_parent"]
    require(not found, "VALIDATION_FAILED", f"Unsupported FK control graph: {found[:8]}")
    parent = target.parent
    while parent:
        require(not parent.animation_data and not parent.constraints and parent.parent_type == "OBJECT",
                "VALIDATION_FAILED", "Native FK requires a static object-parent chain")
        parent = parent.parent
    require(not target.parent or target.parent_type == "OBJECT", "VALIDATION_FAILED", "Native FK excludes bone parenting")
    bound = rt.binding(target)
    require(bound["action"] is None or bound["slot"] is not None, "VALIDATION_FAILED", "Select the original native FK slot")
    require(not target.animation_data or not target.animation_data.use_nla
            or all(track.mute for track in target.animation_data.nla_tracks), "VALIDATION_FAILED",
            "Native FK requires a single base Action without active NLA composition")
    clone = fk.isolate_target(target, acknowledged=True)["rig"]
    for bone in clone.pose.bones:
        original = target.pose.bones[bone.name]
        bone.lock_rotation = original.lock_rotation
        bone.lock_rotation_w = original.lock_rotation_w
        bone.lock_location = original.lock_location
    mapping = profile.mapped_mapping(document)
    result = fk.convert(source, clone, source_mapping=mapping, target_mapping=params["target_mapping"],
                        acknowledged=True, **{**options, "action_name": "_Phase3 FK solver"})
    controls = result["report"]["controls"]
    samples = _preview(bpy, clone, result["action"], result["slot"], controls, rt, managed=True)
    action, slot = _write_curves(bpy, target, result, options["action_name"], bound["action"], bound["slot"])
    report = {key: result["report"][key] for key in ("source_fps", "source_range", "target_fps", "output_range",
                                                   "motion", "motion_scale", "motion_scale_basis")}
    report.update(root_bone=result["report"]["target_policy"]["root"], parent_retained=target.parent is not None)
    return action, slot, report, samples


def _armature(bpy, params):
    target = bpy.context.view_layer.objects.get(params["rig"])
    require(target is not None and target.type == "ARMATURE" and not (target.library or target.override_library
            or target.data.library or target.data.override_library), "NOT_FOUND", "Expected an exact local target armature")
    return target


def execute(bpy, operation, params, job_dir):
    from rigging_workflow import retarget_action as rt, retarget_calibration as cal, retarget_source as source_api
    from rigging_workflow import retarget_contact_correction as correction, retarget_generic_target as fk
    from rigging_workflow import retarget_profile as profile

    require(bpy.context.mode == "OBJECT", "CONFLICT", "FBX retarget requires saved Object mode")
    before_ids = _data_ids(bpy)
    working = operation == "retarget-body"
    target = _armature(bpy, params) if working else None
    baseline = _target_state(bpy, target, cal, correction, rt) if working else None
    imported, clip, metadata = _import(bpy, params, source_api)
    if operation in READ_ONLY:
        proposal = source_api.propose_fbx_mapping(imported["rig"])
        result = {"source": metadata, "proposal": proposal}
        if operation == "retarget-profile-build":
            result["profile"] = _profile_build(profile, imported, params, proposal)
        return result
    document, profile_sha = _load_profile(profile, params)
    result = {"fbx": metadata}
    name = params.get("action_name", target.name + " body retarget")
    require(name not in bpy.data.actions, "CONFLICT", "The requested candidate Action name already exists")
    options = {key: params[key] for key in ("start", "end", "output_start", "motion") if key in params}
    options["action_name"] = name
    c94 = params["target_kind"] == "C94"
    if c94:
        converted = profile.convert(imported["rig"], target, document, clip=clip,
                                    root_rotation=params.get("root_rotation", "HEADING"), **options)
        action, slot, report = converted["action"], converted["slot"], converted["report"]
        points = _preview(bpy, target, action, slot, report["controls"], rt, managed=True)
    else:
        action, slot, report, points = _fk_convert(bpy, target, imported["rig"], document, params, options, fk, profile, rt)
    require(action.name == name, "VALIDATION_FAILED", "Blender changed the requested candidate name")
    bpy.data.batch_remove(ids=(_data_ids(bpy) - before_ids) - {action})
    require(fingerprint(_target_state(bpy, target, cal, correction, rt, excluded=action)) == fingerprint(baseline),
            "VALIDATION_FAILED", "Original target/Action state changed")
    result.update(
        target=target.name, target_kind=params["target_kind"], profile_sha256=profile_sha, action=action.name,
        slot=slot.identifier, action_sha256=correction.action_fingerprint(action),
        report={key: report[key] for key in ("source_fps", "source_range", "target_fps", "output_range", "motion",
                "motion_scale", "motion_scale_basis", "root_rotation", "root_bone", "parent_retained",
                "profile_calibration_reused", "profile_cache_hit") if key in report},
        original_state_sha256=fingerprint(baseline), samples=points,
    )
    if c94:
        result["profile_with_calibration"] = converted["profile_with_calibration"]
    return result


def verify(bpy, operation, params, result):
    from rigging_workflow import retarget_action as rt, retarget_calibration as cal
    from rigging_workflow import retarget_contact_correction as correction

    require(operation == "retarget-body", "INVALID_REQUEST", "Read-only retarget operations do not write a scene")
    target = _armature(bpy, params)
    action = bpy.data.actions.get(result["action"])
    require(action is not None and action.use_fake_user and result["slot"] in action.slots
            and correction.action_fingerprint(action) == result["action_sha256"], "VALIDATION_FAILED",
            "Cold reopen changed or lost the native Action/Slot")
    require(fingerprint(_target_state(bpy, target, cal, correction, rt, excluded=action)) == result["original_state_sha256"],
            "VALIDATION_FAILED", "Cold reopen changed the original objects, rest or animation binding")
    points = _preview(bpy, target, action, action.slots[result["slot"]], result["samples"][0]["points"],
                      rt, managed=params["target_kind"] == "C94")
    error = max(math.dist(actual["points"][name], expected["points"][name])
                for actual, expected in zip(points, result["samples"], strict=True) for name in expected["points"])
    require(error <= 2e-6, "VALIDATION_FAILED", f"Cold native pose diverged by {error:.6g} m")
    return {"action": action.name, "slot": result["slot"], "max_pose_error_m": error, "binding_unchanged": True}
