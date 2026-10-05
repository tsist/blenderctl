# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in dispatch to a reviewed, separate Hard Surface Workbench checkout.

Selecting a checkout is permission to execute its Python source, not a sandbox.
No plugin code is imported into blenderctl, discovered on PATH, or installed.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tomllib
import uuid

VERSION = "0.2.0"
ROOT_ENV = "BLENDERCTL_HARDSURFACE_ROOT"
_VALUE_FLAGS = {"--blender", "--jobs-dir", "--transactions-dir", "--timeout"}
_SWITCH_FLAGS = {"--async", "--human", "--compact"}
_PATH_FLAGS = {"--jobs-dir", "--request", "--cases", "--reference-approval", "--file", "--destination"}
_REQUIRED_FILES = (
    "blender_manifest.toml", "hardsurface/__init__.py", "hardsurface/host.py",
    "hardsurface/io.py", "hardsurface/jobs.py", "hardsurface/linux_file_guard.py",
    "hardsurface/protection.py", "hardsurface/budgets.py",
    "blender_worker.py", "schemas/request.schema.json", "hardsurface-cli",
)


class DispatchFailure(ValueError):
    def __init__(self, detail_code, message):
        self.detail_code = detail_code
        super().__init__(message)


def _scan(argv):
    """Read global flags only; never interpret a later argument as a command."""
    globals_ = []
    i = 0
    options = _VALUE_FLAGS | _SWITCH_FLAGS | {"--help", "--version"}
    while i < len(argv):
        token = argv[i]
        if not token.startswith("-"):
            return token, globals_, argv[i:]
        option, separator, value = token.partition("=")
        if option not in options:
            matches = [name for name in options if option.startswith("--") and name.startswith(option)]
            if len(matches) != 1:
                return None, [], []  # Baseline argparse owns unknown/ambiguous flags.
            option = matches[0]
        if option in {"--help", "--version"}:
            return None, [], []
        if option in _VALUE_FLAGS:
            if not separator:
                i += 1
                if i >= len(argv) or (argv[i].startswith("-") and not re.fullmatch(r"-\d+(?:\.\d*)?|-\.\d+", argv[i])):
                    return None, [], []
                value = argv[i]
            globals_.append((option, value))
        elif option in _SWITCH_FLAGS and not separator:
            globals_.append((option, None))
        else:
            return None, [], []
        i += 1
    return None, [], []


def _checked_path(path, *, directory=False):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise DispatchFailure("HARDSURFACE_UNSAFE_PATH", "An absolute path without '..' is required")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if stat.S_ISLNK(current.lstat().st_mode):
            raise DispatchFailure("HARDSURFACE_UNSAFE_PATH", "Symlink path components are not accepted")
    info = path.stat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise DispatchFailure("HARDSURFACE_UNSAFE_PATH", "Expected a regular file or directory")
    return path


def _read(path, limit=256 * 1024):
    _checked_path(path)
    # Bound reads, reject nonregular files and final-component substitution.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise DispatchFailure("HARDSURFACE_UNSAFE_PATH", "Expected a regular metadata file")
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise DispatchFailure("HARDSURFACE_INVALID_METADATA", "Metadata exceeds its size limit")
    return value.decode("utf-8")


def validate_root(value):
    if not value:
        raise DispatchFailure("HARDSURFACE_ROOT_REQUIRED", f"Set {ROOT_ENV} to an absolute, reviewed Hard Surface Workbench {VERSION} source checkout")
    root = _checked_path(value, directory=True)
    for name in _REQUIRED_FILES:
        _checked_path(root / name)
        if name.endswith(".py"):
            ast.parse(_read(root / name, 1024 * 1024), filename=name)
    # Reject importable escape paths even when a module is imported lazily.
    count = 0
    for parent, directories, files in os.walk(root / "hardsurface", followlinks=False):
        for name in directories + files:
            path = Path(parent) / name
            count += 1
            if count > 2048:
                raise DispatchFailure("HARDSURFACE_INVALID_METADATA", "Package file count exceeds the source-checkout bound")
            if path.is_symlink():
                raise DispatchFailure("HARDSURFACE_UNSAFE_PATH", "Symlinks are not accepted in the Python package tree")
            if path.is_file() and (path.suffix in {".so", ".pyd"} or (path.suffix == ".pyc" and path.parent.name != "__pycache__")):
                raise DispatchFailure("HARDSURFACE_UNSAFE_PATH", "Importable binary or sourceless modules are not accepted in a source checkout")
            if path.suffix == ".py" and path.is_file():
                ast.parse(_read(path, 1024 * 1024), filename=str(path.relative_to(root)))
    manifest = tomllib.loads(_read(root / "blender_manifest.toml"))
    if manifest.get("schema_version") != "1.0.0" or manifest.get("id") != "hard_surface_workbench" or manifest.get("type") != "add-on":
        raise DispatchFailure("HARDSURFACE_INVALID_MANIFEST", "The selected checkout has an unexpected extension manifest")
    if manifest.get("version") != VERSION:
        raise DispatchFailure("HARDSURFACE_VERSION_MISMATCH", f"This integration requires plugin version {VERSION}")
    module = ast.parse(_read(root / "hardsurface/__init__.py"))
    versions = []
    for node in module.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets):
            versions.append(ast.literal_eval(node.value))
    if versions != [VERSION]:
        raise DispatchFailure("HARDSURFACE_VERSION_MISMATCH", "The package version must statically match the manifest")
    return root


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate owner marker key")
        value[key] = item
    return value


def owns_job_store(value):
    """Read existing identity only; never create/adopt a store during routing."""
    root = Path(value).absolute()
    marker = root / "owner.json"
    if not os.path.lexists(marker):
        return False
    _checked_path(root, directory=True)
    data = json.loads(_read(marker, 4096), object_pairs_hook=_unique_pairs)
    if not isinstance(data, dict) or data.get("kind") != "hardsurface_job_store":
        return False
    expected = {"kind": "hardsurface_job_store", "version": 1, "root": str(root), "uid": os.geteuid()}
    if data != expected or type(data.get("version")) is not int or type(data.get("uid")) is not int or root.stat().st_uid != os.geteuid() or marker.stat().st_uid != os.geteuid():
        raise DispatchFailure("HARDSURFACE_JOB_OWNER_MISMATCH", "Hard-surface job-store identity does not match its location or current user")
    return True


def _absolute(value, cwd):
    return str(Path(cwd) / value)


def _arguments(globals_, tail, cwd):
    result = []
    for flag, value in globals_:
        if flag in {"--human", "--timeout", "--transactions-dir"}:
            raise DispatchFailure("HARDSURFACE_UNSUPPORTED_GLOBAL", f"{flag} is not supported by Hard Surface Workbench; use request budgets and JSON output")
        if value is None:
            result.append(flag)
        else:
            if flag in _PATH_FLAGS or flag == "--blender":
                value = _absolute(value, cwd)
            result.extend((flag, value))
    i = 0
    while i < len(tail):
        token = tail[i]
        flag, separator, value = token.partition("=")
        if flag not in _PATH_FLAGS and flag.startswith("--") and any(item.startswith(flag) for item in _PATH_FLAGS):
            raise DispatchFailure("HARDSURFACE_FULL_PATH_FLAG_REQUIRED", "Use the full spelling of path options so they can be resolved before dispatch")
        if flag in _PATH_FLAGS:
            if separator:
                token = flag + "=" + _absolute(value, cwd)
            elif i + 1 < len(tail) and not tail[i + 1].startswith("--"):
                result.extend((flag, _absolute(tail[i + 1], cwd)))
                i += 2
                continue
        result.append(token)
        i += 1
    return result


def dispatch(argv):
    """Return None for legacy commands, otherwise the child's exact exit code."""
    command, globals_, tail = _scan(argv)
    if command not in {"hardsurface", "job"}:
        return None
    try:
        if not sys.platform.startswith("linux"):
            if command == "job":
                return None
            raise DispatchFailure("HARDSURFACE_PLATFORM_UNSUPPORTED", "Hard Surface Workbench integration is Linux-only")
        from protocol import DEFAULT_JOBS
        jobs = next((value for flag, value in reversed(globals_) if flag == "--jobs-dir"), None)
        if command == "job":
            jobs = jobs if jobs is not None else str(DEFAULT_JOBS)
            if not owns_job_store(jobs):
                return None
            if not any(flag == "--jobs-dir" for flag, _ in globals_):
                globals_.append(("--jobs-dir", jobs))
        elif jobs is None and os.environ.get("BLENDERCTL_JOBS_DIR"):
            globals_.append(("--jobs-dir", os.environ["BLENDERCTL_JOBS_DIR"]))
        arguments = _arguments(globals_, tail, os.getcwd())
        root = validate_root(os.environ.get(ROOT_ENV))
        env = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
        env.update(PYTHONPATH=str(root), PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
        # -B suppresses writes; a fresh nonexistent prefix also avoids stale .pyc reads.
        cache = "/tmp/blenderctl-hardsurface-unused-bytecode-" + uuid.uuid4().hex
        return subprocess.run(
            [sys.executable, "-B", "-S", "-P", "-X", "pycache_prefix=" + cache, "-m", "hardsurface.host", *arguments],
            cwd=root, env=env, check=False,
        ).returncode
    except (DispatchFailure, OSError, ValueError, SyntaxError, TypeError, RecursionError) as exc:
        from protocol import envelope
        print(json.dumps(envelope(error={"code": "INVALID_REQUEST", "detail_code": getattr(exc, "detail_code", "HARDSURFACE_DISPATCH_FAILED"), "message": str(exc)}), ensure_ascii=False, allow_nan=False))
        return 2
