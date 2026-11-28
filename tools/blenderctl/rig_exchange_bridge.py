"""Thin CLI bridge to the single qualified installed exchange runtime.

The bridge does not add the add-on directory to sys.path, does not import
generic development modules, and does not contain a second exchange algorithm.
It verifies the installed package manifest, loads the add-on package by a
qualified file-location spec, and forwards only the two shared-rig entry points.
Discovery order is: explicit C94_RIGGING_ADDON_ROOT (fail-closed), already
imported rigging_workflow, the confirmed C94 product path under ROOT/work,
then the normal Blender user/add-on script paths.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import bpy

from protocol import CODES, Failure, ROOT

_PACKAGE_NAME = "c94_rigging_workflow_runtime"
_CACHE = None
_MANIFEST_NAME = "exchange-runtime-manifest.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _valid_manifest(root: Path) -> tuple[Path, dict]:
    root = root.resolve()
    manifest_path = root / _MANIFEST_NAME
    if not manifest_path.is_file():
        raise Failure(
            "UNSUPPORTED",
            "Installed rigging_workflow add-on has no exchange-runtime manifest: "
            + str(root),
        )
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise Failure("CONFLICT", "Invalid exchange-runtime manifest") from exc
    if set(doc) != {"schema", "files", "source_map_sha256", "host", "scope"}:
        raise Failure("CONFLICT", "Invalid exchange-runtime manifest fields")
    if doc["schema"] != "c94.exchange-runtime.package.v1":
        raise Failure("CONFLICT", "Unsupported exchange-runtime manifest schema")
    declared = doc["files"]
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.name != _MANIFEST_NAME
    }
    if set(declared) != actual:
        raise Failure("CONFLICT", "Installed exchange package file set differs")
    for relative, expected in declared.items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise Failure("CONFLICT", "Exchange package path escapes root: " + relative)
        if _sha256(path) != expected:
            raise Failure("CONFLICT", "Installed exchange module fingerprint differs: " + relative)
    source_map = root / "SOURCE-MAP.json"
    if not source_map.is_file() or _sha256(source_map) != doc["source_map_sha256"]:
        raise Failure("CONFLICT", "Installed exchange source map differs")
    return manifest_path, doc


def _candidate_roots():
    explicit = os.environ.get("C94_RIGGING_ADDON_ROOT")
    if explicit:
        yield Path(explicit).expanduser()
    existing = sys.modules.get("rigging_workflow")
    existing_file = getattr(existing, "__file__", None) if existing is not None else None
    if existing_file:
        yield Path(existing_file).resolve().parent
    # Confirmed C94 product location. This is a code-root fallback, not the
    # cancelled product locator/architecture.
    yield ROOT / "runtime" / "rigging-workflow" / "c94" / "rigging_workflow"
    try:
        script_paths = bpy.utils.script_paths(subdir="addons", check_all=True)
    except TypeError:
        script_paths = bpy.utils.script_paths(subdir="addons")
    for base in script_paths or ():
        yield Path(base) / "rigging_workflow"
    try:
        user_addons = bpy.utils.user_resource("SCRIPTS", path="addons", create=False)
    except (AttributeError, TypeError):
        user_addons = None
    if user_addons:
        yield Path(user_addons) / "rigging_workflow"


def _discover_root() -> Path:
    explicit = os.environ.get("C94_RIGGING_ADDON_ROOT")
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_absolute():
            raise Failure(
                "UNSUPPORTED",
                "C94_RIGGING_ADDON_ROOT must be an absolute path",
            )
        root = candidate.resolve()
        if not (root / _MANIFEST_NAME).is_file():
            raise Failure(
                "UNSUPPORTED",
                "C94_RIGGING_ADDON_ROOT has no exchange-runtime manifest: "
                + str(root),
            )
        return root
    seen = set()
    for candidate in _candidate_roots():
        try:
            root = candidate.resolve()
        except OSError:
            continue
        if root in seen:
            continue
        seen.add(root)
        if (root / _MANIFEST_NAME).is_file():
            return root
    raise Failure(
        "UNSUPPORTED",
        "Installed rigging_workflow exchange package not found; install/enable the "
        "add-on or set C94_RIGGING_ADDON_ROOT",
    )


def _load():
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    root = _discover_root()
    manifest_path, _ = _valid_manifest(root)
    manifest_sha = _sha256(manifest_path)
    existing = sys.modules.get("rigging_workflow")
    existing_file = getattr(existing, "__file__", None) if existing is not None else None
    if existing_file and Path(existing_file).resolve().parent == root:
        prefix = "rigging_workflow"
        package = existing
    else:
        prefix = _PACKAGE_NAME
        package = sys.modules.get(prefix)
        if package is None:
            spec = importlib.util.spec_from_file_location(
                prefix,
                root / "__init__.py",
                submodule_search_locations=[str(root)],
            )
            if spec is None or spec.loader is None:
                raise Failure("UNSUPPORTED", "Cannot create qualified add-on loader")
            package = importlib.util.module_from_spec(spec)
            sys.modules[prefix] = package
            try:
                spec.loader.exec_module(package)
            except Exception:
                sys.modules.pop(prefix, None)
                raise
    try:
        core = importlib.import_module(prefix + ".exchange_runtime.core")
        native = importlib.import_module(prefix + ".exchange_runtime.native_import")
        io_support = importlib.import_module(prefix + ".exchange_runtime.io_support")
    except Exception as exc:
        if prefix != "rigging_workflow":
            sys.modules.pop(prefix, None)
        raise Failure(
            "UNSUPPORTED",
            "Qualified add-on exchange runtime could not load: " + str(exc),
        ) from exc
    _CACHE = (core, native, io_support, root, manifest_sha)
    return _CACHE


def _mapped_code(code: str) -> str:
    return code if code in CODES else "WORKER_FAILED"


def _forward_failure(exc: Exception):
    raise Failure(_mapped_code(getattr(exc, "code", "WORKER_FAILED")), str(exc)) from exc


def identity() -> dict:
    core, native, _, root, manifest_sha = _load()
    return {
        "addon_root": str(root),
        "package_prefix": str(core.__package__).split(".")[0],
        "manifest_sha256": manifest_sha,
        "core_file": str(Path(core.__file__)),
        "core_sha256": _sha256(Path(core.__file__)),
        "native_import_file": str(Path(native.__file__)),
    }


def export_shared(params, spec, job, started, hashes=None):
    core, _, io_support, _, _ = _load()
    try:
        return core.export_shared(
            params, spec, job, started, hashes=hashes
        )
    except io_support.Failure as exc:
        _forward_failure(exc)
    except OSError as exc:
        raise Failure("IO_ERROR", str(exc)) from exc


def native_import(params, spec, job):
    _, native, io_support, _, _ = _load()
    try:
        return native.import_(params, spec, job)
    except io_support.Failure as exc:
        _forward_failure(exc)
    except OSError as exc:
        raise Failure("IO_ERROR", str(exc)) from exc
