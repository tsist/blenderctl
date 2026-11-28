"""Edit saved Pencil viewport and sequence controls through the native scene properties."""

import math
import os

from material_draft_handoff import fingerprint, require
from protocol import digest
import toon_viewport_lines_contract as contract


READ = "inspect-scene-controls"
WRITE = "edit-scene-controls"
TIMING = {"frame_start", "frame_end"}
SEQUENCE = {
    "sequence_use_scene_range", "sequence_start", "sequence_end",
    "sequence_step", "sequence_format", "sequence_elements",
}
BASIC = {
    "line_width", "line_color", "silhouette", "open_edges", "marked_edges", "material_edges",
    "wireframe_edges", "crease_edges", "crease_angle", "basic_line_set_id", "basic_elements_only",
}
PRESET = {"width_multiplier", "intersection_enabled"}
COMMON = {"enabled", "style_mode", "display_mode", "opacity", "visibility"}
PROPERTIES = BASIC | PRESET | COMMON | SEQUENCE
BOOL = {
    "enabled", "silhouette", "open_edges", "marked_edges", "material_edges", "wireframe_edges",
    "crease_edges", "intersection_enabled", "basic_elements_only", "sequence_use_scene_range", "sequence_elements",
}
FLOAT = {"line_width": (.1, 32), "opacity": (0, 1), "width_multiplier": (0, 8), "crease_angle": (0, 180)}
ENUM = {
    "style_mode": ("BASIC", "PRESET"),
    "visibility": ("VISIBLE", "HIDDEN", "BOTH"),
    "display_mode": ("OVERLAY", "LINES", "WHITE"),
    "sequence_format": ("PNG", "OPEN_EXR"),
}
INT = {
    "basic_line_set_id": (1, 8),
    "sequence_step": (1, 2_147_483_647),
    **{field: (-1048574, 1048574) for field in {"sequence_start", "sequence_end"} | TIMING},
}


def validate_patch(patch):
    require(isinstance(patch, dict) and 0 < len(patch) and patch.keys() <= PROPERTIES | TIMING,
            "INVALID_REQUEST", "Use a nonempty patch of saved Pencil scene controls")
    for field, value in patch.items():
        if field in BOOL:
            valid = type(value) is bool
        elif field in FLOAT:
            lo, hi = FLOAT[field]
            valid = contract.finite(value) and lo <= value <= hi
        elif field in INT:
            lo, hi = INT[field]
            valid = type(value) is int and lo <= value <= hi
        elif field in ENUM:
            valid = value in ENUM[field]
        else:
            valid = (isinstance(value, list) and len(value) == 3
                     and all(contract.finite(channel) and 0 <= channel <= 1 for channel in value))
        require(valid, "INVALID_REQUEST", "Invalid value for " + field)
    return patch


def normalize_request(document, editions):
    operation = document["operation"]
    extras = {"controls_patch", "expected_controls_sha256"}
    payload = {key: value for key, value in document.items() if key not in extras}
    require(not {"scope", "frames", "elements", "patch"} & payload.keys(),
            "INVALID_REQUEST", "Scene controls do not use group scope or render parameters")
    placeholder = {"operation": "inspect", "scope": {"groups": ["scene-controls"]}}
    result = contract.normalize_request({**payload, **placeholder}, editions)
    result.pop("scope")
    result["operation"] = operation
    if operation == WRITE:
        controls_sha = document.get("expected_controls_sha256")
        require("expected_author_sha256" in result and isinstance(controls_sha, str)
                and contract.SHA256.fullmatch(controls_sha), "INVALID_REQUEST",
                "Supply the saved author and scene-controls SHA-256 values")
        result.update(controls_patch=validate_patch(document.get("controls_patch")), expected_controls_sha256=controls_sha)
    else:
        require(not extras & document.keys(), "INVALID_REQUEST", "Inspection does not edit scene controls")
    return result


def values(scene):
    settings = scene.toon_viewport_preview
    state = {field: getattr(settings, field) for field in sorted(PROPERTIES)}
    state["line_color"] = list(state["line_color"])
    return {**state, **{field: getattr(scene, field) for field in sorted(TIMING)}}


def masked_scene_states(bpy, target):
    from inspection import custom_value, identity

    records = []
    for scene in sorted(bpy.data.scenes, key=lambda item: item.name_full):
        native = custom_value(scene.get("toon_viewport_preview")) or {}
        timing = [getattr(scene, field) for field in sorted(TIMING)] if scene != target else None
        if scene == target:
            for field in PROPERTIES:
                native.pop(field, None)
            for element in native.get("elements", []):
                element.pop("group_filters", None)
        records.append({"scene": identity(scene), "camera": identity(scene.camera) if scene.camera else None,
                        "native": native, "timing": timing,
                        "objects": sorted(scene.objects.keys()),
                        "frames": [scene.frame_current, scene.frame_subframe, scene.use_preview_range,
                                   scene.frame_preview_start, scene.frame_preview_end]})
    return records


def filter_state(settings):
    return {element.name: {group.name: group.enabled for group in element.group_filters}
            for element in settings.elements}


def apply(scene, patch, before):
    settings = scene.toon_viewport_preview
    proposed = {**before, **patch}
    mode = proposed["style_mode"]
    inactive = BASIC if mode == "PRESET" else PRESET
    require(not inactive & patch.keys(), "CONFLICT", "Patch contains fields for an inactive Pencil style")
    require(mode != "PRESET" or settings.preset_text is not None, "CONFLICT",
            "Load a saved PRESET document before selecting PRESET")
    require(proposed["frame_start"] <= proposed["frame_end"] and proposed["sequence_start"] <= proposed["sequence_end"],
            "INVALID_REQUEST", "Sequence and scene ranges require start <= end")
    old_filters = filter_state(settings)
    if mode != before["style_mode"]:
        settings.style_mode = mode
        target_groups = [group.name for group in settings.groups] if mode == "PRESET" else ["__BASIC__"]
        want = {item.name: {gid: old_filters[item.name].get(gid, not item.use_group_filter) for gid in target_groups}
                for item in settings.elements}
        require(filter_state(settings) == want, "VALIDATION_FAILED", "Mode switch did not reconcile saved element filters")
    for field in patch.keys() - TIMING - {"style_mode"}:
        setattr(settings, field, patch[field])
    for field in ("frame_end", "frame_start") if proposed["frame_start"] > before["frame_end"] else ("frame_start", "frame_end"):
        if field in patch:
            setattr(scene, field, patch[field])
    after = values(scene)
    for field, expected in proposed.items():
        actual = after[field]
        if field in FLOAT:
            matches = math.isclose(actual, expected, rel_tol=1e-7, abs_tol=1e-6)
        elif field == "line_color":
            matches = all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(actual, expected))
        else:
            matches = actual == expected
        require(matches, "VALIDATION_FAILED", "Saved Pencil setting did not round-trip: " + field)
    if mode == before["style_mode"]:
        require(filter_state(settings) == old_filters, "VALIDATION_FAILED", "An element group filter changed")
    return after


def work(bpy, context, request, author, *, bindings, dependencies, save_candidate, author_sha):
    scene = context.scene
    settings = scene.toon_viewport_preview
    operation = request["operation"]

    def protected():
        return fingerprint([masked_scene_states(bpy, scene), bindings(), dependencies()])

    def snapshot(state):
        return fingerprint([state, filter_state(settings)])

    before = values(scene)
    controls_sha = snapshot(before)
    scene_sha = protected()
    result = {"operation": operation, "controls": before, "controls_sha256": controls_sha,
              "state_sha256": controls_sha, "scene_sha256": scene_sha, "author_sha256": author,
              "context": request["context"], "product": request["product"],
              "camera": scene.camera.name_full if scene.camera else None,
              "source": {"file": request["file"], "sha256": request["expected_sha256"], "unchanged": True},
              "runtime": {"blender": bpy.app.version_string, "build": bpy.app.build_hash.decode(), "pid": os.getpid()}}
    if operation == READ:
        require(request.get("verify_state_sha256", controls_sha) == controls_sha
                and request.get("verify_scene_sha256", scene_sha) == scene_sha,
                "VALIDATION_FAILED", "Cold Pencil controls or protected scene changed")
        return result

    require(scene.is_editable and scene.library is None and scene.override_library is None,
            "UNSUPPORTED", "Scene controls require a local editable scene")
    require(request["expected_controls_sha256"] == controls_sha, "CONFLICT", "Saved Pencil controls SHA differs")
    after = apply(scene, request["controls_patch"], before)
    require(protected() == scene_sha, "VALIDATION_FAILED", "Changing Pencil controls affected protected data")
    saved_author = author_sha(scene)
    saved_state = snapshot(after)
    retained = save_candidate(bpy, request)
    return {**result, "controls": after, "controls_sha256": saved_state, "state_sha256": saved_state,
            "author_before_sha256": author, "author_after_sha256": saved_author,
            "changed": {key: {"before": before[key], "after": after[key]} for key in after if before[key] != after[key]},
            "candidate_sha256": digest(request["stage"]), "retained_orphans": retained,
            "protected": {"status": "passed", "scene_sha256": scene_sha, "changed_sections": []}}
