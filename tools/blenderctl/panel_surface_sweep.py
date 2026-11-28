"""Saved station preview and commit adapter for the 0.3.7 sweep core."""

import math
import re

from protocol import Failure

INSPECT = "inspect-sweep"
COMMIT = "commit-sweep"
OPERATIONS = {INSPECT, COMMIT}
EDITION = "0.3.7"
POSITIONS = "station_positions_mm"
_SHA256 = re.compile(r"[0-9a-f]{64}")


def validate(request, validate_name):
    if not validate_name(request["object"]):
        raise Failure("INVALID_REQUEST", "Name a managed sweep mesh or station control")
    if POSITIONS in request:
        positions = request[POSITIONS]
        if not isinstance(positions, dict) or len(positions) != 1:
            raise Failure("INVALID_REQUEST", f"{POSITIONS} must name one editable station")
        (station, coords), = positions.items()
        if not (validate_name(station) and isinstance(coords, list) and len(coords) == 3
                and all(type(v) is int or type(v) is float and math.isfinite(v) for v in coords)):
            raise Failure("INVALID_REQUEST", f"{POSITIONS} requires a station name and three finite world millimetres")
    if request["operation"] == COMMIT:
        expected = request["expected"]
        if not (isinstance(expected, dict) and set(expected) == {"preview_sha256"}
                and isinstance(expected["preview_sha256"], str)
                and _SHA256.fullmatch(expected["preview_sha256"])):
            raise Failure("INVALID_REQUEST", "commit-sweep requires expected.preview_sha256 from inspect-sweep")


def _resolve(context, requested):
    from hard_surface_workflow_addon import surface_sweep as api

    if context.mode != "OBJECT":
        raise Failure("CONFLICT", "Open the saved sweep in Object Mode")
    selected = context.view_layer.objects.get(requested)
    if selected is None:
        raise Failure("NOT_FOUND", f"Sweep mesh or station control is missing: {requested!r}")
    output = api.find_output(selected)
    if output is None or context.view_layer.objects.get(output.name) != output:
        raise Failure("CONFLICT", f"{requested!r} does not identify a managed sweep in the saved View Layer")
    try:
        target, params = api.params_from_controls(output)
    except (ValueError, KeyError, RuntimeError) as error:
        raise Failure("CONFLICT", str(error)) from error
    return api, output, target, params


def _pending_params(context, request):
    api, output, target, params = _resolve(context, request["object"])
    if POSITIONS in request:
        (name, coords), = request[POSITIONS].items()
        station = next((value for value in params["stations"] if value["name"] == name), None)
        if station is None:
            raise Failure("NOT_FOUND", f"Sweep station is missing: {name!r}")
        station["position_mm"] = coords
    return api, output, target, params


def inspect(context, request):
    api, output, target, params = _pending_params(context, request)
    try:
        report = api.preview_geometry(target, params)["report"]
    except (ValueError, RuntimeError) as error:
        raise Failure("VALIDATION_FAILED", str(error)) from error
    followup = {key: request[key] for key in ("file", "expected_sha256", "edition", "object")}
    if POSITIONS in request:
        followup[POSITIONS] = request[POSITIONS]
    followup.update(operation=COMMIT, expected={"preview_sha256": report["preview_signature"]})
    return {"object": request["object"], "managed_output": output.name,
            "sweep_preview": report, "commit_request": followup}


def commit(context, request):
    api, _, target, params = _pending_params(context, request)
    params["expected_preview_signature"] = request["expected"]["preview_sha256"]
    try:
        return api.execute(target, api.TOOL, params)
    except (ValueError, RuntimeError) as error:
        raise Failure("CONFLICT", str(error)) from error


def observe(context, requested, *, committed=False):
    from mathutils import Vector

    api, output, target, params = _resolve(context, requested)
    try:
        record = api._record(output)
        mesh_sha = api._mesh_hash(output.data)
    except (ValueError, KeyError, RuntimeError) as error:
        raise Failure("VALIDATION_FAILED", str(error)) from error
    if record["mesh_sha256"] != mesh_sha:
        raise Failure("CONFLICT", "The managed sweep mesh hash differs from its signed recipe")
    pending = []
    for saved, current in zip(record["recipe"]["stations"], params["stations"], strict=True):
        actual = current["position_mm"]
        if (Vector(actual) - Vector(saved["position_mm"])).length > api.COORD_EPS_MM:
            pending.append(current["name"])
    if committed and pending:
        raise Failure("VALIDATION_FAILED", f"Saved sweep has uncommitted stations: {', '.join(pending)}")
    return {"output": output.name, "target": target.name, "signature": record["signature"],
            "mesh_sha256": mesh_sha, "target_sha256": record["target_sha256"],
            "vertex_count": len(output.data.vertices), "face_count": len(output.data.polygons),
            "station_objects": record["controls"], "pending_stations": pending,
            "controls_mm": {station["name"]: station["position_mm"] for station in params["stations"]}}


def protection(meshes, scene_state, sweep):
    from hard_surface_workflow_addon.surface_sweep import RECIPE_KEY

    # Snapshots are fresh dictionaries. Mask only the managed mesh, its signed record,
    # and the position of named controls; parent objects and other scene data stay guarded.
    output = sweep["output"]
    meshes.pop(output)
    objects = scene_state["objects"]
    objects[output].pop("data")
    objects[output]["custom_properties"].pop(RECIPE_KEY)
    for name in sweep["station_objects"].values():
        for row in objects[name]["matrix_world"][:3]:
            row[3] = 0.0
    return {"mesh": meshes, "scene": scene_state}
