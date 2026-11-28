"""Export or apply a UV candidate's bound layout settings and usage recipe."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from protocol import CODES, Failure, atomic_json, envelope, read_json, timeout_value
from uv_saved_reader import new_destination, run_saved_reader
from uv_workflow_contract import addon_contract, normalize_params

MARKER = "--uv-policy-worker"
CONTEXT = ("scene", "view_layer", "objects", "candidate")


def export_in_blender(request, scratch):
    import bpy
    from uv_workflow import _activate, _check_identity, _open_source, _resources, _targets
    from uv_workflow_addon import core, recipes, ui

    base, kind = request["base"], request["kind"]
    _check_identity(base)
    _open_source(base)
    _resources(base)
    manifest = base["manifest"]
    scene = _activate(manifest)
    if bpy.context.mode != "OBJECT":
        raise Failure("CONFLICT", "Export requires a saved Object Mode working copy")
    state = core.state(scene)
    record = state["candidates"].get(manifest["candidate"])
    if not record:
        raise Failure("NOT_FOUND", "No such managed UV candidate")
    if record["objects"] != manifest["objects"]:
        raise Failure("CONFLICT", "Use the saved candidate's complete object list in its original order")
    objects = _targets(scene, {**manifest, "objects": record["objects"]})
    ui.register()
    scene.uvw_candidate = manifest["candidate"]
    operator = bpy.ops.uvw.settings_file if kind == "settings" else bpy.ops.uvw.recipe_file
    try:
        status = operator(direction="EXPORT", filepath=str(scratch))
    except RuntimeError as exc:
        raise Failure("CONFLICT", str(exc)) from exc
    if status != {"FINISHED"}:
        raise Failure("CONFLICT", "The UV panel could not export the requested policy")
    value = read_json(scratch)
    binding = recipes.binding(objects, record["manifest"], scene.unit_settings.scale_length)
    recipes.require_binding(value, binding)
    # The panel can archive a bound policy even when native edits require a
    # fresh candidate check. Only expose replay parameters for a matching snapshot.
    issues = []
    adopted = state.get("adopted") or {}
    for obj in objects:
        mesh = obj.data
        if (mesh.name != record["meshes"].get(obj.name) or mesh.users != 1 or record["layer"] not in mesh.uv_layers
                or binding["geometry"][obj.name] != record["geometry"].get(obj.name)):
            issues.append(f"{obj.name}: mesh identity, geometry, sharing or candidate layer changed")
        elif kind == "settings" and core._uv_fingerprint(mesh, record["layer"]) != record["uv_hash"].get(obj.name):
            issues.append(f"{obj.name}: native UV edits require revalidation")
        if kind == "settings" and adopted.get("candidate") == manifest["candidate"]:
            seams = [e.index for e in mesh.edges if e.use_seam]
            if core._hash(seams) != adopted["applied_seams"].get(obj.name):
                issues.append(f"{obj.name}: native seam edits require revalidation")
    if kind == "recipe" and "usage_binding" in value:
        usage = recipes.usage_binding(record.get("settings"), value["policy"]["density_basis"])
        if usage != value["usage_binding"]:
            issues.append("Candidate usage changed; review and explicitly rebind the recipe")
    result = {"kind": kind, "value": value, "replay_check": {
        "status": "requires_revalidation" if issues else "ready_to_submit", "issues": issues},
        "runtime": {"version": bpy.app.version_string, "build": bpy.app.build_hash.decode()}}
    if not issues:
        result["params"] = normalize_params({
            **base,
            "manifest": {"version": "1.0", "operation": kind,
                         **{key: manifest[key] for key in CONTEXT}, kind: value},
        }, "uv.run")
    return result


def worker(input_path, result_path):
    try:
        data = export_in_blender(read_json(input_path), Path(result_path).with_name("export.json"))
        result = {"data": data}
    except (Failure, addon_contract.WorkflowError) as exc:
        result = {"error": {"code": exc.code, "message": str(exc)}}
    atomic_json(result_path, result)


def apply_file(args, base):
    import cli
    from filesystem import FileGuard

    path = args.policy
    if not re.fullmatch(r"[a-f0-9]{64}", args.expected_policy_sha256):
        raise Failure("INVALID_REQUEST", "Expected the lowercase SHA-256 of the policy file")
    if not path.is_absolute() or path.suffix.lower() != ".json":
        raise Failure("INVALID_REQUEST", "--policy must be an absolute .json path")
    if not path.is_file():
        raise Failure("NOT_FOUND", f"Policy not found: {path}")
    with FileGuard(path) as source:
        limit = 65536 if args.kind == "recipe" else 1048576
        if path.stat().st_size > limit:
            raise Failure("INVALID_REQUEST", "File exceeds the UV panel's import limit")
        if source.sha256() != args.expected_policy_sha256:
            raise Failure("CONFLICT", "Policy SHA differs from the reviewed export")
        value = read_json(path)
    replay = normalize_params({
        **base, "manifest": {"version": "1.0", "operation": args.kind, args.kind: value,
                             **{k: base["manifest"][k] for k in CONTEXT}},
    }, "uv.run")
    with tempfile.TemporaryDirectory(prefix="blenderctl-uv-policy-") as tmp:
        params = Path(tmp) / "params.json"
        atomic_json(params, replay)
        argv = ["--blender", str(args.blender), "--timeout", str(args.timeout)]
        for key in ("jobs_dir", "transactions_dir"):
            if getattr(args, key):
                argv += ["--" + key.replace("_", "-"), getattr(args, key)]
        if args.format:
            argv.append("--" + args.format)
        # The existing job owns the candidate, validation and recovery receipt.
        return cli.main([*argv, "uv", "run", "--params", str(params)])


def main():
    from filesystem import FileGuard

    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise Failure("INVALID_REQUEST", message)

    parser = Parser(prog="blenderctl uv", description=__doc__, allow_abbrev=False)
    parser.add_argument("--blender", type=Path, required=True, help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=60, help=argparse.SUPPRESS)
    parser.add_argument("--jobs-dir", help=argparse.SUPPRESS)
    parser.add_argument("--transactions-dir", help=argparse.SUPPRESS)
    output_format = parser.add_mutually_exclusive_group()
    for option in ("compact", "full", "human"):
        output_format.add_argument("--" + option, action="store_const", const=option, dest="format", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(required=True, dest="action")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--params", type=Path, required=True,
                        help="JSON with file, expected_sha256, resources, scene, view_layer, candidate and its ordered objects")
    common.add_argument("--kind", choices=("settings", "recipe"), required=True)
    export = sub.add_parser("export-policy", parents=[common], description=__doc__)
    export.add_argument("--output", type=Path, required=True, help="New absolute .json path")
    attach = sub.add_parser("import-policy", parents=[common], description="Apply a bound JSON policy as a new uv.run candidate")
    attach.add_argument("--policy", type=Path, required=True, help="Bound JSON from the UV panel or export-policy")
    attach.add_argument("--expected-policy-sha256", required=True, help="Reviewed policy file SHA-256")
    args = None
    data = error = staged = None
    try:
        args = parser.parse_args()
        if not args.blender.is_absolute() or not args.blender.is_file():
            raise Failure("NOT_FOUND", "The selected Blender executable is unavailable")
        timeout_value(args.timeout)
        spec = read_json(args.params)
        if not isinstance(spec, dict) or set(spec) != {"file", "expected_sha256", "resources", *CONTEXT}:
            raise Failure("INVALID_REQUEST", "Expected source file/SHA, resources and explicit candidate context")
        base = normalize_params({
            **{k: spec[k] for k in ("file", "expected_sha256", "resources")},
            "manifest": {"version": "1.0", "operation": "inspect", **{k: spec[k] for k in CONTEXT}},
        }, "uv.inspect")
        if args.action == "import-policy":
            sys.stdout.reconfigure(encoding="utf-8")
            return apply_file(args, base)
        dest = new_destination(args.output, ".json")
        result = run_saved_reader(args.blender, {"base": base, "kind": args.kind}, __file__, MARKER, args.timeout)
        staged = dest.with_name(f".uv-policy-{uuid.uuid4().hex}.json")
        atomic_json(staged, result.pop("value"))
        with FileGuard(staged, renameable=True) as guard:
            checksum = guard.sha256()
            guard.rename_new(dest)
        staged = None
        data = {**result, "export": {"file": str(dest), "sha256": checksum}}
    except (Failure, addon_contract.WorkflowError) as exc:
        error = {"code": exc.code, "message": str(exc)}
    except subprocess.TimeoutExpired:
        error = {"code": "TIMEOUT", "message": "Timed out reading the saved UV policy"}
    except OSError as exc:
        error = {"code": "IO_ERROR", "message": str(exc)}
    finally:
        if staged:
            staged.unlink(missing_ok=True)
    sys.stdout.reconfigure(encoding="utf-8")
    command = "uv." + args.action if args else "uv"
    if data and args.format == "compact":
        data = {key: data[key] for key in ("kind", "runtime", "export", "replay_check")}
    result = envelope(command, data=data, error=error)
    if args and args.format == "human":
        print(f"{'ERROR' if error else 'OK'} {command}")
        print(json.dumps(error or data, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False))
    return CODES[error["code"]] if error else 0


if __name__ == "__main__":
    if MARKER in sys.argv and "--background" in sys.argv:
        worker(*sys.argv[sys.argv.index(MARKER) + 1:])
    else:
        raise SystemExit(main())
