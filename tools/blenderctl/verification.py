# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage-5 validation adapters, evaluated observations and ID working candidates."""
import math
import os
from pathlib import Path
import uuid

import bpy
from protocol import Failure, atomic_json, digest, read_json
from inspection import all_ids, identity, key, path_value, mesh_content, select, snapshot, value


def timeline(profile):
    t = profile.get("timeline")
    if not t:
        return None, []
    scene = bpy.data.scenes.get(t["scene"])
    if not scene:
        raise Failure("NOT_FOUND", "Profile scene does not exist")
    return scene, list(range(t["start"], t["end"] + 1, t.get("step", 1)))


def resolve(items, selector):
    matches = [i for i in items if (str(i.get("asset_id", "")).lower() == selector["asset_id"].lower() if "asset_id" in selector else identity(i) == selector)]
    if len(matches) != 1:
        raise Failure("CONFLICT" if matches else "NOT_FOUND", "Profile selector must resolve uniquely")
    return matches[0]


def declared_resources(items, profile, reachable, frames):
    from dependencies import resource, file_fingerprint
    results = []
    for spec in profile.get("resources", []):
        owner = resolve(items, spec["owner"])
        raw = owner.get(spec["property"])
        if not isinstance(raw, str) or not raw:
            raise Failure("VALIDATION_FAILED", "Resource adapter property must contain a directory path")
        root = Path(path_value(raw, owner.library))
        files = []
        for entry in spec["files"]:
            path = Path(entry["path"])
            absolute = Path(os.path.abspath(root / path))
            if path.is_absolute() or not absolute.is_relative_to(root) or not absolute.resolve().is_relative_to(root.resolve()):
                raise Failure("INVALID_REQUEST", "Declared resources must stay inside the owner directory")
            files.append(os.path.normcase(str(absolute)))
        dep = resource(identity(owner), "declared_" + spec["kind"], raw, owner.library, expected=files,
                       reachability="scene_or_asset" if key(identity(owner)) in reachable else "retained_unreferenced",
                       binding={"adapter": spec["adapter"], "property": spec["property"], "frames": frames})
        missing_frames = sorted(set(frames) - {f["frame"] for f in spec["files"] if "frame" in f}) if spec["kind"] == "cache" else []
        mismatched = [path for entry, path in zip(spec["files"], files) if entry.get("sha256") and Path(path).is_file() and file_fingerprint(path)["sha256"] != entry["sha256"]]
        dep.update(completeness="declared_contract_only", contract={"missing_frames": missing_frames, "hash_mismatches": mismatched})
        if missing_frames or mismatched:
            dep["status"] = "incomplete"
        results.append(dep)
    return results


def evaluate(profile):
    scene, frames = timeline(profile)
    if not profile.get("evaluate"):
        return [], []
    bpy.context.window.scene = scene
    objects = [(resolve(list(all_ids()), s["selector"]), s) for s in profile["evaluate"]]
    if any(not isinstance(o, bpy.types.Object) or o.name not in scene.objects for o, _ in objects):
        raise Failure("INVALID_REQUEST", "Evaluation selectors must be objects in the selected scene")
    observations, findings = [], []
    for frame in frames:
        scene.frame_set(frame)
        graph = bpy.context.evaluated_depsgraph_get()
        for obj, spec in objects:
            evaluated = obj.evaluated_get(graph)
            entry = {"owner": identity(obj), "frame": frame, "location": list(evaluated.matrix_world.translation),
                     "matrix_world": value(evaluated.matrix_world), "pose": {b.name: [v for row in b.matrix for v in row] for b in evaluated.pose.bones} if evaluated.pose else {}}
            if obj.type == "MESH":
                mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=graph)
                try:
                    entry["mesh"] = mesh_content(mesh)
                    coords = [evaluated.matrix_world @ v.co for v in mesh.vertices]
                    entry["bounds"] = [min(v[i] for v in coords) for i in range(3)] + [max(v[i] for v in coords) for i in range(3)] if coords else None
                finally:
                    evaluated.to_mesh_clear()
                if entry["mesh"]["non_finite_values"]:
                    findings.append({"severity": "error", "code": "INVALID_EVALUATED_GEOMETRY", "owner": identity(obj), "frame": frame})
            from inspection import _numbers
            if any(not math.isfinite(n) for n in _numbers(entry)):
                findings.append({"severity": "error", "code": "NON_FINITE_EVALUATION", "owner": identity(obj), "frame": frame})
            for ex in spec.get("expect", []):
                if ex["frame"] != frame:
                    continue
                actual = {"vertices": entry.get("mesh", {}).get("counts", {}).get("vertices"),
                          "location": entry["location"], "bounds": entry.get("bounds"), "bone_matrix": entry["pose"].get(ex.get("bone"))}
                for field in ("vertices", "location", "bounds", "bone_matrix"):
                    if field not in ex:
                        continue
                    a, b = actual[field], ex[field]
                    ok = a == b if field == "vertices" else isinstance(a, list) and len(a) == len(b) and all(abs(x-y) <= ex.get("tolerance", 1e-5) for x, y in zip(a, b))
                    if not ok:
                        findings.append({"severity": "error", "code": "EVALUATION_MISMATCH", "owner": identity(obj), "frame": frame, "field": field, "expected": b, "actual": a})
            observations.append(entry)
        for item in all_ids():
            animation = getattr(item, "animation_data", None)
            if animation:
                for curve in animation.drivers:
                    if not curve.is_valid or not curve.driver.is_valid:
                        findings.append({"severity": "error", "code": "INVALID_DRIVER", "owner": identity(item), "frame": frame, "path": curve.data_path})
    return observations, findings


def extend_validation(report, data, profile, job):
    observations, findings = evaluate(profile)
    if observations:
        atomic_json(job / "evaluation.json", {"profile": profile, "observations": observations})
        report["evaluation"] = {"samples": len(observations), "artifact": str(job / "evaluation.json"), "mode": "depsgraph_viewport", "scope": "explicit_frames_and_expectations"}
        report["not_run"].remove("animation_evaluation")
    if "asset_records" in profile or "asset_index" in profile:
        from asset_validation import validate_records
        records = read_json(profile["asset_index"]["file"]) if "asset_index" in profile else profile["asset_records"]
        if not isinstance(records, list) or len(records) > 1000:
            raise Failure("INVALID_REQUEST", "Asset index must be an array of at most 1000 records")
        findings.extend(validate_records(records, profile.get("catalogs", []), data, scope_file='asset_index' in profile))
        report['asset_index_scope']='record_schema_and_unique_ids_globally; blend_consistency_for_requested_file' if 'asset_index' in profile else 'all_inline_records_for_requested_file'
        report["not_run"].remove("asset_index_and_catalog_consistency")
        report["asset_records_checked"] = len(records)
    if profile.get("render"):
        scene, frames = timeline(profile)
        scene = scene or bpy.context.scene
        if not scene.camera:
            findings.append({"severity": "error", "code": "RENDER_CAMERA_MISSING"})
        else:
            scene.render.engine = "CYCLES"
            scene.cycles.device = "CPU"
            scene.cycles.samples = 4
            scene.render.resolution_x = scene.render.resolution_y = 32
            scene.render.resolution_percentage = 100
            scene.render.use_compositing = False
            scene.render.use_sequencer = False
            scene.render.use_multiview = False
            scene.render.image_settings.file_format = "PNG"
            scene.render.filepath = str(job / "validation-render.png")
            scene.frame_set(frames[0] if frames else scene.frame_current)
            bpy.ops.render.render(write_still=True, scene=scene.name)
            image = bpy.data.images.load(scene.render.filepath, check_existing=False)
            pixels = list(image.pixels)
            if not pixels or any(not math.isfinite(v) for v in pixels):
                findings.append({"severity": "error", "code": "RENDER_INVALID_PIXELS"})
            report["render"] = {"file": scene.render.filepath, "sha256": digest(scene.render.filepath), "size": list(image.size), "engine": "CYCLES_CPU", "scope": "smoke_only_not_visual_approval"}
            report["not_run"].remove("render")
    report["findings"].extend(findings)
    if profile.get("require_complete"):
        for finding in report["findings"]:
            if finding["code"] == "RESOURCE_COVERAGE_UNVERIFIED":
                finding["severity"] = "error"
    report["valid_within_scope"] = not any(f["severity"] == "error" for f in report["findings"])


def prepare_identity(data, params, job):
    if tuple(bpy.data.version[:2]) != tuple(bpy.app.version[:2]):
        raise Failure("UNSUPPORTED", "ID candidate refuses cross-version save")
    from inspection import validate_snapshot
    if any(f["code"] in {"INVALID_ASSET_ID", "DUPLICATE_ASSET_ID"} for f in validate_snapshot(data)["findings"]):
        raise Failure("CONFLICT", "Resolve invalid/duplicate IDs before registration")
    chosen = [resolve(list(all_ids()), s) for s in params["selectors"]]
    if len({o.as_pointer() for o in chosen}) != len(chosen):
        raise Failure("CONFLICT", "Duplicate registration target")
    if any(o.library or o.override_library or not o.asset_data for o in chosen):
        raise Failure("INVALID_REQUEST", "Registration requires local non-override marked assets")
    mapping = []
    for item in chosen:
        old = item.get("asset_id")
        assigned = str(uuid.UUID(old)) if old else str(uuid.uuid4())
        item["asset_id"] = assigned
        mapping.append({"selector": identity(item), "asset_id": assigned, "reused": bool(old)})
    candidate = job / "identity-candidate.blend"
    if candidate.exists():
        raise Failure("CONFLICT", "Candidate already exists")
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(candidate), copy=True, relative_remap=True)
    reopened = snapshot(str(candidate))
    for m in mapping:
        if select(reopened, {"asset_id": m["asset_id"]})["selected"]["name"] != m["selector"]["name"]:
            raise Failure("VALIDATION_FAILED", "Saved ID differs")
    atomic_json(job / "identity-map.json", {"source": params["file"], "source_sha256": digest(params["file"]), "candidate": str(candidate), "candidate_sha256": digest(candidate), "mapping": mapping})
    return {"candidate": str(candidate), "sha256": digest(candidate), "mapping": mapping,
            "next_step": "project plan-copy candidate --output NEW_WORKING_COPY then transaction apply; independent reopen in transaction"}
