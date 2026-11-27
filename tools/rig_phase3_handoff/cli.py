# SPDX-License-Identifier: GPL-3.0-or-later
"""Public Win53 rigging candidate and owned live handoff."""
import json
import os
from pathlib import Path
import sys
import time
import uuid
from contextlib import ExitStack


sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_pin import BUILD, EDITION, SCENE_FIELDS, activate, execution_inputs, open_scene, scene_input, user_environment
from filesystem import FileGuard, PathLocks
from handoff_io import Parser, canonical, error_payload, guard_file, read_document, require, run_worker
from protocol import CODES, DEFAULT_JOBS, Failure, atomic_json, envelope, read_json, timeout_value


COMMAND = "rigging-workflow.phase3"
ENTRY = Path(__file__).resolve()
BASE_FIELDS = {"operation", "parameters"}
FACTORY_OPERATIONS = {"retarget-fbx-inspect", "retarget-profile-build"}


def operation_modules():
    import posing_ops
    import retarget_ops

    return {name: module for module in (posing_ops, retarget_ops) for name in module.OPERATIONS}


def request_data(args, held):
    request = read_document(guard_file(args.params.absolute(), held))
    require(isinstance(request, dict) and BASE_FIELDS <= request.keys()
            and request.keys() <= BASE_FIELDS | SCENE_FIELDS, "INVALID_REQUEST",
            "Use operation, parameters, and the saved scene fields where applicable")
    operation = request["operation"]
    modules = operation_modules()
    require(isinstance(operation, str) and operation in modules and isinstance(request["parameters"], dict),
            "INVALID_REQUEST", "Expected a supported operation and a parameters object")
    if operation in FACTORY_OPERATIONS:
        require(request.keys() == BASE_FIELDS, "INVALID_REQUEST", "Use a factory scene for FBX source-only operations")
    else:
        request = scene_input(request, held)
    module = modules[operation]
    try:
        module.validate(operation, request["parameters"])
        request["parameters"] = module.guard_inputs(operation, request["parameters"], held)
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError(str(exc)) from exc
    require((args.output is None) == (operation in module.READ_ONLY), "INVALID_REQUEST",
            "Supply --output for candidate generation and omit it for read-only operations")
    return request


def output_stage(output, held, job):
    require(output.is_absolute() and output.suffix.lower() == ".blend", "INVALID_REQUEST",
            "Use a new absolute .blend output")
    parent = canonical(output.parent)
    for path in reversed((parent, *parent.parents)):
        if path != Path(path.anchor):
            held.enter_context(FileGuard(path, directory=True))
    held.enter_context(PathLocks([output]))
    require(not os.path.lexists(output), "CONFLICT", "Candidate output already exists")
    return parent / (".rigging-phase3-" + job.name + ".blend")


def work(phase, request_file):
    import bpy

    job = request_file.parent
    data = read_json(request_file)
    request = data["params"]
    operation = request["operation"]
    module = operation_modules()[operation]
    if "file" in request:
        open_scene(bpy, data["stage"] if phase == "verify" else request["file"], request)
    else:
        require(bpy.ops.wm.read_factory_settings(use_empty=True) == {"FINISHED"},
                "WORKER_FAILED", "Factory scene could not be initialized")
    runtime = activate(bpy, job / (phase + "-package"), data["package"], background=True)
    if phase == "verify":
        authored = read_json(job / "author-result.json")["data"]
        result = module.verify(bpy, operation, request["parameters"], authored["result"])
    else:
        result = module.execute(bpy, operation, request["parameters"], job)
        if "stage" in data:
            require(bpy.ops.wm.save_as_mainfile(filepath=data["stage"], copy=True, relative_remap=True,
                                                check_existing=False) == {"FINISHED"},
                    "VALIDATION_FAILED", "Could not save the candidate")
    require(isinstance(result, dict), "WORKER_FAILED", "Native adapter must return a result object")
    return {"worker_pid": os.getpid(), "runtime": runtime, "result": result}


def worker(phase, request):
    try:
        reply = {"ok": True, "data": work(phase, request)}
    except Exception as exc:
        reply = {"ok": False, "error": error_payload(exc, "WORKER_FAILED")}
    atomic_json(request.parent / (phase + "-result.json"), reply)


def run(args):
    deadline = time.monotonic() + timeout_value(args.timeout)
    job = args.jobs_dir.resolve() / uuid.uuid4().hex
    job.mkdir(parents=True)
    artifacts = {"job_directory": str(job), "result": str(job / "result.json")}
    result, error = None, None

    def checkpoint():
        require(time.monotonic() < deadline, "TIMEOUT", "Rigging job timed out")
        require(not (job / "cancel.request").exists(), "CANCELLED", "Rigging job cancelled")

    def heartbeat(state, phase):
        atomic_json(job / "status.json", {"job_id": job.name, "state": state, "phase": phase,
                                          "heartbeat_at": time.time()})

    def execute(phase):
        heartbeat("running", phase)
        environment = user_environment(job / (phase + "-profile"))
        return run_worker(blender, job, phase, deadline, checkpoint, heartbeat, ENTRY, environment)

    try:
        with ExitStack() as held:
            blender, package = execution_inputs(args, held)
            request = request_data(args, held)
            data = {"command": COMMAND, "params": request, "package": package}
            if args.output is not None:
                stage = output_stage(args.output, held, job)
                held.callback(stage.unlink, missing_ok=True)
                data["stage"] = str(stage)
            atomic_json(job / "request.json", data)
            authored = execute("author")
            result = {"operation": request["operation"], "runtime": authored["runtime"], **authored["result"]}
            if "file" in request:
                result["source"] = {"file": request["file"], "sha256": request["expected_sha256"]}
                result["source_unchanged"] = True
            if args.output is not None:
                with FileGuard(stage) as guard:
                    cold = execute("verify")
                    require(cold["runtime"] == authored["runtime"] and cold["worker_pid"] != authored["worker_pid"],
                            "VALIDATION_FAILED", "Independent cold worker identity differs")
                    sha = guard.sha256(checkpoint)
                with FileGuard(stage, renameable=True) as guard:
                    require(guard.sha256(checkpoint) == sha, "CONFLICT", "Candidate changed after cold reopen")
                    checkpoint()
                    guard.rename_new(args.output)
                result["candidate"] = {"file": str(args.output), "sha256": sha}
                result["independent_reopen"] = cold
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, Failure) as exc:
        result, error = None, error_payload(exc, "INVALID_REQUEST")
    reply = envelope(COMMAND, job.name, result, error, artifacts)
    atomic_json(job / "result.json", reply)
    heartbeat({"CANCELLED": "cancelled", "TIMEOUT": "timed_out"}.get(error["code"], "failed")
              if error else "succeeded", "done")
    return reply


def describe():
    modules = operation_modules()
    return envelope(COMMAND, data={"edition": EDITION, "build": BUILD,
                                    "package_required_for_execution": True,
                                    "operations": {name: {"writes_candidate": name not in module.READ_ONLY,
                                                          "requires_saved_scene": name not in FACTORY_OPERATIONS}
                                                   for name, module in sorted(modules.items())},
                                    "live": ["live-open", "live-status", "live-capture", "live-result", "live-close"]})


def main():
    import live_session

    sys.stdout.reconfigure(encoding="utf-8")
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--blender", type=Path, help="Absolute Windows Blender 5.3.0 executable")
    parser.add_argument("--rigging-package", type=Path, help="Absolute ZIP built from the Win53 rigging source")
    parser.add_argument("--rigging-package-sha256", help="Lowercase SHA-256 of that ZIP")
    parser.add_argument("--jobs-dir", type=Path, default=DEFAULT_JOBS)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--transactions-dir", help="Unused by this working-copy command")
    display = parser.add_mutually_exclusive_group()
    for flag in ("--compact", "--human", "--full"):
        display.add_argument(flag, action="store_true")
    root = parser.add_subparsers(required=True, parser_class=Parser).add_parser("rigging-workflow")
    family = root.add_subparsers(required=True, parser_class=Parser).add_parser("phase3")
    actions = family.add_subparsers(required=True, parser_class=Parser)
    actions.add_parser("describe", help="List operations and the bound Blender/add-on edition").set_defaults(
        handler=lambda _: describe())
    job = actions.add_parser("run", help="Run a typed saved-file operation in an isolated Blender worker")
    job.add_argument("--params", type=Path, required=True)
    job.add_argument("--output", type=Path, help="New absolute .blend for a mutating operation")
    job.set_defaults(handler=run)
    live_session.add_commands(actions)
    try:
        args = parser.parse_args()
        reply = args.handler(args)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, Failure) as exc:
        reply = envelope(COMMAND, error=error_payload(exc, "INVALID_REQUEST"))
    print(json.dumps(reply, ensure_ascii=False, allow_nan=False))
    return 0 if reply["ok"] else CODES[reply["error"]["code"]]


if __name__ == "__main__":
    if "--background" in sys.argv and "--" in sys.argv:
        phase, filename = sys.argv[sys.argv.index("--") + 1:]
        worker(phase, Path(filename))
    else:
        raise SystemExit(main())
