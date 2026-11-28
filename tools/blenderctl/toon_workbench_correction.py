"""Native Toon workbench correction and shot presets on saved work copies."""
import json
import os
from pathlib import Path

from material_draft_handoff import fingerprint, require
from protocol import Failure, digest
import toon_viewport_lines_contract as contract


READ = "inspect-workbench-correction"
EDIT = "edit-workbench-correction"
UPGRADE = "save-workbench-53-upgrade"
OPERATIONS = (READ, EDIT, UPGRADE)
HELP = ("inspect-workbench-correction reads all native corrections and shot presets. edit-workbench-correction "
        "uses correction_actions ZERO_WEIGHT, CREATE, CONFIGURE, DEFINE_SHOT, SHOT_STRENGTH or APPLY_SHOT. "
        "save-workbench-53-upgrade makes an independently reopened new 5.3 EEVEE work copy.")
FIELDS = {
    "ZERO_WEIGHT": ({"target", "attribute"}, set()),
    "CREATE": ({"target", "camera", "reference", "attribute"}, set()),
    "CONFIGURE": ({"target", "expected_owner"}, {"strength", "camera", "show_viewport", "show_render"}),
    "DEFINE_SHOT": ({"camera", "reference", "targets", "strength", "replace"}, set()),
    "SHOT_STRENGTH": ({"camera", "expected_shot_id", "strength"}, set()),
    "APPLY_SHOT": (set(), set()),
}


def normalize_request(document, editions):
    operation = document["operation"]
    payload = {key: value for key, value in document.items() if key != "correction_actions"}
    require(not {"scope", "patch", "frames", "elements"} & payload.keys(),
            "INVALID_REQUEST", "Use explicit saved workbench targets and cameras")
    if operation in (READ, UPGRADE):
        require("correction_actions" not in document, "INVALID_REQUEST", "This operation accepts no correction edits")
        payload.update(operation="inspect", scope={"groups": ["workbench-correction"]})
    else:
        payload["operation"] = "render-camera"
    result = contract.normalize_request(payload, editions)
    result.pop("scope", None)
    result["operation"] = operation
    if operation == UPGRADE:
        require(result["edition"] == "0.52.1", "UNSUPPORTED", "5.3 upgrade requires Toon 0.52.1")
    if operation != EDIT:
        return result
    actions = document.get("correction_actions")
    require(type(actions) is list and 1 <= len(actions) <= 64, "INVALID_REQUEST", "Expected 1..64 correction actions")
    for row in actions:
        require(type(row) is dict and isinstance(row.get("action"), str) and row["action"] in FIELDS,
                "INVALID_REQUEST", "Unknown native correction action")
        required, extras = FIELDS[row["action"]]
        require(required <= (row.keys() - {"action"}) <= required | extras, "INVALID_REQUEST", "Unexpected correction action fields")
        for field in {"target", "camera", "reference", "attribute", "expected_owner", "expected_shot_id"} & row.keys():
            require(contract.name(row[field], 63 if field == "attribute" else 256), "INVALID_REQUEST", "Expected named " + field)
        for field in {"replace", "show_viewport", "show_render"} & row.keys():
            require(type(row[field]) is bool, "INVALID_REQUEST", "Expected Boolean " + field)
        if "strength" in row:
            require(type(row["strength"]) in (int, float) and 0 <= row["strength"] <= 1, "INVALID_REQUEST", "Strength needs [0,1]")
        if "targets" in row:
            targets = row["targets"]
            require(type(targets) is list and 1 <= len(targets) <= 256
                    and all(contract.name(target, 256) for target in targets) and len(set(targets)) == len(targets),
                    "INVALID_REQUEST", "Expected unique saved mesh target names")
    result["correction_actions"] = actions
    return result


def inspect_state(context, workbench, correction, shots):
    rows = {}
    for target in context.scene.objects:
        if target.type != "MESH":
            continue
        modifier = workbench._correction(target)
        if modifier:
            report = correction.inspect(target, modifier, evaluate=modifier.show_viewport)
            rows[target.name] = report
    return {"corrections": rows, "shots": shots.inspect(context.scene),
            "weights": {target.name: [dict(name=a.name, size=len(a.data), nonzero=sum(x.value != 0. for x in a.data))
                                      for a in target.data.attributes if not a.is_internal and a.domain == "POINT" and a.data_type == "FLOAT"]
                        for target in context.scene.objects if target.type == "MESH"}}


def native_object(context, name, kind=None):
    obj = context.view_layer.objects.get(name)
    require(obj is not None and (kind is None or obj.type == kind) and obj.library is None and obj.override_library is None,
            "NOT_FOUND", "Expected named local " + (kind or "object") + " in saved view layer: " + name)
    return obj


def edit(bpy, context, workbench, correction, shots, rows):
    scene = context.scene
    panel = getattr(scene, workbench.PROP)
    changes = set()
    results = []
    for row in rows:
        action = row["action"]
        event = {"action": action}
        if action in ("ZERO_WEIGHT", "CREATE", "CONFIGURE"):
            target = native_object(context, row["target"], "MESH")
            changes.add(target.name_full)
            require(target.mode == "OBJECT", "CONFLICT", "Use saved Object Mode for correction")
        if action == "ZERO_WEIGHT":
            original = target.data
            panel.weight_attribute = row["attribute"]
            with context.temp_override(object=target, active_object=target):
                require(bpy.ops.toon_workbench.zero_weight() == {"FINISHED"}, "CONFLICT", "Native zero weight creation failed")
            attribute = target.data.attributes[row["attribute"]]
            require(attribute.data_type == "FLOAT" and attribute.domain == "POINT" and not any(item.value for item in attribute.data),
                    "VALIDATION_FAILED", "Native zero attribute is not zero FLOAT POINT")
            require(original == target.data or original.attributes.get(row["attribute"]) is None,
                    "VALIDATION_FAILED", "A shared source mesh was mutated")
            event.update(attribute=attribute.name, vertices=len(attribute.data))
        if action in ("CREATE", "DEFINE_SHOT"):
            panel.correction_camera = native_object(context, row["camera"], "CAMERA")
            panel.correction_reference = native_object(context, row["reference"])
        if action == "CREATE":
            panel.weight_attribute = row["attribute"]
            modifier = correction.build(target, panel.correction_camera, panel.correction_reference, panel.weight_attribute)
            event.update(modifier=modifier.name, owner=modifier.node_group[correction.OWNER])
        if action == "CONFIGURE":
            modifier = workbench._correction(target)
            require(modifier is not None and modifier.node_group.get(correction.OWNER) == row["expected_owner"],
                    "CONFLICT", "Native correction owner or modifier differs")
            _, ids = correction._owned(target, modifier)
            old = {key: correction._input(modifier, ids, key).value for key in ("camera", "reference", "weight", "strength")}
            new_camera = native_object(context, row["camera"], "CAMERA") if "camera" in row else old["camera"]
            correction.configure(target, modifier, camera=new_camera, reference=old["reference"], weight_attribute=old["weight"],
                                 strength=row.get("strength", old["strength"]))
            for flag in ("show_viewport", "show_render"):
                if flag in row:
                    setattr(modifier, flag, row[flag])
        if action == "DEFINE_SHOT":
            panel.shot_strength, panel.shot_replace = row["strength"], row["replace"]
            event["shot"] = shots.define(scene, panel.correction_camera, panel.correction_reference,
                                         [native_object(context, name, "MESH") for name in row["targets"]],
                                         strength=panel.shot_strength, replace=panel.shot_replace)
        if action == "SHOT_STRENGTH":
            stored = shots._read(scene)
            matching = [shot for shot in stored["shots"] if shot["camera"].name == row["camera"]] if stored else []
            require(len(matching) == 1 and matching[0]["id"] == row["expected_shot_id"], "CONFLICT", "Saved shot identity differs")
            matching[0]["collection"][shots._STRENGTH] = row["strength"]
        if action == "APPLY_SHOT":
            applied = shots.apply(scene)
            require(applied is not None, "NOT_FOUND", "No saved shot for this scene")
            changes.update(target["target"] for target in applied["targets"])
            event["shot"] = applied
        results.append(event)
    context.view_layer.update()
    return results, changes


def work(bpy, context, request, author, *, author_sha, save_candidate, read_bindings, dependencies):
    from toon_workflow.toon_workflow_addon import camera_correction, camera_shots, workbench
    from inspection import record

    scene = context.scene
    op = request["operation"]
    before = inspect_state(context, workbench, camera_correction, camera_shots)
    baseline = read_bindings()
    refs = dependencies()
    scene_sha = fingerprint([baseline, refs])
    response = {"operation": op, "product": request["product"], "context": request["context"],
                "source": {"file": request["file"], "sha256": request["expected_sha256"], "unchanged": True},
                "runtime": {"blender": bpy.app.version_string, "build": bpy.app.build_hash.decode(), "pid": os.getpid()}}
    if op == READ:
        observed = fingerprint(before)
        require(observed == request.get("verify_state_sha256", observed)
                and scene_sha == request.get("verify_scene_sha256", scene_sha),
                "VALIDATION_FAILED", "Independent reopen changed native correction, shot or saved references")
        return {**response, "state": before, "author_sha256": author, "state_sha256": observed, "scene_sha256": scene_sha}

    if op == UPGRADE:
        from toon_workflow.toon_workflow_addon import lighting_53

        stage = Path(request["stage"])
        require(stage.stat().st_size == 0, "CONFLICT", "Upgrade stage is not the reserved empty file")
        original_version = list(bpy.data.version)
        stage.unlink()
        bpy.context.preferences.filepaths.save_version = 0
        lighting_53.save_upgrade(stage)
        require(author_sha(scene) == author and digest(request["file"]) == request["expected_sha256"],
                "VALIDATION_FAILED", "Upgrade altered the original source or live Toon author")
        return {**response, "state": before, "event": {"source_version": original_version, "target_version": list(bpy.app.version)},
                "author_before_sha256": author, "author_after_sha256": author, "candidate_sha256": digest(stage),
                "scene_sha256": scene_sha, "state_sha256": fingerprint(before), "retained_orphans": [],
                "protected": {"status": "passed", "native_saved_upgrade": True}}

    objects = {obj.name_full: fingerprint(record(obj)) for obj in bpy.data.objects}
    try:
        events, changed = edit(bpy, context, workbench, camera_correction, camera_shots, request["correction_actions"])
        after = inspect_state(context, workbench, camera_correction, camera_shots)
    except (KeyError, ValueError, TypeError, RuntimeError) as error:
        raise Failure("CONFLICT", str(error)) from error
    updated_bindings = read_bindings()
    require(before != after and author_sha(scene) == author and refs == dependencies()
            and {obj.name_full: fingerprint(record(obj)) for obj in bpy.data.objects if obj.name_full not in changed}
            == {key: value for key, value in objects.items() if key not in changed}
            and [row for row in baseline if row["object"] not in changed] == [row for row in updated_bindings if row["object"] not in changed],
            "VALIDATION_FAILED", "Correction made no native change or affected non-target objects or line authors")
    retained = save_candidate(bpy, request)
    return {**response, "state": after, "events": events, "author_before_sha256": author, "author_after_sha256": author,
            "scene_sha256": fingerprint([updated_bindings, refs]), "state_sha256": fingerprint(after), "candidate_sha256": digest(request["stage"]),
            "retained_orphans": retained, "protected": {"status": "passed", "non_target_objects_and_line_author": True}}


def open_upgrade(bpy, request):
    require(bpy.ops.wm.open_mainfile(filepath=request["file"], load_ui=True, use_scripts=False) == {"FINISHED"}
            and Path(bpy.data.filepath).samefile(request["file"]), "VALIDATION_FAILED", "Saved upgrade source could not open")
    require(tuple(bpy.data.version[:2]) in ((5, 2), (5, 3)), "UNSUPPORTED", "Explicit 5.3 upgrade accepts only 5.2/5.3 files")
    context = bpy.context
    observed = {"scene": context.scene.name, "view_layer": context.view_layer.name, "frame": context.scene.frame_current}
    require(observed == request["context"] and not context.scene.frame_subframe, "CONFLICT", "Saved upgrade context differs")
    return context
