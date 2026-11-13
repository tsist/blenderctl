"""Cross-frame evaluation, kinematics diagnostics, penetration check, and cache/edit reporting."""

from __future__ import annotations

import json
import math
from typing import Any

import bpy

from . import bake as pnw_bake
from . import subgroups as pnw_subgroups


def _sample_object_state(obj: bpy.types.Object, frame: int, ground_z: float) -> dict[str, Any]:
    scene = bpy.context.scene
    scene.frame_set(frame)
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(dg)

    # Check instanced bodies first
    inst_positions: list[tuple[float, float, float]] = []
    for inst in dg.object_instances:
        if not inst.is_instance:
            continue
        parent = inst.parent
        if parent == eval_obj or (parent is not None and parent.original == obj):
            loc = inst.matrix_world.translation
            inst_positions.append((float(loc.x), float(loc.y), float(loc.z)))

    mesh = eval_obj.to_mesh()
    vert_coords: list[tuple[float, float, float]] = []
    velocities: list[float] = []
    face_count = 0
    if mesh is not None:
        face_count = len(mesh.polygons)
        mat = eval_obj.matrix_world
        for v in mesh.vertices:
            w = mat @ v.co
            vert_coords.append((float(w.x), float(w.y), float(w.z)))
        vel_attr = mesh.attributes.get("pnw_velocity")
        if vel_attr is not None and vel_attr.data_type == "FLOAT_VECTOR":
            for d in vel_attr.data:
                vx, vy, vz = d.vector
                velocities.append(math.sqrt(vx * vx + vy * vy + vz * vz))
        eval_obj.to_mesh_clear()

    # Prefer realized mesh vertices when geometry is a deformed mesh/curve/volume (or single wrapper instance),
    # and use instance transforms when outputting multiple rigid scatter instances (or pure empty-mesh instances).
    coords = inst_positions if (len(inst_positions) > 1 or (inst_positions and not vert_coords)) else vert_coords
    if coords:
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        zs = [c[2] for c in coords]
        bbox_min = [round(min(xs), 5), round(min(ys), 5), round(min(zs), 5)]
        bbox_max = [round(max(xs), 5), round(max(ys), 5), round(max(zs), 5)]
        centroid = [
            round(sum(xs) / len(xs), 5),
            round(sum(ys) / len(ys), 5),
            round(sum(zs) / len(zs), 5),
        ]
        ground_min = round(min(zs) - ground_z, 5)
        penetration_count = sum(1 for z in zs if z < ground_z - 1e-4)
    else:
        bbox_min = [0.0, 0.0, 0.0]
        bbox_max = [0.0, 0.0, 0.0]
        centroid = [0.0, 0.0, 0.0]
        ground_min = 0.0
        penetration_count = 0

    return {
        "frame": int(frame),
        "instance_count": len(inst_positions),
        "vertex_count": len(vert_coords),
        "face_count": face_count,
        "sample_points": coords[:500],
        "bbox_min": bbox_min,
        "bbox_max": bbox_max,
        "centroid": centroid,
        "ground_clearance_min": ground_min,
        "ground_penetration_count": penetration_count,
        "velocities": velocities,
    }


def inspect_workflow(
    obj: bpy.types.Object,
    mod: bpy.types.Modifier | None,
    frame_start: int = 1,
    frame_end: int = 120,
    ground_z: float = 0.0,
    sleep_velocity: float = 0.05,
    params_hash: str = "",
) -> dict[str, Any]:
    mid = (frame_start + frame_end) // 2
    pre_end = max(frame_start, frame_end - 5)
    sample_frames = sorted(set([frame_start, mid, pre_end, frame_end]))

    per_frame: dict[int, dict[str, Any]] = {}
    for f in sample_frames:
        per_frame[f] = _sample_object_state(obj, f, ground_z)

    # Late frame positional delta between pre_end and frame_end
    pts_a = per_frame[pre_end]["sample_points"]
    pts_b = per_frame[frame_end]["sample_points"]
    if pts_a and pts_b and len(pts_a) == len(pts_b):
        deltas = [
            math.sqrt((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2 + (b[2] - a[2]) ** 2)
            for a, b in zip(pts_a, pts_b)
        ]
        late_jitter = round(sum(deltas) / len(deltas), 6)
        max_late_delta = round(max(deltas), 6)
    else:
        late_jitter = 0.0
        max_late_delta = 0.0

    # End-frame speed stats if velocity attribute available, else approximate from late delta
    end_vels = per_frame[frame_end]["velocities"]
    if end_vels:
        mean_speed = round(sum(end_vels) / len(end_vels), 5)
        max_speed = round(max(end_vels), 5)
        resting_ratio = round(sum(1 for v in end_vels if v <= max(sleep_velocity, 1e-4)) / len(end_vels), 4)
    else:
        dt_span = max(1, frame_end - pre_end) / 24.0
        mean_speed = round(late_jitter / dt_span, 5)
        max_speed = round(max_late_delta / dt_span, 5)
        resting_ratio = 1.0 if mean_speed <= max(sleep_velocity, 0.05) else 0.0

    # Check start->end displacement to verify real motion occurred
    c_start = per_frame[frame_start]["centroid"]
    c_end = per_frame[frame_end]["centroid"]
    centroid_displacement = round(
        math.sqrt((c_end[0] - c_start[0]) ** 2 + (c_end[1] - c_start[1]) ** 2 + (c_end[2] - c_start[2]) ** 2),
        5,
    )

    # Cache & manual edit inspection
    cache_info: dict[str, Any] = {"is_baked": False, "hash_valid": False}
    manual_info: dict[str, Any] = {"wrapper_edited": False, "edited_subgroups": []}
    if mod is not None and mod.type == "NODES" and mod.node_group is not None:
        wrapper = mod.node_group
        core_node = wrapper.nodes.get("PNW_CoreNode") or next(
            (n for n in wrapper.nodes if n.bl_idname == "GeometryNodeGroup"), None
        )
        core_tree = core_node.node_tree if core_node else None
        graph_fp = pnw_subgroups.tree_fingerprint(core_tree) if core_tree else ""
        if params_hash:
            valid, reason, bdir = pnw_bake.is_cache_valid(obj, mod, params_hash, graph_fp)
        else:
            bdir = pnw_bake.resolve_bake_directory(obj, mod, mod.bake_directory or None)
            stats = pnw_bake.cache_stats(bdir)
            valid = stats["meta_files"] > 0 and stats["blob_files"] > 0
            reason = "no_hash_provided"
        stats = pnw_bake.cache_stats(bdir)
        cache_info = {
            "is_baked": stats["meta_files"] > 0 and stats["blob_files"] > 0,
            "hash_valid": valid,
            "reason": reason,
            "bake_directory": str(bdir),
            "meta_files": stats["meta_files"],
            "blob_files": stats["blob_files"],
            "total_bytes": stats["total_bytes"],
        }
        manual_info = {
            "wrapper_edited": pnw_subgroups.is_tree_manually_edited(wrapper),
            "edited_subgroups": [
                ng.name
                for ng in bpy.data.node_groups
                if ng.get(pnw_subgroups.SUBGROUP_ROLE_KEY) and pnw_subgroups.is_tree_manually_edited(ng)
            ],
        }

    frames_summary = {}
    for f, data in per_frame.items():
        frames_summary[str(f)] = {
            k: v for k, v in data.items() if k not in ("sample_points", "velocities")
        }

    return {
        "object_name": obj.name,
        "modifier_name": mod.name if mod else None,
        "frame_range": [frame_start, frame_end],
        "frames": frames_summary,
        "centroid_displacement": centroid_displacement,
        "late_frame_jitter": late_jitter,
        "max_late_delta": max_late_delta,
        "mean_speed": mean_speed,
        "max_speed": max_speed,
        "resting_ratio": resting_ratio,
        "cache_status": cache_info,
        "manual_edit_status": manual_info,
    }
