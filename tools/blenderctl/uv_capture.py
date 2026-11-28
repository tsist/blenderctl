"""Prepare a replayable uv.run edit from a saved UV selection or captured binding."""

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import uuid

# Blender executes this file as a script with its own Python path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from uv_saved_reader import new_destination, run_saved_reader


COMMAND = "uv.capture-selection"
WORKER_MODE = "--uv-capture-worker"
REFERENCE_STAGE = "stack_reference"
CAPTURE_FIELDS = {
    "file", "expected_sha256", "resources", "scene", "view_layer", "candidate",
    "objects", "source", "domain", "action",
}
SELECTION_ACTIONS = {"stitch": ("pairs", "stitch_pairs"), "orient": ("edge_loops", "edge_loops")}


def prepare_request(value):
    from protocol import Failure
    from uv_workflow_contract import normalize_params

    if not isinstance(value, dict) or set(value) != CAPTURE_FIELDS:
        raise Failure("INVALID_REQUEST", "Capture params require " + ", ".join(sorted(CAPTURE_FIELDS)))
    if value["source"] not in ("saved_selection", "recorded"):
        raise Failure("INVALID_REQUEST", "source must be saved_selection or recorded")
    if value["domain"] not in ("FACES", "ISLANDS", "LOOPS"):
        raise Failure("INVALID_REQUEST", "domain must be FACES, ISLANDS or LOOPS")
    action = value["action"]
    if not isinstance(action, dict) or not isinstance(action.get("kind"), str) or not action["kind"]:
        raise Failure("INVALID_REQUEST", "action.kind is required")
    kind = action["kind"]
    if kind == REFERENCE_STAGE and (
        value["source"] != "saved_selection" or value["domain"] != "ISLANDS" or set(action) != {"kind"}
    ):
        raise Failure("INVALID_REQUEST", "Stack reference capture requires saved_selection, ISLANDS and action.kind")
    if kind == "stack":
        if value["domain"] != "ISLANDS" or set(action) != {"kind", "reference", "mirrored", "group_id"}:
            raise Failure("INVALID_REQUEST", "Stack requires ISLANDS, the expected reference, mirrored and group_id")
        ref = action["reference"]
        if (not isinstance(ref, dict) or set(ref) != {"object", "face"} or
            not isinstance(ref["object"], str) or not ref["object"] or type(ref["face"]) is not int or ref["face"] < 0 or
            type(action["mirrored"]) is not bool):
            raise Failure("INVALID_REQUEST", "Stack reference requires an object name and face index, and mirrored is boolean")
    if kind in SELECTION_ACTIONS and SELECTION_ACTIONS[kind][0] in action:
        raise Failure("INVALID_REQUEST", "Action selection data must come from the saved Blender capture")

    # Reuse the UV contract's source, resources, and explicit scene/object validation.
    base = normalize_params({
        "file": value["file"], "expected_sha256": value["expected_sha256"],
        "resources": value["resources"],
        "manifest": {
            "version": "1.0", "operation": "inspect",
            **{key: value[key] for key in ("scene", "view_layer", "candidate", "objects")},
        },
    }, "uv.inspect")
    return {"base": base, "source": value["source"], "domain": value["domain"], "action": action}


def saved_draft(encoded, label):
    from protocol import Failure

    try:
        draft = json.loads(encoded or "{}")
        if isinstance(draft, dict) and draft:
            return draft
    except (TypeError, ValueError):
        pass
    raise Failure("CONFLICT", f"The working copy has no valid {label}; return to the saved selection")


def capture(request, snapshot):
    import bpy
    from protocol import Failure
    from uv_workflow import _activate, _check_identity, _open_source, _resources, _targets
    from uv_workflow_addon import core, editing, ui
    from uv_workflow_contract import normalize_params

    if not bpy.app.background or bpy.app.online_access:
        raise Failure("UNSUPPORTED", "UV capture requires offline background Blender")
    base = request["base"]
    _check_identity(base)
    _open_source(base)
    if not bpy.data.filepath or Path(bpy.data.filepath).resolve() != Path(base["file"]):
        raise Failure("VALIDATION_FAILED", "Blender did not reopen the specified working copy")

    context = bpy.context
    manifest = base["manifest"]
    mode_before = context.mode
    matching_context = context.scene.name == manifest["scene"] and context.view_layer.name == manifest["view_layer"]
    if request["source"] == "saved_selection":
        if not matching_context or mode_before != "EDIT_MESH":
            raise Failure("CONFLICT", "Save the named scene and view layer in Edit Mode with the intended selection")
    elif mode_before != "OBJECT":
        raise Failure("CONFLICT", "A recorded selection requires a saved Object Mode working copy")
    scene = context.scene if matching_context else _activate(manifest)

    # Setting the candidate refreshes panel fields and clears GUI drafts.
    # Preserve the disk capture before that in-memory update.
    stored_selection = scene.get("uvw_edit_selection")
    stored_reference = scene.get("uvw_stack_reference")
    ui.register()
    candidate = manifest["candidate"]
    record = core.state(scene)["candidates"].get(candidate)
    if record is None or (scene.uvw_candidate and scene.uvw_candidate != candidate):
        raise Failure("CONFLICT", "The saved UV candidate differs from the requested candidate")
    scene.uvw_candidate = candidate
    selected_names = set(manifest["objects"])
    if not selected_names <= set(record["objects"]):
        raise Failure("CONFLICT", "The requested selection includes an object outside the UV candidate")
    targets = _targets(scene, {**manifest, "objects": record["objects"]})
    selected_targets = [obj for obj in targets if obj.name in selected_names]
    for obj in selected_targets:
        active = obj.data.uv_layers.active
        if active is None or active.name != record["layer"]:
            raise Failure("CONFLICT", f"Select the candidate's editing UV layer on {obj.name} before saving")

    action = request["action"].copy()
    kind = action["kind"]
    if request["source"] == "saved_selection":
        if not selected_names <= {obj.name for obj in context.objects_in_mode}:
            raise Failure("CONFLICT", "A requested selection object is outside the saved Edit Mode context")
        scene.uvw_edit_domain = request["domain"]
        try:
            scene.uvw_edit_action = "stack" if kind == REFERENCE_STAGE else kind
        except TypeError as exc:
            raise Failure("INVALID_REQUEST", f"Unknown UV edit action {kind}") from exc
        if kind == "stack":
            reference = saved_draft(stored_reference, "stack reference")
            if reference.get("reference") != action["reference"]:
                raise Failure("CONFLICT", "Saved stack reference differs from the requested object and face")
            scene["uvw_stack_reference"] = stored_reference
            scene.uvw_stack_mirrored = action["mirrored"]
        operator = {
            "stitch": bpy.ops.uvw.capture_stitch,
            "orient": bpy.ops.uvw.capture_direction,
            REFERENCE_STAGE: bpy.ops.uvw.stack_reference,
            "stack": bpy.ops.uvw.stack_moving,
        }.get(kind, bpy.ops.uvw.capture_edit_selection)
        try:
            captured = operator()
        except RuntimeError as exc:
            raise Failure("CONFLICT", str(exc)) from exc
        if captured != {"FINISHED"} or context.mode != "OBJECT":
            raise Failure("CONFLICT", "Blender could not capture the requested saved selection")
        stored_selection = scene.get("uvw_edit_selection")
        stored_reference = scene.get("uvw_stack_reference")

    selection = saved_draft(stored_reference if kind == REFERENCE_STAGE else stored_selection,
                            "stack reference" if kind == REFERENCE_STAGE else "recorded selection")
    try:
        binding = selection["binding"]
        if kind == REFERENCE_STAGE:
            reference = selection["reference"]
            scope = {reference["object"]: {"domain": "ISLANDS", "indices": [reference["face"]]}}
        else:
            scope = selection["selection"]
        domain_matches = all(row["domain"] == request["domain"] for row in scope.values())
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise Failure("CONFLICT", "The file has no valid recorded selection for the requested action") from exc
    if set(scope) != selected_names or not domain_matches:
        raise Failure("CONFLICT", "The recorded objects or selection domain differ from the request")

    bindings = [binding]
    if kind in SELECTION_ACTIONS:
        action_field, capture_field = SELECTION_ACTIONS[kind]
        if capture_field not in selection:
            raise Failure("CONFLICT", f"Saved selection has no {kind} capture")
        action[action_field] = selection[capture_field]
    if kind == "stack":
        stack = selection.get("stack_draft")
        if (not isinstance(stack, dict) or stack.get("reference") != action["reference"] or
            stack.get("mirrored") is not action["mirrored"]):
            raise Failure("CONFLICT", "Save a moving-island draft with the expected reference and mirrored mode")
        action["pairs"] = stack.get("pairs")
        bindings.append(stack.get("reference_binding"))

    if kind == REFERENCE_STAGE:
        handoff = {"stack_reference": selection}
    else:
        edit = {
            "version": "1.0", "operation": "edit", "scene": manifest["scene"],
            "view_layer": manifest["view_layer"], "objects": record["objects"],
            "candidate": candidate, "selection": scope, "selection_binding": binding, "action": action,
        }
        handoff = {"params": normalize_params({**base, "manifest": edit}, "uv.run")}

    if any(obj.mode != "OBJECT" or obj.data.name != record.get("meshes", {}).get(obj.name) or
           obj.data.users != 1 or obj.library or obj.data.library or obj.override_library or
           record["layer"] not in obj.data.uv_layers for obj in targets):
        raise Failure("CONFLICT", "UV candidate target mesh identity or editing state changed")
    try:
        for recorded_binding in bindings:
            ui._check_binding(context, record, recorded_binding)
    except (AttributeError, ValueError) as exc:
        raise Failure("CONFLICT", str(exc)) from exc
    current = ui._binding(context, record, record["objects"])
    if current["geometry"] != record["geometry"]:
        raise Failure("CONFLICT", "Candidate geometry changed; create a new candidate and capture again")
    if current["uv_hash"] != record["uv_hash"]:
        raise Failure("CONFLICT", "Candidate UV coordinates or pins changed; revalidate and capture again")
    for obj in selected_targets:
        editing.selected_loops(obj, record["layer"], scope[obj.name])

    _resources(base)
    if snapshot is not None:
        bpy.context.preferences.filepaths.save_version = 0
        status = bpy.ops.wm.save_as_mainfile(
            filepath=str(snapshot), check_existing=False, copy=True, relative_remap=True,
        )
        if status != {"FINISHED"} or not snapshot.is_file():
            raise Failure("IO_ERROR", "Failed to save the Object Mode snapshot")

    return {
        **handoff, "source_mode": mode_before, "source": request["source"],
        "blender": {"version": bpy.app.version_string, "build": bpy.app.build_hash.decode()},
    }


def worker(request_path, result_path, snapshot_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from protocol import Failure, atomic_json, read_json
    from uv_workflow_contract import addon_contract

    try:
        result = {"data": capture(read_json(request_path), Path(snapshot_path) if snapshot_path else None)}
    except (Failure, addon_contract.WorkflowError) as exc:
        result = {"error": {"code": exc.code, "message": str(exc)}}
    atomic_json(result_path, result)


def run(blender, request, snapshot, timeout):
    return run_saved_reader(blender, request, __file__, WORKER_MODE, timeout, snapshot or "")


def main():
    from filesystem import FileGuard
    from protocol import CODES, Failure, atomic_json, envelope, read_json

    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise Failure("INVALID_REQUEST", message)

    parser = Parser(prog="blenderctl uv capture-selection", description=__doc__, allow_abbrev=False)
    parser.add_argument("--blender", type=Path, required=True, help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=30, help=argparse.SUPPRESS)
    parser.add_argument("--params", type=Path, required=True, help="Capture request JSON")
    parser.add_argument("--snapshot", type=Path, help="New Object Mode .blend for saved_selection")
    parser.add_argument("--output", type=Path, help="Write uv.run parameters to a new .json file")
    data, error = None, None
    staged_snapshot, staged_output = None, None
    try:
        args = parser.parse_args()
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise Failure("INVALID_REQUEST", "--timeout must be finite and positive")
        if not args.blender.is_absolute() or not args.blender.is_file():
            raise Failure("NOT_FOUND", f"Blender executable is not available at {args.blender}")
        request = prepare_request(read_json(args.params))
        snapshot = new_destination(args.snapshot, ".blend")
        output = new_destination(args.output, ".json")
        if (request["source"] == "saved_selection") != (snapshot is not None):
            raise Failure("INVALID_REQUEST", "Supply --snapshot exactly for source=saved_selection")
        if request["action"]["kind"] == REFERENCE_STAGE and output is not None:
            raise Failure("INVALID_REQUEST", "Stack reference capture emits a snapshot and receipt. Use --output for the moving capture")

        if snapshot:
            staged_snapshot = snapshot.with_name(f".uv-capture-{uuid.uuid4().hex}.blend")
        handoff = run(args.blender, request, staged_snapshot, args.timeout)
        if snapshot:
            with FileGuard(staged_snapshot, renameable=True) as guard:
                snapshot_sha256 = guard.sha256()
                guard.rename_new(snapshot)
            staged_snapshot = None
            handoff["snapshot"] = {"file": str(snapshot), "sha256": snapshot_sha256}
            if "params" in handoff:
                handoff["params"].update(file=str(snapshot), expected_sha256=snapshot_sha256)
        # The receipt stays replayable even if the separate params export fails.
        data = handoff
        if output:
            staged_output = output.with_name(f".uv-capture-{uuid.uuid4().hex}.json")
            atomic_json(staged_output, data["params"])
            with FileGuard(staged_output, renameable=True) as guard:
                guard.rename_new(output)
            staged_output = None
            data["params_file"] = str(output)
    except Failure as exc:
        error = {"code": exc.code, "message": str(exc)}
    except subprocess.TimeoutExpired:
        error = {"code": "TIMEOUT", "message": "Timed out while capturing the saved Blender selection"}
    except OSError as exc:
        error = {"code": "IO_ERROR", "message": str(exc)}
    finally:
        for staged in (staged_snapshot, staged_output):
            if staged:
                staged.unlink(missing_ok=True)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(envelope(COMMAND, data=data, error=error), ensure_ascii=False))
    return CODES[error["code"]] if error else 0


if __name__ == "__main__":
    if WORKER_MODE in sys.argv and "--background" in sys.argv:
        worker(*sys.argv[sys.argv.index(WORKER_MODE) + 1:])
    else:
        sys.exit(main())
