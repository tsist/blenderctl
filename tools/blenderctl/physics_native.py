"""Public, non-overwriting transactions for the qualified native fluid add-on."""

import argparse
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

from filesystem import FileGuard, PathLocks
from host_compat import is_supported_host
from physics_native_resource import PROFILES, PROFILE_DISK, PROFILE_THREADS
from protocol import CODES, Failure, atomic_json, digest, envelope, read_json, timeout_value


ROOT = Path(__file__).resolve().parents[2]
VERSION = "1.0.0-rc.1"
MODULE = ROOT / "tools/physics_nodes_workflow_addon"
FLUID_PRESETS = {"native_liquid": "LIQUID", "native_gas": "GAS"}
POINT_CACHE_PRESETS = {"native_cloth": "CLOTH", "native_rigid_body": "RIGID_BODY"}
PRESETS = {**FLUID_PRESETS, **POINT_CACHE_PRESETS}
SCENE_OPERATIONS = {"adopt", "build", "parameters", "bake", "invalidate_cache", "guide_density"}
OPERATIONS = SCENE_OPERATIONS | {"inspect", "preview", "export", "export_density"}
FIELDS = {"schema_version", "operation", "preset_id", "bridge_mode", "input_blend", "expected_sha256",
          "target_object", "modifier_name", "parameters", "resource_profile", "preserve_objects",
          "native_setup", "archive_mode", "force_field_objects", "reuse_data"}
EXTRA = {"parameters": {"native_settings"}, "build": {"native_settings"}, "guide_density": {"native_density"},
         "export": {"export_format", "export_frame", "export_path"}, "export_density": {"native_density"},
         "preview": {"preview"}}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure("INVALID_REQUEST", message)


def require(condition, message, code="INVALID_REQUEST"):
    if not condition:
        raise Failure(code, message)


def clean_path(value, field, suffix=None):
    require(isinstance(value, (str, Path)) and '\0' not in str(value), f"{field} must be a path")
    path = Path(value)
    require(path.is_absolute() and ".." not in path.parts, f"{field} must be an absolute path without parent traversal")
    require(not suffix or path.suffix.lower() == suffix, f"{field} must end with {suffix}")
    for part in (path, *path.parents):
        try:
            entry = part.lstat()
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(entry.st_mode) and not getattr(entry, "st_file_attributes", 0) & 0x400,
                f"{field} contains a link or reparse point: {part}", "CONFLICT")
    return path


def load_contract():
    key = "_pnw_qualified_native_fluid"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, MODULE / "__init__.py",
                                                        submodule_search_locations=[str(MODULE)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        previous_prefix, previous_write = sys.pycache_prefix, sys.dont_write_bytecode
        try:
            sys.pycache_prefix = str(MODULE.parent / (".unused-bytecode-" + uuid.uuid4().hex))
            sys.dont_write_bytecode = True
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(key, None)
            raise
        finally:
            sys.pycache_prefix, sys.dont_write_bytecode = previous_prefix, previous_write
    return sys.modules[key].pnw_contract


def package(guards):
    import tomllib

    manifest_path = clean_path(MODULE / "blender_manifest.toml", "add-on manifest")
    manifest = guards.enter_context(FileGuard(manifest_path))
    require(tomllib.loads(manifest_path.read_text(encoding="utf-8")).get("version") == VERSION,
            "The workspace add-on version differs from the CLI version", "CONFLICT")
    files = {path.name for path in MODULE.iterdir() if path.suffix == ".py" or path.name in {"LICENSE", "blender_manifest.toml"}}
    fracture = {"cell_fracture/" + name for name in ("__init__.py", "blender_manifest.toml",
                                                   "fracture_cell_calc.py", "fracture_cell_setup.py")}
    files.update(fracture)
    require({"__init__.py", "bridge.py", "native_fluid.py", "native_density.py"} <= files,
            "The workspace add-on source is incomplete", "NOT_FOUND")
    inventory = {manifest_path.name: manifest.sha256()}
    for name in sorted(files - inventory.keys()):
        bound = guards.enter_context(FileGuard(clean_path(MODULE / name, "package file")))
        inventory[name] = bound.sha256()
    return inventory


def probe_host(blender):
    try:
        observed = subprocess.run(
            [str(blender), "--version"], capture_output=True, encoding="utf-8",
            timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise Failure("TIMEOUT", "Could not read the selected Blender build") from exc
    version = re.search(r"^Blender (\d+)\.(\d+)\.(\d+)", observed.stdout, re.MULTILINE)
    build = re.search(r"^\s*build hash:\s*([0-9a-f]+)\s*$", observed.stdout, re.MULTILINE)
    require(observed.returncode == 0 and version and build, "Could not verify the selected Blender", "UNSUPPORTED")
    parts = tuple(map(int, version.groups()))
    require(is_supported_host(parts), "Native physics requires Blender 5.3 or newer", "UNSUPPORTED")
    return {"blender_version": list(parts), "blender_build": build.group(1)}


def describe(blender):
    with ExitStack() as guards:
        package(guards)
        guards.enter_context(FileGuard(blender))
        host = probe_host(blender)
        addon = load_contract().describe_contract()["native_fluid"]
    return {**addon, **host, "available": True, "implementation_version": VERSION,
            "launcher": ["physics", "run", "--request"], "operations": sorted(OPERATIONS),
            "requires_configured_domain": False, "resource_profiles_mib": PROFILES,
            "resource_profiles": {name: {"memory_limit_mib": memory, "threads": PROFILE_THREADS[name],
                                         "disk_limit_bytes": PROFILE_DISK[name]} for name, memory in PROFILES.items()},
            "job_commands": ["run", "sweep", "batch", "status", "cancel", "resume"],
            "fields": {"required": ["input_blend", "preset_id", "target_object", "modifier_name", "operation", "parameters"],
                       "optional": ["expected_sha256", "resource_profile", "preserve_objects",
                                    "native_setup", "archive_mode", "force_field_objects", "reuse_data"],
                       "work_copy_operations": sorted(SCENE_OPERATIONS), "work_copy_output": "output_blend",
                       "export_output": "export_path", "density_output": "native_density.output_directory",
                       "preview": {"path": "new absolute .png", "frame": "signed frame", "width": [128, 800],
                                   "samples": [1, 32]}},
            "cold_reopen": True, "keep_job_cache_with_work_copy": True}


def route_request(argv):
    """Leave historical argument parsing and responses to the frozen runner."""
    if not argv or argv[0] != "run":
        return None
    value = None
    for index, option in enumerate(argv[1:], 1):
        if option == "--request" and index + 1 < len(argv):
            value = argv[index + 1]
        elif option.startswith("--request="):
            value = option.partition("=")[2]
    if value is None:
        return None
    try:
        if value.lstrip().startswith("{"):
            payload = json.loads(value)
        else:
            # The document is parsed only once for native execution.
            payload = read_json(Path(value))
    except (Failure, OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) and isinstance(payload.get("preset_id"), str) and payload["preset_id"] in PRESETS else None


def validate(payload):
    operation = payload.get("operation")
    require(isinstance(operation, str) and operation in OPERATIONS, "Unknown native fluid operation")
    preset = payload.get("preset_id")
    is_bridge = preset in POINT_CACHE_PRESETS
    allowed = FIELDS | EXTRA.get(operation, set())
    if is_bridge:
        require(operation in {"build", "bake", "inspect"}, "Unsupported native point-cache operation")
    elif operation == "bake":
        allowed |= {"native_settings"}
    if operation in SCENE_OPERATIONS:
        allowed |= {"output_blend"}
    require(set(payload) <= allowed, f"Inapplicable native fluid fields: {', '.join(sorted(set(payload)-allowed))}")
    require(payload.get("schema_version", "1.0") == "1.0", "Expected native fluid schema version 1.0")
    source = clean_path(payload.get("input_blend"), "input_blend", ".blend")
    require(source.is_file(), f"Input blend not found: {source}", "NOT_FOUND")
    expected = payload.get("expected_sha256")
    require(expected is None or isinstance(expected, str) and re.fullmatch(r"[a-fA-F0-9]{64}", expected),
            "expected_sha256 must contain 64 hexadecimal characters")
    profile = payload.get("resource_profile", "light")
    require(isinstance(profile, str) and profile in PROFILES,
            f"resource_profile must be one of {', '.join(PROFILES)}")
    preserved = payload.get("preserve_objects", [])
    require(isinstance(preserved, list) and all(isinstance(name, str) and name and '\0' not in name for name in preserved)
            and len(set(preserved)) == len(preserved), "preserve_objects must contain distinct editable Mesh object names")
    core = {key: payload[key] for key in ("schema_version", "operation", "preset_id", "bridge_mode", "target_object",
                                          "modifier_name", "parameters", "native_settings", "native_density",
                                          "export_format", "export_frame", "native_setup", "archive_mode",
                                          "force_field_objects", "reuse_data") if key in payload}
    preview = None
    output = None
    if operation in SCENE_OPERATIONS:
        output = clean_path(payload.get("output_blend"), "output_blend", ".blend")
        if operation == "guide_density":
            density = core.get("native_density", {})
            require(isinstance(density, dict) and "output_directory" not in density,
                    "Guide source storage is owned by the CLI job")
    elif operation == "export":
        output = clean_path(payload.get("export_path"), "export_path", ".blend")
    elif operation == "export_density":
        density = core.get("native_density")
        require(isinstance(density, dict), "export_density requires native_density settings")
        output = clean_path(density.get("output_directory"), "native_density.output_directory")
        core["native_density"] = {**density, "output_directory": str(output)}
    elif operation == "preview":
        item = payload.get("preview")
        require(isinstance(item, dict) and {"path", "frame"} <= item.keys() <= {"path", "frame", "width", "samples"},
                "preview requires path and frame; width and samples are optional")
        output = clean_path(item["path"], "preview.path", ".png")
        preview = {"frame": item["frame"], "width": item.get("width", 400), "samples": item.get("samples", 8)}
        for field, lo, hi in (("frame", 1, 100000), ("width", 128, 800), ("samples", 1, 32)):
            require(type(preview[field]) is int and lo <= preview[field] <= hi, f"preview.{field} must be in {lo}..{hi}")
        core["operation"] = "inspect"
    if output:
        require(output.parent.is_dir(), f"Output parent directory not found: {output.parent}", "NOT_FOUND")
        require(not os.path.lexists(output), f"Output already exists: {output}", "CONFLICT")
    contract = load_contract()
    try:
        contract.normalize_request(core)
    except contract.PhysicsContractError as exc:
        raise Failure("INVALID_REQUEST", str(exc)) from exc
    return source, output, core, preview, profile, preserved


def worker_result(job, phase, status):
    receipt = job / f"{phase}-result.json"
    if receipt.is_file():
        packet = read_json(receipt)
        if isinstance(packet, dict) and not packet.get("ok"):
            error = packet.get("error") or {}
            code = error.get("code", "WORKER_FAILED")
            raise Failure(code, error.get("message", f"{phase} failed"))
    else:
        packet = None
    stopped = status.get("stop_reason")
    if stopped:
        code = {"timeout": "TIMEOUT", "cancelled": "CANCELLED"}.get(stopped, "RESOURCE_LIMIT")
        raise Failure(code, f"Native physics {phase} was stopped: {stopped}")
    require(status.get("returncode") == 0 and isinstance(packet, dict) and packet.get("ok"),
            f"Native fluid {phase} failed; see {job} logs", "WORKER_FAILED")
    return packet["data"]


def run(blender, payload, argv):
    parser = Parser(prog="blenderctl physics run", allow_abbrev=False)
    parser.add_argument("--request", required=True)
    parser.add_argument("--jobs-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    args = parser.parse_args(argv)
    response, code = execute_run(blender, payload, args.jobs_dir, args.timeout_seconds)
    print(json.dumps(response, ensure_ascii=True))
    return code


def execute_run(blender, payload, jobs_dir, timeout_seconds, *, job=None, cancel_check=None):
    from physics_native_resource import run_worker, workstation_slot

    timeout = timeout_value(timeout_seconds)
    staging = None
    cloth_cache = None
    command = "physics.run"
    resources = {}
    try:
        with ExitStack() as guards:
            inventory = package(guards)
            source, output, core, preview, profile, preserved = validate(payload)
            operation = payload["operation"]
            if job is None:
                folder = clean_path(Path(jobs_dir).absolute(), "jobs directory")
                folder.mkdir(parents=True, exist_ok=True)
                job = folder / ("native-" + uuid.uuid4().hex)
                job.mkdir()
            else:
                job = clean_path(job, "owned job directory")
            job_id = job.name
            guards.enter_context(FileGuard(blender))
            host = probe_host(blender)
            paths = [source, output] if output else [source]
            if output and core["preset_id"] == "native_cloth" and operation == "bake":
                cloth_cache = output.with_suffix(".pnw-cache")
                paths.append(cloth_cache)
            guards.enter_context(PathLocks(paths))
            source_guard = guards.enter_context(FileGuard(source))
            before = source_guard.sha256()
            expected = payload.get("expected_sha256")
            require(not expected or expected.lower() == before, "The input .blend fingerprint changed", "CONFLICT")
            if output:
                guards.enter_context(FileGuard(output.parent, directory=True))
                require(not os.path.lexists(output), f"Output already exists: {output}", "CONFLICT")
                staging = output.with_name(".pnw-" + uuid.uuid4().hex[:12] + ("" if operation == "export_density" else output.suffix))
            if cloth_cache:
                require(not os.path.lexists(cloth_cache), f"Cloth cache already exists: {cloth_cache}", "CONFLICT")
            atomic_json(job / "request.json", {"operation": operation, "core": core, "preview": preview,
                        "input": str(source), "source_sha256": before, "preserve_objects": preserved,
                        "staging": str(staging) if staging else None, "output": str(output) if output else None,
                        "module": str(MODULE), "inventory": inventory, **host,
                        "cloth_cache": str(cloth_cache) if cloth_cache else None,
                        "job": str(job), "package_version": VERSION})
            deadline = time.monotonic() + timeout
            worker = Path(__file__).with_name("physics_native_worker.py")
            watched = (staging,) if staging else ()
            if cloth_cache:
                watched += (cloth_cache, staging.parent / f"blendcache_{staging.stem}")
            with workstation_slot():
                def execute(phase):
                    require(not cancel_check or not cancel_check(), "Native job cancelled before launch", "CANCELLED")
                    remaining = deadline - time.monotonic()
                    require(remaining > 0, "Native fluid deadline expired", "TIMEOUT")
                    stats = run_worker(blender, worker, job, phase, remaining, profile, watch_paths=watched,
                                       cancel_check=cancel_check)
                    resources[phase] = stats
                    atomic_json(job / "resources.json", resources)
                    return worker_result(job, phase, stats)

                result = execute("run")
                checked_identity = None
                verification = None
                if operation == "export_density":
                    verification = execute("verify")
                elif staging and operation != "preview":
                    # A DELETE-capable handle prevents Blender from opening the file on Windows.
                    with FileGuard(staging) as reading:
                        checked_identity = reading.identity()
                        verification = execute("verify")
                require(source_guard.sha256() == before, "The source .blend changed during the job", "CONFLICT")
                require(not cancel_check or not cancel_check(), "Native job cancelled before publication", "CANCELLED")
                artifacts = {"job_directory": str(job), "receipt": str(job / "result.json"),
                             "resources": str(job / "resources.json")}
                result.update(input_blend=str(source), input_sha256=before, input_unchanged=True,
                              implementation_version=VERSION, resource_profile=profile, resource_evidence=resources)
                if verification:
                    result["independent_cold_reopen"] = verification
                if operation == "export_density":
                    entries = result["density"]["frames"]
                    require(digest(staging / "density-sequence.json") == verification["manifest_sha256"]
                            and all(digest(staging / item["path"]) == item["sha256"] for item in entries),
                            "Density staging changed after cold verification", "CONFLICT")
                    require(not os.path.lexists(output), f"Output already exists: {output}", "CONFLICT")
                    os.rename(staging, output)
                    manifest = output / "density-sequence.json"
                    result["density"].update(manifest=str(manifest), output_directory=str(output))
                    artifacts["density_manifest"] = str(manifest)
                elif staging:
                    with FileGuard(staging, renameable=True) as publication:
                        signature = publication.sha256()
                        if verification:
                            require(publication.identity() == checked_identity and verification["sha256"] == signature,
                                    "The cold-verified candidate changed", "CONFLICT")
                        else:
                            require(result["preview"]["sha256"] == signature, "The preview changed", "CONFLICT")
                        publication.rename_new(output)
                    if operation == "preview":
                        result["preview"].update(path=str(output), sha256=signature)
                        artifacts["preview"] = str(output)
                    else:
                        result.update(output_blend=str(output), output_sha256=signature)
                        if operation == "export":
                            result["export"]["saved_path"] = str(output)
                        artifacts["blend"] = str(output)
                if result.get("bake"):
                    artifacts["cache_directory"] = result["bake"]["cache_directory"]
                if cloth_cache:
                    artifacts["cache_directory"] = str(cloth_cache)
                response = envelope(command, job_id, result, artifacts=artifacts)
                atomic_json(job / "result.json", response)
    except (Failure, OSError, ValueError, RuntimeError) as exc:
        code = exc.code if isinstance(exc, Failure) else ("IO_ERROR" if isinstance(exc, OSError) else "WORKER_FAILED")
        details = {"job_directory": str(job), "receipt": str(job / "result.json")} if job else {}
        if resources and job:
            details["resources"] = str(job / "resources.json")
        if staging and staging.exists():
            details["unpublished_candidate"] = str(staging)
        if cloth_cache and cloth_cache.exists():
            details["unpublished_cache"] = str(cloth_cache)
        response = envelope(command, job.name if job else None, error={"code": code, "message": str(exc)}, artifacts=details)
        if job:
            atomic_json(job / "result.json", response)
        return response, CODES.get(code, CODES["INVALID_REQUEST"] if code in {"VALIDATION_ERROR", "DENSITY_REQUEST_INVALID"}
                                   else CODES["WORKER_FAILED"])
    return response, 0
