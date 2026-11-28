"""Blender-side native fluid operations and independent delivery verification."""

import hashlib
import importlib
import json
import math
from pathlib import Path
import struct
import sys
import traceback

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from host_compat import is_supported_host
from physics_native import require
from physics_native_resource import check_worker_gate
from protocol import Failure, atomic_json, digest, read_json


def open_scene(bpy, path):
    require(bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False, use_scripts=False) == {"FINISHED"}
            and Path(bpy.data.filepath).samefile(path), f"Failed to open {path}", "VALIDATION_FAILED")
    bpy.context.preferences.filepaths.save_version = 0


def save_scene(bpy, path):
    from physics_nodes_workflow_addon.bridge import STATE, point_cache_path, resolved_point_cache

    # Blender remaps RNA paths when saving to a new location. Custom point-cache
    # certificates need the same remap because Blender treats them as plain JSON.
    if Path(bpy.data.filepath).parent != path.parent:
        for obj in bpy.context.scene.objects:
            state_json = obj.get(STATE)
            if not state_json:
                continue
            state = json.loads(state_json)
            if state.get("bridge_mode") == "CLOTH" and state.get("files"):
                state["files"] = {point_cache_path(resolved_point_cache(name), blend=path): files
                                  for name, files in state["files"].items()}
                obj[STATE] = json.dumps(state, sort_keys=True)
    require(bpy.ops.wm.save_as_mainfile(filepath=str(path), relative_remap=True) == {"FINISHED"} and path.is_file(),
            "Blender did not save the working copy", "IO_ERROR")


def target(bpy, core):
    obj = bpy.context.view_layer.objects.get(core["target_object"])
    require(obj is not None, "The selected object is unavailable in the saved active view layer", "NOT_FOUND")
    from physics_nodes_workflow_addon import native_fluid
    was_hidden = obj.hide_get()
    if was_hidden:
        obj.hide_set(False)
    modifier, settings = native_fluid.resolve_domain(obj, core)
    require(settings.cache_type in {"ALL", "MODULAR"}, "Use Blender All or Modular native fluid caches", "UNSUPPORTED")
    return obj, modifier, settings, was_hidden


def execute(addon, core):
    result = addon.execute_request(core)
    if not result.get("ok"):
        error = result.get("error") or {}
        raise Failure(error.get("code", "WORKER_FAILED"), error.get("message", "Native fluid add-on rejected the request"))
    return result


def mesh_hash(mesh):
    hash_bytes = hashlib.sha256()
    for vertex in mesh.vertices:
        hash_bytes.update(struct.pack("<3f", *vertex.co))
    for face in mesh.polygons:
        verts = tuple(face.vertices)
        hash_bytes.update(struct.pack(f"<{len(verts) + 1}I", len(verts), *verts))
    return hash_bytes.hexdigest()


def preserve(bpy, names):
    files = {}
    for name in names:
        obj = bpy.context.scene.objects.get(name)
        require(obj is not None and obj.type == "MESH", f"Preserved mesh not found: {name}", "NOT_FOUND")
        files[name] = {"sha256": mesh_hash(obj.data), "vertices": len(obj.data.vertices),
                       "mesh": obj.data.name, "matrix": [list(row) for row in obj.matrix_world]}
    return files


def guide_state(guide):
    from physics_nodes_workflow_addon import native_density, subgroups

    modifier = guide.modifiers.get(native_density._MODIFIER)
    tree = modifier.node_group
    strength, _ = native_density._validate_guide_identity(guide)
    sockets = {}
    for item in tree.interface.items_tree:
        if (item.item_type == "SOCKET" and item.in_out == "INPUT"
                and item.identifier not in (guide[native_density._STRENGTH], guide[native_density._INSPECTION])
                and item.socket_type in {"NodeSocketFloat", "NodeSocketInt", "NodeSocketBool"}):
            value = getattr(modifier.properties.inputs, item.identifier).value
            if type(value) in (float, int, bool):
                sockets[item.name] = value
    return {"graph_sha256": subgroups.tree_fingerprint(tree), "strength": strength.value, "artist_inputs": sockets,
            "controls": {key: list(guide[attribute].matrix_world.translation) for key, attribute in
                         (("bypass", native_density._BYPASS), ("exit", native_density._EXIT))}}


def render_preview(bpy, stage, preview, proof, threads):
    scene = bpy.context.scene
    require(scene.camera is not None, "Save the shot camera to render a native preview")
    scene.frame_set(preview["frame"])
    settings = scene.render
    y = min(800, max(1, round(preview["width"] * settings.resolution_y / settings.resolution_x)))
    settings.engine = "CYCLES"
    settings.threads_mode = "FIXED"
    settings.threads = threads
    settings.resolution_x, settings.resolution_y, settings.resolution_percentage = preview["width"], y, 100
    settings.image_settings.file_format = "PNG"
    settings.image_settings.color_depth = "8"
    settings.use_file_extension = True
    settings.filepath = str(stage)
    scene.cycles.device = "CPU"
    scene.cycles.samples = preview["samples"]
    scene.cycles.use_denoising = True
    require(bpy.ops.render.render(write_still=True) == {"FINISHED"} and stage.is_file(),
            "Blender did not write the preview PNG", "WORKER_FAILED")
    with stage.open("rb") as stream:
        prefix = stream.read(24)
    require(prefix[:16] == b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" and struct.unpack(">II", prefix[16:24]) == (preview["width"], y),
            "Preview PNG header does not match the requested image size", "VALIDATION_FAILED")
    return {"frame": preview["frame"], "camera": scene.camera.name, "dimensions": [preview["width"], y],
            "samples": preview["samples"], "sha256": digest(stage), "signed_cache": proof["frame_file"]}


def run(bpy, addon, request, *, threads):
    from physics_nodes_workflow_addon import native_density, native_fluid
    from physics_native import POINT_CACHE_PRESETS, SCENE_OPERATIONS

    operation = request["operation"]
    core = request["core"]
    source = Path(request["input"])
    stage = Path(request["staging"]) if request["staging"] else None
    job = Path(request["job"])
    open_scene(bpy, source)
    preserved = preserve(bpy, request["preserve_objects"])
    if stage:
        native_fluid.assert_isolated_output(Path(request["output"]))
        native_fluid.assert_isolated_output(stage)
        require(not stage.exists(), "Private staging already exists", "CONFLICT")

    if core["preset_id"] in POINT_CACHE_PRESETS:
        if stage:
            save_scene(bpy, stage)
        core = {**core, "bake_directory": request.get("cloth_cache") or str(job / "cache")}
        result = execute(addon, core)
        result.pop("ok", None)
        require(preserve(bpy, request["preserve_objects"]) == preserved, "A preserved mesh changed", "VALIDATION_FAILED")
        result["preserved_objects"] = preserved
        if stage:
            save_scene(bpy, stage)
        return result

    obj, was_hidden = None, False
    if core.get("native_setup") is None:
        obj, _, _, was_hidden = target(bpy, core)

    if operation == "preview":
        peek = request["preview"]
        req = addon.pnw_contract.normalize_request(core)
        proof = native_fluid.require_cached_frame(obj, req, peek["frame"])
        if was_hidden:
            obj.hide_set(True)
        return {"preview": render_preview(bpy, stage, peek, proof, threads), "preserved_objects": preserved}

    if operation in SCENE_OPERATIONS:
        if was_hidden:
            obj.hide_set(True)
        save_scene(bpy, stage)
        if obj is not None:
            obj, _, _, was_hidden = target(bpy, core)
        if operation in {"adopt", "build", "parameters", "bake"}:
            core = {**core, "bake_directory": str(job / "cache")}
        if operation == "guide_density":
            core = {**core, "native_density": {**core.get("native_density", {}),
                                                 "output_directory": str(job / "guide-source")}}
    elif operation == "export_density":
        core = {**core, "native_density": {**core["native_density"], "output_directory": str(stage)}}

    prior_guide = None
    if operation == "guide_density":
        prior_guide = native_density._find_guide(obj, core["native_density"].get("guide_name"))
        prior_guide = guide_state(prior_guide) if prior_guide else None
    result = execute(addon, core)
    result.pop("ok", None)
    require(preserve(bpy, request["preserve_objects"]) == preserved, "A preserved mesh changed", "VALIDATION_FAILED")
    result["preserved_objects"] = preserved
    if operation == "guide_density":
        guide = native_density._find_guide(obj, result["density"]["guide_object"])
        observed = guide_state(guide)
        if prior_guide:
            require(all(prior_guide[key] == observed[key] for key in ("graph_sha256", "artist_inputs", "controls")),
                    "Rejoining the smoke guide changed the artist's graph or controls", "VALIDATION_FAILED")
        result["guide_state"] = observed
    if operation == "export":
        info = result["export"]
        static = bpy.context.scene.objects[info["static_object_name"]]
        info["mesh_sha256"] = mesh_hash(static.data)
    if operation in SCENE_OPERATIONS or operation == "export":
        if was_hidden:
            obj.hide_set(True)
        save_scene(bpy, stage)
    return result


def cold_density(bpy, stage, native_density, expected, start, end):
    manifest_path = stage / "density-sequence.json"
    manifest = read_json(manifest_path)
    frames = manifest.get("frames", [])
    names = [f"density_{frame:04d}.vdb" for frame in range(start, end + 1)]
    require(manifest.get("schema") == "pnw.native-density-sequence/1" and manifest.get("grid") ==
            {"name": "density", "type": "FloatGrid", "grid_class": "FOG_VOLUME"}
            and [item["frame"] for item in frames] == list(range(start, end + 1))
            and [item["path"] for item in frames] == names and frames == expected["frames"]
            and {item.name for item in stage.iterdir()} == {*names, manifest_path.name},
            "Unexpected density delivery manifest or files", "VALIDATION_FAILED")
    vdb = native_density._openvdb()
    for entry in frames:
        path = stage / entry["path"]
        grid = native_density._volume_grid(vdb, path, delivery=True)
        require(digest(path) == entry["sha256"] and path.stat().st_size == entry["byte_count"]
                and grid.activeVoxelCount() == entry["active_voxels"]
                and native_density._manifest_transform(grid) == entry["transform"],
                f"Density validation failed for {path.name}", "VALIDATION_FAILED")
    lineage = manifest["source_manifest"]
    origin = (stage / lineage["path"]).resolve(strict=True)
    require(origin.name == "source-density.json", "The density export has no readable source lineage", "VALIDATION_FAILED")
    scene = bpy.context.scene
    sequence = manifest["blender_sequence"]
    volume = bpy.data.volumes.new("CLI cold density verification")
    volume.filepath = str(stage / sequence["filepath"])
    volume.is_sequence = True
    volume.frame_start = sequence["frame_start"]
    volume.frame_offset = sequence["frame_offset"]
    volume.frame_duration = sequence["frame_duration"]
    volume.sequence_mode = sequence["mode"]
    obj = bpy.data.objects.new(volume.name, volume)
    scene.collection.objects.link(obj)
    seek = []
    for frame in (end, start, (start + end) // 2, end):
        scene.frame_set(frame)
        grids = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data.grids
        grids.load()
        actual = Path(bpy.path.abspath(grids.frame_filepath))
        require(actual == stage / f"density_{frame:04d}.vdb" and [grid.name for grid in grids] == ["density"],
                f"Blender Volume could not load delivered frame {frame}", "VALIDATION_FAILED")
        seek.append({"frame": frame, "file": actual.name})
    return {"frames": len(frames), "manifest_sha256": digest(manifest_path), "grid": manifest["grid"],
            "frame_start": start, "frame_end": end, "source_manifest": str(origin), "volume_seek": seek}


def read_native_frames(bpy, obj, inspected):
    from physics_nodes_workflow_addon import native_density

    scene = bpy.context.scene
    start, end = inspected["frame_start"], inspected["frame_end"]
    vdb = native_density._openvdb() if inspected["domain_type"] == "GAS" else None
    observations = []
    for frame in (end, start, (start + end) // 2, end):
        scene.frame_set(frame)
        path = Path(inspected["frame_files"][frame])
        if vdb:
            grid = native_density._volume_grid(vdb, path)
            sample = {"file_sha256": digest(path), "active_density_voxels": grid.activeVoxelCount(),
                      "has_positive_density": any(item.value > 0 for item in grid.iterOnValues())}
        else:
            evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
            sample = {"vertices": len(evaluated.data.vertices), "mesh_sha256": mesh_hash(evaluated.data)}
        observations.append({"frame": frame, **sample})
    require(observations[0] == observations[-1], "Repeat native frame differs after seeking", "VALIDATION_FAILED")
    return observations


def verify(bpy, addon, request, *, threads):
    from physics_nodes_workflow_addon import native_density, native_fluid
    from physics_native import POINT_CACHE_PRESETS

    operation = request["operation"]
    core = request["core"]
    stage = Path(request["staging"])
    previous = read_json(Path(request["job"]) / "run-result.json")["data"]
    open_scene(bpy, Path(request["input"]) if operation == "export_density" else stage)
    if core["preset_id"] in POINT_CACHE_PRESETS:
        inspection = execute(addon, {**core, "operation": "inspect"})["inspection"]
        require(inspection == previous["workflow"], "Saved native point-cache setup differs", "VALIDATION_FAILED")
        require(preserve(bpy, request["preserve_objects"]) == previous["preserved_objects"],
                "A saved hand edit changed after cold open", "VALIDATION_FAILED")
        from physics_nodes_workflow_addon.bridge import resolved_point_cache

        return {"sha256": digest(stage), "native_point_cache": inspection,
                "resolved_point_cache_files": {str(resolved_point_cache(name)): files
                                               for name, files in inspection.get("files", {}).items()}}
    obj, mod, settings, _ = target(bpy, core)
    fields = {"frame_start": settings.cache_frame_start, "frame_end": settings.cache_frame_end}
    inspection = execute(addon, {"preset_id": core["preset_id"], "target_object": obj.name,
                                  "modifier_name": mod.name, "operation": "inspect", "parameters": fields})["inspection"]
    require(preserve(bpy, request["preserve_objects"]) == previous["preserved_objects"],
            "A saved hand edit changed after cold open", "VALIDATION_FAILED")
    if operation == "export_density":
        start, end = core["parameters"]["frame_start"], core["parameters"]["frame_end"]
        addon_req = addon.pnw_contract.normalize_request(core)
        native_fluid.require_cached_sequence(obj, addon_req, start, end, "GAS")
        return cold_density(bpy, stage, native_density, previous["density"], start, end)

    result = {"sha256": digest(stage), "cache_state": inspection["cache_state"], "generation": inspection["generation"],
              "resolution_max": inspection["resolution"], "cache_type": inspection["cache_type"],
              "fluid_effectors": inspection["effectors"], "fluid_force_fields": inspection.get("force_fields", []),
              "frame_range": [fields["frame_start"], fields["frame_end"]], "preserved_objects": previous["preserved_objects"]}
    if operation == "bake":
        require(inspection["cache_valid"] and inspection["generation"] == previous["bake"]["generation"],
                "Baked generation did not cold-open as a valid cache", "VALIDATION_FAILED")
        result["observations"] = read_native_frames(bpy, obj, inspection)
    elif operation == "guide_density":
        marker = previous["density"]
        guide = native_density._find_guide(obj, marker["guide_object"])
        native_density._validate_guide_identity(guide)
        start, end = marker["frame_start"], marker["frame_end"]
        require(inspection["cache_valid"] and Path(marker["source_manifest"]).is_file(),
                "Saved guide source or native cache is absent", "VALIDATION_FAILED")
        native_density._check_source(guide, Path(inspection["cache_directory"]) / "data", range(start, end + 1))
        require(guide_state(guide) == previous["guide_state"],
                "The graph or guide controls changed after cold open", "VALIDATION_FAILED")
        result["guide"] = marker["guide_object"]
        result["guide_state"] = previous["guide_state"]
    elif operation == "export":
        mesh = bpy.context.scene.objects.get(previous["export"]["static_object_name"])
        require(mesh is not None and mesh.type == "MESH" and not mesh.modifiers
                and mesh_hash(mesh.data) == previous["export"]["mesh_sha256"] and inspection["cache_valid"],
                "The static result or its native source did not cold-open", "VALIDATION_FAILED")
        result["editable_mesh"] = mesh.name
        result["mesh_sha256"] = mesh_hash(mesh.data)
    elif operation == "invalidate_cache":
        require(inspection["cache_state"] == "detached", "The saved cache did not detach", "VALIDATION_FAILED")
    else:
        expected = previous["workflow"]
        state = native_fluid._read_state(obj, mod)
        require(state and state["owner_uuid"] == expected["owner_uuid"], "The adopted cache binding is missing", "VALIDATION_FAILED")
        if operation == "parameters":
            for key, value in expected["settings"].items():
                actual = getattr(settings, key)
                require(math.isclose(actual, value, abs_tol=1e-6) if isinstance(value, float) else actual == value,
                        f"Saved solver setting changed: {key}", "VALIDATION_FAILED")
            result["settings"] = expected["settings"]
    return result


def main():
    request_file, phase, gate, limit, cpu_threads = sys.argv[sys.argv.index("--") + 1:]
    response_file = Path(request_file).parent / f"{phase}-result.json"
    try:
        threads = int(cpu_threads)
        observed = check_worker_gate(gate, int(limit), threads)
        import bpy

        request = read_json(request_file)
        version = tuple(bpy.app.version)
        build = bpy.app.build_hash.decode()
        require(is_supported_host(version) and version == tuple(request["blender_version"])
                and build == request["blender_build"],
                "The running Blender differs from the selected host identity", "CONFLICT")
        directory = Path(request["module"])
        for name, sha in request["inventory"].items():
            require(digest(directory / name) == sha, f"Loaded add-on content differs: {name}", "CONFLICT")
        sys.pycache_prefix = str(Path(request["job"]) / "unused-bytecode")
        sys.path.insert(0, str(directory.parent))
        addon = importlib.import_module("physics_nodes_workflow_addon")
        require(Path(addon.__file__).resolve().parent == directory.resolve(), "Wrong add-on loaded", "CONFLICT")
        addon.register()
        actions = {"run": run, "verify": verify}
        data = actions[phase](bpy, addon, request, threads=threads)
        data.update(blender_version=list(version), blender_build=build, worker_resource_guard=observed)
        atomic_json(response_file, {"ok": True, "data": data})
    except Exception as exc:
        traceback.print_exc()
        atomic_json(response_file, {"ok": False, "error": {
            "code": getattr(exc, "code", "WORKER_FAILED"),
            "message": getattr(exc, "message", str(exc)),
        }})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
