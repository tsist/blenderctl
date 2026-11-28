"""Read the Render Batch 0.3.0 sidebar's project lookup, receipts, log and media."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib

ENTRY = Path(__file__).resolve()
sys.path.insert(0, str(ENTRY.parent))
from protocol import CODES, DEFAULT_BLENDER, Failure, atomic_json, digest, envelope, read_json, timeout_value

RELEASE = ENTRY.parents[2] / "opensource/blender-render-batch"
ADDON = RELEASE / "runtime/tools/render_batch_workflow_addon"
COMMAND = "render.batch-panel"
OPERATIONS = ("scan-project", "inspect", "status", "cli-log", "review", "preview", "final")


def checked_file(path, root):
    if not path.is_file():
        raise Failure("NOT_FOUND", f"Batch file is absent: {path.name}")
    if path.resolve() != path or not path.is_relative_to(root):
        raise Failure("VALIDATION_FAILED", f"Expected a file inside the selected batch: {path.name}")
    return str(path)


def observe(request):
    # Import the released sidebar in an isolated background Blender. Its scan limits,
    # ordering, legacy manifests and status interpretation are shared with the GUI.
    import bpy
    sys.dont_write_bytecode = True
    with (ADDON/"blender_manifest.toml").open("rb") as stream:
        edition = tomllib.load(stream)["version"]
    if edition != "0.3.0":
        raise Failure("UNSUPPORTED", f"Unexpected render panel edition: {edition}")
    sys.path.insert(0, str(ADDON.parent))
    import render_batch_workflow_addon as panel

    operation = request["operation"]
    result = {"operation": operation, "panel_version": edition, "blender": bpy.app.version_string}
    if operation == "scan-project":
        jobs = Path(request["jobs"])
        scan = panel.find_latest_project_batch(jobs, request["project"])
        batch = scan.pop("path")
        result.update(scan, jobs_directory=str(jobs), project=request["project"], matched=batch is not None)
        if batch is None:
            return {**result, "batch_directory": None, "shots": []}
        if batch.resolve() != batch or batch.parent != jobs / "b":
            raise Failure("VALIDATION_FAILED", "Project scan returned a directory outside the selected jobs root")
        launch = panel.read_batch_launch(batch)
        shots = panel._matching_launch_shots(launch, request["project"])
        if not shots:
            raise Failure("CONFLICT", "The launch record no longer contains the scanned project")
        manifest = launch.get("manifest")
        if isinstance(manifest, dict) and isinstance(manifest.get("shots"), list):
            # The panel matches paths. Report the separately recorded submission
            # identity only when the requested shot can be matched unambiguously.
            identities = {}
            for item in manifest["shots"]:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    key = (item["id"], panel._project_key(item.get("project")))
                    identities.setdefault(key, []).append(item.get("expected_sha256"))
            for shot in shots:
                values = identities.get((shot["id"], panel._project_key(shot["project"])), [])
                if len(values) == 1 and isinstance(values[0], str) and re.fullmatch(r"[0-9a-fA-F]{64}", values[0]):
                    shot["submitted_sha256"] = values[0].lower()
        return {**result, "batch_directory": str(batch), "batch_id": batch.name,
                "shots": shots, "launch": checked_file(batch/"launch.json", batch)}

    batch = Path(request["batch"])
    result.update(batch_directory=str(batch), batch_id=batch.name)
    if operation == "cli-log":
        log = batch / panel.CLI_LOG_NAME
        return {**result, "file": checked_file(log, batch), "last_message": panel._read_cli_tail(log)}
    if operation == "inspect":
        # The sidebar can still show submitted shots while status is missing, or
        # show progress for an older batch that no longer has its launch record.
        panel.refresh(str(batch))
        cached = panel._cached
        launch = cached["launch"]
        return {**result, "status": cached["status"], "status_error": cached["status_error"],
                "requested_shots": None if launch is None else launch["shots"],
                "launch_warnings": [] if launch is None else launch["panel_warnings"],
                "launch_error": cached["launch_error"], "last_cli_message": cached["cli_tail"]}
    status = panel.read_batch_status(batch)
    if operation == "status":
        return {**result, "file": checked_file(batch/"status.json", batch), "status": status}
    if operation == "review":
        relative = status.get("review")
    else:
        matches = [shot for shot in status["shots"] if shot["id"] == request["shot_id"]]
        if len(matches) != 1:
            raise Failure("CONFLICT" if matches else "NOT_FOUND", "Expected one exact shot in the batch status")
        relative = matches[0].get(operation)
        result["shot_id"] = request["shot_id"]
    if not relative:
        raise Failure("NOT_FOUND", "The requested movie has not been published")
    media = panel.resolve_batch_artifact(str(batch), relative)
    checked_file(media, batch)
    records = {"review": media.with_suffix(".json")} if operation == "review" else {
        "video": media.parent / (operation + "-video.json"),
        "frames": media.parent / (operation + "-frames.json"),
    }
    result["handoff_records"] = {
        name: {"file": checked_file(path, batch), "sha256": digest(path)}
        for name, path in records.items() if path.is_file()
    }
    return {**result, "media": {"file": str(media), "sha256": digest(media), "bytes": media.stat().st_size}}


def worker(input_path, output_path):
    try:
        result = {"data": observe(read_json(input_path))}
    except Failure as exc:
        result = {"error": {"code": exc.code, "message": str(exc)}}
    except FileNotFoundError as exc:
        result = {"error": {"code": "NOT_FOUND", "message": str(exc)}}
    except (ValueError, RuntimeError, OSError) as exc:
        result = {"error": {"code": "VALIDATION_FAILED", "message": str(exc)}}
    atomic_json(output_path, result)


def run(args):
    scan = args.operation == "scan-project"
    if scan != (args.project is not None) or scan == (args.batch_directory is not None):
        raise Failure("INVALID_REQUEST", "scan-project requires --project. Other operations require --batch-directory")
    if (args.operation in ("preview", "final")) != (args.shot_id is not None):
        raise Failure("INVALID_REQUEST", "Only preview and final require --shot-id")
    request = {"operation": args.operation, "shot_id": args.shot_id}
    if scan:
        if not args.jobs_dir or not args.jobs_dir.is_absolute() or not args.jobs_dir.is_dir():
            raise Failure("INVALID_REQUEST", "Provide the absolute jobs root using global --jobs-dir")
        if not args.project.is_absolute() or not args.project.is_file() or args.project.suffix.lower() != ".blend":
            raise Failure("NOT_FOUND", "Provide an existing saved .blend with an absolute --project path")
        jobs = args.jobs_dir.resolve()
        if jobs.name.casefold() == "b":
            jobs = jobs.parent
        request.update(jobs=str(jobs), project=str(args.project.resolve()))
    else:
        batch = args.batch_directory
        if not batch.is_absolute() or not batch.is_dir() or batch.resolve() != batch:
            raise Failure("NOT_FOUND", "Provide the existing canonical batch directory")
        request["batch"] = str(batch)
    with tempfile.TemporaryDirectory(prefix="blenderctl-render-panel-") as tmp:
        directory = Path(tmp)
        source, output = directory/"input.json", directory/"result.json"
        atomic_json(source, request)
        env = {k:v for k,v in os.environ.items() if not k.startswith(("BLENDER_USER_", "BLENDER_SYSTEM_", "PYTHON"))}
        env.update(BLENDER_USER_RESOURCES=str(directory/"profile"), PYTHONDONTWRITEBYTECODE="1", TEMP=tmp, TMP=tmp)
        proc = subprocess.run([str(args.blender), "--background", "--factory-startup", "--offline-mode", "--disable-autoexec",
                               "--python-exit-code", "23", "--python", str(ENTRY), "--", "read-panel", str(source), str(output)],
                              env=env, cwd=tmp, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=timeout_value(args.timeout), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode or not output.is_file():
            raise Failure("WORKER_FAILED", "Blender failed to read the render batch: " +
                          (proc.stderr or proc.stdout).decode("utf-8","replace")[-500:])
        result = read_json(output)
        if "error" in result:
            raise Failure(result["error"]["code"], result["error"]["message"])
        return result["data"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blender", type=Path, default=DEFAULT_BLENDER)
    parser.add_argument("--jobs-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=60)
    cmd = parser.add_subparsers(required=True).add_parser("batch-panel", description=__doc__)
    cmd.add_argument("--operation", choices=OPERATIONS, required=True)
    cmd.add_argument("--project", type=Path, help="Saved .blend to locate in the global --jobs-dir")
    cmd.add_argument("--batch-directory", type=Path, help="Directory from scan-project or a submission receipt")
    cmd.add_argument("--shot-id", help="Exact shot ID for preview or final")
    data = error = None
    try:
        data = run(parser.parse_args())
    except Failure as exc:
        error = {"code": exc.code, "message": str(exc)}
    except subprocess.TimeoutExpired:
        error = {"code": "TIMEOUT", "message": "Render panel read timed out"}
    except OSError as exc:
        error = {"code": "IO_ERROR", "message": str(exc)}
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(envelope(COMMAND, data=data, error=error), ensure_ascii=False))
    return CODES.get(error["code"], 1) if error else 0


if __name__ == "__main__":
    if "--" in sys.argv and sys.argv[sys.argv.index("--") + 1:][:1] == ["read-panel"]:
        worker(*map(Path, sys.argv[sys.argv.index("--") + 2:]))
    else:
        raise SystemExit(main())
