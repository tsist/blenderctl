"""Public host adapter for the camera-motion and VSE finishing workers."""
import argparse
from contextlib import ExitStack
import json
import math
from pathlib import Path
import subprocess
import sys
import uuid

from processes import ProcessTree
from filesystem import FileGuard, PathLocks
from protocol import CODES, Failure, envelope


CORE = Path(__file__).resolve().parent
WORKERS = {
    "motion": ("camera_motion.py", {"generate", "diagnose"}),
    "finish": ("c5_video_finish.py", {"rough", "conform", "export"}),
}
COMMANDS = {"motion": "camera.motion", "finish": "video.finish"}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure("INVALID_REQUEST", message)


def declared_files(action, request):
    if action == "motion":
        yield request["file"], request["expected_sha256"]
        for entry in request.get("resources", []):
            yield entry["file"], entry["sha256"]
    elif request["operation"] == "rough":
        for entry in request["resources"]:
            yield entry["file"], entry["expected_sha256"]
    else:
        source = request["source"]
        yield source["file"], source["sha256"]
        for shot in request.get("shots", []):
            for frame in shot["frames"]:
                yield frame["path"], frame["sha256"]


def guard_inputs(guards, action, request):
    sources = {}
    for raw, expected in declared_files(action, request):
        path = Path(raw)
        if not path.is_absolute():
            raise Failure("INVALID_REQUEST", f"Expected an absolute input path: {raw}")
        path = path.resolve(strict=True)
        if path in sources:
            if sources[path] != expected:
                raise Failure("CONFLICT", f"Conflicting input fingerprints: {path}")
            continue
        source = guards.enter_context(FileGuard(path))
        if source.sha256() != expected:
            raise Failure("CONFLICT", f"Input SHA-256 differs: {path}")
        sources[path] = expected
    return set(sources)


def dispatch(args, guards):
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("Supply a finite positive timeout in seconds")
    snapshot_input = guards.enter_context(FileGuard(args.params))
    request = json.loads(b"".join(snapshot_input.chunks()).decode("utf-8-sig"))
    if not isinstance(request, dict):
        raise ValueError("Expected a JSON request object")
    worker, operations = WORKERS[args.action]
    if request.get("operation") not in operations:
        raise ValueError("Expected one of: " + ", ".join(sorted(operations)))
    if args.jobs_dir and not args.jobs_dir.is_absolute():
        raise ValueError("--jobs-dir must be absolute")
    job = (args.jobs_dir or CORE.parents[1] / "runtime/blenderctl/jobs") / uuid.uuid4().hex
    receipt = Path(request.get("report", str(job / "result.json")))
    if not receipt.is_absolute() or receipt.exists():
        raise ValueError("The worker report must use a new absolute path")
    inputs = guard_inputs(guards, args.action, request)
    targets = [receipt]
    for key in ("output", "directory"):
        if key in request:
            destination = Path(request[key])
            if not destination.is_absolute():
                raise ValueError("Output paths must be absolute")
            targets.append(destination)
    if len(set(p.resolve() for p in targets)) != len(targets) or any(p.resolve() in inputs for p in targets):
        raise Failure("CONFLICT", "Worker outputs must be distinct from each other and from inputs")
    guards.enter_context(PathLocks(targets))
    if any(path.exists() for path in targets):
        raise Failure("CONFLICT", "Worker output already exists")
    # Freeze the exact request consumed by the worker. Each call has its own job.
    job.mkdir(parents=True)
    request["report"] = str(receipt)
    snapshot = job / "request.json"
    snapshot.write_text(json.dumps(request, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    argv = [str(args.blender.resolve(strict=True)), "-b", "--factory-startup", "--disable-autoexec",
            "--offline-mode", "-t", "4", "--python-exit-code", "5", "-P", str(CORE / worker), "--", str(snapshot)]
    (job / "argv.json").write_text(json.dumps(argv), encoding="utf-8")
    with (job / "worker.log").open("xb") as log:
        tree = ProcessTree(argv, cwd=str(job), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            code = tree.process.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            tree.terminate()
            tree.process.wait()
            code = 6
        finally:
            tree.close()
    data = None
    error = None
    files = {"job_directory": str(job), "log": str(job / "worker.log")}
    if code == 0:
        data = json.loads(receipt.read_bytes())
        files["report"] = str(receipt)
    else:
        error = {"code": "TIMEOUT" if code == 6 else "WORKER_FAILED",
                 "message": "Worker timed out" if code == 6 else "Blender worker failed"}
        code = CODES[error["code"]]
    result = envelope(COMMANDS[args.action], job.name, data=data, error=error, artifacts=files)
    result.update(files, exit_code=code)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return code


def main():
    parser = Parser(description=__doc__)
    parser.add_argument("--blender", required=True, type=Path)
    parser.add_argument("--jobs-dir", type=Path)
    parser.add_argument("--transactions-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("action", choices=WORKERS)
    parser.add_argument("--params", required=True, type=Path)
    try:
        args = parser.parse_args()
        with ExitStack() as guards:
            return dispatch(args, guards)
    except Failure as error:
        print(json.dumps(envelope(error={"code": error.code, "message": str(error)}), ensure_ascii=True))
        return CODES[error.code]
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps(envelope(error={"code": "INVALID_REQUEST", "message": str(error)}), ensure_ascii=True))
        return CODES["INVALID_REQUEST"]


def compose(binary, options, argv, *, compact=False):
    """Run the C5 sequence compositor in a fresh, offline Blender process."""
    parser = Parser(prog="blenderctl compositing compose-c5", description="Compose one C5 shot from a verified EXR handoff")
    parser.add_argument("handoff", type=Path)
    parser.add_argument("table", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--ocio-profile", type=Path)
    parser.add_argument("--finishing", type=Path)
    args = parser.parse_args(argv)
    timeout = float(next((flag.split("=", 1)[1] for flag in options if flag.startswith("--timeout=")), 3600))
    if not math.isfinite(timeout) or timeout <= 0:
        raise Failure("INVALID_REQUEST", "Supply a finite positive timeout in seconds")
    output = args.output.resolve()
    if output.exists():
        raise Failure("CONFLICT", f"C5 output already exists: {output}")
    files = [str(args.handoff.resolve(strict=True)), str(args.table.resolve(strict=True)), str(output)]
    for name in ("ocio_profile", "finishing"):
        path = getattr(args, name)
        if path:
            files.extend(("--" + name.replace("_", "-"), str(path.resolve(strict=True))))
    command = [str(binary), "-b", "--factory-startup", "--disable-autoexec", "--offline-mode",
               "--python-exit-code", "5", "-P", str(CORE / "compose_c5.py"), "--", *files]
    tree = ProcessTree(command, stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr)
    try:
        code = tree.process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        tree.terminate()
        tree.process.wait()
        raise Failure("TIMEOUT", f"C5 worker timed out after {timeout:g} seconds") from exc
    finally:
        tree.close()
    report = output / "handoff.json"
    if code != 0 or not report.is_file():
        raise Failure("WORKER_FAILED", f"C5 worker did not produce a handoff (exit {code})")
    result = envelope("compositing.compose_c5", artifacts={"handoff": str(report)})
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":") if compact else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
