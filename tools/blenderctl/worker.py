# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixed worker. All user data arrives through JSON, never Python interpolation."""
import json
import os
from pathlib import Path
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
from protocol import Failure, atomic_json, read_json
import bpy


def identity(item):
    return {"type": item.bl_rna.identifier, "name": item.name,
            "library": item.library.filepath if item.library else None}


def doctor():
    from projects import startup_profile
    build = {key: getattr(bpy.app.build_options, key) for key in dir(bpy.app.build_options)
             if not key.startswith("_") and isinstance(getattr(bpy.app.build_options, key), bool)}
    paths = {key: bpy.utils.user_resource(key) for key in ("CONFIG", "SCRIPTS", "EXTENSIONS", "DATAFILES")}
    # Disk presence is deliberately separate from loaded and tested add-ons.
    install = Path(bpy.app.binary_path).parent
    candidates = []
    bases = [install / "portable/scripts/addons", install / "portable/external-scripts/addons"]
    extensions = install / "portable/extensions"
    if extensions.is_dir():
        bases.extend(p for p in extensions.iterdir() if p.is_dir() and not p.name.startswith("."))
    for base in bases:
        if base.is_dir():
            candidates.append({"path": str(base), "entries": sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith((".", "__"))),
                               "status": "on_disk_only_not_loaded_or_tested"})
    devices = []
    device_error = None
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        for backend in ("CUDA", "OPTIX"):
            for entry in prefs.get_devices_for_type(backend):
                devices.append({"backend": backend, "name": entry.name, "type": entry.type,
                                "id": entry.id, "status": "detected_not_render_verified"})
    except Exception as exc:
        device_error = str(exc)
    return {"startup_profile":startup_profile(),"blender_version": bpy.app.version_string, "version": list(bpy.app.version),
            "build_hash": bpy.app.build_hash.decode(), "binary": bpy.app.binary_path,
            "python_version": sys.version, "background": bpy.app.background,
            "autoexec_enabled": bpy.context.preferences.filepaths.use_scripts_auto_execute,
            "online_access": bpy.app.online_access, "paths": paths,
            "enabled_addons": sorted(bpy.context.preferences.addons.keys()),
            "installed_candidates": candidates, "build_options": build,
            "cycles_devices": devices, "device_detection_error": device_error,
            "limitations": ["Factory configuration; user UI preferences and plug-ins are not enabled.",
                            "Detected device does not establish successful rendering."]}


def capabilities():
    names = ["wm.open_mainfile", "wm.save_as_mainfile", "wm.obj_import", "wm.obj_export",
             "wm.stl_import", "wm.stl_export", "wm.usd_import", "wm.usd_export",
             "import_scene.gltf", "export_scene.gltf", "import_scene.fbx", "export_scene.fbx",
             "uv.smart_project", "uv.pack_islands", "object.modifier_apply", "object.bake",
             "ptcache.bake_all", "fluid.bake_all", "rigidbody.bake_to_keyframes",
             "render.render", "nla.bake", "clip.solve_camera", "sculpt.brush_stroke",
             "ed.lib_id_load_custom_preview"]
    operators = []
    for name in names:
        group, op = name.split(".")
        try:
            getattr(getattr(bpy.ops, group), op).get_rna_type()
            exists = True
        except (AttributeError, RuntimeError):
            exists = False
        operators.append({"name": name, "registered": exists, "functional_status": "not_tested"})
    from scene_contract import OPS
    from model_contract import OPS as MODEL_OPS
    from node_contract import OPS as NODE_OPS
    from rig_contract import OPS as RIG_OPS
    from simulation_contract import OPS as SIMULATION_OPS
    from protocol import COMMANDS
    from execution_contract import capabilities as execution_capabilities
    return {"blender_version": bpy.app.version_string, "operators": operators,
            "execution_contract":execution_capabilities(),
            "scene_operations":sorted(OPS),
            "model_operations":sorted(MODEL_OPS),"node_operations":sorted(NODE_OPS),"rig_operations":sorted(RIG_OPS),"simulation_operations":sorted(SIMULATION_OPS),
            "implemented_commands": [c.replace('.',' ').replace('_','-') for c in COMMANDS if c!='probe']+['job status','job result','job cancel'],
            "scene_commands":["scene prepare","scene inspect"],
            "development_probes": ["scene", "geometry", "nodes", "animation", "render", "exchange"],
            "note": "Registration is not functional support. Inspect is a structural snapshot, not a publish validator."}


def dependency(item, kind):
    raw = item.filepath
    packed = bool(getattr(item, "packed_file", None)) or bool(getattr(item, "packed_files", []))
    linked_packed = bool(getattr(item, "is_linked_packed", False))
    if kind == "library":
        linked_ids = [block for prop in bpy.data.bl_rna.properties if prop.type == "COLLECTION"
                      for block in getattr(bpy.data, prop.identifier, ())
                      if isinstance(block, bpy.types.ID) and block.library == item]
        packed = bool(linked_ids) and all(getattr(block, "is_linked_packed", False) for block in linked_ids)
    source = getattr(item, "source", None)
    sequence = source in ("TILED", "SEQUENCE") or "<UDIM>" in raw or "<UVTILE>" in raw or "#" in raw
    if kind == "font" and raw == "<builtin>":
        status, absolute = "builtin", None
    elif packed or linked_packed:
        status, absolute = "packed", None
    elif not raw:
        status, absolute = "no_external_path", None
    else:
        absolute = bpy.path.abspath(raw, library=item.library)
        status = "pattern_needs_expansion" if sequence else "exists" if Path(absolute).is_file() else "missing"
    return {"owner": identity(item), "kind": kind, "raw_path": raw, "resolved_path": absolute,
            "source": source, "users": item.users, "packed": packed, "linked_content_packed": linked_packed,
            "status": status, "usage_status": "requires_workflow_review"}


def inspect_file(path):
    bpy.ops.wm.open_mainfile(filepath=path, load_ui=False, use_scripts=False)
    ids = []
    seen = set()
    for prop in bpy.data.bl_rna.properties:
        if prop.type != "COLLECTION":
            continue
        for item in getattr(bpy.data, prop.identifier, ()):
            if not isinstance(item, bpy.types.ID):
                continue
            pointer = item.as_pointer()
            if pointer in seen:
                continue
            seen.add(pointer)
            record = {**identity(item), "collection": prop.identifier, "users": item.users,
                      "fake_user": item.use_fake_user, "asset": None}
            if item.asset_data:
                asset = item.asset_data
                record["asset"] = {"description": asset.description, "author": asset.author,
                                   "catalog_id": asset.catalog_id, "tags": [tag.name for tag in asset.tags],
                                   "asset_id": item.get("asset_id") if isinstance(item.get("asset_id"), str) else None}
            if isinstance(item, bpy.types.Object):
                record.update(object_type=item.type, data=identity(item.data) if item.data else None,
                              parent=identity(item.parent) if item.parent else None,
                              location=list(item.location), scale=list(item.scale),
                              modifiers=[{"name": mod.name, "type": mod.type} for mod in item.modifiers])
            if isinstance(item, bpy.types.Mesh):
                record.update(vertices=len(item.vertices), edges=len(item.edges), polygons=len(item.polygons),
                              uv_layers=[layer.name for layer in item.uv_layers])
            ids.append(record)
    ids.sort(key=lambda i: (i["type"], i["library"] or "", i["name"]))
    users = bpy.data.user_map()
    references = [{"target": identity(item), "users": sorted([identity(u) for u in use], key=lambda i: (i["type"], i["library"] or "", i["name"]))}
                  for item, use in users.items() if use]
    references.sort(key=lambda i: (i["target"]["type"], i["target"]["library"] or "", i["target"]["name"]))
    deps = []
    for collection, kind in (("images", "image"), ("libraries", "library"), ("sounds", "sound"),
                             ("fonts", "font"), ("movieclips", "movieclip"), ("cache_files", "cache"), ("volumes", "volume")):
        for item in getattr(bpy.data, collection, ()):
            deps.append(dependency(item, kind))
    return {"file": path, "saved_version": list(bpy.data.version), "reader_version": bpy.app.version_string,
            "datablocks": ids, "references": references, "dependencies": deps,
            "scene_count": len(bpy.data.scenes), "object_count": len(bpy.data.objects),
            "asset_count": sum(i["asset"] is not None for i in ids),
            "validation": {"open": "pass", "render": "not_run", "animation": "not_run",
                           "dependency_closure": "partial", "licensing": "not_run"},
            "limitations": ["UDIM/sequences require frame/tile expansion.",
                            "Simulation directories, VSE strip files and arbitrary plug-in resources are not enumerated yet.",
                            "ID users are structural references, not proof of use in a specific render or animation.",
                            "Disabled auto-execution can affect scripted drivers; inspection does not validate animation.",
                            "Historical UI paths are intentionally excluded."]}


def main():
    job = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
    deadline = time.monotonic() + 10
    while not (job / "worker.gate").exists():
        if time.monotonic() > deadline:
            raise RuntimeError("Supervisor did not acquire process-tree ownership")
        time.sleep(0.02)
    request = read_json(job / "request.json")
    try:
        command = request["command"]
        if command == "doctor":
            data = doctor()
        elif command == "capabilities":
            data = capabilities()
        elif command=='sculpt.review':
            import sculpt_review
            data=sculpt_review.run(request['params'],job)
        elif command in ("inspect", "_inspect_staged"):
            data = inspect_file(request["params"]["file"])
        elif command=='dependency.audit' and request['params'].get('profile',{}).get('closure'):
            import typed_dependencies
            data=typed_dependencies.run(request['params'],job)
        elif command in ('dependency.plan-relink','dependency.prepare'):
            import dependency_relink
            data=(dependency_relink.plan_relink if command=='dependency.plan-relink' else dependency_relink.prepare)(request['params'],job)
        elif command in ('project.link.prepare', 'project.override.prepare', 'project.override.resync'):
            import linking
            data = linking.run(command, request['params'], job)
        elif command in ("query", "diff", "dependency.audit", "validate", "identity.prepare"):
            from inspection import run
            data = run(command, request["params"], job)
        elif command=='asset.preview.prepare':
            import asset_previews
            data=asset_previews.prepare(request['params'],job)
        elif command in ("asset.prepare", "asset.index"):
            import assets
            data = getattr(assets, command.split(".")[1])(request["params"], job)
        elif command in ('scene.prepare','scene.inspect'):
            import scenes
            data=getattr(scenes,command.split('.')[1])(request['params'],job)
        elif command in ('model.prepare','model.inspect'):
            import modeling
            data=getattr(modeling,command.split('.')[1])(request['params'],job)
        elif command in ('node.prepare','node.inspect'):
            import nodes
            data=getattr(nodes,command.split('.')[1])(request['params'],job)
        elif command=='rig.package':
            from rig_package import package
            data=package(request['params'],job)
        elif command in ('rig.prepare','rig.inspect','animation.sample'):
            import rigging
            data=getattr(rigging,command.split('.')[1])(request['params'],job)
        elif command in ('simulation.prepare','simulation.inspect','simulation.bake'):
            import simulation
            data=getattr(simulation,command.split('.')[1])(request['params'],job)
        elif command.startswith('exchange.'):
            import exchange
            data=getattr(exchange,'import_' if command.endswith('.import') else command.split('.')[1])(request['params'],job)
        elif command.startswith('extension.'):
            import extensions
            data=getattr(extensions,command.split('.')[1])(request['params'],job)
        elif command in ('render.devices','render.run'):
            import rendering
            data=getattr(rendering,command.split('.')[1])(request['params'],job)
        elif command in ('project.verify','project.prepare_copy'):
            import projects
            data=getattr(projects,command.split('.')[1])(request['params'],job)
        elif command in ('tracking.prepare','tracking.inspect','tracking.solve'):
            import tracking
            data=getattr(tracking,command.split('.')[1])(request['params'],job)
        elif command in ('media.prepare','media.export'):
            import media
            data=getattr(media,command.split('.')[1])(request['params'],job)
        elif command=='material.run':
            import material_workflow
            data=material_workflow.run(request['params'],job)
        elif command=='material.batch':
            import material_batch
            data=material_batch.run(request['params'],job)
        elif command=='material.study':
            import material_study
            data=material_study.run(request['params'],job)
        elif command=='material.template-save':
            import material_workflow
            data=material_workflow.save_template(request['params'],job)
        elif command in ('material.preview','texture.bake'):
            import material_outputs
            data=getattr(material_outputs,command.split('.')[1])(request['params'],job)
        elif command=='_library_validate':
            from library import validate_worker
            data=validate_worker(request['params'])
        elif command == "_save_copy":
            output = Path(request["params"]["output"])
            if os.path.lexists(output):
                raise Failure("CONFLICT", "Staging path already exists")
            before = inspect_file(request["params"]["file"])
            if tuple(bpy.data.version[:2]) != tuple(bpy.app.version[:2]):
                raise Failure("UNSUPPORTED", "Copy saving across Blender major/minor versions requires a separate migration policy")
            bpy.ops.wm.save_as_mainfile(filepath=str(output), check_existing=False, relative_remap=True, copy=True)
            data = {"before": before, "output": str(output), "reader_version": bpy.app.version_string}
        elif command == "probe":
            from probes import run
            data = run(request["params"]["case"], job)
        else:
            raise ValueError("Unsupported worker command")
        atomic_json(job / "worker-result.json", {"ok": True, "data": data, "error": None})
    except Exception as exc:
        atomic_json(job / "worker-result.json", {"ok": False, "data": None,
                                                "error": {"type": type(exc).__name__, "message": str(exc), "code": exc.code if isinstance(exc, Failure) else "WORKER_FAILED"}})
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
