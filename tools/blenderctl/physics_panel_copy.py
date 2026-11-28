"""Save a Physics Workbench candidate that can be continued in the Blender panel."""

import argparse
from contextlib import ExitStack
import importlib
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
sys.path.insert(0, str(ENTRY.parent))

from filesystem import FileGuard, PathLocks
from material_draft_handoff import Parser, canonical, fingerprint, guard_file, read_document, require
from material_projection_handoff import error_payload, run_worker
from physics_pin import PACKAGE, guard_products, load_product
from protocol import CODES, DEFAULT_BLENDER, DEFAULT_JOBS, SCHEMA, Failure, atomic_json, digest, envelope, read_json, timeout_value


COMMAND = "physics.panel-copy"
OPERATIONS = {"inspect", "build", "copy", "reset-preset", "set-bridge-mode"}
WORKING_SOURCE = "pnw_working_copy_source"
PANEL_FIELDS = {"preset_id", "modifier_name", "protect_source", "bridge_mode"}
POINTER_FIELDS = {"collider_object": "objects", "collider_collection": "collections",
                  "instance_collection": "collections"}


def parser():
    cli = Parser(description=__doc__, allow_abbrev=False)
    cli.add_argument("--blender", type=Path, default=DEFAULT_BLENDER)
    cli.add_argument("--jobs-dir", type=Path, default=DEFAULT_JOBS)
    cli.add_argument("--transactions-dir", help=argparse.SUPPRESS)
    cli.add_argument("--timeout", type=float, default=120)
    display = cli.add_mutually_exclusive_group()
    for field in ("--compact", "--full", "--human"):
        display.add_argument(field, action="store_true")
    subparsers = cli.add_subparsers(required=True, parser_class=Parser)
    action = subparsers.add_parser("panel-copy", allow_abbrev=False, description=__doc__,
        epilog="Params: file, expected_sha256, scene, view_layer, object, operation. "
               "build follows the native protect_source setting and reuses a marked working object; "
               "copy makes a new native working copy; reset-preset resets draft parameters without applying them; "
               "inspect reads the saved draft. build/copy accept optional preset_id, modifier_name, protect_source, "
               "bridge_mode, collider_object, collider_collection, instance_collection and parameters. "
               "reset-preset accepts optional preset_id. set-bridge-mode saves only the solver dropdown: "
               "GN_NATIVE is available, XPBD_ASSET_BRIDGE, BULLET_BRIDGE and MANTAFLOW_BRIDGE are reserved. "
               "Editing takes a new --output .blend.")
    action.add_argument("--params", type=Path, required=True, help="Closed UTF-8 JSON request")
    action.add_argument("--output", type=Path, help="New absolute .blend for a writing operation")
    return cli


def name(value):
    return isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 63 and all(ord(c) >= 32 for c in value)


def read_inputs(args, stack):
    request = read_document(guard_file(args.params.absolute(), stack))
    common = {"file", "expected_sha256", "scene", "view_layer", "object", "operation"}
    require(isinstance(request, dict) and common <= request.keys(), "INVALID_REQUEST", "Missing saved physics panel fields")
    require(isinstance(request["file"], str) and request["file"], "INVALID_REQUEST", "Expected a saved project path")
    operation = request["operation"]
    require(isinstance(operation, str) and operation in OPERATIONS, "INVALID_REQUEST", "Unknown physics panel operation")
    if operation in ("build", "copy"):
        allowed = PANEL_FIELDS | POINTER_FIELDS.keys() | {"parameters"}
    else:
        allowed = {"inspect": set(), "reset-preset": {"preset_id"}, "set-bridge-mode": {"bridge_mode"}}[operation]
    require(operation != "set-bridge-mode" or "bridge_mode" in request, "INVALID_REQUEST",
            "set-bridge-mode requires the requested bridge_mode")
    require(request.keys() <= common | allowed, "INVALID_REQUEST", "Unexpected fields for the physics panel operation")
    require(all(name(request[field]) for field in ("scene", "view_layer", "object")), "INVALID_REQUEST",
            "Expected explicit scene, view layer and object names")
    for field in request.keys() & (PANEL_FIELDS - {"protect_source"}):
        require(name(request[field]), "INVALID_REQUEST", f"Invalid {field}")
    for field in request.keys() & POINTER_FIELDS.keys():
        require(request[field] is None or name(request[field]), "INVALID_REQUEST", f"Expected a name or null for {field}")
    require("protect_source" not in request or type(request["protect_source"]) is bool,
            "INVALID_REQUEST", "protect_source must be boolean")
    require(isinstance(request.get("parameters", {}), dict), "INVALID_REQUEST", "parameters must be an object")
    require((args.output is None) == (operation == "inspect"), "INVALID_REQUEST", "Provide --output only for an edit")
    source = guard_file(request["file"], stack, request["expected_sha256"])
    require(source.suffix.lower() == ".blend", "INVALID_REQUEST", "Expected an input .blend")
    request["file"] = str(source)
    return request


def active_context(bpy, path, request, target_name):
    require(bpy.ops.wm.open_mainfile(filepath=path, load_ui=True, use_scripts=False) == {"FINISHED"}
            and Path(bpy.data.filepath).samefile(path), "VALIDATION_FAILED", "Could not open the saved project")
    require(bpy.data.version[:2] == bpy.app.version[:2], "UNSUPPORTED", "Use a Blender version matching the saved major/minor")
    context = bpy.context
    require(context.mode == "OBJECT" and context.scene.name == request["scene"]
            and context.view_layer.name == request["view_layer"], "CONFLICT",
            "Saved project must open in the named scene and view layer in Object Mode")
    target = context.view_layer.objects.get(target_name)
    require(target is not None and target.type in {"MESH", "CURVE", "CURVES"}, "NOT_FOUND", "Expected a named mesh or curve in the view layer")
    for item in (target, target.data):
        require(item is not None and not (item.library or item.override_library), "UNSUPPORTED",
                "Use a local object and local geometry")
    return target


def physical_content(obj):
    from inspection import _attribute_fingerprint, record

    data = record(obj.data)
    # A copied datablock has its own identity. Compare its authored content.
    data.pop("name", None)
    if "curve" in data:
        data["curve"]["settings"].pop("name", None)
    if obj.type == "CURVES":
        data["curve_sizes"] = [len(curve.points) for curve in obj.data.curves]
        data["attributes"] = sorted((attr.name, attr.domain, attr.data_type, len(attr.data), _attribute_fingerprint(attr)[0])
                                    for attr in obj.data.attributes)
    slots = [{"link": slot.link, "material": record(slot.material) if slot.material else None}
             for slot in obj.material_slots]
    return fingerprint({"data": data, "materials": slots})


def observed(bpy, obj):
    from inspection import record
    from physics_nodes_workflow_addon.subgroups import tree_fingerprint

    settings = obj.pnw_settings
    context = bpy.context
    source = obj.get(WORKING_SOURCE)
    require(source is None or isinstance(source, str) and source and source != obj.name,
            "CONFLICT", "Invalid native physics working-copy source")
    parent = context.scene.objects.get(source) if source else None
    require(source is None or parent and parent.data != obj.data, "CONFLICT",
            "Native physics working-copy source is missing or shares the working geometry")
    draft = settings.to_request(obj, "build")
    expected_modifier = obj.modifiers.get(settings.modifier_name)
    managed = bool(expected_modifier and expected_modifier.type == "NODES" and expected_modifier.node_group
                   and expected_modifier.node_group.get("pnw_role") == "wrapper")
    states = {key: obj[key] for key in sorted(obj.keys()) if key.startswith("pnw_state_json__")}
    current = json.loads(states.get("pnw_state_json__" + settings.modifier_name, "{}"))
    graphs = {}
    for mod in obj.modifiers:
        tree = mod.node_group if mod.type == "NODES" else None
        if tree and tree.get("pnw_role") == "wrapper":
            core = tree.nodes.get("PNW_CoreNode")
            graphs[mod.name] = {"wrapper": tree_fingerprint(tree),
                                "core": tree_fingerprint(core.node_tree) if core and core.node_tree else None}
    next_action = "build_workflow"
    if managed and (current.get("preset_id") != settings.preset_id or current.get("bridge_mode") != draft["bridge_mode"]):
        next_action = "rebuild_workflow"
    return {
        "object": obj.name, "data": obj.data.name, "content_sha256": physical_content(obj),
        "working_copy_source": source, "protect_source": bool(settings.protect_source),
        "draft": draft, "managed_modifier": settings.modifier_name if managed else None,
        "solver_selection": {"panel_mode": settings.bridge_mode, "effective_mode": draft["bridge_mode"],
                             "reserved": draft["bridge_mode"] != "GN_NATIVE"},
        "wrapper": expected_modifier.node_group.name if managed else None, "next_panel_operation": next_action,
        "object_sha256": fingerprint(record(obj)), "managed_state_sha256": fingerprint(states),
        "managed_graphs": graphs,
        "materials": [{"link": slot.link, "name": slot.material.name if slot.material else None}
                      for slot in obj.material_slots],
        "source_object_sha256": fingerprint(record(parent)) if parent else None,
        "source_content_sha256": physical_content(parent) if parent else None,
        "active": context.view_layer.objects.active.name if context.view_layer.objects.active else None,
        "selected": sorted(item.name for item in context.selected_objects),
        "objects": sorted(item.name for item in context.scene.objects),
    }


def property_value(value):
    if hasattr(value, "to_list"):
        return tuple(value.to_list())
    if type(value).__name__ == "bpy_prop_array":
        return tuple(value)
    return value


def assert_value(actual, expected, field):
    if isinstance(expected, list):
        valid = isinstance(actual, (tuple, list)) and len(actual) == len(expected) and all(
            type(value) in (int, float) and math.isfinite(value) and math.isclose(got, value, abs_tol=1e-6, rel_tol=1e-6)
            for got, value in zip(actual, expected, strict=True))
    elif type(expected) is float:
        valid = math.isfinite(expected) and math.isclose(actual, expected, abs_tol=1e-6, rel_tol=1e-6)
    else:
        valid = type(actual) is type(expected) and actual == expected
    require(valid, "INVALID_REQUEST", f"Blender did not retain the requested panel value for {field}")


def set_fields(bpy, obj, request, contract):
    settings = obj.pnw_settings
    preset = request.get("preset_id", settings.preset_id)
    require(preset in contract.PRESET_DEFINITIONS, "INVALID_REQUEST", "Unknown Physics Workbench preset")
    parameters = request.get("parameters", {})
    require(parameters.keys() <= contract.PRESET_DEFINITIONS[preset]["defaults"].keys(),
            "INVALID_REQUEST", "Unknown panel parameter for this preset")
    fields = {key: request[key] for key in request.keys() & PANEL_FIELDS}
    for field in request.keys() & POINTER_FIELDS.keys():
        value = request[field]
        item = getattr(bpy.data, POINTER_FIELDS[field]).get(value) if value else None
        require(value is None or item is not None, "NOT_FOUND", f"Missing panel input {field}: {value}")
        fields[field] = item
    fields.update(parameters)
    originals = {key: (settings.is_property_set(key), property_value(getattr(settings, key))) for key in fields}
    for key, value in fields.items():
        if key in parameters:
            old = originals[key][1]
            if isinstance(old, tuple):
                valid = isinstance(value, list) and len(value) == len(old) and all(
                    type(item) in (int, float) and math.isfinite(item) for item in value)
            else:
                valid = type(value) is type(old) or type(old) is float and type(value) is int
                if valid and type(old) is float:
                    valid = math.isfinite(value)
                    value = float(value)
            require(valid, "INVALID_REQUEST", f"Invalid panel parameter type for {key}")
        try:
            setattr(settings, key, value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise Failure("INVALID_REQUEST", f"Invalid physics panel value for {key}: {exc}") from exc
        actual = property_value(getattr(settings, key))
        if key in POINTER_FIELDS:
            require(actual == value, "INVALID_REQUEST", f"Panel did not retain the requested {key}")
        else:
            assert_value(actual, value, key)
    return originals


def restore_fields(obj, originals, had_settings):
    settings = obj.pnw_settings
    for field, (was_set, value) in originals.items():
        if was_set:
            setattr(settings, field, value)
        else:
            settings.property_unset(field)
    if not had_settings and not settings.keys() and "pnw_settings" in obj:
        del obj["pnw_settings"]


def check_editable_graph(obj, draft):
    mod = obj.modifiers.get(draft["modifier_name"])
    require(mod is None or mod.type == "NODES" and mod.node_group and mod.node_group.get("pnw_role") == "wrapper",
            "CONFLICT", "Choose a new modifier name or an already managed Physics Workbench modifier")
    require(mod is None or not (mod.bake_directory or any(row.directory for row in mod.bakes)),
            "UNSUPPORTED", "A disk-backed simulation requires the dedicated Physics CLI cache workflow")
    return mod is not None


def apply_panel(bpy, target, request, before, contract):
    settings = target.pnw_settings
    operation = request["operation"]
    if operation == "set-bridge-mode":
        require(settings.preset_id != "rigid_box_tipping", "UNSUPPORTED",
                "The box-tipping preset has a fixed GN Native solver and hides the selector")
        require(request["bridge_mode"] in contract.BRIDGE_MODES, "INVALID_REQUEST", "Unknown solver mode")
        set_fields(bpy, target, request, contract)
        return target
    had_settings = "pnw_settings" in target
    originals = set_fields(bpy, target, request, contract)
    context = bpy.context
    context.view_layer.objects.active = target
    target.select_set(True)
    if operation == "reset-preset":
        require(bpy.ops.pnw.load_preset_defaults() == {"FINISHED"}, "WORKER_FAILED", "Preset reset did not finish")
        return target

    draft = settings.to_request(target, "build")
    try:
        contract.normalize_request(draft)
    except contract.PhysicsContractError as exc:
        raise Failure("INVALID_REQUEST", str(exc)) from exc
    managed = check_editable_graph(target, draft)
    is_copy = operation == "copy" or settings.protect_source and not before["working_copy_source"] and not managed
    if is_copy:
        require(not before["working_copy_source"] and not managed, "CONFLICT",
                "Reuse the existing managed object with build")
    op = bpy.ops.pnw.create_working_copy if operation == "copy" else bpy.ops.pnw.build_workflow
    require(op() == {"FINISHED"}, "WORKER_FAILED", "Native Physics Workbench operation did not finish")
    authored = context.view_layer.objects.active
    if is_copy:
        restore_fields(target, originals, had_settings)
        require(authored != target and authored.get(WORKING_SOURCE) == target.name and authored.data != target.data,
                "VALIDATION_FAILED", "The native operator did not make the expected working copy")
    else:
        require(authored == target, "VALIDATION_FAILED", "An existing work object was unexpectedly replaced")
    return authored


def work(phase, request_file):
    import bpy

    require(bpy.app.background and not bpy.app.online_access, "UNSUPPORTED", "Use the isolated offline Blender worker")
    record = read_json(request_file)
    request = record["params"]
    job = request_file.parent
    _, product = load_product(bpy, job, record["products"])
    package = importlib.import_module(PACKAGE)
    package.register()
    contract = importlib.import_module(PACKAGE + ".contract")
    runtime = {"blender": bpy.app.version_string, "build": bpy.app.build_hash.decode(), "pid": os.getpid()}
    if phase == "verify":
        authored = read_json(job / "author-result.json")["data"]
        target = active_context(bpy, request["stage"], request, authored["panel"]["object"])
        return {"runtime": runtime, "product": product, "panel": observed(bpy, target),
                "sha256": digest(Path(request["stage"]))}

    target = active_context(bpy, request["file"], request, request["object"])
    before = observed(bpy, target)
    result = {"runtime": runtime, "product": product, "panel": before}
    if phase == "inspect":
        return result
    authored = apply_panel(bpy, target, request, before, contract)
    state = observed(bpy, authored)
    require(state["content_sha256"] == before["content_sha256"], "VALIDATION_FAILED",
            "The panel operation changed base geometry or materials")
    if request["operation"] in ("reset-preset", "set-bridge-mode"):
        require(state["managed_state_sha256"] == before["managed_state_sha256"]
                and state["managed_graphs"] == before["managed_graphs"]
                and state["wrapper"] == before["wrapper"] and state["objects"] == before["objects"],
                "VALIDATION_FAILED", "Changing a panel draft unexpectedly altered an applied graph")
    else:
        wanted = sorted([*before["objects"], authored.name]) if authored != target else before["objects"]
        require(state["managed_modifier"] and state["objects"] == wanted, "VALIDATION_FAILED",
                "Managed modifier or object inventory does not match the requested build")
    if authored != target:
        require(state["source_content_sha256"] == before["content_sha256"]
                and state["source_object_sha256"] == before["object_sha256"], "VALIDATION_FAILED",
                "The original object's saved state, geometry or materials changed")
    bpy.context.preferences.filepaths.save_version = 0
    stage = Path(request["stage"])
    require(bpy.ops.wm.save_as_mainfile(filepath=str(stage), copy=True, relative_remap=True, check_existing=False) == {"FINISHED"},
            "IO_ERROR", "Could not save the panel candidate")
    return {**result, "before": before, "panel": state, "sha256": digest(stage)}


def worker(phase, request_file):
    try:
        result = {"ok": True, "data": work(phase, request_file)}
    except Exception as exc:
        result = {"ok": False, "error": error_payload(exc, "VALIDATION_FAILED")}
    atomic_json(request_file.parent / (phase + "-result.json"), result)


def run(args):
    deadline = time.monotonic() + timeout_value(args.timeout)
    job = args.jobs_dir.resolve() / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    artifacts = {"job_directory": str(job), "result": str(job / "result.json")}

    def checkpoint():
        require(not (job / "cancel.request").exists(), "CANCELLED", "Panel operation cancelled before publication")
        require(time.monotonic() < deadline, "TIMEOUT", "Panel operation exceeded its timeout")

    def status(state, phase):
        atomic_json(job / "status.json", {"state": state, "job_id": job.name, "pid": os.getpid(),
                    "phase": phase, "heartbeat_at": time.time()})

    status("running", "verify_inputs")
    data = error = None
    try:
        with ExitStack() as stack:
            request = read_inputs(args, stack)
            products = guard_products(stack)
            blender = canonical(args.blender)
            if args.output:
                require(args.output.is_absolute() and args.output.suffix.lower() == ".blend", "INVALID_REQUEST",
                        "Expected a new absolute .blend output")
                parent = canonical(args.output.parent)
                output = parent / args.output.name
                for directory in (parent, *parent.parents):
                    if directory != Path(directory.anchor):
                        stack.enter_context(FileGuard(directory, directory=True))
                stack.enter_context(PathLocks([output]))
                require(not os.path.lexists(output), "CONFLICT", f"Output already exists: {output}")
                stage = parent / (".pnw-" + job.name + ".blend")
                require(not os.path.lexists(stage), "CONFLICT", "Private stage path already exists")
                request["stage"] = str(stage)
                stack.callback(stage.unlink, missing_ok=True)
            atomic_json(job / "request.json", {"schema_version": SCHEMA, "command": COMMAND,
                                              "params": request, "products": products})
            phase = "author" if args.output else "inspect"
            status("running", phase)
            data = run_worker(blender, job, phase, deadline, checkpoint, status, entry=ENTRY, private_temp=True)
            data.update(operation=request["operation"], source={key: request[key] for key in ("file", "expected_sha256")})
            if args.output:
                status("running", "verify")
                verified = run_worker(blender, job, "verify", deadline, checkpoint, status, entry=ENTRY, private_temp=True)
                require(all(verified[key] == data[key] for key in ("panel", "sha256", "product"))
                        and verified["runtime"]["pid"] != data["runtime"]["pid"], "VALIDATION_FAILED",
                        "Independent reopen disagrees with the saved panel, geometry or material state")
                with FileGuard(stage, renameable=True) as locked:
                    require(locked.sha256(checkpoint) == verified["sha256"], "CONFLICT", "Stage changed after verification")
                    checkpoint()
                    locked.rename_new(output)
                data["candidate"] = {"file": str(output), "sha256": verified["sha256"]}
                data["independent_reopen"] = verified["runtime"]
                artifacts.update(candidate=str(output), independent_reopen=str(job / "verify-result.json"))
    except (Failure, OSError, RuntimeError, ValueError) as exc:
        data, error = None, error_payload(exc, "IO_ERROR")
    for path in sorted(job.glob("*.log")):
        artifacts[path.name] = str(path)
    result = envelope(COMMAND, job.name, data, error, artifacts)
    atomic_json(job / "result.json", result)
    states = {"TIMEOUT": "timed_out", "CANCELLED": "cancelled"}
    status(states.get(error["code"], "failed") if error else "succeeded", "done")
    return result


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = None
    try:
        args = parser().parse_args()
        result = run(args)
    except (Failure, OSError, ValueError) as exc:
        result = envelope(command=COMMAND, error=error_payload(exc, "INVALID_REQUEST"))
    if args and args.human:
        print(result["error"]["message"] if not result["ok"] else
              result["data"].get("candidate", {}).get("file", result["data"]["panel"]["object"]))
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["ok"] else CODES.get(result["error"]["code"], 1)


if __name__ == "__main__":
    if "--background" in sys.argv and "--" in sys.argv:
        phase, file = sys.argv[sys.argv.index("--") + 1:]
        worker(phase, Path(file))
    else:
        raise SystemExit(main())
