"""Read a UV surface report for an exact saved Blender snapshot."""

import argparse
import json
from pathlib import Path
import subprocess
import sys


ENTRY = Path(__file__).resolve()
sys.path.insert(0, str(ENTRY.parent))

from filesystem import FileGuard
from protocol import CODES, DEFAULT_BLENDER, Failure, atomic_json, envelope, read_json, timeout_value
from uv_saved_reader import run_saved_reader
from uv_workflow import _open_source
from uv_workflow_contract import _descriptor


COMMAND = "uv.read-surface-report"
WORKER_MODE = "--uv-surface-report-worker"
STATUSES = ("pass", "fail", "unknown")


def input_request(raw):
    if not isinstance(raw, dict) or raw.keys() != {"file", "expected_sha256", "report"}:
        raise Failure("INVALID_REQUEST", "Expected file, expected_sha256, and report")
    source = _descriptor({key: raw[key] for key in ("file", "expected_sha256")}, "source", blend=True)
    report = _descriptor(raw["report"], "report")
    if Path(report["file"]).suffix.lower() != ".json":
        raise Failure("INVALID_REQUEST", "report.file must be the full uv-report.json")
    return {"base": {**source, "resources": []}, "report": report}


def surface_target(surface):
    import bpy

    layers = surface["request"].get("layers")
    if (not isinstance(layers, dict) or not layers or
            any(not name or not isinstance(layer, str) or not layer for name, layer in layers.items())):
        raise Failure("VALIDATION_FAILED", "UV surface report requires an object-to-layer mapping")
    controls = surface.get("controls_before")
    if not isinstance(controls, dict) or not all(
            isinstance(controls.get(key), str) and controls[key]
            for key in ("window_scene", "window_view_layer")):
        raise Failure("VALIDATION_FAILED", "UV surface report is missing its scene and view layer")
    scene_name, view_layer_name = controls["window_scene"], controls["window_view_layer"]
    scene = bpy.data.scenes.get(scene_name)
    view_layer = scene.view_layers.get(view_layer_name) if scene else None
    if view_layer is None:
        raise Failure("VALIDATION_FAILED", "UV surface report scene or view layer is absent from the saved file")
    # UV layers are observed on evaluated meshes. A missing layer can be a
    # valid unknown sample even when the target object exists in the snapshot.
    for name in layers:
        obj = view_layer.objects.get(name)
        if obj is None or obj.type != "MESH":
            raise Failure("VALIDATION_FAILED", f"UV surface report mesh is absent from the saved file: {name}")
    return {"scene": scene_name, "view_layer": view_layer_name, "layers": layers}


def summarize(surface):
    spec = surface["request"]
    target = surface_target(surface)
    layers = target["layers"]
    samples = []
    for sample in surface["samples"]:
        if sample["status"] not in STATUSES:
            raise Failure("VALIDATION_FAILED", "Unknown UV surface sample status")
        observations = sample.get("objects", sample.get("objects_observed", []))
        if not isinstance(observations, list):
            raise Failure("VALIDATION_FAILED", "UV surface sample objects must be an array")
        seen = set()
        for observation in observations:
            name = observation.get("object") if isinstance(observation, dict) else None
            if (not isinstance(name, str) or name not in layers or name in seen or
                    observation.get("layer") != layers[name]):
                raise Failure("VALIDATION_FAILED", "Sample object or UV layer differs from the surface request")
            seen.add(name)
        if sample["complete"] and seen != layers.keys():
            raise Failure("VALIDATION_FAILED", "Complete sample lacks observations for its declared UV layers")
        row = {key: sample[key] for key in ("frame", "mode", "status", "complete")}
        if isinstance(sample.get("reason"), str):
            row["reason"] = sample["reason"][:160]
        if row.get("reason") == "unsupported" and isinstance(sample.get("message"), str):
            row["message"] = sample["message"][:320]
        if type(sample.get("reuse_used")) is bool:
            row["reuse_used"] = sample["reuse_used"]
        samples.append(row)
    result = {
        **target,
        "status": surface["status"],
        "usage": spec["usage"],
        "frames": spec["frames"],
        "modes": spec["modes"],
        "sample_count": len(samples),
        "samples": samples,
        "controls_restored": surface["controls_restored"],
        "policy_declared": bool(spec.get("policy")),
        "quality_acceptance": "requires_surface_purpose_and_visual_review",
        "configured_renderer_equivalence": "not_verified",
    }
    if spec["usage"] == "CANDIDATE_EXACT":
        result["candidate"] = spec["candidate"]
    return result


def execute(request):
    import bpy
    from uv_workflow_addon import surface_ui

    if (not bpy.app.background or bpy.app.online_access or
            bpy.context.preferences.filepaths.use_scripts_auto_execute):
        raise Failure("UNSUPPORTED", "UV report reading requires offline background Blender with auto-execution disabled")
    base, report = request["base"], request["report"]
    try:
        _open_source(base)
    except RuntimeError as exc:
        raise Failure("VALIDATION_FAILED", f"Could not open the saved Blender file: {exc}") from exc
    if not bpy.data.filepath or Path(bpy.data.filepath).resolve() != Path(base["file"]):
        raise Failure("VALIDATION_FAILED", "Blender did not reopen the specified saved file")
    if Path(report["file"]).stat().st_size > surface_ui.REPORT_BYTE_LIMIT:
        raise Failure("RESOURCE_LIMIT", "The full UV surface report exceeds the panel reader's 128 MiB limit")
    bpy.utils.register_class(surface_ui.UVW_OT_surface_report)
    try:
        loaded = bpy.ops.uvw.surface_report(filepath=report["file"])
    except RuntimeError as exc:
        raise Failure("VALIDATION_FAILED", str(exc).strip()) from exc
    if loaded != {"FINISHED"} or surface_ui._REPORT is None:
        raise Failure("VALIDATION_FAILED", "The UV panel reader rejected the report")
    return {
        "status": "loaded",
        "scope": "SAVED_DISK_V1",
        "blender": {"version": bpy.app.version_string, "build": bpy.app.build_hash.decode()},
        "source": {"file": base["file"], "sha256": base["expected_sha256"]},
        "report": {"file": report["file"], "sha256": report["expected_sha256"]},
        "surface": summarize(surface_ui._REPORT["surface"]),
    }


def worker(params_path, result_path):
    try:
        result = {"data": execute(read_json(params_path))}
    except Failure as exc:
        result = {"error": {"code": exc.code, "message": str(exc)}}
    except OSError as exc:
        result = {"error": {"code": "IO_ERROR", "message": str(exc)}}
    atomic_json(Path(result_path), result)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure("INVALID_REQUEST", message)


def parser():
    cli = Parser(prog="blenderctl uv", description=__doc__, allow_abbrev=False)
    cli.add_argument("--blender", type=Path, default=DEFAULT_BLENDER, help=argparse.SUPPRESS)
    cli.add_argument("--timeout", type=float, default=30, help=argparse.SUPPRESS)
    for flag in ("--jobs-dir", "--transactions-dir"):
        cli.add_argument(flag, type=Path, help=argparse.SUPPRESS)
    display = cli.add_mutually_exclusive_group()
    display.add_argument("--compact", dest="display", action="store_const", const="compact",
                         help="single-line compact JSON")
    display.add_argument("--human", dest="display", action="store_const", const="human",
                         help="human-readable receipt")
    display.add_argument("--full", dest="display", action="store_const", const="full",
                         help="complete JSON receipt with a reference to the original report")
    actions = cli.add_subparsers(dest="action", required=True)
    action = actions.add_parser(
        "read-surface-report", description=__doc__, allow_abbrev=False,
        epilog="Provide file and expected_sha256 for the saved .blend, and report "
               "with file and expected_sha256 for the full uv-report.json from uv inspect. "
               "The response summarizes recorded surface pass, fail or unknown for the "
               "reported frames and modes. It leaves both inputs read-only. "
               "Place --human, --compact, or --full before uv to select the receipt format. "
               "The full measurements are in the report referenced by data.report.",
    )
    action.add_argument("--params", type=Path, required=True, help="UTF-8 JSON request")
    return cli


def run(args):
    timeout = timeout_value(args.timeout)
    if not args.blender.is_absolute() or not args.blender.is_file():
        raise Failure("NOT_FOUND", f"Blender executable is not available at {args.blender}")
    request = input_request(read_json(args.params))
    report = request["report"]
    path = Path(report["file"])
    if not path.is_file():
        raise Failure("NOT_FOUND", f"UV surface report not found: {path}")
    with FileGuard(path) as guard:
        if guard.sha256() != report["expected_sha256"]:
            raise Failure("CONFLICT", f"UV surface report SHA mismatch: {path}")
        return run_saved_reader(args.blender, request, ENTRY, WORKER_MODE, timeout)


def print_human(data, error):
    if error:
        print(f"{error['code']} {error['message']}")
        return
    surface = data["surface"]
    print(f"UV surface report loaded. Result {surface['status']} ({surface['usage']}).")
    print(f"Source: {data['source']['file']}")
    print(f"Report: {data['report']['file']}")
    print(f"Scene and view layer: {surface['scene']} / {surface['view_layer']}")
    for name, layer in surface["layers"].items():
        print(f"  {name} -> {layer}")
    for sample in surface["samples"]:
        reason = sample.get("message") or sample.get("reason")
        suffix = f" ({reason})" if reason else ""
        print(f"  Frame {sample['frame']} / {sample['mode']}: {sample['status']}{suffix}")


def main():
    args, data, error = None, None, None
    try:
        args = parser().parse_args()
        data = run(args)
    except Failure as exc:
        error = {"code": exc.code, "message": str(exc)}
    except subprocess.TimeoutExpired:
        error = {"code": "TIMEOUT", "message": "Timed out while validating the saved UV surface report"}
    except OSError as exc:
        error = {"code": "IO_ERROR", "message": str(exc)}
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args and args.display == "human":
        print_human(data, error)
    else:
        separators = (",", ":") if args and args.display == "compact" else None
        print(json.dumps(envelope(COMMAND, data=data, error=error), ensure_ascii=False, separators=separators))
    return CODES[error["code"]] if error else 0


if __name__ == "__main__":
    if WORKER_MODE in sys.argv and "--background" in sys.argv:
        worker(*sys.argv[sys.argv.index(WORKER_MODE) + 1:])
    else:
        sys.exit(main())
