"""Replay saved Pencil line-function targets and group visibility switches."""

import copy
import os

from material_draft_handoff import fingerprint, require
from protocol import Failure, digest
import toon_viewport_lines_contract as contract
from toon_viewport_scene_controls import masked_scene_states


READ = "inspect-line-visibility"
WRITE = "edit-line-visibility"
KINDS = {"draw_objects", "draw_materials", "mask_objects", "mask_materials"}
CHANNELS = {"VISIBLE", "HIDDEN"}


def normalize_request(document, editions):
    operation = document["operation"]
    controls = {"target_steps", "group_patches"}
    payload = {key: value for key, value in document.items() if key not in controls}
    require(not {"scope", "patch", "frames", "elements"} & payload.keys(), "INVALID_REQUEST",
            "Use exact saved container and LineSet IDs")
    normalized = contract.normalize_request({**payload, "operation": "inspect",
                                             "scope": {"groups": ["line-visibility"]}}, editions)
    normalized.pop("scope")
    normalized["operation"] = operation
    if operation == READ:
        require(not controls & document.keys(), "INVALID_REQUEST", "Inspection accepts no edits")
        return normalized

    steps = document.get("target_steps", [])
    groups = document.get("group_patches", [])
    require("expected_author_sha256" in normalized and isinstance(steps, list) and isinstance(groups, list)
            and 0 < len(steps) + len(groups) <= 128, "INVALID_REQUEST",
            "Supply saved author SHA and up to 128 target or group edits")
    checked = []
    for step in steps:
        require(isinstance(step, dict) and step.keys() == {"action", "container", "kind", "target"}
                and step["action"] in ("ADD", "REMOVE") and contract.name(step["container"], 128)
                and isinstance(step["kind"], str) and step["kind"] in KINDS, "INVALID_REQUEST",
                "A target step needs ADD or REMOVE, container ID, kind and {name,library}")
        checked.append({**step, "target": contract.ref(step["target"], "Line-function target")})
    seen = set()
    for patch in groups:
        require(isinstance(patch, dict) and {"group"} < patch.keys() <= {"group", "hidden_mask", "self_intersections"}
                and contract.name(patch["group"], 128) and patch["group"] not in seen,
                "INVALID_REQUEST", "A group patch needs a unique LineSet ID and hidden_mask or self_intersections")
        require(type(patch.get("hidden_mask", False)) is bool, "INVALID_REQUEST", "hidden_mask must be boolean")
        if "self_intersections" in patch:
            channels = patch["self_intersections"]
            require(isinstance(channels, dict) and 0 < len(channels) and channels.keys() <= CHANNELS
                    and all(type(value) is bool for value in channels.values()),
                    "INVALID_REQUEST", "Self-intersection switches use VISIBLE and HIDDEN booleans")
        seen.add(patch["group"])
    return {**normalized, "target_steps": checked, "group_patches": groups}


def state(scene, document, preview):
    native = preview.line_functions
    extension = native.records(document) or {"containers": [], "masks": []}
    archived = preview.group_lifecycle.records(document)["archived"]
    fallback = document["request"]["intersection"]["settings"].get("self_intersection", False)
    masks = {row["group"]: row["enabled"] for row in extension["masks"]}
    labels = {row.name: row.label for row in scene.toon_viewport_preview.groups}
    groups = {row["id"]: {"label": labels.get(row["id"], row["id"]), "hidden_mask": masks.get(row["id"], False),
                         "self_intersections": native.self_channels(document, row["id"], fallback)}
              for row in document["request"]["boundary"]["groups"] if row["id"] not in archived}
    return {"containers": {row["id"]: row for row in extension["containers"]}, "groups": groups}


def protection(bpy, scene, preview, wrapped, bindings, dependencies):
    from toon_workflow.toon_workflow_addon import joint_workflow

    document = copy.deepcopy(wrapped["document"])
    document["request"].pop("line_functions", None)
    actuals = preview.source_bindings.recover(document)
    controls = copy.deepcopy(wrapped["scene_controls"])
    controls["chain_controls"] = controls["chain_controls"] or preview.chain_controls.seed_controls(actuals)
    controls["dash_patterns"] = controls["dash_patterns"] or preview.dash_controls.defaults(actuals)
    controls["map_amounts"] = controls["map_amounts"] or preview.map_amount_controls.normalize(
        actuals, preview.map_amount_controls.defaults(actuals))
    other_scenes = {sc.name_full: sc.toon_viewport_preview.preset_text.as_string()
                    for sc in bpy.data.scenes if sc != scene and sc.toon_viewport_preview.preset_text}
    return fingerprint([document, wrapped["recipe"], controls, other_scenes,
                        joint_workflow.non_author_signature(scene), bindings(), dependencies()])


def apply(scene, preview, current, steps, patches):
    settings = scene.toon_viewport_preview
    preset = settings.preset_text
    require(scene.is_editable and scene.library is None and scene.override_library is None
            and preset.library is None and preset.get("toon_viewport_preview_owned"), "UNSUPPORTED",
            "Line functions require an owned PRESET in a local editable scene")
    tree = settings.curve_tree
    curve = preview.curve_editor
    if tree is not None and tree.get("toon_viewport_curve_role") == curve.ROLE:
        require(curve.signature(tree, settings) == tree.get("last_signature"), "CONFLICT",
                "Commit saved curve edits before changing line visibility")
    expected = copy.deepcopy(current)
    touched = set()
    material_names = {slot.material.name_full for obj in scene.objects for slot in obj.material_slots
                      if slot.material is not None and slot.material.library is None}
    for step in steps:
        cid, kind, target = step["container"], step["kind"], step["target"]
        require(cid in expected["containers"], "NOT_FOUND", "Unknown saved line-function container: " + cid)
        values = expected["containers"][cid][kind]
        if step["action"] == "ADD":
            if kind.endswith("objects"):
                obj = scene.objects.get(target["name"])
                exists = obj is not None and obj.library is None and obj.type == "MESH"
            else:
                exists = target["name"] in material_names
            require(exists, "NOT_FOUND", "Target is not a local mesh or used material in the saved scene: " + target["name"])
            require(target not in values, "CONFLICT", "Target already exists in " + kind)
            values.append(target)
        else:
            require(target in values, "CONFLICT", "Exact target is no longer present in " + kind)
            values.remove(target)
        touched.add(cid)

    masks, self_rows = [], []
    for patch in patches:
        gid = patch["group"]
        require(gid in expected["groups"], "NOT_FOUND", "Unknown active LineSet: " + gid)
        row = expected["groups"][gid]
        if "hidden_mask" in patch:
            row["hidden_mask"] = patch["hidden_mask"]
            masks.append({"group": gid, "enabled": row["hidden_mask"]})
        if "self_intersections" in patch:
            row["self_intersections"].update(patch["self_intersections"])
            self_rows.append({"group": gid, **row["self_intersections"]})
    updates = {"containers": [row for cid, row in expected["containers"].items() if cid in touched],
               "masks": masks}
    if self_rows:
        updates["self_intersections"] = self_rows
    try:
        event = preview.line_function_ui.set_functions(scene, updates)
    except (RuntimeError, ValueError) as exc:
        raise Failure("CONFLICT", str(exc)) from exc
    return expected, event


def work(bpy, context, request, wrapped, author, *, bindings, dependencies, save_candidate, author_sha):
    from toon_workflow import toon_viewport_preview as preview

    scene = context.scene
    operation = request["operation"]
    before = state(scene, wrapped["document"], preview)

    def outside():
        return fingerprint([row for row in masked_scene_states(bpy, scene) if row["scene"]["name"] != scene.name_full])

    scene_sha = fingerprint([protection(bpy, scene, preview, wrapped, bindings, dependencies), outside()])
    state_sha = fingerprint(before)
    result = {"operation": operation, "visibility": before, "state_sha256": state_sha, "scene_sha256": scene_sha,
              "author_sha256": author, "source": {"file": request["file"], "sha256": request["expected_sha256"], "unchanged": True},
              "context": request["context"], "product": request["product"],
              "runtime": {"blender": bpy.app.version_string, "build": bpy.app.build_hash.decode(), "pid": os.getpid()}}
    if operation == READ:
        require(request.get("verify_state_sha256", state_sha) == state_sha
                and request.get("verify_scene_sha256", scene_sha) == scene_sha,
                "VALIDATION_FAILED", "Cold line visibility or protected style differs")
        return result
    expected, event = apply(scene, preview, before, request["target_steps"], request["group_patches"])
    authored = preview.file_authoring.read_scene(scene)
    after = state(scene, authored["document"], preview)
    actual_protection = protection(bpy, scene, preview, authored, bindings, dependencies)
    require(after == expected and fingerprint([actual_protection, outside()]) == scene_sha,
            "VALIDATION_FAILED", "Line visibility edit changed protected style or did not round-trip")
    after_author = author_sha(scene)
    retained = save_candidate(bpy, request)
    return {**result, "visibility": after, "state_sha256": fingerprint(after),
            "author_before_sha256": author, "author_after_sha256": after_author,
            "candidate_sha256": digest(request["stage"]), "event": event, "retained_orphans": retained,
            "protected": {"status": "passed", "scene_sha256": scene_sha}}
