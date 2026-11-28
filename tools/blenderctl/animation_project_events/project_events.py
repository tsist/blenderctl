"""animation.project-events: reviewed multi-event project reads and candidates.

Public surface intended for the existing launcher:

    blenderctl animation project-events --params request.json
    blenderctl animation project-events --params request.json --output NEW.blend

Read operations are inspect, select and review.  Write operations are capture,
adopt, retime, set_relations and set_lock; they require a reviewed plan and a
new --output path. The Blender worker uses the canonical 0.5.0 animation author.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
import uuid
from contextlib import ExitStack
from pathlib import Path


# action: PF-01 formal fixed paths, no search/env/request runtime.
from paths import BLENDER_53
from paths import ROOT

from filesystem import FileGuard, PathLocks
from host_compat import is_supported_host
from material_draft_handoff import guard_file, read_document, require
from material_projection_handoff import run_worker
from protocol import (CODES, DEFAULT_JOBS, Failure, atomic_json,
                      envelope as protocol_envelope, read_json, timeout_value)


COMMAND = "animation.project-events"
REQUEST_SCHEMA = "animation-project-events-request.v1"
PLAN_SCHEMA = "animation-project-events-plan.v1"
RUN_ACTIONS = ("capture", "adopt", "retime", "set_relations", "set_lock")
READ_ACTIONS = ("inspect", "select", "review")
INTEGRATION = Path(__file__).resolve().parent
WORKER = INTEGRATION / "project_events_worker.py"
def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))


def verify_native_runtime(blender):
    runtime = ROOT / "tools"
    package = runtime / "animation_curves_workflow_addon"
    if not (package / "clip_project.py").is_file() or not blender.is_file():
        raise Failure("NOT_FOUND", "The local animation author or selected Blender is missing")
    host = subprocess.run([str(blender), "--version"], capture_output=True, text=True, timeout=15,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    version = re.search(r"^Blender (\d+\.\d+\.\d+)", host.stdout, re.MULTILINE)
    if host.returncode or not version or not is_supported_host(version[1]):
        raise Failure("UNSUPPORTED", "Project events require Windows Blender 5.3.0 or later")
    files = {path.relative_to(package).as_posix(): sha256_file(path)
             for path in sorted(package.rglob("*.py")) if "__pycache__" not in path.parts}
    return {"runtime": "0.5.0", "runtime_dir": str(runtime), "blender": str(blender),
            "blender_sha256": sha256_file(blender),
            "author_sha256": hashlib.sha256(canonical(files).encode("utf-8")).hexdigest()}


# action: PF-01 launcher subcommand and common default jobs.
def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("project-events",))
    parser.add_argument("--params", required=True, type=Path)
    parser.add_argument("--edition", choices=("0.5.0",), default="0.5.0")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--jobs-dir", type=Path,
                        default=DEFAULT_JOBS / "animation_project_events")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--blender", type=Path)
    parser.add_argument("--transactions-dir", type=Path)
    parser.add_argument("--human", action="store_true")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--full", action="store_true")
    return parser.parse_args()


def operation_of(data):
    value = data.get("operation")
    if value == "set-relations":
        value = "set_relations"
    if value == "set-lock":
        value = "set_lock"
    return value


def normalize_request(data):
    if not isinstance(data, dict):
        raise Failure("INVALID_REQUEST", "params must be an object")
    allowed = {"schema", "operation", "file", "expected_sha256", "context",
               "manifest", "event_id", "plan", "resources"}
    unknown = set(data) - allowed
    if unknown:
        raise Failure("INVALID_REQUEST",
                      "unknown params fields: " + ", ".join(sorted(unknown)))
    if "schema" in data and data["schema"] != REQUEST_SCHEMA:
        raise Failure("INVALID_REQUEST",
                      "params.schema must be " + REQUEST_SCHEMA)
    operation = operation_of(data)
    if operation not in READ_ACTIONS + RUN_ACTIONS:
        raise Failure("INVALID_REQUEST",
                      "operation must be inspect, select, review, capture, "
                      "adopt, retime, set_relations or set_lock")
    file = data.get("file")
    expected = data.get("expected_sha256")
    if not isinstance(file, str) or not Path(file).is_absolute() \
            or Path(file).suffix.lower() != ".blend":
        raise Failure("INVALID_REQUEST", "file must be an absolute .blend path")
    if not isinstance(expected, str) or len(expected) != 64 \
            or any(char not in "0123456789abcdef" for char in expected):
        raise Failure("INVALID_REQUEST",
                      "expected_sha256 must be lowercase SHA-256")
    context = data.get("context")
    if not isinstance(context, dict) or set(context) != {"scene", "view_layer"}:
        raise Failure("INVALID_REQUEST",
                      "context requires exactly scene and view_layer")
    require(isinstance(context["scene"], str) and context["scene"],
            "INVALID_REQUEST", "context.scene is required")
    require(isinstance(context["view_layer"], str) and context["view_layer"],
            "INVALID_REQUEST", "context.view_layer is required")
    manifest = data.get("manifest")
    if operation in ("inspect", "select") and manifest is None:
        manifest = {"schema": "animation-event-project-v1",
                    "action": "inspect", "context": context}
    if not isinstance(manifest, dict):
        raise Failure("INVALID_REQUEST", "manifest must be an object")
    if operation in ("inspect", "select") and manifest.get("action") != "inspect":
        raise Failure("INVALID_REQUEST",
                      operation + " requires manifest.action inspect")
    if operation == "review" and manifest.get("action") not in RUN_ACTIONS:
        raise Failure("INVALID_REQUEST", "review manifest.action must be a write action")
    if operation in RUN_ACTIONS and manifest.get("action") != operation:
        raise Failure("INVALID_REQUEST", "manifest.action must be " + operation)
    if manifest.get("context") != context:
        raise Failure("INVALID_REQUEST",
                      "manifest.context must equal params.context")
    event_id = data.get("event_id")
    if event_id is not None and (operation != "select" or not isinstance(event_id, str) or not event_id):
        raise Failure("INVALID_REQUEST", "event_id at the request level requires select and a nonempty string")
    if operation in RUN_ACTIONS:
        plan = data.get("plan")
        if not isinstance(plan, dict) or set(plan) != {"file", "expected_sha256"}:
            raise Failure("INVALID_REQUEST",
                          "write operations require plan {file, expected_sha256}")
        if not isinstance(plan["file"], str) or not Path(plan["file"]).is_absolute():
            raise Failure("INVALID_REQUEST",
                          "plan.file must be absolute")
        if (not isinstance(plan["expected_sha256"], str)
                or len(plan["expected_sha256"]) != 64
                or any(char not in "0123456789abcdef"
                       for char in plan["expected_sha256"])):
            raise Failure("INVALID_REQUEST",
                          "plan.expected_sha256 must be SHA-256")
    elif data.get("plan") is not None:
        raise Failure("INVALID_REQUEST", "read operations do not take a plan")
    resources = data.get("resources", [])
    if not isinstance(resources, list):
        raise Failure("INVALID_REQUEST", "resources must be an array")
    for index, row in enumerate(resources):
        if not isinstance(row, dict) or set(row) != {"file", "expected_sha256"}:
            raise Failure("INVALID_REQUEST",
                          "resources[" + str(index) + "] requires file and expected_sha256")
        if not isinstance(row["file"], str) or not Path(row["file"]).is_absolute():
            raise Failure("INVALID_REQUEST",
                          "resources[" + str(index) + "].file must be absolute")
        if (not isinstance(row["expected_sha256"], str)
                or len(row["expected_sha256"]) != 64
                or any(char not in "0123456789abcdef"
                       for char in row["expected_sha256"])):
            raise Failure("INVALID_REQUEST",
                          "resources[" + str(index) + "].expected_sha256 must be SHA-256")
    return {
        "operation": operation,
        "file": str(Path(file).resolve()),
        "expected_sha256": expected,
        "context": {"scene": context["scene"], "view_layer": context["view_layer"]},
        "manifest": manifest,
        "event_id": event_id,
        "plan": data.get("plan"),
        "resources": resources,
    }


def envelope(job, data=None, error=None, artifacts=None):
    return protocol_envelope(
        COMMAND,
        job.name if job is not None else None,
        data if error is None else None,
        error,
        artifacts or ({"job_directory": str(job)} if job else {}),
    )


def checkpoint_factory(job, deadline):
    def checkpoint():
        require(time.monotonic() < deadline, "TIMEOUT",
                "animation project-events timed out")
        require(not (job / "cancel.request").exists(), "CANCELLED",
                "animation project-events cancelled")
    return checkpoint


def heartbeat(job, phase):
    atomic_json(job / "status.json", {
        "job_id": job.name, "state": "running", "phase": phase,
        "heartbeat_at": time.time()})


# action: PF-04 persistent success/failure receipt.
def write_final_receipt(job, result):
    atomic_json(job / "result.json", result)
    if result["ok"]:
        state = "succeeded"
    else:
        code = (result.get("error") or {}).get("code")
        state = {"TIMEOUT": "timed_out",
                 "CANCELLED": "cancelled"}.get(code, "failed")
    atomic_json(job / "status.json", {
        "job_id": job.name,
        "state": state,
        "phase": "done",
        "finished_at": time.time(),
        "error": result.get("error"),
    })


def finish_success(job, state, data, artifacts):
    result = envelope(job, data=data, artifacts=artifacts)
    try:
        write_final_receipt(job, result)
    except OSError as error:
        data["receipt_error"] = str(error)
        result = envelope(job, data=data, artifacts=artifacts)
    state["finished"] = True
    return result


def run_project_worker(phase, request, job, deadline, checkpoint):
    worker_job = job / phase
    worker_job.mkdir()
    atomic_json(worker_job / "request.json", request)
    heartbeat(job, phase)

    def hb(status, phase):
        heartbeat(job, phase)

    binary = Path(request["runtime"]["blender"])
    return run_worker(
        binary, worker_job, phase, deadline, checkpoint,
        hb, entry=WORKER, private_temp=True)


def select_row(inspection, event_id):
    rows = inspection.get("events") or []
    if not rows:
        raise Failure("NOT_FOUND", "project has no events to select")
    if event_id is None:
        row = sorted(rows, key=lambda item: (item["contact"], item["event_id"]))[0]
    else:
        row = next((item for item in rows if item["event_id"] == event_id), None)
        if row is None:
            raise Failure("NOT_FOUND", "project event not found: " + str(event_id))
    return {
        "event_id": row["event_id"],
        "contact": row["contact"],
        "target_contact": row["contact"],
        "locked": row["locked"],
        "status": row["status"],
        "row": row,
    }


def compare_plan(plan, current_plan):
    failures = []
    if plan.get("review_digest") != current_plan.get("review_digest"):
        failures.append("review_digest")
    if plan.get("timing") != current_plan.get("timing"):
        failures.append("timing")
    for key in ("key_window", "resources", "blend_resources",
                "resource_manifest", "normalized", "runtime"):
        if plan.get(key) != current_plan.get(key):
            failures.append(key)
    if failures:
        raise Failure("CONFLICT",
                      "review differs from the saved plan: "
                      + ", ".join(failures)
                      + " (reviewed=" + str(plan.get("review_digest"))
                      + " current=" + str(current_plan.get("review_digest")) + ")")


# action: PF-02 public resource closure must match the plan.
def publish(candidate, expected_sha256, output, checkpoint):
    output = Path(output).resolve()
    if not output.is_absolute() or output.suffix.lower() != ".blend":
        raise Failure("INVALID_REQUEST", "output must be an absolute .blend path")
    output.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        stack.enter_context(FileGuard(output.parent, directory=True))
        stack.enter_context(PathLocks([output]))
        if os.path.lexists(output):
            raise Failure("CONFLICT", "output already exists: " + str(output))
        with FileGuard(candidate, renameable=True) as guard:
            if guard.sha256(checkpoint) != expected_sha256:
                raise Failure("CONFLICT",
                              "candidate changed before publication")
            checkpoint()
            guard.rename_new(output)
    with FileGuard(output) as guard:
        # Publication already happened.  A late cancel.request must not turn a
        # persistent success into a failure.
        if guard.sha256() != expected_sha256:
            raise Failure("IO_ERROR", "published candidate SHA changed")
    return {"file": str(output), "sha256": expected_sha256,
            "bytes": output.stat().st_size}


def run_native_read(operation, request, data, job, state, artifacts, deadline, checkpoint):
    observed = run_project_worker("inspect", request, job, deadline, checkpoint)
    inspection = observed["inspection"]
    report = {"status": "inspected" if operation == "inspect" else "selected",
              "operation": operation, "source": {"file": request["file"], "sha256": request["expected_sha256"]},
              "runtime": request["runtime"], "inspection": inspection, "checks": observed["checks"],
              "resources": observed["resources"], "worker_pid": observed["worker_pid"]}
    if operation == "select":
        report["selected"] = select_row(inspection, data.get("event_id"))
    return finish_success(job, state, report, {**artifacts, "inspection": str(job / "inspect" / "inspect-result.json")})


def run_native_write(request, current_plan, plan_path, plan_sha, output_path, output_reported,
                     job, state, artifacts, deadline, checkpoint):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    stage = output_path.parent / ("." + output_path.name + "." + job.name + ".stage.blend")
    try:
        written = run_project_worker("apply", {**request, "plan": current_plan, "stage": str(stage)},
                                     job, deadline, checkpoint)
        stage_sha = written["stage_sha256"]
        with FileGuard(stage) as guard:
            require(guard.sha256(checkpoint) == stage_sha, "CONFLICT", "project stage changed before cold reopen")
            cold = run_project_worker("verify", {**request, "file": str(stage), "expected_sha256": stage_sha,
                                     "plan": current_plan, "expected_parent": str(output_path.parent),
                                     "native_animation": written["animation"], "native_resources": written["resources"]},
                                      job, deadline, checkpoint)
            require(cold["status"] == "passed" and cold["worker_pid"] != written["worker_pid"],
                    "VALIDATION_FAILED", "project stage was not independently verified")
        require(verify_native_runtime(Path(request["runtime"]["blender"])) == request["runtime"], "CONFLICT", "Project author changed during the job")
        committed = publish(stage, stage_sha, output_path, checkpoint)
    finally:
        stage.unlink(missing_ok=True)
    return finish_success(job, state, {
        "status": "passed", "operation": current_plan["action"],
        "source": {"file": request["file"], "sha256": request["expected_sha256"]}, "runtime": request["runtime"],
        "plan": {"file": str(plan_path), "sha256": plan_sha},
        "review": {"review_digest": current_plan["review_digest"], "native_review_digest": written["review_digest"]},
        "resources": {"plan": current_plan["resource_manifest"], "native": written["resources"], "stage": cold["resources"]},
        "native_run": {"author": "clip_project.execute", "pid": written["worker_pid"], "execution": written["execution"],
                       "checks": written["checks"]},
        "verification": cold, "candidate": committed, "timing": current_plan["timing"],
        "output": {"reported": output_reported, "resolved": str(output_path)},
        "protected": {"key_window": current_plan.get("key_window"), "review_rechecked": True,
                      "independent_reopen": True, "stage_parent": str(output_path.parent)},
    }, {**artifacts, "plan": str(plan_path), "review_result": str(job / "review" / "review-result.json"),
        "apply_result": str(job / "apply" / "apply-result.json"), "verify_result": str(job / "verify" / "verify-result.json"),
        "candidate": committed["file"], "output_reported": output_reported, "output_resolved": str(output_path)})


def run(args, state):
    timeout_value(args.timeout)
    if args.timeout > 14400:
        raise Failure("INVALID_REQUEST", "timeout must be <= 14400 seconds")
    binary = Path(args.blender).resolve() if args.blender else BLENDER_53
    runtime = verify_native_runtime(binary)
    deadline = time.monotonic() + args.timeout
    jobs_root = Path(args.jobs_dir).resolve()
    jobs_root.mkdir(parents=True, exist_ok=True)
    job = jobs_root / uuid.uuid4().hex
    job.mkdir(parents=True, exist_ok=False)
    state["job"] = job
    with ExitStack() as guards:
        params_path = guard_file(Path(args.params).resolve(), guards)
        data = normalize_request(read_document(params_path))
        source = guard_file(Path(data["file"]), guards, data["expected_sha256"])
        if data["operation"] in READ_ACTIONS and args.output is not None:
            raise Failure("INVALID_REQUEST",
                          "read operations do not take --output")
        output_path = None
        output_reported = None
        if args.output is not None:
            output_reported = str(args.output)
            if Path(args.output).is_symlink():
                raise Failure("CONFLICT",
                              "output symlink is not accepted: "
                              + str(args.output))
            output_path = Path(args.output).resolve()
            if (not output_path.is_absolute()
                    or output_path.suffix.lower() != ".blend"):
                raise Failure("INVALID_REQUEST",
                              "output must be an absolute .blend path")
            if os.path.lexists(output_path):
                raise Failure("CONFLICT",
                              "output already exists: " + str(output_path))
        resources = []
        for index, row in enumerate(data["resources"]):
            resource = guard_file(Path(row["file"]), guards,
                                  row["expected_sha256"])
            resources.append({"file": str(resource),
                              "expected_sha256": row["expected_sha256"]})
        checkpoint = checkpoint_factory(job, deadline)
        heartbeat(job, "start")
        operation = data["operation"]
        manifest = data["manifest"]
        request = {"file": str(source), "expected_sha256": data["expected_sha256"],
                   "context": data["context"], "manifest": manifest, "resources": resources}
        artifacts = {"job_directory": str(job), "params": str(params_path)}
        request.update(edition=args.edition, runtime=runtime)
        if operation in ("inspect", "select"):
            return run_native_read(operation, request, data, job, state, artifacts, deadline, checkpoint)

        if operation == "review":
            reviewed = run_project_worker("review", request, job, deadline, checkpoint)
            plan = reviewed["plan"]
            if plan.get("schema") != PLAN_SCHEMA:
                raise Failure("WORKER_FAILED", "worker returned an unexpected plan")
            plan_path = job / "plan.json"
            atomic_json(plan_path, plan)
            with FileGuard(plan_path) as guard:
                plan_sha = guard.sha256(checkpoint)
            return finish_success(job, state, {
                "status": "reviewed",
                "operation": operation,
                "source": {"file": str(source),
                           "sha256": data["expected_sha256"]},
                "runtime": runtime,
                "plan": {"file": str(plan_path), "sha256": plan_sha,
                         "bytes": plan_path.stat().st_size},
                "review_digest": plan["review_digest"],
                "addon_review_sha256": plan.get("addon_review_sha256"),
                "review_summary": reviewed["review_summary"],
                "timing": plan["timing"],
                "key_window": plan["key_window"],
            }, {
                "job_directory": str(job),
                "params": str(params_path),
                "plan": str(plan_path),
                "review_result": str(job / "review" / "review-result.json"),
                "review_log": str(job / "review" / "review.log"),
            })

        # Write operation: a reviewed plan is mandatory.
        if output_path is None:
            raise Failure("INVALID_REQUEST",
                          "write operations require --output NEW.blend")
        plan_path = guard_file(Path(data["plan"]["file"]).resolve(), guards,
                               data["plan"]["expected_sha256"])
        plan = read_document(plan_path)
        if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
            raise Failure("CONFLICT", "plan schema is not " + PLAN_SCHEMA)
        if plan.get("runtime") != request.get("runtime"):
            raise Failure("CONFLICT", "project plan uses a different Animation Curves Workflow author or edition")
        if plan.get("action") != operation:
            raise Failure("CONFLICT", "plan action differs from params.operation")
        if plan.get("source") != {"file": str(source),
                                  "sha256": data["expected_sha256"]}:
            raise Failure("CONFLICT", "plan belongs to a different source")
        if plan.get("context") != data["context"]:
            raise Failure("CONFLICT", "plan context differs from params.context")
        if canonical(plan.get("manifest")) != canonical(manifest):
            raise Failure("CONFLICT", "plan manifest differs from params.manifest")
        if canonical(plan.get("resources")) != canonical(resources):
            raise Failure("CONFLICT",
                          "plan resources differ from params.resources")
        plan_sha = data["plan"]["expected_sha256"]

        # Re-review the source at apply time before the public write route.
        current = run_project_worker("review", request, job, deadline, checkpoint)
        current_plan = current["plan"]
        compare_plan(plan, current_plan)
        missing = (current_plan.get("resource_manifest") or {}).get("missing") or []
        if missing:
            raise Failure(
                "NOT_FOUND",
                "source has unresolved external resources: "
                + ", ".join(str(row.get("raw")) for row in missing))
        return run_native_write(request, current_plan, plan_path, plan_sha, output_path, output_reported,
                                job, state, artifacts, deadline, checkpoint)


def main():
    args = parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    state = {"job": None}

    def failure_result(error):
        result = envelope(state["job"], error=error)
        if state["job"] is not None and not state.get("finished"):
            try:
                write_final_receipt(state["job"], result)
                state["finished"] = True
            except OSError:
                pass
        return result

    try:
        # run() creates the job; expose it for failure envelopes.
        original = run(args, state)
        result = original
    except Failure as error:
        result = failure_result({
            "code": error.code, "message": str(error)})
    except (OSError, ValueError) as error:
        result = failure_result({
            "code": "INVALID_REQUEST", "message": str(error)})
    except Exception as error:
        if state["job"] is not None:
            (state["job"] / "traceback.txt").write_text(
                traceback.format_exc(), encoding="utf-8")
        result = failure_result({
            "code": "WORKER_FAILED",
            "message": type(error).__name__ + ": " + str(error)})
    if args.human:
        print(("OK " if result["ok"] else "ERROR ") + COMMAND)
        print(json.dumps(result["data"] if result["ok"] else result["error"],
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["ok"] else CODES.get(
        (result["error"] or {}).get("code"), 8)


if __name__ == "__main__":
    raise SystemExit(main())
