"""Saved Toon Omni panel controls with verified background preview and page handoff."""
import argparse
import importlib
import json
import math
import os
from contextlib import ExitStack
from pathlib import Path
import sys
import time
import uuid

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
if str(ENTRY.parent) not in sys.path:
    sys.path.insert(0, str(ENTRY.parent))

from filesystem import FileGuard, PathLocks
from material_draft_handoff import Parser, canonical, guard_file, open_saved_context, read_document, require
from material_projection_handoff import error_payload, run_worker
from protocol import CODES, DEFAULT_BLENDER, DEFAULT_JOBS, SCHEMA, Failure, atomic_json, digest, envelope, read_json, timeout_value
from toon_panel_handoff import PRODUCTS, bindings, load_modules, name, native_product
from toon_viewport_lines_contract import context_value


COMMAND = "toon-workflow.omni-preview"
ANGLES = {"light_yaws", "light_elevations", "camera_yaws", "camera_elevations"}
OBJECTS = {"reference", "sun", "camera"}
PRESETS = {"LIGHT_9", "LIGHT_16", "LIGHT_36", "LIGHT_64", "MULTI_16", "CUSTOM"}
SETTINGS = ANGLES | OBJECTS | {"preset", "camera_mode", "tile_size", "samples", "page_size",
                               "orbit_distance", "focus_offset", "output_parent"}
SOURCE_OPS = {"read", "plan", "submit"}
TERMINAL = {"completed", "cancelled", "failed"}


def parser():
    cli = Parser(description=__doc__)
    cli.add_argument("--blender", type=Path, default=DEFAULT_BLENDER, help=argparse.SUPPRESS)
    cli.add_argument("--jobs-dir", type=Path, default=DEFAULT_JOBS)
    cli.add_argument("--transactions-dir", help=argparse.SUPPRESS)
    cli.add_argument("--timeout", type=float, default=240)
    mode = cli.add_mutually_exclusive_group()
    for flag in ("--human", "--compact", "--full"):
        mode.add_argument(flag, action="store_true")
    route = cli.add_subparsers(required=True, parser_class=Parser).add_parser("omni-preview",
        description="Operations read, plan, submit, status, cancel and select-page. "
                    "read/plan/submit bind saved source SHA and context. submit uses new --output job directory "
                    "and waits for terminal native worker evidence; use a separate cancel request on that directory. "
                    "status/cancel/select-page use job_directory; select-page adds a 1-based page_index.")
    route.add_argument("--params", type=Path, required=True)
    route.add_argument("--output", type=Path, help="New absolute native preview job directory")
    return cli


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def settings_for(panel, context, values):
    if "preset" in values:
        panel.preset = values["preset"]
    for key, value in values.items():
        if key in OBJECTS:
            obj = context.scene.objects.get(value)
            require(obj is not None and (key == "reference" or (key == "camera" and obj.type == "CAMERA")
                    or (key == "sun" and obj.type == "LIGHT" and obj.data.type == "SUN")),
                    "NOT_FOUND", "Named Omni " + key + " does not match the saved scene")
            setattr(panel, key, obj)
        elif key in ANGLES:
            setattr(panel, key, json.dumps(value))
        elif key == "page_size":
            panel.page_size = str(value)
        elif key != "preset":
            setattr(panel, key, value)


def request_for(args, guards):
    request = read_document(guard_file(args.params.absolute(), guards))
    require(type(request) is dict and {"operation", "edition"} <= request.keys()
            and isinstance(request["edition"], str) and request["edition"] in PRODUCTS,
            "INVALID_REQUEST", "Expected a supported Omni operation and Toon edition")
    op = request["operation"]
    require(isinstance(op, str) and op in SOURCE_OPS | {"status", "cancel", "select-page"},
            "INVALID_REQUEST", "Unknown Omni operation")
    require((args.output is not None) == (op == "submit"), "INVALID_REQUEST", "Only submit requires a new --output")
    common = {"operation", "edition"}
    if op in SOURCE_OPS:
        required = common | {"file", "expected_sha256", "context"}
        require(required <= request.keys() <= required | {"settings", "resources"}, "INVALID_REQUEST",
                "Omni read/plan/submit require saved file, SHA and context")
        request["context"] = context_value(request["context"])
        require(isinstance(request["file"], str) and Path(request["file"]).suffix.lower() == ".blend",
                "INVALID_REQUEST", "Expected a saved .blend")
        request["file"] = str(guard_file(request["file"], guards, request["expected_sha256"]))
        values = request.setdefault("settings", {})
        require(type(values) is dict and values.keys() <= SETTINGS, "INVALID_REQUEST", "Unknown Omni setting")
        checks = {"preset": lambda v: isinstance(v, str) and v in PRESETS,
                  "camera_mode": lambda v: v in ("SOURCE", "ORBIT"),
                  "tile_size": lambda v: type(v) is int and 64 <= v <= 1024,
                  "samples": lambda v: type(v) is int and 1 <= v <= 512,
                  "page_size": lambda v: type(v) is int and v in (9, 16, 36, 64),
                  "orbit_distance": lambda v: finite(v) and v >= 0,
                  "focus_offset": lambda v: type(v) is list and len(v) == 3 and all(finite(x) for x in v),
                  "output_parent": lambda v: isinstance(v, str) and Path(v).is_absolute(),
                  **{field: name for field in OBJECTS},
                  **{field: lambda v: type(v) is list and 1 <= len(v) <= 256 and all(finite(x) for x in v)
                     for field in ANGLES}}
        for key, value in values.items():
            require(checks[key](value), "INVALID_REQUEST", "Invalid Omni " + key)
        resources = request.setdefault("resources", [])
        require(type(resources) is list and len(resources) <= 256, "INVALID_REQUEST", "Expected bounded resource pins")
        for row in resources:
            require(type(row) is dict and set(row) == {"file", "expected_sha256"}, "INVALID_REQUEST", "Unknown resource ref")
            row["file"] = str(guard_file(row["file"], guards, row["expected_sha256"]))
        if op == "submit":
            target = args.output
            require(target.is_absolute() and target.name not in ("", ".", ".."), "INVALID_REQUEST", "Expected a new absolute Omni job")
            parent = canonical(target.parent)
            values.setdefault("output_parent", str(parent))
            require(canonical(values["output_parent"]) == parent, "CONFLICT", "Omni output_parent differs from --output")
            for folder in reversed((parent, *parent.parents)):
                if folder != Path(folder.anchor):
                    guards.enter_context(FileGuard(folder, directory=True))
            target = parent / target.name
            guards.enter_context(PathLocks([target]))
            require(not os.path.lexists(target), "CONFLICT", "Omni job directory already exists")
            request["job_directory"] = str(target)
    else:
        fields = common | {"job_directory"} | ({"page_index"} if op == "select-page" else set())
        require(request.keys() == fields, "INVALID_REQUEST", "Expected an existing job directory and optional operation page index")
        path = canonical(request["job_directory"])
        require(path.is_dir(), "INVALID_REQUEST", "Omni job must be a directory")
        request["job_directory"] = str(path)
        if op == "select-page":
            require(type(request["page_index"]) is int and 1 <= request["page_index"] <= 256,
                    "INVALID_REQUEST", "Invalid 1-based Omni page index")
    request["product"] = native_product(request, guards)
    return request


def panel_state(panel):
    return {key: (getattr(panel, key).name if getattr(panel, key) else None) if key in OBJECTS else
            list(panel.focus_offset) if key == "focus_offset" else getattr(panel, key) for key in sorted(SETTINGS)}


def approved_job(core, directory, package):
    submission = read_json(directory / "submission.json")
    require(submission.get("schema") == core.SCHEMA and submission.get("directory") == str(directory)
            and submission.get("package_directory") == package,
            "CONFLICT", "Job directory belongs to a different Omni edition or root")
    status = core.status(str(directory))
    if status.get("report"):
        require(Path(status["report"]) == directory / "preview/REPORT.json", "CONFLICT", "Unexpected native report path")
    return status


def page_for(directory, pages, index):
    require(index <= len(pages), "NOT_FOUND", "This completed Omni page does not exist")
    row = pages[index - 1]
    path = canonical(row["file"])
    require(path.is_relative_to(directory / "preview") and path.suffix.lower() == ".png"
            and digest(path) == row["sha256"], "CONFLICT", "Omni page is missing, changed or outside its preview")
    return {**row, "page_index": index}


def work(bpy, request, job):
    surface = load_modules(bpy, request)
    ui = importlib.import_module(surface.__package__ + ".omni_preview_ui")
    core = ui.omni_preview
    op = request["operation"]
    directory = Path(request["job_directory"]) if "job_directory" in request else job / "preview"
    result = {"operation": op, "product": request["product"],
              "runtime": {"blender": bpy.app.version_string, "build": bpy.app.build_hash.decode(), "pid": os.getpid()}}
    if op not in SOURCE_OPS:
        state = approved_job(core, directory, request["product"]["directory"])
        if op == "cancel":
            state = core.request_cancel(str(directory))
        if op == "select-page":
            result["page"] = page_for(directory, state.get("pages", []), request["page_index"])
        return {**result, "job_directory": str(directory), "native_status": state}
    ui.register()
    context = open_saved_context(bpy, request)
    panel = ui.settings(context)
    before = bindings(bpy)
    source_refs = {os.path.normcase(row["file"]) for row in request["resources"]}
    resources = ui.omni_preview._source_linear_name(context.scene) if op != "read" else None
    ui.read_current(context)
    settings_for(panel, context, request["settings"])
    result.update(source={"file": request["file"], "sha256": request["expected_sha256"]},
                  context=request["context"], panel=panel_state(panel))
    if op in ("plan", "submit"):
        doc, plan = core.normalize(ui._document(context, directory))
        result.update(document=doc, plan=plan, source_linear=resources)
    if op == "submit":
        workbench = importlib.import_module(surface.__package__ + ".workbench")
        require(all(os.path.normcase(str(Path(path).resolve())) in source_refs for path in workbench.collect_resources()),
                "CONFLICT", "Omni source contains unpinned external resources")
        submitted = core.submit(context, doc)
        result.update(job_directory=str(directory), submission={key: submitted[key] for key in
            ("snapshot", "snapshot_sha256", "pid", "snapshot_host_checks", "total")})
        while True:
            observed = core.status(str(directory))
            if observed["status"] in TERMINAL and observed.get("process_exit_code") is not None:
                break
            time.sleep(.25)
        require(observed["status"] in ("completed", "cancelled"), "WORKER_FAILED",
                "Native Omni worker failed: " + str(observed.get("error") or directory / "stderr.log"))
        result["native_status"] = observed
        result["pages"] = [page_for(directory, observed["pages"], index)
                           for index in range(1, len(observed.get("pages", [])) + 1)]
        if observed["status"] == "completed":
            require(observed["completed"] == plan["tiles"] and len(result["pages"]) == plan["pages"]
                    and all(observed.get("restoration", {}).values()),
                    "VALIDATION_FAILED", "Native Omni worker did not complete the exact frozen plan")
    require(bindings(bpy) == before and digest(request["file"]) == request["expected_sha256"],
            "VALIDATION_FAILED", "Omni preview changed original Blender bindings or source bytes")
    result["source_unchanged"] = True
    return result


def worker(phase, path):
    import bpy

    try:
        request = read_json(path)["params"]
        result = {"ok": True, "data": work(bpy, request, Path(path).parent)}
    except Exception as error:
        result = {"ok": False, "error": error_payload(error, "CONFLICT")}
    atomic_json(Path(path).parent / (phase + "-result.json"), result)


def run(args):
    job = args.jobs_dir.resolve() / uuid.uuid4().hex
    job.mkdir(parents=True)
    deadline = time.monotonic() + timeout_value(args.timeout)
    artifacts = {"job_directory": str(job), "result": str(job / "result.json")}
    data = error = None

    def checkpoint():
        require(time.monotonic() < deadline, "TIMEOUT", "Omni launcher exceeded its deadline")
        require(not (job / "cancel.request").exists(), "CANCELLED", "Omni launcher cancelled")

    def status(state, phase):
        atomic_json(job / "status.json", {"job_id": job.name, "state": state, "phase": phase})

    status("running", "inputs")
    try:
        with ExitStack() as guards:
            request = request_for(args, guards)
            op = request["operation"]
            atomic_json(job / "request.json", {"schema_version": SCHEMA, "command": COMMAND, "params": request})
            status("running", op)
            data = run_worker(canonical(args.blender), job, op, deadline, checkpoint, status, entry=ENTRY, private_temp=True)
            if "job_directory" in data:
                artifacts["native_directory"] = data["job_directory"]
            if "page" in data:
                artifacts["page"] = data["page"]["file"]
    except (Failure, OSError, RuntimeError, ValueError) as exc:
        error = error_payload(exc, "IO_ERROR")
    state = ("cancelled" if data and data.get("native_status", {}).get("status") == "cancelled" else
             "failed" if error else "succeeded")
    for path in sorted(job.glob("*.log")):
        artifacts[path.name] = str(path)
    result = envelope(COMMAND, job.name, data, error, artifacts)
    atomic_json(job / "result.json", result)
    status(state, "done")
    return result


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = None
    try:
        args = parser().parse_args()
        result = run(args)
    except (Failure, OSError, ValueError) as error:
        result = envelope(command=COMMAND, error=error_payload(error, "INVALID_REQUEST"))
    if args and args.human:
        print(result["artifacts"]["result"] if result["ok"] else result["error"]["message"])
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["ok"] else CODES[result["error"]["code"]]


if __name__ == "__main__":
    if "--background" in sys.argv and "--" in sys.argv:
        worker(*sys.argv[sys.argv.index("--") + 1:])
    else:
        raise SystemExit(main())
