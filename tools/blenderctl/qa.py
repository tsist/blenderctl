"""Public saved-file QA worker built on the mesh probe and department contracts."""

import importlib
import math
from pathlib import Path
import re
import subprocess
import time

import bpy
from mathutils import Vector

from filesystem import FileGuard
import mesh_qa
from protocol import Failure, atomic_json, read_json
from qa_albedo import sample_material
from qa_udim import tiled_files


def finding(code, gradient, **details):
    return {"code": code, "gradient": gradient, "witness": details}


def _boolean_cutters(objects):
    cutters = set()
    for obj in objects:
        for mod in obj.modifiers:
            if mod.type != "BOOLEAN":
                continue
            if mod.operand_type == "OBJECT" and mod.object:
                cutters.add(mod.object)
            elif mod.operand_type == "COLLECTION" and mod.collection:
                cutters.update(mod.collection.all_objects)
    return cutters


def _is_helper_cutter(obj):
    return obj.hide_viewport or obj.hide_get() or obj.display_type in {"WIRE", "BOUNDS"}


def _linked_image_nodes(material):
    if not material.use_nodes or not material.node_tree:
        return []
    pending = [material.node_tree]
    visited = set()
    images = set()
    while pending:
        tree = pending.pop()
        if tree.as_pointer() in visited:
            continue
        visited.add(tree.as_pointer())
        for node in tree.nodes:
            if node.type == "GROUP" and node.node_tree:
                pending.append(node.node_tree)
            elif node.type == "TEX_IMAGE" and node.image and any(out.is_linked for out in node.outputs):
                images.add(node.image)
    return sorted(images, key=lambda image: image.name)


def _topology_findings(obj, profile):
    import bmesh

    source = obj.data
    if len(source.polygons) > 150_000:
        return [finding("control_cage_unmeasured", None, object=obj.name, faces=len(source.polygons))]
    deforming = any(mod.type in {"SUBSURF", "MULTIRES", "ARMATURE", "CURVE", "LATTICE", "SURFACE_DEFORM"}
                   and (mod.show_viewport or mod.show_render) for mod in obj.modifiers)
    result = []
    mesh = bmesh.new()
    try:
        mesh.from_mesh(source)
        mesh.normal_update()
        loose = [vertex.index for vertex in mesh.verts if not vertex.link_edges]
        ngons = []
        for face in mesh.faces:
            if len(face.verts) <= 4:
                continue
            origin = face.verts[0].co
            planar = all(abs(face.normal.dot(vertex.co - origin)) <= 1e-5 for vertex in face.verts)
            if deforming or not planar or profile == "mobile_strict":
                ngons.append(face.index)
        rows = [("loose_vertices", loose), ("unsupported_control_cage_ngons", ngons)]
        if deforming and obj.dimensions.length > 0.015:
            poles = []
            for vertex in mesh.verts:
                if vertex.is_boundary or len(vertex.link_edges) < 6 or len(vertex.link_faces) < 2:
                    continue
                first, *rest = vertex.link_faces
                if any(first.normal.dot(face.normal) < 0.985 for face in rest):
                    poles.append(vertex.index)
            rows.append(("curved_control_cage_poles", poles))
        for code, indices in rows:
            if indices:
                result.append(finding(code, "G2", object=obj.name, count=len(indices), indices=indices[:25]))
    finally:
        mesh.free()
    return result


def _engineering(scene, objects, core):
    findings = []
    for row in core["hard_blockers"]:
        gradient = "G2" if row["code"] in {"unapplied_scale", "zfighting"} else "G1"
        findings.append(finding(row["code"], gradient, count=row["count"], examples=row["witnesses"]))
    for row in core["review_signals"]:
        if row["code"] in {"unapplied_scale", "zfighting", "inside_out_islands", "degenerate_faces",
                           "loose_vertices", "uv_oob", "uv_overlap_within_tile",
                           "udim_tile_margin", "udim_texture_tile_mismatch", "udim_texture_resolution_mismatch",
                           "udim_texture_token_missing"}:
            findings.append(finding(row["code"], "G2", count=row["count"], examples=row["witnesses"]))

    materials = {slot.material for obj in objects for slot in obj.material_slots if slot.material}
    materials.update(material for material in bpy.data.materials if material.asset_data)
    albedo = []
    cache = {}
    for material in sorted(materials, key=lambda value: value.name):
        albedo.extend(sample_material(material, cache))
    for row in albedo:
        if row['status'] == 'review':
            severe = row['srgb_value_min'] < 15 or row['srgb_value_max'] > 248
            strict = 'strict' in core['profile'] or 'game' in core['profile']
            findings.append(finding('dielectric_albedo_out_of_range', 'G5', **row,
                                    blocking=severe and strict))
    images = {image for mat in materials for image in _linked_image_nodes(mat)}
    for image in sorted(images, key=lambda value: value.name):
        if image.source in {"FILE", "TILED", "SEQUENCE", "MOVIE"} and image.filepath and not image.packed_file:
            if not image.filepath.startswith("//"):
                findings.append(finding("absolute_texture_path", "G1", image=image.name, path=image.filepath))
        if not objects and image.source in {"FILE", "TILED", "SEQUENCE", "MOVIE"} and image.filepath:
            packed = {Path(entry.filepath).name for entry in image.packed_files}
            files = (tiled_files(image) if image.source == "TILED"
                     else [(None, Path(bpy.path.abspath(image.filepath, library=image.library)))])
            for tile, path in files:
                if not path.is_file() and path.name not in packed:
                    findings.append(finding("missing_texture_file", "G1", image=image.name, tile=tile, path=str(path)))

    cutters = _boolean_cutters(objects)
    for cutter in sorted(cutters, key=lambda item: item.name):
        if _is_helper_cutter(cutter) and not cutter.hide_render:
            findings.append(finding("cutter_render_visible", "G1", object=cutter.name))

    if objects and (scene.unit_settings.system != "METRIC" or abs(scene.unit_settings.scale_length - 1) > 1e-5):
        findings.append(finding("unit_scale_mismatch", "G2", scene=scene.name,
                                system=scene.unit_settings.system, scale=scene.unit_settings.scale_length))
    collections = [collection for collection in scene.collection.children
                   if not re.match(r"^(rig|wgt|light|cam|studio|turntable|ref|helper|cutter)[_ -]", collection.name, re.I)]
    if objects and (len(collections) != 1 or any(obj.name in scene.collection.objects for obj in objects)):
        findings.append(finding("root_collection_check", "G2", collections=[c.name for c in collections]))
    default_name = re.compile(r"^(Cube|Plane|Sphere|Cylinder|Cone|Torus|Circle|Mesh|Material|Collection)(\.\d+)?$")
    for item in [*objects, *(obj.data for obj in objects), *materials, *collections]:
        if default_name.fullmatch(item.name):
            findings.append(finding("default_datablock_name", "G2", type=item.bl_rna.identifier, name=item.name))
    orphan_names = [item.name for group in (bpy.data.meshes, bpy.data.materials)
                    for item in group if item.users == 0 and not item.library]
    if objects and orphan_names:
        findings.append(finding("orphan_datablocks", "G2", count=len(orphan_names), examples=sorted(orphan_names)[:25]))

    orientation = []
    for obj in objects:
        findings.extend(_topology_findings(obj, core["profile"]))
        front = obj.get("qa_front_axis")
        if front is not None:
            axes = {"-Y": (0, -1, 0), "+Y": (0, 1, 0), "-X": (-1, 0, 0), "+X": (1, 0, 0),
                    "-Z": (0, 0, -1), "+Z": (0, 0, 1)}
            if not isinstance(front, str) or front not in axes:
                findings.append(finding("invalid_front_axis_declaration", "G2", object=obj.name))
            else:
                world = obj.matrix_world.to_3x3() @ Vector(axes[front])
                angle = math.degrees(world.angle(Vector((0, -1, 0)), math.pi))
                orientation.append({"object": obj.name, "local_front": front, "world_angle_to_minus_y": angle})
                if angle > 0.1:
                    findings.append(finding("front_axis_not_minus_y", "G2", object=obj.name, world_angle=angle))

        for mod in obj.modifiers:
            if mod.type not in {"SUBSURF", "MULTIRES"} or not (mod.show_viewport or mod.show_render):
                continue
            if mod.levels != mod.render_levels:
                findings.append(finding("modifier_level_mismatch", "G1", object=obj.name, modifier=mod.name,
                                        viewport_levels=mod.levels, render_levels=mod.render_levels))
            if mod.type == "SUBSURF" and mod.subdivision_type == "CATMULL_CLARK":
                sharp_attribute = obj.data.attributes.get("sharp_edge")
                sharp = [index for index, attr in enumerate(sharp_attribute.data) if attr.value] if sharp_attribute else []
                if sharp:
                    findings.append(finding("subdivision_mark_sharp", "G2", object=obj.name, modifier=mod.name,
                                            count=len(sharp), edge_indices=sharp[:25]))
    return findings, albedo, orientation


def _scene_profile(scene, depsgraph, eval_seconds):
    rows = []
    total_faces = 0
    instances = 0
    for item in depsgraph.object_instances:
        obj = item.object
        if item.is_instance:
            instances += 1
        if obj.type == "MESH" and obj.data:
            total_faces += len(obj.data.polygons)
    for obj in depsgraph.objects:
        if obj.type != "MESH":
            continue
        modifiers = [
            {"name": mod.name, "type": mod.type, "seconds": float(mod.execution_time)}
            for mod in obj.modifiers if mod.show_viewport
        ]
        started = time.perf_counter()
        mesh = obj.to_mesh()
        seconds = time.perf_counter() - started
        if mesh is not None:
            rows.append({"object": obj.name, "evaluated_faces": len(mesh.polygons), "mesh_access_seconds": seconds,
                         "modifier_seconds": sum(mod["seconds"] for mod in modifiers),
                         "modifiers": sorted(modifiers, key=lambda item: -item["seconds"])})
            obj.to_mesh_clear()
    textures = []
    for image in bpy.data.images:
        if image.source in {"VIEWER", "RENDER_RESULT"} or image.users == 0:
            continue
        width, height = image.size
        tiles = len(image.tiles) if image.source == "TILED" else 1
        textures.append({"image": image.name, "width": width, "height": height, "tiles": tiles,
                         "estimated_bytes": width * height * 4 * (4 if image.is_float else 1) * tiles})
    return {
        "scene": scene.name, "frame": scene.frame_current,
        "depsgraph_evaluation_seconds": eval_seconds,
        "modifier_evaluation_hotlist": sorted(rows, key=lambda item: -item["modifier_seconds"])[:10],
        "mesh_access_hotlist": sorted(rows, key=lambda item: -item["mesh_access_seconds"])[:10],
        "texture_memory_top10": sorted(textures, key=lambda item: -item["estimated_bytes"])[:10],
        "texture_memory_method": "uncompressed RGBA, using active tile size across each UDIM image",
        "evaluated_faces": total_faces, "instance_count": instances,
    }


def _registered_findings(context, profile):
    registry = read_json(Path(__file__).with_name("registry.json"))
    names = list(dict.fromkeys(name for plugin in registry["plugins"] for name in plugin["qa_probes"]))
    reports = []
    for name in names:
        if not isinstance(name, str) or not re.fullmatch(r"qa_probe_[a-z][a-z0-9_]*", name):
            raise Failure("INVALID_REQUEST", f"Invalid registry QA probe module: {name!r}")
        module = importlib.import_module(name)
        rows = module.run_probe(context, profile)
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("code"), str)
            or row.get("gradient") not in {"G1", "G2", "G5", None} for row in rows
        ):
            raise Failure("INVALID_REQUEST", f"Invalid QA findings from {name}")
        reports.extend(rows)
    return names, reports


def _media_report(descriptor):
    from doctor import media_tools

    tools = media_tools(Path(__file__).resolve().parents[2])
    if not tools["ok"]:
        raise Failure("UNSUPPORTED", "FFmpeg and FFprobe are required for finished-media QA")
    with FileGuard(descriptor["file"]) as guard:
        if guard.sha256() != descriptor["expected_sha256"]:
            raise Failure("CONFLICT", "Finished media SHA-256 differs from the request")
        run_options = dict(capture_output=True, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        probe = subprocess.run([tools["ffprobe"]["path"], "-v", "error", "-show_entries",
                                "stream=codec_type", "-of", "csv=p=0", descriptor["file"]], **run_options)
        if probe.returncode:
            raise Failure("WORKER_FAILED", probe.stderr[-1500:])
        streams = probe.stdout.splitlines()
        if "video" not in streams:
            raise Failure("INVALID_REQUEST", "Finished-media QA requires a video stream")
        has_audio = "audio" in streams
        args = [
            tools["ffmpeg"]["path"], "-hide_banner", "-nostdin", "-i", descriptor["file"],
            "-map", "0:v:0", "-vf", "blackdetect=d=0:pix_th=0.05",
            *(["-map", "0:a:0", "-af", "ebur128=peak=true"] if has_audio else ["-an"]),
            "-f", "null", "-",
        ]
        result = subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise Failure("WORKER_FAILED", result.stderr[-1500:])
        black = re.findall(r"black_start:([0-9.]+) black_end:([0-9.]+) black_duration:([0-9.]+)", result.stderr)
        summary = result.stderr.rpartition("Summary:")[2]
        lufs = re.search(r"\bI:\s*(-?[\d.]+) LUFS", summary)
        peak = re.search(r"\bPeak:\s*(-?[\d.]+) dBFS", summary)
        return {"source": descriptor, "has_audio": has_audio, "black_segments": [[float(value) for value in row] for row in black],
                "integrated_lufs": float(lufs.group(1)) if lufs else None,
                "true_peak_dbtp": float(peak.group(1)) if peak else None}


def inspect(params, job):
    source = {"file": params["file"], "sha256": params["expected_sha256"]}
    if bpy.ops.wm.open_mainfile(filepath=source["file"], load_ui=False, use_scripts=False) != {"FINISHED"}:
        raise Failure("WORKER_FAILED", "Could not cold-open QA source")
    scene = bpy.context.scene
    if params.get("profile_scene"):
        for obj in scene.objects:
            obj.update_tag(refresh={"OBJECT", "DATA"})
    started = time.perf_counter()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    depsgraph.update()
    eval_seconds = time.perf_counter() - started
    options = params["options"]
    declaration = params.get("udim")
    if declaration and "udim_measurements" in params:
        declaration = {**declaration, **params["udim_measurements"]}
    try:
        core = mesh_qa.run(**options, udim=declaration)
        objects = [bpy.data.objects[row["name"]] for row in core["objects"]]
    except mesh_qa.UVLayerNotFound as exc:
        raise Failure("NOT_FOUND", str(exc)) from exc
    except ValueError as exc:
        raise Failure("INVALID_REQUEST", str(exc)) from exc
    findings, albedo, orientation = _engineering(scene, objects, core)
    probes, department = _registered_findings({"scene": scene, "objects": objects, "mesh_qa": core}, options["profile"])
    findings.extend(department)
    media = _media_report(params["probe_file"]) if "probe_file" in params else None
    if media and media["black_segments"]:
        findings.append(finding("black_segments", "G1", count=len(media["black_segments"])))
    if media and media["integrated_lufs"] is not None:
        if not (-15 <= media["integrated_lufs"] <= -13 or -24 <= media["integrated_lufs"] <= -22):
            findings.append(finding("media_loudness_out_of_range", "G1", integrated_lufs=media["integrated_lufs"]))
        if media["true_peak_dbtp"] is not None and media["true_peak_dbtp"] > -1:
            findings.append(finding("media_true_peak_out_of_range", "G1", true_peak_dbtp=media["true_peak_dbtp"]))
    scene_profile_file = None
    if params.get("profile_scene"):
        scene_profile_file = job / "scene-profile.json"
        atomic_json(scene_profile_file, _scene_profile(scene, depsgraph, eval_seconds))

    g1 = [row for row in findings if row["gradient"] == "G1"]
    g2 = [row for row in findings if row["gradient"] == "G2"]
    unmeasured_cages = any(row["code"] == "control_cage_unmeasured" for row in findings)
    g5 = [row for row in findings if row["gradient"] == "G5"]
    albedo_findings = [row for row in g5 if row["code"] == "dielectric_albedo_out_of_range"]
    engineering_passed = (not g1 and not g2 and not core["skipped"] and not unmeasured_cages
                          and not any(row["witness"]["blocking"] for row in albedo_findings))
    g1_deductions = {
        "data_complete": {"negative_scale", "non_finite"},
        "texture_integrity": {"absolute_texture_path", "missing_texture_file", "data_texture_srgb",
                              "empty_material_slot_in_use"},
    }
    g1_codes = {row["code"] for row in g1}
    g1_points = {name: 0 if g1_codes & codes else 5 for name, codes in g1_deductions.items()}
    g1_points["evaluation_and_visibility"] = 0 if g1_codes - set().union(*g1_deductions.values()) else 5
    g2_codes = {row["code"] for row in g2}
    outline = {"default_datablock_name", "root_collection_check", "orphan_datablocks"}
    dimensions = {"unit_scale_mismatch", "unapplied_scale", "invalid_front_axis_declaration", "front_axis_not_minus_y"}
    g2_points = {
        "outliner": 8 if not g2_codes & outline else 0,
        "dimensions_and_axis": (5 if len(orientation) == len(objects) else 3) if not g2_codes & dimensions else 0,
        "control_cage": 12 if not (g2_codes - outline - dimensions or unmeasured_cages or core["skipped"]) else 0,
    } if objects else {}
    scorecard = {
        "schema_version": "1.0", "source": source, "stage": "objective_probes",
        "G1_engineering": {"max": 15, "score": sum(g1_points.values()), "points": g1_points, "findings": g1},
        "G2_structure_topology": {"max": 25 if objects else 0,
                                 "score": sum(g2_points.values()), "points": g2_points, "findings": g2,
                                 "orientation": orientation, "orientation_unmeasured": len(objects) - len(orientation)},
        "G5_dielectric_albedo": {"max": 5, "score": 0 if albedo_findings else
                                 5 if any(row["status"] == "pass" for row in albedo) else None,
                                 "findings": albedo_findings},
        "engineering_gates_passed": engineering_passed,
        "visual_review_required": True,
    }
    qa = {
        **core, "source": source, "engineering_gates_passed": engineering_passed,
        "department_probes": probes, "findings": findings,
        "albedo": albedo, "media": media,
    }
    report = job / "qa-report.json"
    score = job / "quality-scorecard.json"
    atomic_json(report, qa)
    atomic_json(score, scorecard)
    return {"read_only": True, "source": source,
            "qa": {k: qa[k] for k in ("status", "profile", "stats", "counts", "engineering_gates_passed")},
            "report": str(report), "scorecard": str(score),
            "scene_profile": str(scene_profile_file) if scene_profile_file else None}
