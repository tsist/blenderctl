"""Read-only worktree and toolchain checks for the public doctor worker."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib


def registry_health(root):
    path = root / "tools/blenderctl/registry.json"
    problems = []
    plugins = []
    try:
        registry = json.loads(path.read_text(encoding="utf-8-sig"))
        if registry["schema_version"] != "1.0":
            raise ValueError("Unsupported registry schema")
        launcher = registry["launcher"]
        handoffs = launcher["handoffs"]
        lookups = launcher["lookups"]
        for name, target in lookups.items():
            if not (root / target).is_file():
                problems.append(f"{name}: missing {target}")
        for name, route in handoffs.items():
            entry = route.get("entry")
            if "alias_for" in route:
                entry = handoffs.get(route["alias_for"], {}).get("entry")
            if not entry or not (root / entry).is_file():
                problems.append(f"{name}: missing launcher target")
        for plugin in registry["plugins"]:
            domain = plugin["domain"]
            from plugin_registry import source_version
            actual = None
            try:
                actual = source_version(root, plugin)
            except (OSError, ValueError, KeyError, SyntaxError) as exc:
                problems.append(f"{domain}: {exc}")
            if actual != plugin["version"]:
                problems.append(f"{domain}: registry version {plugin['version']}, manifest version {actual}")
            if not (root / plugin["cli_adapter"]).is_file():
                problems.append(f"{domain}: missing CLI adapter")
            plugins.append({"domain": domain, "version": actual})
        cell_fracture = cell_fracture_health(root)
        if not cell_fracture["ok"]:
            problems.append(f"cell_fracture: {cell_fracture['problem']}")
        return {"ok": not problems, "handoffs": len(handoffs), "lookups": len(lookups),
                "plugins": plugins, "registration_test": "not_run", "problems": problems,
                "cell_fracture": cell_fracture}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return {"ok": False, "problems": [*problems, str(exc)], "registration_test": "not_run"}


def cell_fracture_health(root):
    source = root / "tools/physics_nodes_workflow_addon/cell_fracture"
    try:
        manifest = tomllib.loads((source / "blender_manifest.toml").read_text(encoding="utf-8"))
        if manifest["id"] != "cell_fracture":
            raise ValueError("Unexpected extension identity")
        # Check that the same bundled operator used by physics registers with factory preferences.
        sys.path.insert(0, str(source.parent.parent))
        try:
            from physics_nodes_workflow_addon.bridge import load_cell_fracture

            extension, registered = load_cell_fracture()
            try:
                if Path(extension.__file__).resolve().parent != source.resolve():
                    raise ValueError("The managed extension was not loaded from the workspace")
            finally:
                if registered:
                    extension.unregister()
        finally:
            sys.path.pop(0)
        return {"ok": True, "status": "ok", "version": manifest["version"], "source": str(source)}
    except (OSError, ImportError, KeyError, RuntimeError, ValueError) as exc:
        return {"ok": False, "status": "failed", "source": str(source), "problem": str(exc)}


def media_tools(root):
    """Probe a matched pair from the same local distribution."""
    bins = sorted((root / "tools/ffmpeg").glob("*/bin"))
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        bins.append(Path(system_ffmpeg).parent)
    pair = ("ffmpeg", "ffprobe")
    directory = next((folder for folder in bins if all((folder / f"{name}.exe").is_file() for name in pair)), None)
    if directory is None:
        return {"ok": False, "problem": "No local FFmpeg and FFprobe pair found"}
    result = {}
    for name in pair:
        binary = directory / f"{name}.exe"
        try:
            probe = subprocess.run([str(binary), "-version"], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=30,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            version = probe.stdout.partition("\n")[0].strip()
            result[name] = {"path": str(binary), "version": version,
                            "ok": probe.returncode == 0 and version.startswith(f"{name} version ")}
        except (OSError, subprocess.TimeoutExpired) as exc:
            result[name] = {"path": str(binary), "ok": False, "problem": str(exc)}
    return {"ok": all(item["ok"] for item in result.values()), **result}

def plugin_registration(root, blender, job, plugins):
    """Run independent registrations without installing add-ons or saving preferences."""
    from concurrent.futures import ThreadPoolExecutor
    from plugin_registry import source_path
    directory = job / "plugin-registration"
    directory.mkdir(exist_ok=True)
    script = Path(__file__).with_name("doctor_registration.py")

    def check(plugin):
        domain = plugin["domain"]
        receipt = directory / (domain + ".json")
        source = source_path(root, plugin["addon_dir"])
        argv = [blender, "--background", "--factory-startup", "--offline-mode", "--disable-autoexec",
                "--threads", "1", "--python-exit-code", "1", "--python", str(script),
                "--", str(source), str(receipt)]
        try:
            from os import environ
            env = {**environ, "PYTHONDONTWRITEBYTECODE": "1"}
            # Some bundled add-ons write user resources during registration.
            for key in ("CONFIG", "SCRIPTS", "DATAFILES", "EXTENSIONS"):
                env["BLENDER_USER_" + key] = str(directory / domain / key.lower())
            with (directory / (domain + ".log")).open("wb") as log:
                proc = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, env=env,
                                      timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            detail = json.loads(receipt.read_bytes()) if receipt.is_file() else {}
            return {"domain": domain, **detail, "exit_code": proc.returncode,
                    "ok": proc.returncode == 0 and detail.get("ok") is True}
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            return {"domain": domain, "ok": False, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=2) as workers:
        return list(workers.map(check, plugins))

def library_health(directory):
    """Check files declared by managed libraries at this root or one level below."""
    report = {"path": str(directory), "reachable": directory.is_dir(), "readable": False,
              "indexes": [], "checked_files": 0, "broken_links": []}
    try:
        children = list(directory.iterdir())
        report["readable"] = True
        roots = [directory, *(p for p in children if p.is_dir() and not p.is_symlink())]
        for root in roots:
            index = root / "asset-index.json"
            if not index.is_file():
                continue
            report["indexes"].append(str(index))
            rows = json.loads(index.read_bytes())
            if not isinstance(rows, list):
                raise ValueError(f"Expected asset records in {index}")
            for row in rows:
                files = row["files"]
                primary, = (f for f in files if f["role"] == "primary")
                primary_file = Path(primary.get("path_base") or root) / primary["path"]
                for entry in [*files, *(row.get("dependencies") or [])]:
                    path = entry["path"]
                    if path.startswith("//"):
                        resolved = primary_file.parent / path[2:]
                    else:
                        resolved = Path(entry.get("path_base") or primary_file.parent) / path
                    report["checked_files"] += 1
                    if not resolved.is_file():
                        report["broken_links"].append({"asset_id": row["asset_id"], "file": str(resolved)})
    except (OSError, TypeError, KeyError, ValueError) as exc:
        report["error"] = str(exc)
    report["ok"] = report["readable"] and not report["broken_links"] and "error" not in report
    return report


def workstation_health(root, bpy, job, devices):
    import os
    directory = Path(os.environ.get("BLENDERCTL_ASSET_LIBRARY", Path(root.anchor) / "Blender Models"))
    storage = shutil.disk_usage(job)
    gpu = {}
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.compute_device_type = "OPTIX"
        prefs.get_devices_for_type("OPTIX")
        for dev in prefs.devices:
            dev.use = dev.type == "OPTIX"
        gpu["dlss_reported_by_cycles"] = bool(prefs.has_dlss_gpu_devices())
        gpu["optix_denoiser_reported_by_cycles"] = bool(prefs.has_optixdenoiser_gpu_devices())
    except (AttributeError, KeyError, RuntimeError) as exc:
        gpu["device_probe_error"] = str(exc)
    gpu["rtx_5080"] = any("RTX 5080" in row["name"] and row["type"] in ("CUDA", "OPTIX") for row in devices)
    nvidia = shutil.which("nvidia-smi")
    if nvidia:
        fields = ["name", "driver_version", "memory.total", "memory.free"]
        proc = subprocess.run([nvidia, "--query-gpu=" + ",".join(fields), "--format=csv,noheader,nounits"],
                              capture_output=True, text=True, timeout=12, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        gpu["driver_inventory"] = [dict(zip(fields, (v.strip() for v in line.split(","))))
                                   for line in proc.stdout.splitlines() if proc.returncode == 0 and line]
    library = library_health(directory)
    if gpu["rtx_5080"] and gpu.get("optix_denoiser_reported_by_cycles"):
        try:
            gpu["render"] = gpu_render_smoke(bpy, job)
        except (RuntimeError, OSError, ValueError) as exc:
            gpu["render"] = {"ok": False, "error": str(exc)}
    return {"asset_library": library, "disk": {"free_bytes": storage.free, "total_bytes": storage.total},
            "gpu": gpu}

def gpu_render_smoke(bpy, job):
    """Render the factory scene in the disposable doctor worker and read the saved pixels."""
    from protocol import digest
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "GPU"
    scene.cycles.samples = 1
    scene.cycles.use_denoising = False
    scene.render.resolution_x = 48
    scene.render.resolution_y = 48
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    output = job / "doctor-gpu-smoke.png"
    scene.render.filepath = str(output)
    if bpy.ops.render.render(write_still=True) != {"FINISHED"} or not output.is_file():
        return {"ok": False, "error": "Blender did not write the diagnostic frame"}
    decoded = bpy.data.images.load(str(output), check_existing=False)
    try:
        values = list(decoded.pixels)[::4]
        return {"ok": len(values) == 48 * 48 and max(values) > min(values), "engine": "CYCLES",
                "selected_device": "GPU", "backend": "OPTIX", "sha256": digest(output),
                "image": str(output), "resolution": list(decoded.size), "red_range": [min(values), max(values)]}
    finally:
        bpy.data.images.remove(decoded)
