# SPDX-License-Identifier: GPL-3.0-or-later
"""Background adapters for Phase 2 native pose authoring and contact candidates."""

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import re


OPERATIONS = {"shoulder-pose", "rom-inspect", "three-beats", "fk-heel-contact", "gait-contact"}
READ_ONLY = {"rom-inspect"}

_FIELDS = {
    "shoulder-pose": ({"rig", "action", "slot", "frame", "direction", "elevation"},
                      {"follow", "manual", "advisory", "action_name"}),
    "rom-inspect": ({"rig", "reference_frame", "frame"}, {"strength"}),
    "three-beats": ({"rig", "action_name", "frames", "controls", "poses"}, set()),
    "fk-heel-contact": ({"rig", "mesh", "calibration"}, {"action_name"}),
    "gait-contact": ({"rig", "action_name", "detection_report", "calibration", "issue_id",
                      "frames", "motion_usage"}, {"tangent_release", "solve_parameters"}),
}
_DOCUMENTS = {"fk-heel-contact": ("calibration",), "gait-contact": ("detection_report", "calibration")}
_CHANNEL_SIZE = {"location": 3, "rotation_euler": 3, "rotation_quaternion": 4, "scale": 3}
_SOLVE_PARAMETERS = {"clearance_m", "max_delta_m", "match_tolerance_m", "solve_tolerance_m"}


def _need(condition, message):
    if not condition:
        raise ValueError(message)


def _keys(value, required, optional, label):
    _need(isinstance(value, dict) and required <= value.keys() <= required | optional,
          f"{label} requires {sorted(required)} and only accepts optional {sorted(optional)}")


def _name(value):
    return isinstance(value, str) and bool(value.strip()) and not any(ord(ch) < 32 for ch in value)


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _frame(value):
    return _number(value) and -1048574 <= value <= 1048574


def _bounded(value, lower, upper):
    return _number(value) and lower <= value <= upper


def _validate_pose(params):
    frames, controls, poses = (params[key] for key in ("frames", "controls", "poses"))
    _need(isinstance(frames, list) and len(frames) == 3 and all(map(_frame, frames))
          and frames[0] < frames[1] < frames[2], "frames must contain three increasing frame numbers")
    _need(isinstance(controls, list) and controls and all(map(_name, controls))
          and len(set(controls)) == len(controls), "controls must contain unique original bone names")
    _need(isinstance(poses, list) and len(poses) == 3, "poses must contain exactly three channel lists")
    for pose in poses:
        _need(isinstance(pose, list) and pose, "each pose requires at least one explicit channel")
        channels = set()
        for row in pose:
            _keys(row, {"bone", "property", "index", "value"}, set(), "pose channel")
            bone, prop, index = (row[key] for key in ("bone", "property", "index"))
            _need(_name(bone) and bone in controls and isinstance(prop, str) and prop in _CHANNEL_SIZE
                  and type(index) is int and 0 <= index < _CHANNEL_SIZE[prop] and _number(row["value"]),
                  "pose channel must identify a declared control, native component and finite value")
            _need((bone, prop, index) not in channels, "duplicate component in one beat")
            channels.add((bone, prop, index))


def _validate_gait(params):
    _need(params["motion_usage"] == "GAIT", "gait-contact requires explicit GAIT usage")
    frames = params["frames"]
    _need(isinstance(frames, list) and len(frames) >= 2
          and all(type(frame) is int and _frame(frame) for frame in frames)
          and all(next_frame == frame + 1 for frame, next_frame in zip(frames, frames[1:])),
          "gait frames must be consecutive integers")
    _need(_name(params["issue_id"]), "issue_id must identify one detected issue")
    release = params.get("tangent_release")
    if release is not None:
        _keys(release, {"start", "end", "blend_frames"}, set(), "tangent_release")
        _need(all(type(value) is int for value in release.values())
              and frames[0] <= release["start"] <= release["end"] <= frames[-1]
              and release["blend_frames"] >= 1, "tangent_release must fit the requested frame span")
    solve = params.get("solve_parameters", {})
    _keys(solve, set(), _SOLVE_PARAMETERS, "solve_parameters")
    _need(all(_number(value) and (value >= 0 if key == "clearance_m" else value > 0)
              for key, value in solve.items()), "invalid contact solve parameter")


def validate(operation, params):
    """Check the public JSON contract in the launcher before Blender is started."""
    _need(operation in OPERATIONS, f"unknown posing operation: {operation}")
    required, optional = _FIELDS[operation]
    _keys(params, required, optional, "posing parameters")
    for key in {"rig", "action", "slot", "mesh", "action_name"} & params.keys():
        _need(_name(params[key]), f"{key} must be an explicit Blender name")
    if "action_name" in params:
        _need(params["action_name"] == params["action_name"].strip()
              and len(params["action_name"].encode("utf-8")) <= 63,
              "action_name must be trimmed and no more than 63 UTF-8 bytes")
    for key in _DOCUMENTS.get(operation, ()):
        ref = params[key]
        _keys(ref, {"file", "expected_sha256"}, set(), key)
        _need(isinstance(ref["file"], str) and Path(ref["file"]).is_absolute()
              and Path(ref["file"]).suffix.lower() == ".json"
              and isinstance(ref["expected_sha256"], str)
              and re.fullmatch(r"[0-9a-f]{64}", ref["expected_sha256"]),
              f"{key} needs an absolute JSON file and lowercase SHA-256")
    if operation == "shoulder-pose":
        _need(type(params["frame"]) is int and _frame(params["frame"])
              and params["direction"] in ("FORWARD", "ABDUCTION")
              and _bounded(params["elevation"], 0, 135)
              and _bounded(params.get("follow", 1), 0, 1)
              and _bounded(params.get("manual", 0), -15, 15)
              and _bounded(params.get("advisory", 1), 0, 1), "unsupported left FK shoulder pose")
    elif operation == "rom-inspect":
        _need(_frame(params["frame"]) and _frame(params["reference_frame"])
              and _bounded(params.get("strength", 1), 0, 1), "invalid ROM frame or strength")
    elif operation == "three-beats":
        _validate_pose(params)
    elif operation == "gait-contact":
        _validate_gait(params)
    return params


def guard_inputs(operation, params, guards):
    """Pin auxiliary documents with the shared file/parent guards."""
    from handoff_io import guard_file, read_document

    normalized = dict(params)
    for key in _DOCUMENTS.get(operation, ()):
        ref = params[key]
        path = guard_file(ref["file"], guards, expected=ref["expected_sha256"])
        _need(isinstance(read_document(path), dict), f"{key} must contain a JSON object")
        normalized[key] = {**ref, "file": str(path)}
    return normalized


def _document(params, key):
    ref = params[key]
    path = Path(ref["file"])
    _need(path.stat().st_size <= 16 * 1024 * 1024, f"{key} exceeds the JSON size limit")
    raw = path.read_bytes()
    _need(hashlib.sha256(raw).hexdigest() == ref["expected_sha256"], f"{key} changed after validation")
    value = json.loads(raw.decode("utf-8-sig"))
    _need(isinstance(value, dict), f"{key} must contain a JSON object")
    return value


def _rig(bpy, params):
    rig = bpy.context.view_layer.objects.get(params["rig"])
    _need(rig and rig.type == "ARMATURE" and rig.library is None and rig.override_library is None
          and rig.data.library is None and rig.data.override_library is None and rig.mode != "EDIT",
          "rig must be an editable local armature in the current view layer")
    return rig


def _free_action(bpy, name):
    if name is not None:
        _need(name not in bpy.data.actions, f"action already exists: {name}")


def _set_frame(scene, value):
    whole = math.floor(value)
    scene.frame_set(whole, subframe=value - whole)


@contextmanager
def _time(scene):
    original = scene.frame_current, scene.frame_subframe
    try:
        yield
    finally:
        scene.frame_set(original[0], subframe=original[1])


@contextmanager
def _sample_binding(bpy, rig, action, slot):
    from rigging_workflow import retarget_action as rt

    before = rt.binding(rig)
    with _time(bpy.context.scene):
        try:
            binding = rig.animation_data_create()
            binding.action, binding.action_slot = action, slot
            _need(binding.action == action and binding.action_slot == slot, "candidate slot did not bind")
            yield
        finally:
            rt.restore_binding(rig, before)


def _binding_identity(rig):
    from rigging_workflow import retarget_action as rt
    from rigging_workflow.retarget_contact_correction import action_fingerprint

    binding = rt.binding(rig)
    action, slot = binding["action"], binding["slot"]
    return {"action": action.name if action else None, "slot": slot.identifier if slot else None,
            "fingerprint": action_fingerprint(action) if action else None}


def _pose_samples(bpy, rig, action, slot, frames, names):
    results = []
    scene = bpy.context.scene
    with _sample_binding(bpy, rig, action, slot):
        for frame in frames:
            _set_frame(scene, frame)
            evaluated = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
            result = {name: [value for row in evaluated.matrix_world @ evaluated.pose.bones[name].matrix
                             for value in row] for name in names}
            results.append({"frame": frame, "matrices": result})
    return results


def _rom(bpy, rig, params):
    from rigging_workflow import posing_rom_ui as core

    scene = bpy.context.scene
    with _time(scene):
        _set_frame(scene, params["reference_frame"])
        reference = core.capture_rom_reference(rig)
        _set_frame(scene, params["frame"])
        observed = core.inspect_rom(rig, reference, params.get("strength", 1))
    return {"reference": reference, "observed": observed, "frame": params["frame"],
            "reference_frame": params["reference_frame"], "binding": _binding_identity(rig)}


def _shoulder(bpy, rig, params):
    from rigging_workflow import posing_rom_ui as core

    binding = _binding_identity(rig)
    _need(binding["action"] == params["action"] and binding["slot"] == params["slot"],
          "active primary Action/Slot differs from the request")
    name = params.get("action_name")
    _free_action(bpy, name)
    frame = params["frame"]
    scene = bpy.context.scene
    action, report = core.build_action(bpy.context, rig, frame, params["direction"], params["elevation"],
                                       params.get("follow", 1), params.get("manual", 0), params.get("advisory", 1))
    if name is not None:
        action.name = name
    with _time(scene):
        _set_frame(scene, frame)
        spec = core.profile(rig)
    slot = action.slots[report["slot"]]
    return action, slot, {"native": report, "frames": [frame],
                          "bones": [spec["upper"], spec["elbow"]]}


@contextmanager
def _pose_selection(bpy, rig, names):
    context = bpy.context
    before_mode = context.mode
    active = context.view_layer.objects.active
    _need(before_mode == "OBJECT" or (before_mode == "POSE" and active == rig),
          "three-beats requires Object Mode or this rig's Pose Mode")
    before_selected = tuple(context.selected_objects)
    _need(before_mode == "OBJECT" or all(obj == rig or obj.mode != "POSE" for obj in before_selected),
          "three-beats requires one active Pose Mode armature")
    bone_active = rig.data.bones.active
    bone_selected = {bone.name: bone.select for bone in rig.pose.bones}
    try:
        if before_mode == "OBJECT":
            for obj in before_selected:
                obj.select_set(False)
            rig.select_set(True)
            context.view_layer.objects.active = rig
            _need(bpy.ops.object.mode_set(mode="POSE") == {"FINISHED"}, "could not enter Pose Mode")
        for bone in rig.pose.bones:
            bone.select = bone.name in names
        rig.data.bones.active = rig.data.bones[names[0]]
        yield
    finally:
        for bone in rig.pose.bones:
            bone.select = bone_selected[bone.name]
        rig.data.bones.active = bone_active
        if before_mode == "OBJECT":
            if rig.mode == "POSE":
                bpy.ops.object.mode_set(mode="OBJECT")
            rig.select_set(False)
            for obj in before_selected:
                obj.select_set(True)
            context.view_layer.objects.active = active


def _three_beats(bpy, rig, params):
    from rigging_workflow import three_beats_ui as core

    context = bpy.context
    options = getattr(context.scene, core.OPTIONS)
    _need(not options.draft, "source already contains an unfinished native three-beat draft")
    frames, names = params["frames"], sorted(params["controls"])
    contract = core._contract(rig, names)
    allowed = {(row["name"], prop, index) for row in contract for prop, index in row["channels"]}
    for beat in params["poses"]:
        _need(all((row["bone"], row["property"], row["index"]) in allowed for row in beat),
              "a beat references a locked, driven or unsupported native channel")
    with _pose_selection(bpy, rig, names):
        op = bpy.ops.c94_workflow.three_beats
        options.action_name = params["action_name"]
        options.step_frame, options.transfer_frame, options.reach_frame = frames
        _need(op(operation="START") == {"FINISHED"}, "native three-beats could not start")
        try:
            initial_pose = core._pose(rig, contract)
            for beat in params["poses"]:
                core._restore_pose(rig, contract, initial_pose)
                for row in beat:
                    getattr(rig.pose.bones[row["bone"]], row["property"])[row["index"]] = row["value"]
                for item in contract:
                    bone = rig.pose.bones[item["name"]]
                    if bone.rotation_mode == "QUATERNION":
                        _need(bone.rotation_quaternion.magnitude > 1e-8,
                              f"{bone.name} has a zero quaternion")
                context.view_layer.update()
                _need(op(operation="CAPTURE") == {"FINISHED"}, "native three-beats could not capture")
        except BaseException:
            if options.draft:
                op(operation="CANCEL")
            raise
    action, slot = rig.animation_data.action, rig.animation_data.action_slot
    _need(action is not None and action == options.result and action.name == params["action_name"] and slot,
          "native recording did not retain the intended Action and Slot")
    return action, slot, {"frames": frames, "bones": names}


def _fk_probe(bpy, rig, action, slot, document, frames):
    from rigging_workflow import retarget_fk_contact as core

    scene = bpy.context.scene
    mesh = bpy.context.view_layer.objects.get(document["mesh"])
    _need(mesh and mesh.type == "MESH", "calibrated mesh is missing from the current view layer")
    rows = []
    with _sample_binding(bpy, rig, action, slot):
        for frame in frames:
            _set_frame(scene, frame)
            center, low = core._point(mesh, document["heel_vertex_ids"], bpy.context.evaluated_depsgraph_get())
            rows.append({"frame": frame, "center": list(center), "minimum_z": low})
    return rows


def _fk_contact(bpy, rig, params):
    from rigging_workflow import retarget_fk_contact as core

    document = _document(params, "calibration")
    mesh = bpy.context.view_layer.objects.get(params["mesh"])
    _need(mesh and mesh.type == "MESH", "mesh must identify a visible calibration mesh in this view layer")
    name = params.get("action_name")
    _free_action(bpy, name)
    action = core.build(rig, mesh, document)
    if name is not None:
        action.name = name
    report = json.loads(action[core.META])
    end = float(action.frame_range[1])
    start = float(document["touchdown"])
    frames = [start, (start + end) / 2, end]
    return action, action.slots[document["slot"]], {"native": report, "frames": frames}


def _gait_probe(bpy, rig, action, slot, frames):
    from rigging_workflow import retarget_contact_correction as core
    from rigging_workflow import retarget_contact_job as adapter
    from rigging_workflow import retarget_action as rt
    from rigging_workflow import pose_switching

    plan = core.reopen_for_review(rig, action, graph_fingerprint=adapter.graph_fingerprint)
    before = rt.binding(rig)
    scene = bpy.context.scene
    rows = []
    try:
        _need(plan.slot == slot and plan.preview()["status"] == "preview", "native gait preview did not start")
        try:
            for frame in frames:
                _set_frame(scene, frame)
                pose_switching._update(rig)
                center, low = core._read_patch(plan)
                rows.append({"frame": frame, "center": list(center), "minimum_z": low})
        finally:
            _need(plan.stop_preview()["status"] == "needs_review" and rt.binding(rig) == before,
                  "native gait stop did not restore its base binding")
    finally:
        core._plans.pop(plan.token, None)
    return rows


def _gait_contact(bpy, rig, params, job_dir):
    from rigging_workflow import retarget_contact_correction as core
    from rigging_workflow import retarget_contact_job as adapter

    _free_action(bpy, params["action_name"])
    report = _document(params, "detection_report")
    calibration = _document(params, "calibration")
    issue_id, frames = params["issue_id"], params["frames"]
    issues = [issue for issue in report.get("issues", ()) if issue.get("id") == issue_id]
    _need(len(issues) == 1, "gait-contact needs exactly one signed issue")
    window = core.gait_support_window(report, issues[0], calibration, frames,
                                      tangent_release=params.get("tangent_release"))
    items, _ = adapter.resolve_items(rig, {
        "kind": adapter.KIND, "detection_report": report, "calibration": calibration,
        "issue_id": issue_id, "frames": frames, "support_window": window,
        "parameters": params.get("solve_parameters", {}), "action_name": params["action_name"],
    }, Path(job_dir).name)
    result = adapter.convert_item(rig, items[0], progress=lambda *_: None, cancelled=lambda: False)
    report = result["report"]
    bounds = [window["touchdown_frame"], frames[len(frames) // 2], frames[-1]]
    if window["tangent_release"]:
        bounds.extend(window["tangent_release"][key] for key in ("start", "end"))
    return result["action"], result["slot"], {
        "native": {key: report[key] for key in ("issue_ids", "review_state", "applied", "parameters", "curve_changes")},
        "frames": sorted(set(bounds)),
    }


def _candidate_samples(bpy, operation, rig, action, slot, details):
    frames = details["frames"]
    if operation == "fk-heel-contact":
        return _fk_probe(bpy, rig, action, slot, details["native"]["document"], frames)
    if operation == "gait-contact":
        return _gait_probe(bpy, rig, action, slot, frames)
    return _pose_samples(bpy, rig, action, slot, frames, details["bones"])


def execute(bpy, operation, params, job_dir):
    """Operate on the dispatcher-owned scene; return a JSON receipt."""
    from rigging_workflow.retarget_contact_correction import action_fingerprint

    rig = _rig(bpy, params)
    if operation == "rom-inspect":
        return _rom(bpy, rig, params)
    base = _binding_identity(rig)
    if operation == "shoulder-pose":
        action, slot, details = _shoulder(bpy, rig, params)
    elif operation == "three-beats":
        action, slot, details = _three_beats(bpy, rig, params)
    elif operation == "fk-heel-contact":
        action, slot, details = _fk_contact(bpy, rig, params)
    else:
        action, slot, details = _gait_contact(bpy, rig, params, job_dir)
    binding = _binding_identity(rig)
    _need(operation == "three-beats" or binding == base, "native operation changed its base binding")
    return {"action": action.name, "slot": slot.identifier, "action_fingerprint": action_fingerprint(action),
            "action_frame_range": list(action.frame_range), "base": base, "binding": binding,
            "details": details, "samples": _candidate_samples(bpy, operation, rig, action, slot, details)}


def _compare_samples(expected, actual, where="samples"):
    if isinstance(expected, dict):
        _need(isinstance(actual, dict) and expected.keys() == actual.keys(), f"{where} fields changed")
        for key in expected:
            _compare_samples(expected[key], actual[key], f"{where}.{key}")
    elif isinstance(expected, list):
        _need(isinstance(actual, list) and len(expected) == len(actual), f"{where} length changed")
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            _compare_samples(left, right, f"{where}[{index}]")
    elif type(expected) is float:
        _need(_number(actual) and abs(expected - actual) <= 1e-5, f"{where} differs after reopening")
    else:
        _need(expected == actual, f"{where} identity changed")


def verify(bpy, operation, params, result):
    """Re-evaluate native outputs on a separately opened Blender file."""
    from rigging_workflow.retarget_contact_correction import action_fingerprint

    rig = _rig(bpy, params)
    if operation == "rom-inspect":
        _compare_samples(result, _rom(bpy, rig, params), "ROM")
        return {"binding": result["binding"], "rom_reproduced": True}
    action = bpy.data.actions.get(result["action"])
    slot = action.slots.get(result["slot"]) if action else None
    _need(action is not None and slot is not None and action_fingerprint(action) == result["action_fingerprint"]
          and list(action.frame_range) == result["action_frame_range"], "candidate Action/Slot or curves changed")
    base = result["base"]
    if base["action"]:
        original = bpy.data.actions.get(base["action"])
        _need(original is not None and original.slots.get(base["slot"]) is not None
              and action_fingerprint(original) == base["fingerprint"], "base Action/Slot or curves changed")
    _need(_binding_identity(rig) == result["binding"], "saved scene has an unexpected bound Action")
    samples = _candidate_samples(bpy, operation, rig, action, slot, result["details"])
    _compare_samples(result["samples"], samples)
    _need(_binding_identity(rig) == result["binding"], "cold observation changed the active binding")
    audit = {"action": action.name, "slot": slot.identifier, "samples_reproduced": len(samples),
             "base_action_retained": True, "binding": result["binding"]}
    if operation == "gait-contact":
        audit["native_preview_stopped"] = True
    return audit
