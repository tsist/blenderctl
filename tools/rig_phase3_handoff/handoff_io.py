# SPDX-License-Identifier: GPL-3.0-or-later
"""File guards, result transport and an isolated Blender worker for Phase 3."""
import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from filesystem import FileGuard, PathLocks
from processes import ProcessTree
from protocol import CODES, Failure, read_json


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure("INVALID_REQUEST", message)


def require(condition, code, detail):
    if not condition:
        raise Failure(code, detail)


def canonical(path):
    path = Path(path)
    require(path.is_absolute(), "INVALID_REQUEST", f"Expected an absolute path: {path}")
    resolved = path.resolve(strict=True)
    require(Path(os.path.abspath(path)) == resolved, "CONFLICT", f"Redirected path: {path}")
    return resolved


def guard_file(path, stack, expected=None):
    path = canonical(path)
    for parent in reversed(path.parents):
        if parent != Path(parent.anchor):
            stack.enter_context(FileGuard(parent, directory=True))
    guard = stack.enter_context(FileGuard(path))
    if expected is not None:
        require(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected),
                "INVALID_REQUEST", f"Expected lowercase SHA-256 for {path}")
        require(guard.sha256() == expected, "CONFLICT", f"SHA-256 mismatch: {path}")
    return path


def read_document(path):
    require(path.suffix.lower() == ".json" and path.stat().st_size <= 16 * 1024 * 1024,
            "INVALID_REQUEST", f"Expected a JSON file up to 16 MiB: {path}")
    return read_json(path)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def error_payload(exc, default):
    code = getattr(exc, "code", "NOT_FOUND" if isinstance(exc, FileNotFoundError) else default)
    return {"code": code if code in CODES else default, "message": str(exc)}


@contextmanager
def admission_lock(directory_marker):
    deadline = time.monotonic() + 2
    with ExitStack() as held:
        while True:
            try:
                held.enter_context(PathLocks([directory_marker]))
                break
            except Failure as exc:
                if exc.code != "CONFLICT" or time.monotonic() >= deadline:
                    raise
                time.sleep(.02)
        yield


def read_result(path):
    try:
        result = read_json(path)
    except Failure as exc:
        if exc.code == "NOT_FOUND":
            return None, None
        raise
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) in (5, 32, 33):
            return None, exc
        raise
    require(isinstance(result, dict), "INVALID_REQUEST", "Persisted session result must be an object")
    return result, None


def run_worker(blender, job, phase, deadline, checkpoint, heartbeat, entry, environment):
    argv = [str(blender), "--background", "--factory-startup", "--disable-autoexec", "--offline-mode",
            "--threads", "2", "--python-exit-code", "23", "--python", str(entry), "--", phase,
            str(job / "request.json")]
    with (job / (phase + ".log")).open("xb") as log:
        tree = ProcessTree(argv, cwd=str(job), env=environment, stdin=subprocess.DEVNULL,
                           stdout=log, stderr=subprocess.STDOUT)
        try:
            while True:
                checkpoint()
                try:
                    exit_code = tree.process.wait(timeout=min(2, max(.01, deadline - time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    heartbeat("running", phase)
        except BaseException:
            tree.terminate()
            raise
        finally:
            tree.close()
    checkpoint()
    result = job / (phase + "-result.json")
    require(exit_code == 0 and result.is_file(), "WORKER_FAILED", f"{phase} exited {exit_code}. See {phase}.log")
    payload = read_json(result)
    if not payload["ok"]:
        raise Failure(payload["error"]["code"], payload["error"]["message"])
    return payload["data"]
