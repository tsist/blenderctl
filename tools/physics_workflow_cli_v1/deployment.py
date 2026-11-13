"""Locate the read-only shared CLI core and Physics Nodes domain package without mutating tools/blenderctl."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import shutil
import sys


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def core_directory() -> Path:
    configured = os.environ.get("PHYSICS_WORKFLOW_CLI_CORE_ROOT")
    path = Path(configured) if configured else project_root() / "tools" / "blenderctl"
    path = path.resolve(strict=True)
    if not all((path / name).is_file() for name in ("filesystem.py", "processes.py")):
        raise RuntimeError(f"Shared core missing filesystem.py/processes.py: {path}")
    return path


def addon_directory() -> Path:
    path = project_root() / "tools" / "physics_nodes_workflow_addon"
    if not path.is_dir() or not (path / "__init__.py").is_file():
        raise RuntimeError(f"Physics Nodes addon directory missing: {path}")
    return path


def default_blender_binary(require_exists: bool = True) -> Path:
    env_bin = os.environ.get("BLENDER_PATH") or os.environ.get("BLENDERCTL_BLENDER")
    if env_bin:
        p = Path(env_bin)
        if not require_exists or p.is_file():
            return p
    candidate = project_root() / "runtime" / "blender" / "releases" / "5.2.1" / "blender-5.2.1-windows-x64" / "blender.exe"
    if candidate.is_file():
        return candidate
    on_path = shutil.which("blender")
    if on_path:
        return Path(on_path)
    if not require_exists:
        return Path(env_bin) if env_bin else Path("blender")
    raise RuntimeError(f"Default Blender 5.2.1 binary not found at {candidate} (set BLENDER_PATH or BLENDERCTL_BLENDER)")


def import_addon_module(submodule: str | None = None):
    tools_dir = str(addon_directory().parent)
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    name = "physics_nodes_workflow_addon" + (f".{submodule}" if submodule else "")
    return importlib.import_module(name)
