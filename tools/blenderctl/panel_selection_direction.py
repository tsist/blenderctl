"""Replay the public 0.3.2 path-direction and preview operators on a saved mesh."""

import json
import re

from protocol import Failure


OPERATION = "pick-path-direction"
FIELDS = {"selector", "ordered_indices"}
RESULT_FIELD = "selection_direction"
_DOMAIN = {"edge_loop": "EDGE", "edge_ring": "EDGE", "vertex_loop": "VERT", "face_loop": "FACE"}
_DOMAIN_MODE = {"VERT": (True, False, False), "EDGE": (False, True, False), "FACE": (False, False, True)}
_MAX_INDEX = 2**31 - 1
ALLOWED_PANEL_CHANGES = {
    "selector_source", "seed_edge", "seed_face", "direction_edge", "sample_path",
    "sample_start", "sample_toward", "sample_step", "sample_phase", "sample_limit",
    "preview_json", "preview_source", "preview_object_name", "preview_object_uid",
    "preview_mesh_name", "preview_mesh_uid", "preview_domain", "preview_hash", "preview_count",
    "preview_indices", "preview_order", "needs_preview", "status", "status_kind",
}


def _require(condition, code, message):
    if not condition:
        raise Failure(code, message)


def _index(value):
    return type(value) is int and 0 <= value <= _MAX_INDEX


def _edge(value):
    return _index(value) or (isinstance(value, list) and len(value) == 2
                             and all(_index(item) for item in value) and value[0] != value[1])


def normalize_fields(params):
    selector = params.get("selector")
    _require(isinstance(selector, dict) and isinstance(selector.get("kind"), str)
             and selector["kind"] in _DOMAIN, "INVALID_REQUEST", "Path selection requires a supported selector.kind")
    kind = selector["kind"]
    seeds = {"seed_face", "direction_edge"} if kind == "face_loop" else {"seed_edge"}
    fields = {"kind", "sampling"} | seeds
    _require(fields <= set(selector) <= fields | {"topology_sha256"}, "INVALID_REQUEST",
             "Path selector requires explicit topology seeds and sampling; direction comes from ordered picks")
    if kind == "face_loop":
        _require(_index(selector["seed_face"]) and _edge(selector["direction_edge"]), "INVALID_REQUEST",
                 "Face path needs seed_face and an edge index or two endpoints for direction_edge")
    else:
        _require(_edge(selector["seed_edge"]), "INVALID_REQUEST", "Path needs a seed edge index or two endpoints")
    if "topology_sha256" in selector:
        sha = selector["topology_sha256"]
        _require(isinstance(sha, str) and re.fullmatch("[0-9a-f]{64}", sha), "INVALID_REQUEST",
                 "topology_sha256 must be a lowercase SHA-256")
    settings = selector["sampling"]
    _require(isinstance(settings, dict) and "step" in settings and set(settings) <= {"step", "phase", "limit"}
             and all(_index(item) for item in settings.values()), "INVALID_REQUEST",
             "sampling requires integer step and accepts phase and limit")
    step, phase, limit = settings["step"], settings.get("phase", 0), settings.get("limit")
    _require(0 <= phase < step and (limit is None or limit > 0), "INVALID_REQUEST",
             "sampling requires step > 0, phase < step, and a positive optional limit")
    sampling = {"step": step, "phase": phase}
    if limit is not None:
        sampling["limit"] = limit
    result = {"selector": {**selector, "sampling": sampling}}
    if "ordered_indices" in params:
        picked = params["ordered_indices"]
        _require(isinstance(picked, list) and 1 <= len(picked) <= 2 and all(_index(item) for item in picked)
                 and len(set(picked)) == len(picked), "INVALID_REQUEST",
                 "ordered_indices must contain one endpoint or two distinct, ordered adjacent indices")
        result["ordered_indices"] = picked
    return result


def _target(context, request):
    settings, target = context.scene.hsw_settings, context.view_layer.objects.active
    _require(context.mode == "EDIT_MESH" and target is not None and target.type == "MESH" and target.mode == "EDIT"
             and list(context.objects_in_mode) == [target] and target.name == request["object"]
             and (settings.target_object is None or settings.target_object == target)
             and not target.library and not target.override_library and not target.data.library
             and not target.data.override_library, "CONFLICT",
             "Selection handoff requires the named panel target as the only active local Edit Mesh")
    _require(target.data.users - int(target.data.use_fake_user) == 1, "UNSUPPORTED",
             "Path preview cannot change the selection of a shared mesh")
    return settings, target


def _resolve(selection, target, selector):
    try:
        return selection.resolve(target, selector)
    except (ValueError, RuntimeError) as exc:
        raise Failure("CONFLICT" if "stale" in str(exc).lower() else "INVALID_REQUEST", str(exc)) from exc


def _ordered_saved(current):
    order = current.get("selection_history", current["indices"])
    _require(1 <= len(order) <= 2 and sorted(order) == current["indices"], "CONFLICT",
             "Saved Edit Mesh must have a complete selection history for one endpoint or two adjacent picks")
    return order


def _direction_selector(selector, order, *, include_topology=True):
    fields = dict(selector)
    sampling = {**selector["sampling"], "start": order[0]}
    if len(order) == 2:
        sampling["toward"] = order[1]
    if not include_topology:
        fields.pop("topology_sha256", None)
    return {**fields, "sampling": sampling}


def _run_operator(operator, name, **properties):
    _require(operator.poll(), "UNSUPPORTED", f"Native {name} is unavailable in this Edit Mesh context")
    try:
        resolved = operator(**properties)
    except RuntimeError as exc:
        raise Failure("INVALID_REQUEST", f"Native {name} failed: {exc}") from exc
    _require(resolved == {"FINISHED"}, "VALIDATION_FAILED", f"Native {name} returned {sorted(resolved)}")
    return {"id": name, "result": sorted(resolved)}


def _set_inputs(settings, selector):
    settings.selector_source = selector["kind"]
    if selector["kind"] == "face_loop":
        settings.seed_face = selector["seed_face"]
        settings.direction_edge = json.dumps(selector["direction_edge"])
    else:
        settings.seed_edge = json.dumps(selector["seed_edge"])
    settings.sample_path = True
    settings.sample_step = selector["sampling"]["step"]
    settings.sample_phase = selector["sampling"]["phase"]
    settings.sample_limit = selector["sampling"].get("limit", 0)


def _read_json(raw, label):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise Failure("VALIDATION_FAILED", f"Native {label} is unreadable") from exc
    _require(isinstance(value, dict), "VALIDATION_FAILED", f"Native {label} is not an object")
    return value


def _verify_preview_lock(settings, target, selection, expected_source, expected):
    domain = expected["domain"]
    native_source = _read_json(settings.preview_source, "preview source")
    _require(native_source == expected_source and not settings.needs_preview,
             "VALIDATION_FAILED", "The persisted preview is not locked to the requested selection source")
    resolved = _read_json(settings.preview_json, "preview selection")
    cached = {key: value for key, value in expected.items() if key != "sampling"}
    _require(resolved == cached and settings.preview_object_name == target.name and settings.preview_mesh_name == target.data.name
             and settings.preview_domain == domain and settings.preview_count == len(resolved["indices"])
             and settings.preview_hash == resolved["topology_sha256"],
             "VALIDATION_FAILED", "Native preview members or target changed")
    object_uid = int(getattr(target, "session_uid", 0) or 0)
    mesh_uid = int(getattr(target.data, "session_uid", 0) or 0)
    _require(not settings.preview_object_uid or settings.preview_object_uid == object_uid,
             "VALIDATION_FAILED", "Native locked preview object identity did not persist")
    _require(not settings.preview_mesh_uid or settings.preview_mesh_uid == mesh_uid,
             "VALIDATION_FAILED", "Native locked preview mesh identity did not persist")
    current = _resolve(selection, target, {"kind": "current", "domain": domain})
    _require(current["indices"] == resolved["indices"]
             and current.get("selection_history", current["indices"]) == resolved.get("selection_history", resolved["indices"]),
             "VALIDATION_FAILED", "The native selection does not match the locked members and order")
    return {"preview_source": native_source, "resolved": expected,
            "target": {"object": target.name, "mesh": target.data.name}}


def readback(context, request):
    """Confirm the saved lock can still be used with the selected object and topology."""
    from hard_surface_workflow_addon import selection

    settings, target = _target(context, request)
    selector = request["selector"]
    domain = _DOMAIN[selector["kind"]]
    _require(tuple(context.tool_settings.mesh_select_mode) == _DOMAIN_MODE[domain] and settings.sample_path
             and settings.selector_source == selector["kind"] and settings.sample_start >= 0,
             "VALIDATION_FAILED", "Native path direction and edit domain did not persist")
    order = [settings.sample_start]
    if settings.sample_toward >= 0:
        order.append(settings.sample_toward)
    if "ordered_indices" in request:
        _require(order == request["ordered_indices"], "VALIDATION_FAILED", "Native ordered picks differ from the request")
    expected_source = _direction_selector(selector, order, include_topology=False)
    expected = _resolve(selection, target, _direction_selector(selector, order))
    return {"direction": {"domain": domain, "start": order[0], "toward": order[1] if len(order) == 2 else None},
            **_verify_preview_lock(settings, target, selection, expected_source, expected)}


def apply(context, request, before_mode):
    import bpy
    from hard_surface_workflow_addon import selection

    settings, target = _target(context, request)
    selector = request["selector"]
    domain = _DOMAIN[selector["kind"]]
    _require(before_mode["target"]["object"] == target.name, "CONFLICT", "The saved mode target changed")
    if "ordered_indices" in request:
        order = request["ordered_indices"]
        picked = _resolve(selection, target, {"kind": "indices", "domain": domain, "indices": order})
        input_kind = "explicit_order"
    else:
        _require(tuple(context.tool_settings.mesh_select_mode) == _DOMAIN_MODE[domain], "CONFLICT",
                 "Saved pick history must use the requested path's single edit domain")
        order = _ordered_saved(_resolve(selection, target, {"kind": "current", "domain": domain}))
        picked = None
        input_kind = "saved_edit_history"
    _resolve(selection, target, _direction_selector(selector, order))
    _set_inputs(settings, selector)
    if picked is not None:
        context.tool_settings.mesh_select_mode = _DOMAIN_MODE[domain]
        try:
            selection.apply(target, picked)
        except (ValueError, RuntimeError) as exc:
            raise Failure("VALIDATION_FAILED", f"Could not replay the ordered picks: {exc}") from exc
    native = _run_operator(bpy.ops.hard_surface.pick_path_direction, "hard_surface.pick_path_direction")
    _require(settings.sample_start == order[0] and settings.sample_toward == (order[1] if len(order) == 2 else -1),
             "VALIDATION_FAILED", "Native path direction did not record the exact ordered picks")
    native["preview"] = _run_operator(bpy.ops.hard_surface.preview_selection, "hard_surface.preview_selection",
                                      source=selector["kind"])
    return {"operator": native, "setup": {"ordered_indices": order, "input_kind": input_kind},
            "evidence": readback(context, request)}
