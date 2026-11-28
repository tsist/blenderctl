"""Run an offline Blender reader while holding the declared UV input files."""
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import tempfile


def run_saved_reader(blender, request, script, marker, timeout, *worker_args):
    from protocol import Failure, atomic_json, read_json, timeout_value
    from uv_workflow import _guard_inputs

    timeout = timeout_value(timeout)
    with tempfile.TemporaryDirectory(prefix="blenderctl-uv-reader-") as directory, ExitStack() as guards:
        root = Path(directory)
        params_path = root / "params.json"
        result_path = root / "result.json"
        atomic_json(params_path, request)
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("BLENDER_USER_", "BLENDER_SYSTEM_", "PYTHON"))
        }
        environment.update(
            BLENDER_USER_RESOURCES=str(root / "user"), PYTHONDONTWRITEBYTECODE="1",
            TEMP=directory, TMP=directory, TMPDIR=directory,
        )
        command = [
            str(blender), "--background", "--factory-startup", "--offline-mode", "--disable-autoexec",
            "--python-exit-code", "23", "--python", str(Path(script).resolve()),
            "--", marker, str(params_path), str(result_path), *map(str, worker_args),
        ]
        source_guard = _guard_inputs(request["base"], guards)
        completed = subprocess.run(
            command, env=environment, cwd=directory, stdin=subprocess.DEVNULL, capture_output=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode or not result_path.is_file():
            details = (completed.stderr or completed.stdout).strip()[-500:]
            raise Failure("WORKER_FAILED", f"Blender UV worker failed: {details}")
        result = read_json(result_path)
        if "error" in result:
            raise Failure(result["error"]["code"], result["error"]["message"])
        if source_guard.sha256() != request["base"]["expected_sha256"]:
            raise Failure("CONFLICT", "Source changed during UV handoff")
        return result["data"]


def new_destination(path, suffix):
    from protocol import Failure

    if path is None:
        return None
    if not path.is_absolute() or path.suffix.lower() != suffix or not path.parent.is_dir():
        raise Failure("INVALID_REQUEST", f"Expected a new absolute {suffix} path in an existing directory")
    if os.path.lexists(path):
        raise Failure("CONFLICT", f"Destination already exists: {path}")
    return path.resolve()
