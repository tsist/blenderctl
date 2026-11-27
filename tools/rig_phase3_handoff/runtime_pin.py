# SPDX-License-Identifier: GPL-3.0-or-later
"""Resolve the caller's explicit SHA-bound Win53 add-on package."""
from contextlib import ExitStack
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import sys
from zipfile import BadZipFile, ZipFile


HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent / "blenderctl"
sys.path.insert(0, str(TOOLS))

from handoff_io import canonical, guard_file, require
from protocol import Failure


EDITION = "0.42.3-win53-phase3"
BUILD = "02f0899d78e3"
SCENE_FIELDS = {"file", "expected_sha256", "scene", "view_layer"}
REQUIRED_MEMBERS = {
    "rigging_workflow/__init__.py", "rigging_workflow/posing_rom_ui.py",
    "rigging_workflow/retarget_fk_contact.py", "rigging_workflow/retarget_profile.py",
    "rigging_workflow/retarget_contact_correction.py",
}


def execution_inputs(args, held):
    require(sys.platform == "win32", "UNSUPPORTED", "Phase 3 workers require Windows")
    require(args.blender is not None and args.rigging_package is not None and args.rigging_package_sha256,
            "INVALID_REQUEST", "Supply --blender, --rigging-package and --rigging-package-sha256")
    blender = canonical(args.blender)
    archive = guard_file(args.rigging_package, held, args.rigging_package_sha256)
    require(blender.suffix.lower() == ".exe" and blender.is_file(), "INVALID_REQUEST", "Expected a Blender executable")
    require(archive.suffix.lower() == ".zip", "INVALID_REQUEST", "Expected a Rigging Workflow ZIP")
    return blender, {"file": str(archive), "sha256": args.rigging_package_sha256}


def scene_input(request, held):
    require(all(isinstance(request.get(key), str) and request[key] and "\0" not in request[key]
                for key in SCENE_FIELDS), "INVALID_REQUEST",
            "A saved source needs file, expected_sha256, scene and view_layer")
    source = guard_file(request["file"], held, request["expected_sha256"])
    require(source.suffix.lower() == ".blend", "INVALID_REQUEST", "Expected a saved .blend")
    return {**request, "file": str(source)}


def extract_package(archive, directory):
    try:
        with ZipFile(archive) as bundle:
            members = bundle.infolist()
            require(len(members) <= 4000 and sum(item.file_size for item in members) <= 128 * 1024 * 1024,
                    "RESOURCE_LIMIT", "Rigging ZIP exceeds the supported extraction budget")
            names = set()
            for member in members:
                path = PurePosixPath(member.filename)
                name = path.as_posix()
                require(path.parts and path.parts[0] == "rigging_workflow" and ".." not in path.parts
                        and name == member.filename.rstrip("/")
                        and member.orig_filename == member.filename
                        and all(part.rstrip(". ") == part and not PureWindowsPath(part).is_reserved()
                                for part in path.parts)
                        and not any(ord(char) < 32 or char in '\\:<>"|?*' for char in member.filename)
                        and not stat.S_ISLNK(member.external_attr >> 16) and name.casefold() not in names,
                        "INVALID_REQUEST", "Invalid or duplicate member in the Rigging ZIP")
                names.add(name.casefold())
            require(REQUIRED_MEMBERS <= names, "INVALID_REQUEST", "The ZIP lacks required Win53 rigging modules")
            directory.mkdir()
            bundle.extractall(directory)
    except BadZipFile as exc:
        raise Failure("INVALID_REQUEST", "Invalid Rigging Workflow ZIP") from exc


def activate(bpy, directory, package, *, background):
    require(sys.platform == "win32" and tuple(bpy.app.version) == (5, 3, 0)
            and bpy.app.build_hash.decode() == BUILD and bpy.app.background == background
            and not bpy.app.online_access, "UNSUPPORTED",
            "Use Windows Blender 5.3.0 build " + BUILD)
    require("rigging_workflow" not in sys.modules, "CONFLICT", "A rigging add-on is already loaded")
    with ExitStack() as held:
        archive = guard_file(package["file"], held, package["sha256"])
        extract_package(archive, directory)
    sys.path.insert(0, str(directory))
    import rigging_workflow

    require(Path(rigging_workflow.__file__).resolve().parent == directory / "rigging_workflow",
            "CONFLICT", "Rigging module did not resolve to the pinned package")
    rigging_workflow.register()
    return {"edition": EDITION, "build": BUILD, "package_sha256": package["sha256"]}


def open_scene(bpy, file, request, *, ui=False):
    require(bpy.ops.wm.open_mainfile(filepath=str(file), load_ui=ui, use_scripts=False) == {"FINISHED"}
            and Path(bpy.data.filepath).samefile(file), "VALIDATION_FAILED",
            "Could not reopen the saved Blender input")
    manager = bpy.context.window_manager
    window = bpy.context.window or next(iter(manager.windows), None)
    scene = bpy.data.scenes.get(request["scene"])
    require(scene is not None and request["view_layer"] in scene.view_layers,
            "CONFLICT", "The requested scene or view layer is absent")
    require(window is not None, "WORKER_FAILED", "Blender did not create a scene window")
    window.scene = scene
    window.view_layer = scene.view_layers[request["view_layer"]]
    return window


def user_environment(profile):
    temporary = profile / "temp"
    temporary.mkdir(parents=True)
    return {**os.environ, "BLENDER_USER_RESOURCES": str(profile),
            "BLENDER_USER_CONFIG": str(profile / "config"),
            "BLENDER_USER_SCRIPTS": str(profile / "scripts"),
            "BLENDER_USER_DATAFILES": str(profile / "datafiles"),
            "BLENDER_USER_EXTENSIONS": str(profile / "extensions"),
            "TEMP": str(temporary), "TMP": str(temporary), "TMPDIR": str(temporary),
            "PYTHONDONTWRITEBYTECODE": "1"}
