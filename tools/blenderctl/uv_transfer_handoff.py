"""Record UV transfer sources, prepare bound requests, and exchange native drafts."""

import argparse
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


ENTRY = Path(__file__).resolve()
sys.path.insert(0, str(ENTRY.parent))

from filesystem import FileGuard
from protocol import (CODES, DEFAULT_BLENDER, DEFAULT_JOBS, DEFAULT_TRANSACTIONS, SCHEMA, Failure,
                      atomic_json, digest, envelope, read_json, timeout_value)
from uv_saved_reader import new_destination, run_saved_reader
from uv_workflow import _activate, _check_identity, _open_source, _resources, _targets
from uv_workflow_contract import RESOURCE_LIMIT, _descriptor, addon_contract, implementation_sha256, normalize_params


WORKER_MODE = "--uv-transfer-handoff-worker"
COMMON = {"file", "expected_sha256", "resources", "scene", "view_layer"}
FIELDS = {
    "transfer-source": ({"source_object", "source_uv"}, set()),
    "transfer-prepare": ({"source_record", "objects", "mode", "candidate"}, {"options"}),
    "transfer-execute": (set(), set()),
    "transfer-export": (set(), {"draft"}),
    "transfer-import": ({"draft"}, set()),
}
OPTIONS = {"width", "height", "pair_budget"}
PROJECTED_OPTIONS = {"max_distance_m", "normal_degrees", "uv_tolerance_px"}


def require(condition, code, message):
    if not condition:
        raise Failure(code, message)


def text_field(value):
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def read_bound_json(descriptor, stack, label):
    path = Path(descriptor["file"])
    require(path.suffix.lower() == ".json", "INVALID_REQUEST", label + " requires a JSON file")
    require(path.is_file(), "NOT_FOUND", f"{label} file does not exist: {path}")
    guard = stack.enter_context(FileGuard(path))
    require(path.stat().st_size <= 64 * 1024 * 1024, "RESOURCE_LIMIT", label + " exceeds 64 MiB")
    require(guard.sha256() == descriptor["expected_sha256"], "CONFLICT", f"{label} SHA mismatch: {path}")
    value = read_json(path)
    require(isinstance(value, dict), "INVALID_REQUEST", label + " must contain a JSON object")
    return value


def input_request(command, raw, stack):
    required, optional = FIELDS[command]
    required = COMMON | required
    require(isinstance(raw, dict) and required <= raw.keys() <= required | optional,
            "INVALID_REQUEST", "Required fields: " + ", ".join(sorted(required)) +
            (". Optional: " + ", ".join(sorted(optional)) if optional else ""))
    context = {key: raw[key] for key in ("scene", "view_layer")}
    require(all(map(text_field, context.values())), "INVALID_REQUEST", "Specify scene and view_layer names")
    frozen = _descriptor({key: raw[key] for key in ("file", "expected_sha256")}, "working copy", blend=True)
    resources = raw["resources"]
    require(isinstance(resources, list) and len(resources) <= RESOURCE_LIMIT,
            "INVALID_REQUEST", f"resources must be an array of up to {RESOURCE_LIMIT} file/SHA descriptors")
    resources = [_descriptor(row, "resource") for row in resources]
    paths = [os.path.normcase(row["file"]) for row in resources]
    require(len(paths) == len(set(paths)), "INVALID_REQUEST", "Duplicate resource path")
    base = {**frozen, "resources": resources, "implementation_sha256": implementation_sha256()}
    request = {"command": command, "base": base, "context": context}

    if command == "transfer-source":
        for key in ("source_object", "source_uv"):
            require(text_field(raw[key]), "INVALID_REQUEST", key + " must be a nonempty name")
            request[key] = raw[key]
    if command == "transfer-prepare":
        objects = raw["objects"]
        require(isinstance(objects, list) and objects and all(map(text_field, objects)) and len(set(objects)) == len(objects),
                "INVALID_REQUEST", "objects must contain unique target names")
        mode = raw["mode"]
        require(mode in ("INDEXED", "PROJECTED"), "INVALID_REQUEST", "Prepare mode must be INDEXED or PROJECTED")
        candidate = raw["candidate"]
        require(isinstance(candidate, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,47}", candidate),
                "INVALID_REQUEST", "candidate must be a short ASCII identifier")
        options = raw.get("options", {})
        allowed = OPTIONS | (PROJECTED_OPTIONS if mode == "PROJECTED" else set())
        require(isinstance(options, dict) and options.keys() <= allowed,
                "INVALID_REQUEST", "Unknown transfer option for the selected mode")
        require(all(map(finite_number, options.values())),
                "INVALID_REQUEST", "Transfer options must be finite numbers")
        source_record = read_bound_json(_descriptor(raw["source_record"], "source_record"), stack, "source record")
        require(source_record.keys() == {"schema_version", "implementation_sha256", "snapshot", "capture"}
                and source_record["schema_version"] == "1.0", "INVALID_REQUEST", "Expected a transfer-source output")
        _descriptor(source_record["snapshot"], "recorded working copy", blend=True)
        require(source_record["implementation_sha256"] == base["implementation_sha256"],
                "CONFLICT", "UV implementation differs from the source recording")
        capture = source_record["capture"]
        require(isinstance(capture, dict) and
                capture.keys() == {"scene", "view_layer", "source_object", "source_uv", "source_binding"} and
                text_field(capture["source_object"]) and text_field(capture["source_uv"]),
                "INVALID_REQUEST", "Invalid native UV source recording")
        require(not set(objects) & {capture["source_object"]}, "INVALID_REQUEST", "Source cannot also be a target")
        request.update(source_capture=capture, objects=sorted(objects), mode=mode, candidate=candidate, options=options)
    if command in ("transfer-export", "transfer-import") and "draft" in raw:
        draft_input = _descriptor(raw["draft"], "draft")
        draft = read_bound_json(draft_input, stack, "transfer draft")
        if command == "transfer-import":
            require(draft.get("operation") == "transfer", "INVALID_REQUEST", "Import requires a native transfer manifest")
            addon_contract.normalize(draft)
            request["draft_input"] = draft_input
        if "manifest" in draft:
            params = normalize_params(draft, "uv.run")
            require(params["file"] == base["file"] and params["expected_sha256"] == base["expected_sha256"]
                    and params["resources"] == base["resources"],
                    "CONFLICT", "Prepared parameters belong to a different working copy or resource declaration")
            draft = params["manifest"]
        request["draft"] = draft
    return request


def local_mesh(context, name):
    obj = context.view_layer.objects.get(name)
    require(obj is not None and obj.type == "MESH", "NOT_FOUND", "Missing UV mesh: " + name)
    require(obj.mode == "OBJECT" and not (obj.library or obj.data.library or obj.override_library),
            "UNSUPPORTED", "UV transfer requires a local Object Mode mesh: " + name)
    return obj


def call_operator(operator, **options):
    try:
        result = operator(**options)
    except RuntimeError as exc:
        raise Failure("CONFLICT", str(exc).strip()) from exc
    require(result == {"FINISHED"}, "CONFLICT", "Blender did not record the UV transfer handoff")


def validate_draft(context, manifest, base):
    from uv_workflow_addon import core, transfer, ui

    try:
        ui._transfer_draft(context, manifest)
    except ValueError as exc:
        raise Failure("CONFLICT", str(exc)) from exc
    params = normalize_params({**base, "manifest": manifest}, "uv.run")
    normalized = params["manifest"]
    scene = context.scene
    require(normalized["transfer_unit_scale"] == scene.unit_settings.scale_length,
            "CONFLICT", "Transfer scene unit scale changed. Prepare a new request")
    candidate = normalized["candidate"]
    require(candidate not in core.state(scene)["candidates"], "CONFLICT", "Transfer candidate already exists: " + candidate)
    for name, row in normalized["transfer"].items():
        source = local_mesh(context, row["source_object"])
        target = local_mesh(context, name)
        require(row["source_uv"] in source.data.uv_layers and
                transfer.source_identity(source, row["source_uv"]) == row["source_binding"],
                "CONFLICT", "Transfer source geometry, UV or pins changed: " + source.name)
        require(transfer.target_identity(target) == row["target_binding"],
                "CONFLICT", "Transfer target geometry, old UV, pins or render identity changed: " + name)
        require(target.data.users == 1, "UNSUPPORTED", "Transfer target mesh is shared: " + name)
        require("UVW_" + candidate not in target.data.uv_layers and len(target.data.uv_layers) < 8,
                "CONFLICT", "Transfer candidate layer collision or UV layer limit: " + name)
    return params


def import_protection(persistence_exceptions):
    """Fingerprint authored scene data, native UV values and managed state."""
    import bpy
    from uv_saved_protection import scene_fingerprint
    from uv_workflow_addon import core

    return core._hash({
        "scene": scene_fingerprint(persistence_exceptions=persistence_exceptions),
        "uv": {
            mesh.name: {
                "active": mesh.uv_layers.active.name if mesh.uv_layers.active else None,
                "layers": [(layer.name, layer.active_render, layer.active_clone,
                            core._uv_fingerprint(mesh, layer.name)) for layer in mesh.uv_layers],
            }
            for mesh in bpy.data.meshes
        },
        "candidates": {scene.name: scene.get(core.STATE) for scene in bpy.data.scenes},
    })


def execute(request, output_path, scratch):
    import bpy
    from uv_workflow_addon import transfer, ui

    require(bpy.app.background and not bpy.app.online_access and
            not bpy.context.preferences.filepaths.use_scripts_auto_execute,
            "UNSUPPORTED", "UV handoffs require offline background Blender with auto-execution disabled")
    base = request["base"]
    _check_identity(base)
    _open_source(base)
    require(bool(bpy.data.filepath) and Path(bpy.data.filepath).resolve() == Path(base["file"]),
            "VALIDATION_FAILED", "Blender did not reopen the specified working copy")
    context = bpy.context
    require(context.mode == "OBJECT", "CONFLICT", "Save the transfer working copy in Object Mode")
    scene = _activate(request["context"])
    _resources(base)
    ui.register()
    operation = request["command"]
    data = {"blender": {"version": bpy.app.version_string, "build": bpy.app.build_hash.decode()}}
    if operation in ("transfer-import", "verify-transfer-import"):
        manifest = request["draft"]
        validate_draft(context, manifest, base)
        if operation == "verify-transfer-import":
            reopened = json.loads(scene.get("uvw_transfer_draft", "{}"))
            require(reopened == manifest, "VALIDATION_FAILED", "Imported draft differs after independent reopen")
            require(import_protection(request["persistence_exceptions"]) == request["expected_protection"],
                    "VALIDATION_FAILED",
                    "Protected scene, UV layers, or candidate state changed after import")
            return data

        from geometry_workflow import _retain_unlinked_ids

        persistence = _retain_unlinked_ids()
        protection = import_protection(persistence)
        native_input = scratch / "native-import.json"
        atomic_json(native_input, manifest)
        call_operator(bpy.ops.uvw.transfer_file, direction="IMPORT", filepath=str(native_input))
        require(json.loads(scene.get("uvw_transfer_draft", "{}")) == manifest,
                "VALIDATION_FAILED", "Native importer did not retain the exact transfer draft")
        require(import_protection(persistence) == protection, "VALIDATION_FAILED",
                "Protected scene or UV data changed during import")
        bpy.context.preferences.filepaths.save_version = 0
        saved = bpy.ops.wm.save_as_mainfile(filepath=str(output_path), check_existing=False,
                                           copy=True, relative_remap=True)
        require(saved == {"FINISHED"} and output_path.is_file(), "IO_ERROR", "Could not save the import-only working copy")
        data.update(transfer_executed=False, protection_sha256=protection, persistence_exceptions=persistence,
                    draft_input=request["draft_input"], source={key: base[key] for key in ("file", "expected_sha256")})
        return data
    if operation == "transfer-source":
        obj = local_mesh(context, request["source_object"])
        active = obj.data.uv_layers.active
        require(active is not None and active.name == request["source_uv"],
                "CONFLICT", "The requested source UV must be the object's saved active UV")
        context.view_layer.objects.active = obj
        call_operator(bpy.ops.uvw.transfer_source)
        data["source_record"] = {
            "schema_version": "1.0",
            "implementation_sha256": base["implementation_sha256"],
            "snapshot": {key: base[key] for key in ("file", "expected_sha256")},
            "capture": json.loads(scene["uvw_transfer_source"]),
        }
        return data
    if operation == "transfer-prepare":
        scene["uvw_transfer_source"] = json.dumps(request["source_capture"], ensure_ascii=False)
        try:
            source, binding = ui._transfer_source(context)
        except (ValueError, KeyError, TypeError) as exc:
            raise Failure("CONFLICT", str(exc)) from exc
        local_mesh(context, source.name)
        targets = _targets(scene, {**request["context"], "objects": request["objects"]})
        manifest = transfer.make_request(
            scene, context.view_layer, source, binding["source_uv"], targets,
            request["candidate"], mode=request["mode"], **request["options"],
        )
    else:
        manifest = request.get("draft")
        if manifest is None:
            try:
                manifest = json.loads(scene.get("uvw_transfer_draft", "{}"))
            except (TypeError, ValueError) as exc:
                raise Failure("CONFLICT", "The working copy has no valid saved transfer draft") from exc
        require(isinstance(manifest, dict) and manifest.get("operation") == "transfer",
                "CONFLICT", "The working copy has no valid saved transfer draft")
    data["params"] = validate_draft(context, manifest, base)
    if operation == "transfer-export":
        # Export the original draft shape via the same operator as the panel.
        scene["uvw_transfer_draft"] = json.dumps(manifest, ensure_ascii=False, allow_nan=False)
        call_operator(bpy.ops.uvw.transfer_file, direction="EXPORT", filepath=str(output_path))
    return data


def worker(request_path, result_path, output_path):
    result_path = Path(result_path)
    try:
        response = {"data": execute(read_json(request_path), Path(output_path), result_path.parent)}
    except (Failure, addon_contract.WorkflowError) as exc:
        response = {"error": {"code": exc.code, "message": str(exc)}}
    atomic_json(result_path, response)


def run(blender, request, timeout, prepared_output):
    importing = request["command"] == "transfer-import"
    native_output = importing or request["command"] == "transfer-export"
    deadline = time.monotonic() + timeout
    data = run_saved_reader(blender, request, ENTRY, WORKER_MODE, timeout,
                            str(prepared_output) if native_output else "")
    if importing:
        with FileGuard(prepared_output) as guard:
            snapshot_sha = guard.sha256()
        verify_request = dict(
            request, command="verify-transfer-import", expected_protection=data["protection_sha256"],
            persistence_exceptions=data["persistence_exceptions"],
            base={**request["base"], "file": str(prepared_output), "expected_sha256": snapshot_sha},
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("independent Blender import verification", timeout)
        verified = run_saved_reader(blender, verify_request, ENTRY, WORKER_MODE, remaining, "")
        require(verified["blender"] == data["blender"], "VALIDATION_FAILED", "Independent Blender build differs")
        data["verification"] = {"status": "passed", "draft": "exact", "protected_scene": "exact",
                                "snapshot_sha256": snapshot_sha}
    elif not native_output and prepared_output is not None:
        field = "source_record" if request["command"] == "transfer-source" else "params"
        atomic_json(prepared_output, data[field])
    return data


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure("INVALID_REQUEST", message)


def execute_saved_transfer(args, handoff, deadline):
    from runner import execute, prepare

    params = handoff["params"]
    manifest = params["manifest"]
    remaining = deadline - time.monotonic()
    require(remaining > 0, "TIMEOUT", "Timed out before submitting the saved UV transfer")
    request = {"schema_version": SCHEMA, "command": "uv.run", "params": params}
    job = prepare(request, args.blender, args.jobs_dir, remaining, args.transactions_dir)
    evidence = job / "uv-transfer-draft.json"
    atomic_json(evidence, {
        "draft_source": "uvw_transfer_draft",
        "file": params["file"],
        "expected_sha256": params["expected_sha256"],
        "scene": manifest["scene"],
        "view_layer": manifest["view_layer"],
        "candidate": manifest["candidate"],
        "objects": manifest["objects"],
        "implementation_sha256": params["implementation_sha256"],
        "handoff_sha256": digest(ENTRY),
        "blender": handoff["blender"],
    })
    return execute(job)


def parser():
    cli = Parser(prog="blenderctl uv", description=__doc__, allow_abbrev=False)
    cli.add_argument("--blender", type=Path, default=DEFAULT_BLENDER, help=argparse.SUPPRESS)
    cli.add_argument("--timeout", type=float, default=30, help=argparse.SUPPRESS)
    cli.add_argument("--jobs-dir", type=Path, default=DEFAULT_JOBS, help=argparse.SUPPRESS)
    cli.add_argument("--transactions-dir", type=Path, default=DEFAULT_TRANSACTIONS, help=argparse.SUPPRESS)
    display = cli.add_mutually_exclusive_group()
    for flag in ("--human", "--compact", "--full"):
        display.add_argument(flag, action="store_true", help=argparse.SUPPRESS)
    actions = cli.add_subparsers(dest="action", required=True, parser_class=Parser)
    action_notes = {
        "transfer-source": "The named source UV must be active on the saved Object Mode mesh. "
                           "The output is the source_record consumed by transfer-prepare.",
        "transfer-prepare": "Supply the source_record as {file, expected_sha256}, both bound to the recorded JSON. "
                            "objects are exact targets. mode is INDEXED or PROJECTED. "
                            "options can set width, height and pair_budget. PROJECTED also accepts "
                            "max_distance_m, normal_degrees and uv_tolerance_px. Use the output with uv run --params.",
        "transfer-execute": "Execute the transfer draft stored in the selected scene of the saved Object Mode .blend. "
                            "The unchanged uv.run job returns a new verified candidate under --jobs-dir.",
        "transfer-export": "Omit draft to export the UV Workbench draft stored in the working copy. "
                           "Otherwise supply {file, expected_sha256} for a prepared params file or a native transfer manifest. "
                           "The output is the bare native manifest. The JSON response also includes direct uv run parameters.",
        "transfer-import": "Supply draft as {file, expected_sha256} for a native transfer manifest. "
                           "Write a new Object Mode .blend with the imported draft and independently verify it. "
                           "This operation records the draft without executing UV transfer.",
    }
    for action, fields in FIELDS.items():
        required, optional = fields
        help_text = "Required input keys: " + ", ".join(sorted(COMMON | required)) + "."
        if optional:
            help_text += " Optional: " + ", ".join(sorted(optional)) + "."
        sub = actions.add_parser(action, epilog=help_text + " " + action_notes[action], allow_abbrev=False)
        sub.add_argument("--params", required=True, type=Path, help="UTF-8 JSON request")
        if action == "transfer-execute":
            continue
        description = {
            "transfer-source": "Write the bound source record to a new JSON file",
            "transfer-prepare": "Write direct uv run parameters to a new JSON file",
            "transfer-export": "Write the native transfer draft to a new JSON file",
            "transfer-import": "Write a new absolute .blend working copy",
        }[action]
        sub.add_argument("--output", required=action in ("transfer-export", "transfer-import"), type=Path, help=description)
    return cli


def main():
    data, command, error, staged = None, None, None, None
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        args = parser().parse_args()
        command = "uv." + args.action
        timeout = timeout_value(args.timeout)
        deadline = time.monotonic() + timeout
        require(args.blender.is_absolute() and args.blender.is_file(), "NOT_FOUND",
                f"Blender executable is not available at {args.blender}")
        suffix = ".blend" if args.action == "transfer-import" else ".json"
        output = new_destination(getattr(args, "output", None), suffix)
        if output is not None:
            staged = output.with_name(f".uv-transfer-{uuid.uuid4().hex}{suffix}")
        with ExitStack() as guards:
            request = input_request(args.action, read_json(args.params), guards)
            handoff = run(args.blender, request, timeout, staged)
            if args.action == "transfer-execute":
                result, code = execute_saved_transfer(args, handoff, deadline)
                print(json.dumps(result, ensure_ascii=False))
                return code
            if output is not None:
                with FileGuard(staged, renameable=True) as guard:
                    sha256 = guard.sha256()
                    if args.action == "transfer-export":
                        manifest = read_json(staged)
                        params = normalize_params({**request["base"], "manifest": manifest}, "uv.run")
                        require(params == handoff["params"], "VALIDATION_FAILED", "Native export differs from the validated draft")
                    if args.action == "transfer-import":
                        require(sha256 == handoff["verification"]["snapshot_sha256"], "CONFLICT",
                                "Imported copy changed after independent verification")
                    guard.rename_new(output)
                staged = None
                handoff["output"] = {"file": str(output), "sha256": sha256}
            data = handoff
    except (Failure, addon_contract.WorkflowError) as exc:
        error = {"code": exc.code, "message": str(exc)}
    except subprocess.TimeoutExpired:
        error = {"code": "TIMEOUT", "message": "Timed out while reading the saved transfer working copy"}
    except OSError as exc:
        error = {"code": "IO_ERROR", "message": str(exc)}
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    print(json.dumps(envelope(command, data=data, error=error), ensure_ascii=False))
    return CODES[error["code"]] if error else 0


if __name__ == "__main__":
    if WORKER_MODE in sys.argv and "--background" in sys.argv:
        worker(*sys.argv[sys.argv.index(WORKER_MODE) + 1:])
    else:
        sys.exit(main())
