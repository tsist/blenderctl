# SPDX-License-Identifier: GPL-3.0-or-later
"""Route production requests through the qualified surface sweep add-on core."""

import re

from protocol import Failure


def _shape(request, required, optional=()):
    missing = required - request.keys()
    if missing:
        raise Failure("INVALID_REQUEST", "Missing hard-surface request fields: " + ", ".join(sorted(missing)))
    unexpected = request.keys() - required - set(optional)
    if unexpected:
        raise Failure("INVALID_REQUEST", "Unexpected hard-surface request fields: " + ", ".join(sorted(unexpected)))


def _name(value):
    return (isinstance(value, str) and value.strip() and len(value.encode("utf-8")) <= 63
            and all(ord(char) >= 32 and ord(char) != 127 for char in value))


def _signature(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value)


def validate_request(request):
    operation = request.get("operation")
    if operation == "describe":
        _shape(request, {"operation"})
        return
    if operation in ("preview", "execute"):
        _shape(request, {"operation", "object", "params"})
        if not _name(request["object"]) or not isinstance(request["params"], dict):
            raise Failure("INVALID_REQUEST", "A sweep requires a target object name and parameter object")
        if "preview" in request["params"]:
            raise Failure("INVALID_REQUEST", "Choose preview or execute in operation")
        if operation == "execute" and not _signature(request["params"].get("expected_preview_signature")):
            raise Failure("INVALID_REQUEST", "Execute requires expected_preview_signature from preview")
        return
    if operation in ("inspect-sweep", "commit-sweep"):
        mandatory = {"operation", "object"} | ({"expected_preview_signature"} if operation == "commit-sweep" else set())
        _shape(request, mandatory, {"station_positions_mm"})
        if not _name(request["object"]):
            raise Failure("INVALID_REQUEST", "Name a managed sweep or its named station control")
        if operation == "commit-sweep" and not _signature(request["expected_preview_signature"]):
            raise Failure("INVALID_REQUEST", "Commit requires expected_preview_signature from inspect-sweep")
        positions = request.get("station_positions_mm")
        if "station_positions_mm" in request:
            if not isinstance(positions, dict) or len(positions) != 1:
                raise Failure("INVALID_REQUEST", "station_positions_mm must name one editable station")
            name, coords = next(iter(positions.items()))
            if (not _name(name) or not isinstance(coords, list) or len(coords) != 3
                    or not all(type(axis) in (int, float) and -1e6 <= axis <= 1e6 for axis in coords)):
                raise Failure("INVALID_REQUEST", "Supply a station name and three finite world-millimetre coordinates")
        return
    raise Failure("INVALID_REQUEST", "Use describe, preview, execute, inspect-sweep or commit-sweep")


def _core():
    from hard_surface_workflow_addon import bl_info, surface_sweep

    if tuple(bl_info["version"]) != (0, 3, 7):
        raise Failure("UNSUPPORTED", "The hard-surface operation requires Hard Surface Workflow 0.3.7")
    return surface_sweep


def _saved(context, api, name):
    selected = context.view_layer.objects.get(name)
    if selected is None:
        raise Failure("NOT_FOUND", f"Sweep mesh or station is missing: {name!r}")
    output = api.find_output(selected)
    if output is None or context.view_layer.objects.get(output.name) != output:
        raise Failure("CONFLICT", f"{name!r} does not identify a managed sweep in this view layer")
    target, params = api.params_from_controls(output)
    return target, params


def apply(context, request):
    api = _core()
    operation = request["operation"]
    if operation == "describe":
        return {"status": "DESCRIBED", "catalog": api.catalog()}, False
    if context.mode != "OBJECT":
        raise Failure("CONFLICT", "Open the saved sweep in Object Mode")
    if operation in ("preview", "execute"):
        target = context.view_layer.objects.get(request["object"])
        if target is None:
            raise Failure("NOT_FOUND", f"Sweep target is missing: {request['object']!r}")
        params = request["params"]
    else:
        target, params = _saved(context, api, request["object"])
        if "station_positions_mm" in request:
            name, coords = next(iter(request["station_positions_mm"].items()))
            station = next((item for item in params["stations"] if item["name"] == name), None)
            if station is None:
                raise Failure("NOT_FOUND", f"Sweep station is missing: {name!r}")
            station["position_mm"] = coords
    if operation in ("preview", "inspect-sweep"):
        report = api.preview_geometry(target, params)["report"]
        if operation == "inspect-sweep":
            report["commit_request"] = {**request, "operation": "commit-sweep",
                                        "expected_preview_signature": report["preview_signature"]}
        return report, False
    if operation == "commit-sweep":
        params["expected_preview_signature"] = request["expected_preview_signature"]
    try:
        result = api.execute(target, api.TOOL, params)
    except ValueError as error:
        message = str(error)
        if "stale" in message.lower() or "preview" in message.lower():
            raise Failure("CONFLICT", message) from error
        raise
    return result, result["status"] in ("CREATED", "UPDATED")


def check_reopen(context, result):
    api = _core()
    target, params = _saved(context, api, result["output"])
    preview = api.preview_geometry(target, params)["report"]
    if preview["signature"] != result["signature"]:
        raise Failure("VALIDATION_FAILED", "The saved sweep signature differs after reopen")
    return {"output": preview["output"], "signature": preview["signature"]}
