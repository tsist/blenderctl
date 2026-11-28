"""Persist the native hard-surface selection preview for a single Edit Mesh."""

import json
import re
from struct import pack, unpack

from panel_selection_direction import (
    ALLOWED_PANEL_CHANGES as DIRECTION_FIELDS, _DOMAIN, _DOMAIN_MODE,
    _edge, _index, _require, _resolve, _run_operator, _target, _verify_preview_lock,
)


OPERATION = "preview-selection"
FIELDS = {"selector"}
RESULT_FIELD = "selection_preview"
PATH_PREVIEW_EDITIONS = frozenset({"0.3.2", "0.3.3", "0.3.4", "0.3.5", "0.3.6", "0.3.7"})
ALLOWED_PANEL_CHANGES = (DIRECTION_FIELDS - {"sample_start", "sample_toward", "sample_step", "sample_phase", "sample_limit"}) | {
    "current_domain", "indices_domain", "explicit_indices", "box_domain", "box_space", "box_minimum", "box_maximum",
}


def _point(value):
    return (isinstance(value, list) and len(value) == 3
            and all(type(axis) in (int, float) and -3.4028234663852886e38 <= axis <= 3.4028234663852886e38
                    for axis in value))


def normalize_fields(params):
    selector = params.get("selector")
    _require(isinstance(selector, dict) and isinstance(selector.get("kind"), str), "INVALID_REQUEST",
             "Preview requires a selector.kind")
    kind = selector["kind"]
    domain = selector.get("domain")
    fields = {"kind"}
    optional = {"topology_sha256"}
    if kind in {"current", "indices", "box"}:
        fields.add("domain")
        _require(isinstance(domain, str) and domain in _DOMAIN_MODE, "INVALID_REQUEST",
                 "Selection domain must be VERT, EDGE, or FACE")
        if kind == "indices":
            fields.add("indices")
            indices = selector.get("indices")
            _require(isinstance(indices, list) and indices and all(_index(value) for value in indices)
                     and len(set(indices)) == len(indices), "INVALID_REQUEST", "Preview indices must be nonempty and unique")
        if kind == "box":
            _require(params["edition"] in PATH_PREVIEW_EDITIONS, "UNSUPPORTED",
                     "Coordinate box preview requires a supported edition from 0.3.2 onward")
            fields.update(("space", "minimum", "maximum"))
            minimum, maximum = selector.get("minimum"), selector.get("maximum")
            _require(selector.get("space") in ("LOCAL", "WORLD") and _point(minimum) and _point(maximum)
                     and all(low <= high for low, high in zip(minimum, maximum, strict=True)),
                     "INVALID_REQUEST", "Box requires ordered XYZ limits and space LOCAL or WORLD")
    elif kind in _DOMAIN:
        optional.add("domain")
        _require("domain" not in selector or domain == _DOMAIN[kind], "INVALID_REQUEST", "Path domain differs from selector kind")
        if kind == "face_loop":
            fields.update(("seed_face", "direction_edge"))
            _require(_index(selector.get("seed_face")) and _edge(selector.get("direction_edge")), "INVALID_REQUEST",
                     "Face path requires a face index and a direction edge")
        else:
            fields.add("seed_edge")
            _require(_edge(selector.get("seed_edge")), "INVALID_REQUEST", "The path requires one explicit seed edge")
    else:
        _require(False, "INVALID_REQUEST", "Preview supports current, explicit indices, a single seeded path or a coordinate box")
    _require(fields <= set(selector) <= fields | optional, "INVALID_REQUEST",
             "Preview selector has unknown fields; sampled paths use pick-path-direction")
    normalized = {key: val for key, val in selector.items() if key in fields or key == "topology_sha256"}
    if "topology_sha256" in normalized:
        sha = normalized["topology_sha256"]
        _require(isinstance(sha, str) and re.fullmatch("[0-9a-f]{64}", sha), "INVALID_REQUEST",
                 "topology_sha256 must be a lowercase SHA-256")
    return {"selector": normalized}


def _native_selector(selector):
    fields = {key: value for key, value in selector.items() if key != "topology_sha256"}
    if fields["kind"] == "box":
        for name in ("minimum", "maximum"):
            fields[name] = [unpack("<f", pack("<f", axis))[0] for axis in fields[name]]
    return fields


def _set_inputs(settings, selector, edition):
    kind = selector["kind"]
    settings.selector_source = kind
    if kind == "current":
        settings.current_domain = selector["domain"]
    elif kind == "indices":
        settings.indices_domain = selector["domain"]
        settings.explicit_indices = json.dumps(selector["indices"])
    elif kind == "box":
        settings.box_domain = selector["domain"]
        settings.box_space = selector["space"]
        settings.box_minimum = selector["minimum"]
        settings.box_maximum = selector["maximum"]
    else:
        if edition in PATH_PREVIEW_EDITIONS:
            settings.sample_path = False
        if kind == "face_loop":
            settings.seed_face = selector["seed_face"]
            settings.direction_edge = json.dumps(selector["direction_edge"])
        else:
            settings.seed_edge = json.dumps(selector["seed_edge"])


def readback(context, request):
    from hard_surface_workflow_addon import selection

    settings, target = _target(context, request)
    selector = request["selector"]
    expected = _resolve(selection, target, selector)
    _require(settings.selector_source == selector["kind"]
             and tuple(context.tool_settings.mesh_select_mode) == _DOMAIN_MODE[expected["domain"]],
             "VALIDATION_FAILED", "Native preview changed the requested selector source or edit domain")
    return _verify_preview_lock(settings, target, selection, _native_selector(selector), expected)


def apply(context, request, before_mode):
    import bpy
    from hard_surface_workflow_addon import selection

    settings, target = _target(context, request)
    selector = request["selector"]
    _require(before_mode["target"]["object"] == target.name, "CONFLICT", "The saved mode target changed")
    expected = _resolve(selection, target, selector)
    domain_mode = _DOMAIN_MODE[expected["domain"]]
    if selector["kind"] == "current":
        _require(tuple(context.tool_settings.mesh_select_mode) == domain_mode, "CONFLICT",
                 "Current preview requires the requested saved single selection domain")
    else:
        _require(_resolve(selection, target, _native_selector(selector)) == expected, "INVALID_REQUEST",
                 "Requested selector changes members at native panel precision")
        context.tool_settings.mesh_select_mode = domain_mode
    _set_inputs(settings, selector, request["edition"])
    operator = _run_operator(bpy.ops.hard_surface.preview_selection, "hard_surface.preview_selection")
    proof = readback(context, request)
    _require(proof["resolved"] == expected, "VALIDATION_FAILED", "Native preview changed the requested selection")
    return {"operator": operator, "setup": {"selector_kind": selector["kind"]}, "evidence": proof}
