"""Public replay of native Toon Line sockets and collection merge domains."""

import os

from material_draft_handoff import fingerprint, require
from protocol import Failure, digest
import toon_viewport_lines_contract as contract


READ = "inspect-line-hierarchy"
WRITE = "manage-line-hierarchy"
LINE_FIELDS = {
    "name", "priority", "enabled", "size_mode", "antialiasing",
    "oversampling", "off_screen_distance", "elements_only",
}
ACTIONS = {
    "new": (set(), LINE_FIELDS - {"enabled"}),
    "set": ({"line"}, LINE_FIELDS),
    "connect": ({"line", "group", "index"}, set()),
    "move": ({"line", "index", "direction"}, set()),
    "socket": ({"line"}, set()),
    "shrink": ({"line"}, set()),
    "remove": ({"line"}, set()),
    "merge_group": ({"group", "visible", "hidden"}, set()),
    "merge_collection": ({"collection", "domain"}, set()),
}


def normalize_request(document, editions):
    operation = document["operation"]
    require(not {"scope", "frames", "elements", "patch"} & document.keys(),
            "INVALID_REQUEST", "Line hierarchy uses saved PRESET identities")
    payload = {k: v for k, v in document.items() if k != "line_action"}
    # Use the existing validation for the saved source, exact context and author SHA.
    if operation == READ:
        require("line_action" not in document, "INVALID_REQUEST", "Inspection accepts no line action")
        payload.update(operation="inspect", scope={"groups": ["line-hierarchy"]})
    else:
        payload["operation"] = "render-camera"
    result = contract.normalize_request(payload, editions)
    result.pop("scope", None)
    result["operation"] = operation
    if operation == WRITE:
        result["line_action"] = normalize_action(document.get("line_action"))
    return result


def normalize_action(step):
    require(isinstance(step, dict), "INVALID_REQUEST", "Expected one line_action object")
    action = step.get("action")
    require(isinstance(action, str) and action in ACTIONS, "INVALID_REQUEST", "Unknown Line action")
    required, optional = ACTIONS[action]
    fields = step.keys() - {"action"}
    require(required <= fields <= required | optional, "INVALID_REQUEST", "Unexpected fields for Line action " + action)
    require(action != "set" or fields & LINE_FIELDS, "INVALID_REQUEST", "Line set requires at least one value")

    for field in ("line", "group", "name"):
        if field in step:
            require(contract.name(step[field], 128), "INVALID_REQUEST", "Invalid Line " + field)
    for field in ("enabled", "elements_only", "visible", "hidden"):
        if field in step:
            require(type(step[field]) is bool, "INVALID_REQUEST", field + " must be boolean")
    for field, low, high in (("index", 0, 63), ("priority", -1000000, 1000000)):
        if field in step:
            require(type(step[field]) is int and low <= step[field] <= high,
                    "INVALID_REQUEST", "Line " + field + " is out of range")
    if "direction" in step:
        require(type(step["direction"]) is int and step["direction"] in (-1, 1),
                "INVALID_REQUEST", "Line move direction must be -1 or 1")
    if "size_mode" in step:
        require(step["size_mode"] in (None, "ABSOLUTE", "RELATIVE"), "INVALID_REQUEST", "Invalid Line size_mode")
    if step.get("oversampling") is not None:
        count = step["oversampling"]
        require(type(count) is int and count in (1, 2, 4), "INVALID_REQUEST", "Invalid Line oversampling")
    for field, high in (("antialiasing", 16), ("off_screen_distance", 65536)):
        value = step.get(field)
        require(value is None or type(value) in (int, float) and 0 <= value <= high,
                "INVALID_REQUEST", "Line " + field + " is out of range")
    if "collection" in step:
        contract.ref(step["collection"], "Merge collection")
        require(step["domain"] is None or contract.name(step["domain"], 128),
                "INVALID_REQUEST", "Use a named merge domain or null to remove membership")
    return dict(step)


def local_collections(scene):
    pending = list(scene.collection.children)
    found = set()
    visited = set()
    while pending:
        collection = pending.pop()
        if collection in visited:
            continue
        visited.add(collection)
        if collection.library is None:
            found.add(collection.name)
        pending.extend(collection.children)
    return [{"name": name, "library": None} for name in sorted(found)]


def hierarchy_state(scene, wrapped):
    from toon_workflow.toon_viewport_preview.line_hierarchy import records

    return {
        "hierarchy": records(wrapped["document"]),
        "groups": [{"id": group.name, "label": group.label} for group in scene.toon_viewport_preview.groups],
        "collections": local_collections(scene),
    }


def protected_state(scene, scene_dependencies, saved_elements):
    from toon_workflow.toon_workflow_addon import joint_workflow

    author = joint_workflow.capture_author(scene)
    document = author["document"]["document"]
    document["request"].pop("line_hierarchy", None)
    document["inputs"]["projection"]["editor_document"].pop("revision", None)
    # Native Line transactions materialize these legacy defaults. Compare their
    # effective values while continuing to protect every explicit author value.
    from toon_workflow.toon_viewport_preview import chain_controls, dash_controls, map_amount_controls, source_bindings

    bindings = source_bindings.recover(document)
    defaults = {
        "chain_controls": chain_controls.seed_controls,
        "dash_patterns": dash_controls.defaults,
        "map_amounts": lambda bindings: map_amount_controls.normalize(bindings, map_amount_controls.defaults(bindings)),
    }
    for controls in (author["controls"], author["document"]["scene_controls"]):
        for field, default in defaults.items():
            if controls[field] is None:
                controls[field] = default(bindings)
    return {
        "author": author,
        "elements": saved_elements(scene.toon_viewport_preview),
        "geometry_and_bindings": joint_workflow.non_author_signature(scene),
        "dependencies": scene_dependencies(),
    }


def apply_action(scene, state, step):
    from toon_workflow.toon_viewport_preview import curve_editor, line_ui

    settings = scene.toon_viewport_preview
    require(scene.library is None and scene.override_library is None and scene.is_editable,
            "CONFLICT", "Line editing requires a local editable scene")
    text = settings.preset_text
    require(text.library is None and text.get("toon_viewport_preview_owned"),
            "UNSUPPORTED", "Line editing requires an owned local PRESET")
    tree = settings.curve_tree
    if tree is not None and tree.get("toon_viewport_curve_role") == curve_editor.ROLE:
        require(curve_editor.signature(tree, settings) == tree.get("last_signature"),
                "CONFLICT", "Commit the saved curve edit before editing Line hierarchy")
    old = state["hierarchy"]
    if "line" in step:
        require(any(row["id"] == step["line"] for row in old["lines"]),
                "NOT_FOUND", "Unknown saved Line ID: " + step["line"])
    if "group" in step:
        require(any(row["id"] == step["group"] for row in state["groups"]),
                "NOT_FOUND", "Unknown saved group ID: " + step["group"])
    if "collection" in step:
        require(step["collection"] in state["collections"], "NOT_FOUND",
                "Missing local scene collection: " + step["collection"]["name"])
    try:
        event = line_ui.set_hierarchy(scene, step)
    except ValueError as error:
        raise Failure("CONFLICT", str(error)) from error
    require(event.get("action") == step["action"] and event.get("protected_source_identity_exact") is True,
            "VALIDATION_FAILED", "Native Line operation failed its protected brush check")
    return event


def work(bpy, context, request, wrapped, author, *, scene_dependencies, saved_elements, save_candidate, author_sha):
    from toon_workflow.toon_viewport_preview import file_authoring

    scene = context.scene
    state = hierarchy_state(scene, wrapped)
    protected = protected_state(scene, scene_dependencies, saved_elements)
    protected_sha = fingerprint(protected)
    operation = request["operation"]
    result = {
        "operation": operation,
        "product": request["product"],
        "context": request["context"],
        "source": {"file": request["file"], "sha256": request["expected_sha256"], "unchanged": True},
        "runtime": {"blender": bpy.app.version_string, "build": bpy.app.build_hash.decode(), "pid": os.getpid()},
    }
    if operation == READ:
        state_sha = fingerprint(state)
        if "verify_state_sha256" in request:
            require(state_sha == request["verify_state_sha256"] and protected_sha == request["verify_scene_sha256"],
                    "VALIDATION_FAILED", "Cold reopen altered Line hierarchy or protected scene state")
        require(digest(request["file"]) == request["expected_sha256"], "CONFLICT", "Source changed while reading Lines")
        return {**result, "author_sha256": author, "state_sha256": state_sha, "scene_sha256": protected_sha, **state}

    step = request["line_action"]
    event = apply_action(scene, state, step)
    after = hierarchy_state(scene, file_authoring.read_scene(scene))
    require(state["hierarchy"] != after["hierarchy"], "CONFLICT", "Line action did not change the saved hierarchy")
    before_rows = {row["id"]: row for row in state["hierarchy"]["lines"]}
    after_rows = {row["id"]: row for row in after["hierarchy"]["lines"]}
    if step["action"] == "new":
        added = after_rows.keys() - before_rows.keys()
        require(len(added) == 1, "VALIDATION_FAILED", "Expected exactly one new Line identity")
        event["line"] = added.pop()
    elif "line" in step:
        event["line"] = step["line"]
    changed = protected_state(scene, scene_dependencies, saved_elements)
    differences = sorted(key for key in protected if fingerprint(protected[key]) != fingerprint(changed[key]))
    require(not differences, "VALIDATION_FAILED", "Line editing changed protected state: " + ", ".join(differences))
    after_author = author_sha(scene)
    retained = save_candidate(bpy, request)
    return {
        **result, **after, "line_event": event,
        "author_before_sha256": author, "author_after_sha256": after_author,
        "state_sha256": fingerprint(after), "scene_sha256": protected_sha,
        "candidate_sha256": digest(request["stage"]), "retained_orphans": retained,
        "protected": {"status": "passed", "scene_sha256": protected_sha, "changed_sections": []},
    }
