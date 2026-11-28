"""File closure for the local, relocatable CLI package."""
from pathlib import Path

from plugin_registry import load_registry, source_path


def package_files(root):
    registry = load_registry(root)
    core = root / "tools/blenderctl"
    files = {root / "blenderctl.ps1", core / "dispatch.ps1", core / "registry.json", *core.glob("*.py")}
    trees = set(registry["package_trees"])
    releases = root / "runtime/blender53-production"
    pointer = releases / "current.json"
    if pointer.is_file():
        from production_contract import pointer_release
        from protocol import Failure, read_json
        current = read_json(pointer)
        files.add(pointer)
        selected = pointer_release(current, releases)
        if selected != "workspace":
            trees.add((releases / selected).relative_to(root).as_posix())
        if "candidate" in current:
            candidate = current["candidate"]
            required = {"release", "manifest", "manifest_sha256"}
            if not isinstance(candidate, dict) or required - candidate.keys():
                raise Failure("VALIDATION_FAILED", "Invalid production candidate pointer")
            release = pointer_release(candidate, releases)
            trees.add((releases / release).relative_to(root).as_posix())
    for row in registry["plugins"]:
        trees.add(row["addon_dir"])
    for row in registry["production"].values():
        trees.add(row["workspace"])
    routing = registry["launcher"]
    entries = list(routing["lookups"].values()) + list(routing["domains"].values())
    entries += [row["entry"] for row in routing["handoffs"].values() if "entry" in row]
    for entry in entries:
        path = source_path(root, entry)
        if not path.is_file():
            raise FileNotFoundError(path)
        files.add(path)
        if path.parent != core:
            trees.add(path.parent.relative_to(root).as_posix())
    for name in sorted(trees):
        base = source_path(root, name)
        if not base.is_dir():
            raise FileNotFoundError(base)
        for file in base.rglob("*"):
            if {"__pycache__", ".git", ".pytest_cache"}.intersection(file.relative_to(base).parts):
                continue
            if file.is_symlink() or file.is_junction():
                raise ValueError(f"Unexpected link in package: {file}")
            if file.is_file() and file.suffix not in {".pyc", ".pyo"}:
                files.add(file)
    for name in registry.get("package_files", []):
        path = source_path(root, name)
        if not path.is_file():
            raise FileNotFoundError(path)
        files.add(path)
    return sorted(files)
