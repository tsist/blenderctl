"""Resolve saved Blender asset-library registration for a named profile."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


RESULT_PREFIX = "BLENDERCTL_ASSET_LIBRARIES="
WORKER_MODE = "--asset-library-inventory-worker"
PROFILE_KEYS = "BLENDER_USER_RESOURCES", "BLENDER_USER_CONFIG"
COMMAND = "asset.libraries"


def library_inventory(scratch, profile, original_profile):
    import bpy

    if not bpy.app.background or bpy.app.online_access:
        raise RuntimeError("Expected an offline background Blender process")
    config = scratch / "config"
    if Path(bpy.utils.user_resource("CONFIG")) != config:
        raise RuntimeError("Worker preferences directory is not isolated")

    if profile:
        source_config = Path(profile) / "config"
    else:
        # Use Blender to resolve portable installs, versioned OS defaults and
        # resource overrides. Only the path query sees the original environment.
        try:
            for key, value in zip(PROFILE_KEYS, original_profile):
                if value:
                    os.environ[key] = value
                else:
                    os.environ.pop(key, None)
            source = bpy.utils.user_resource("CONFIG")
        finally:
            os.environ["BLENDER_USER_RESOURCES"] = str(scratch)
            os.environ.pop("BLENDER_USER_CONFIG", None)
        if not source:
            raise RuntimeError("Blender could not resolve its user configuration directory")
        source_config = Path(source)

    preferences = source_config / "userpref.blend"
    data = {
        "blender": {
            "version": bpy.app.version_string,
            "build": bpy.app.build_hash.decode("ascii", errors="replace"),
        },
        "profile": {
            "config": str(source_config),
            "preferences_file": str(preferences),
            "sha256": None,
            "state": "UNINITIALIZED",
        },
        "libraries": [],
        "selected": None,
    }
    try:
        saved = preferences.read_bytes()
    except FileNotFoundError:
        # Factory settings contain a default library even on a fresh install.
        # Only saved preferences provide evidence of registration.
        return data

    config.mkdir(exist_ok=True)
    (config / "userpref.blend").write_bytes(saved)
    # The reload normally notifies extension handlers, which may activate
    # packages listed in the copied preferences.
    bpy.app.handlers._extension_repos_update_pre.clear()
    bpy.app.handlers._extension_repos_update_post.clear()
    if bpy.ops.wm.read_userpref() != {"FINISHED"}:
        raise RuntimeError("Blender could not load the saved preferences")
    data["profile"].update(sha256=hashlib.sha256(saved).hexdigest(), state="SAVED")
    data["libraries"] = [
        {"name": item.name, "path": item.path, "import_method": item.import_method}
        for item in bpy.context.preferences.filepaths.asset_libraries
    ]
    return data


def read_libraries(args):
    from protocol import Failure

    original_profile = [os.environ.get(key, "") for key in PROFILE_KEYS]
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("BLENDER_USER_", "BLENDER_SYSTEM_", "PYTHON"))
    }
    with tempfile.TemporaryDirectory(prefix="blenderctl-asset-libraries-") as directory:
        scratch = Path(directory)
        environment.update(
            BLENDER_USER_RESOURCES=directory,
            PYTHONDONTWRITEBYTECODE="1",
            TMP=directory,
            TEMP=directory,
            TMPDIR=directory,
        )
        command = [
            str(args.blender), "--background", "--factory-startup", "--disable-autoexec",
            "--offline-mode", "--log", "blend", "--log-level", "1",
            "--python-exit-code", "23", "--python", str(Path(__file__).resolve()),
            "--", WORKER_MODE, str(scratch), str(args.profile or ""), *original_profile,
        ]
        completed = subprocess.run(
            command, env=environment, cwd=directory, stdin=subprocess.DEVNULL,
            capture_output=True, encoding="utf-8", errors="replace", timeout=args.timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lines = completed.stdout.splitlines()
        results = [line.removeprefix(RESULT_PREFIX) for line in lines if line.startswith(RESULT_PREFIX)]
        if completed.returncode or len(results) != 1:
            details = (completed.stderr or completed.stdout).strip()[-500:]
            raise Failure("WORKER_FAILED", f"Blender preference reader failed: {details}")
        data = json.loads(results[0])
        if data["profile"]["state"] == "SAVED":
            # Blender can return FINISHED after silently reverting to factory
            # settings. Its native success log confirms which file it loaded.
            loaded = re.findall(r'\| Read prefs: "([^"\r\n]+)"', completed.stdout + completed.stderr)
            if not any(Path(path) == scratch / "config" / "userpref.blend" for path in loaded):
                source = data["profile"]["preferences_file"]
                raise Failure("VALIDATION_FAILED", f"Blender did not load saved preferences at {source}")
        return data


def main():
    from protocol import CODES, Failure, envelope

    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise Failure("INVALID_REQUEST", message)

    parser = Parser(prog="blenderctl asset libraries", description=__doc__, allow_abbrev=False)
    parser.add_argument("--blender", type=Path, required=True, help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=30, help=argparse.SUPPRESS)
    parser.add_argument("--name", help="Match the exact registered name")
    parser.add_argument("--profile", type=Path, help="Absolute Blender user-resources directory")
    parser.add_argument("--catalog-path", help="Resolve a Catalog path within the library selected by --name")
    data, error = None, None
    try:
        args = parser.parse_args()
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise Failure("INVALID_REQUEST", "--timeout must be positive")
        if not args.blender.is_absolute() or not args.blender.is_file():
            raise Failure("NOT_FOUND", f"Blender executable is not available at {args.blender}")
        if args.profile is not None and (not args.profile.is_absolute() or not args.profile.is_dir()):
            raise Failure("INVALID_REQUEST", "--profile must be an existing absolute directory")
        if args.name is not None and not args.name.strip():
            raise Failure("INVALID_REQUEST", "--name must be nonempty")
        if args.catalog_path is not None and args.name is None:
            raise Failure("INVALID_REQUEST", "--catalog-path requires --name")

        data = read_libraries(args)
        if args.name is not None:
            matches = [library for library in data["libraries"] if library["name"] == args.name]
            if len(matches) > 1:
                raise Failure("CONFLICT", f"注册名 {args.name!r} 对应多个库。请在 Blender 偏好设置中为它们指定唯一名称。")
            if not matches:
                raise Failure(
                    "NOT_FOUND",
                    f"已保存的 Blender 配置未注册资产库 {args.name!r}。请在首选项 → 文件路径 → 资产库注册该库并保存偏好设置，或通过 --profile 指向已注册的配置。",
                )
            data["selected"] = matches[0]
            if args.catalog_path is not None:
                from asset_catalog_resolution import resolve_catalog

                data["catalog"] = resolve_catalog(data["selected"]["path"], args.catalog_path)
    except Failure as exc:
        error = {"code": exc.code, "message": str(exc)}
    except subprocess.TimeoutExpired:
        error = {"code": "TIMEOUT", "message": "Timed out while reading Blender preferences"}
    except OSError as exc:
        error = {"code": "IO_ERROR", "message": str(exc)}

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(envelope(COMMAND, data=data, error=error), ensure_ascii=False))
    return CODES[error["code"]] if error else 0


if __name__ == "__main__":
    if WORKER_MODE in sys.argv and "--background" in sys.argv:
        scratch, profile, *original_profile = sys.argv[sys.argv.index(WORKER_MODE) + 1:]
        report = library_inventory(Path(scratch), profile, original_profile)
        print(RESULT_PREFIX + json.dumps(report))
    else:
        sys.exit(main())
