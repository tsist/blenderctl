"""Replay Toon group lifecycle, render controls and native brush editor handoff."""

import copy
import os

from material_draft_handoff import fingerprint, require
from protocol import Failure, digest
import toon_viewport_lines_contract as contract


READ = "inspect-line-groups"
WRITE = "manage-line-groups"
ACTIONS = {
    "NEW": ({"label"}, set()),
    "ARCHIVE": ({"group"}, set()),
    "RESTORE": ({"group"}, set()),
    "SET_OUTPUT": ({"group"}, {"line_set_id", "elements_only"}),
    "SELECT_EDITOR": ({"group", "slot"}, {"open_curves"}),
}


def normalize_request(document, editions):
    operation = document["operation"]
    payload = {key: value for key, value in document.items() if key != "group_action"}
    require(not {"scope", "frames", "elements", "patch"} & payload.keys(), "INVALID_REQUEST",
            "Line groups use saved PRESET identities")
    if operation == READ:
        require("group_action" not in document, "INVALID_REQUEST", "Group inspection accepts no action")
        payload.update(operation="inspect", scope={"groups": ["line-groups"]})
    else:
        payload["operation"] = "render-camera"
    result = contract.normalize_request(payload, editions)
    result.pop("scope", None)
    result["operation"] = operation
    if operation == WRITE:
        step = document.get("group_action")
        require(isinstance(step, dict) and isinstance(step.get("action"), str) and step["action"] in ACTIONS,
                "INVALID_REQUEST", "Unknown line group action")
        required, optional = ACTIONS[step["action"]]
        require(required <= step.keys() - {"action"} <= required | optional, "INVALID_REQUEST",
                "Unexpected fields for group action " + step["action"])
        for field in ("label", "group", "slot"):
            if field in step:
                require(contract.name(step[field], 256), "INVALID_REQUEST", "Invalid group " + field)
        for field in ("elements_only", "open_curves"):
            if field in step:
                require(type(step[field]) is bool, "INVALID_REQUEST", field + " must be boolean")
        if "line_set_id" in step:
            require(type(step["line_set_id"]) is int and 1 <= step["line_set_id"] <= 8,
                    "INVALID_REQUEST", "line_set_id must be between 1 and 8")
        require(step["action"] != "SET_OUTPUT" or bool(optional & step.keys()),
                "INVALID_REQUEST", "SET_OUTPUT requires a group render setting")
        result["group_action"] = dict(step)
    return result


def group_state(scene, document, saved_elements):
    from toon_workflow.toon_viewport_preview import addon, curve_editor, group_lifecycle, group_ui

    configuration = group_ui.snapshot(scene.toon_viewport_preview)
    records = group_lifecycle.records(document)
    plan = addon._preset(scene)
    configuration["archived"] = records["archived"]
    configuration["owned"] = {row["group"]: row["brushes"] for row in records["authored"]}
    configuration["slots"] = {
        group["id"]: sorted(set(group["brush_slots"]) | set(group.get("intersection_sources", {}).get("authors", {})))
        for group in plan["groups"]
    }
    configuration["saved_elements"] = saved_elements(scene.toon_viewport_preview)
    settings = scene.toon_viewport_preview
    tree = settings.curve_tree
    if tree is not None and tree.get("toon_viewport_curve_role") == curve_editor.ROLE:
        configuration["curve_editor"] = {
            "identity": tree["identity"], "group": tree["selected_group"], "slot": tree["selected_slot"],
            "signature": curve_editor.signature(tree, settings), "saved_signature": tree["last_signature"],
        }
    return configuration


def guarded_group_snapshot(wrapped, state, group, *, baseline, subset, geometry):
    from toon_workflow.toon_viewport_preview import chain_controls, dash_controls, map_amount_controls
    from toon_workflow.toon_viewport_preview import group_lifecycle, selector_bindings, source_bindings
    protected = subset
    protected["group_lifecycle"] = group_lifecycle.records(wrapped["document"])
    protected["group_lifecycle"]["archived"].pop(group, None)
    protected["group_lifecycle"]["authored"] = [row for row in protected["group_lifecycle"]["authored"]
                                               if row["group"] != group]
    protected["scene_controls"].pop("elements", None)
    stable_elements = copy.deepcopy(state["saved_elements"])
    for element in stable_elements["elements"]:
        element["group_filters"] = [row for row in element["group_filters"] if row["id"] != group]
        if element["group_uids"] is not None:
            element["group_uids"] = [value for value in element["group_uids"] if value != group]
    protected["saved_elements"] = stable_elements

    document = wrapped["document"]
    _rows, order = selector_bindings.records(document)
    if order is None:
        order = [row["id"] for row in sorted(document["request"]["boundary"]["groups"],
                                             key=lambda value: value["priority"], reverse=True)]
    protected["selector_order"] = [value for value in order if value != group]
    registry = source_bindings.recover(document)
    actuals = set(baseline) if baseline is not None else set(registry["actuals"])
    protected["source_brushes"] = {key: row for key, row in registry["actuals"].items() if key in actuals}
    controls = wrapped["scene_controls"]
    # Old saved styles omit these tables entirely. Compare their native effective defaults.
    chains = controls["chain_controls"] or chain_controls.seed_controls(registry)
    amounts = controls["map_amounts"] or map_amount_controls.normalize(registry, map_amount_controls.defaults(registry))
    dashes = controls["dash_patterns"] or dash_controls.defaults(registry)
    protected["brush_controls"] = [{key: row for key, row in table.items() if key in actuals}
                                   for table in (chains["brushes"], amounts["amounts"], dashes["patterns"])]
    protected["geometry"] = geometry
    return protected


def apply_step(scene, state, step):
    from toon_workflow.toon_viewport_preview import curve_editor, group_ui

    action = step["action"]
    gid = step.get("group")
    settings = scene.toon_viewport_preview
    require(scene.library is None and scene.override_library is None and scene.is_editable and
            settings.preset_text.library is None and settings.preset_text.get("toon_viewport_preview_owned"),
            "UNSUPPORTED", "Group editing requires an owned PRESET in a local editable scene")
    if "curve_editor" in state:
        editor = state["curve_editor"]
        require(editor["signature"] == editor["saved_signature"], "CONFLICT", "Commit saved curve edits before changing groups")
    target = settings.groups.get(gid) if gid else None
    try:
        if action == "NEW":
            return group_ui.change(scene, "NEW", label=step["label"])
        if action == "RESTORE":
            require(gid in state["archived"], "NOT_FOUND", "Recoverable group does not exist: " + gid)
            return group_ui.change(scene, "RESTORE", group=gid)
        require(target is not None, "NOT_FOUND", "Active group does not exist: " + gid)
        if action == "ARCHIVE":
            return group_ui.change(scene, "ARCHIVE", group=gid)
        event = {"action": action, "group": gid}
        if action == "SET_OUTPUT":
            for field in ("line_set_id", "elements_only"):
                if field in step:
                    setattr(target, field, step[field])
            return event
        slot = step["slot"]
        require(slot in state["slots"][gid], "NOT_FOUND", "Unknown brush slot for this group: " + slot)
        with group_ui.guard(scene):
            settings.curve_group = gid
            allowed = {item[0] for item in curve_editor.slot_items(settings, None)}
            require(slot in allowed, "UNSUPPORTED", "Brush slot does not expose native editor controls: " + slot)
            settings.curve_slot = slot
        if step.get("open_curves"):
            event["curve_editor"] = curve_editor.initialize(scene)
        else:
            curve_editor.target_changed(scene)
        event["slot"] = slot
        return event
    except ValueError as error:
        raise Failure("CONFLICT", str(error)) from error


def work(bpy, context, request, wrapped, author, *, save_candidate, saved_elements, author_sha, protect_subset):
    from toon_workflow.toon_viewport_preview import file_authoring, source_bindings
    from toon_workflow.toon_workflow_addon import joint_workflow

    scene = context.scene
    document = wrapped["document"]
    before = group_state(scene, document, saved_elements)
    response = {
        "operation": request["operation"], "product": request["product"], "context": request["context"],
        "source": {"file": request["file"], "sha256": request["expected_sha256"], "unchanged": True},
        "runtime": {"blender": bpy.app.version_string, "build": bpy.app.build_hash.decode(), "pid": os.getpid()},
    }
    if request["operation"] == READ:
        scene_sha = fingerprint(protect_subset(wrapped, set(), set()))
        if "verify_state_sha256" in request:
            require(fingerprint(before) == request["verify_state_sha256"] and scene_sha == request["verify_scene_sha256"],
                    "VALIDATION_FAILED", "Cold reopen changed the group, saved editor or protected scene state")
        require(digest(request["file"]) == request["expected_sha256"], "CONFLICT", "Source changed during group inspection")
        return {**response, "group_state": before, "state_sha256": fingerprint(before),
                "author_sha256": author, "scene_sha256": scene_sha}

    step = request["group_action"]
    before_ids = set(source_bindings.recover(document)["actuals"])
    # Capture Blender bindings before calling into the native group transaction.
    before_geometry = joint_workflow.non_author_signature(scene)
    before_subset = protect_subset(wrapped, {step["group"]} if "group" in step else set(), set())
    event = apply_step(scene, before, step)
    gid = event["group"]
    after_wrapped = file_authoring.read_scene(scene)
    after = group_state(scene, after_wrapped["document"], saved_elements)
    require(after != before, "CONFLICT", "Group action did not change saved state")
    before_protected = guarded_group_snapshot(wrapped, before, gid, baseline=before_ids,
                                              subset=before_subset, geometry=before_geometry)
    after_protected = guarded_group_snapshot(
        after_wrapped, after, gid, baseline=before_ids, subset=protect_subset(after_wrapped, {gid}, set()),
        geometry=joint_workflow.non_author_signature(scene))
    changed = sorted(key for key in before_protected if before_protected[key] != after_protected[key])
    require(not changed, "VALIDATION_FAILED", "Group action changed protected state: " + ", ".join(changed))
    after_author = author_sha(scene)
    after_scene = fingerprint(protect_subset(after_wrapped, set(), set()))
    retained = save_candidate(bpy, request)
    return {**response, "group_state": after, "group_event": event, "author_before_sha256": author,
            "author_after_sha256": after_author, "state_sha256": fingerprint(after), "scene_sha256": after_scene,
            "candidate_sha256": digest(request["stage"]), "retained_orphans": retained,
            "protected": {"status": "passed", "changed_sections": []}}
