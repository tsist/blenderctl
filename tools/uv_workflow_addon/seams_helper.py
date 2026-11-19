"""Read-only hard-edge/angle seam analysis and explicit base-mesh seam marking helpers."""
import copy
import math

import bmesh

from .contract import fail


def analyze_object_edges(obj, angle_degrees=60.0, include_boundaries=True):
    """Classify sharp, dihedral-angle, and boundary edge indices in world space."""
    if not math.isfinite(angle_degrees) or not (0.0 <= angle_degrees <= 180.0):
        fail("angle_degrees must be between 0 and 180")
    mesh = obj.data
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bm.transform(obj.matrix_world)
        bm.normal_update()
        bm.edges.ensure_lookup_table()
        threshold_rad = math.radians(angle_degrees) - 1e-6
        angle_edges = []
        boundary_edges = []
        for bm_edge in bm.edges:
            faces = bm_edge.link_faces
            if len(faces) == 1:
                boundary_edges.append(bm_edge.index)
            elif len(faces) == 2:
                if abs(bm_edge.calc_face_angle(0.0)) >= threshold_rad:
                    angle_edges.append(bm_edge.index)
    finally:
        bm.free()

    sharp_edges = [e.index for e in mesh.edges if e.use_edge_sharp]
    existing_seams = [e.index for e in mesh.edges if e.use_seam]
    boundary_set = set(boundary_edges) if include_boundaries else set()
    suggested_by_angle = sorted(set(angle_edges) | boundary_set)
    suggested_from_sharp = sorted(set(sharp_edges) | boundary_set)
    return {
        "edge_count": len(mesh.edges),
        "angle_edges": sorted(angle_edges),
        "sharp_edges": sharp_edges,
        "boundary_edges": sorted(boundary_edges),
        "existing_seams": existing_seams,
        "suggested_by_angle": suggested_by_angle,
        "suggested_from_sharp": suggested_from_sharp,
    }


def resolve_seam_indices(obj, seam_action):
    """Return (seam_edge_indices, sharp_edge_indices) for MARK_BY_ANGLE / MARK_FROM_SHARP."""
    mode = seam_action["mode"]
    info = analyze_object_edges(
        obj,
        angle_degrees=seam_action.get("angle_degrees", 60.0),
        include_boundaries=seam_action.get("include_boundaries", True),
    )
    if mode == "MARK_BY_ANGLE":
        return set(info["suggested_by_angle"]), set(info["angle_edges"])
    if mode == "MARK_FROM_SHARP":
        return set(info["suggested_from_sharp"]), set()
    fail("Unsupported automatic seam resolution mode: " + str(mode))


def suggest_seams(objects, angle_degrees=60.0, include_boundaries=True, max_examples=32):
    """Build read-only seam suggestions for ui and uv.inspect without mutating any mesh."""
    out = {}
    for obj in objects:
        info = analyze_object_edges(obj, angle_degrees=angle_degrees, include_boundaries=include_boundaries)
        existing_set = set(info["existing_seams"])
        missing_angle = [i for i in info["suggested_by_angle"] if i not in existing_set]
        missing_sharp = [i for i in info["suggested_from_sharp"] if i not in existing_set]
        out[obj.name] = {
            "edge_count": info["edge_count"],
            "counts": {
                "angle_edges": len(info["angle_edges"]),
                "sharp_edges": len(info["sharp_edges"]),
                "boundary_edges": len(info["boundary_edges"]),
                "existing_seams": len(info["existing_seams"]),
                "suggested_by_angle": len(info["suggested_by_angle"]),
                "suggested_from_sharp": len(info["suggested_from_sharp"]),
                "missing_from_angle": len(missing_angle),
                "missing_from_sharp": len(missing_sharp),
            },
            "examples": {
                "suggested_by_angle": info["suggested_by_angle"][:max_examples],
                "suggested_from_sharp": info["suggested_from_sharp"][:max_examples],
                "missing_from_angle": missing_angle[:max_examples],
                "missing_from_sharp": missing_sharp[:max_examples],
            },
            "examples_complete": {
                "suggested_by_angle": len(info["suggested_by_angle"]) <= max_examples,
                "suggested_from_sharp": len(info["suggested_from_sharp"]) <= max_examples,
                "missing_from_angle": len(missing_angle) <= max_examples,
                "missing_from_sharp": len(missing_sharp) <= max_examples,
            },
        }
    return {
        "angle_degrees": float(angle_degrees),
        "include_boundaries": bool(include_boundaries),
        "max_examples": int(max_examples),
        "basis": "WORLD_SPACE_BASE_MESH",
        "objects": out,
        "note": "Read-only seam suggestions; apply explicitly with uv.run seams (MARK_BY_ANGLE / MARK_FROM_SHARP) before ANGLE_BASED or MINIMUM_STRETCH unwrap.",
    }


def apply_base_mesh_seams(objects, s, v):
    """Explicitly mark base mesh seams prior to candidate generation without touching UVs or adoption state."""
    if s.get("adopted"):
        fail("Restore the active candidate before marking base mesh seams directly", "CONFLICT")
    action = v["seam_action"]
    mode = action["mode"]
    clear_existing = bool(action.get("clear_existing", False))
    sync_sharp = bool(action.get("sync_sharp", False)) and mode == "MARK_BY_ANGLE"
    old_seams = {o.name: [e.index for e in o.data.edges if e.use_seam] for o in objects}
    old_sharp = {o.name: [e.index for e in o.data.edges if e.use_edge_sharp] for o in objects}
    summary = {}
    try:
        for obj in objects:
            seam_ids, sharp_ids = resolve_seam_indices(obj, action)
            marked_seams = 0
            marked_sharp = 0
            for edge in obj.data.edges:
                if clear_existing:
                    edge.use_seam = edge.index in seam_ids
                elif edge.index in seam_ids:
                    edge.use_seam = True
                if edge.use_seam:
                    marked_seams += 1
                if sync_sharp and edge.index in sharp_ids:
                    edge.use_edge_sharp = True
                if edge.use_edge_sharp:
                    marked_sharp += 1
            summary[obj.name] = {
                "before_seam_count": len(old_seams[obj.name]),
                "after_seam_count": marked_seams,
                "target_seam_count": len(seam_ids),
                "sharp_edge_count": marked_sharp,
                "synced_sharp": sync_sharp,
            }
        return {
            "status": "seams_marked",
            "target_scope": "base_mesh",
            "candidate": v.get("candidate") or "base_mesh",
            "objects": summary,
            "seam_action": copy.deepcopy(action),
            "quality_acceptance": "seams_updated_not_uv_evaluated",
        }
    except Exception:
        for obj in objects:
            prev_seam = set(old_seams[obj.name])
            prev_sharp = set(old_sharp[obj.name])
            for edge in obj.data.edges:
                edge.use_seam = edge.index in prev_seam
                edge.use_edge_sharp = edge.index in prev_sharp
        raise
