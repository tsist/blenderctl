# SPDX-License-Identifier: GPL-3.0-or-later
"""View directions, layout presets and exact NDC-verified framing (-Y front convention)."""

from __future__ import annotations

import math
from typing import Any

# Project convention (AGENTS.md G2): Front faces -Y, Right is +X, Up is +Z.
# Direction vectors point FROM origin TOWARD the camera eye.
DIRECTION_VECTORS: dict[str, tuple[float, float, float]] = {
    "FRONT": (0.0, -1.0, 0.0),
    "BACK": (0.0, 1.0, 0.0),
    "REAR": (0.0, 1.0, 0.0),
    "RIGHT": (1.0, 0.0, 0.0),
    "LEFT": (-1.0, 0.0, 0.0),
    "TOP": (0.0, 0.0, 1.0),
    "BOTTOM": (0.0, 0.0, -1.0),
    "FRONT_3Q": (0.85, -1.0, 0.65),
    "FRONT_3Q_L": (-0.85, -1.0, 0.65),
    "REAR_3Q": (-0.85, 1.0, 0.65),
    "REAR_3Q_R": (0.85, 1.0, 0.65),
    "TOP_3Q": (0.0, -0.55, 1.0),
}

VIEW_ALIASES: dict[str, str] = {
    "HERO": "FRONT_3Q",
    "ISO": "FRONT_3Q",
    "3Q": "FRONT_3Q",
    "PERSP": "FRONT_3Q",
    "REAR": "BACK",
}

VIEW_LABELS: dict[str, str] = {
    "FRONT": "FRONT (-Y)",
    "BACK": "BACK (+Y)",
    "RIGHT": "RIGHT (+X)",
    "LEFT": "LEFT (-X)",
    "TOP": "TOP (+Z)",
    "BOTTOM": "BOTTOM (-Z)",
    "FRONT_3Q": "FRONT 3/4",
    "FRONT_3Q_L": "FRONT 3/4 L",
    "REAR_3Q": "REAR 3/4",
    "REAR_3Q_R": "REAR 3/4 R",
    "TOP_3Q": "TOP 3/4",
}

# Default projection per tile for each built-in layout.
LAYOUT_PRESETS: dict[str, dict[str, Any]] = {
    "quad": {
        "grid": (2, 2),
        "views": [
            {"preset": "TOP", "projection": "ORTHO"},
            {"preset": "FRONT_3Q", "projection": "PERSP"},
            {"preset": "FRONT", "projection": "ORTHO"},
            {"preset": "RIGHT", "projection": "ORTHO"},
        ],
    },
    "orbit": {
        "grid": (2, 2),
        "views": [
            {"preset": "FRONT", "projection": "PERSP"},
            {"preset": "RIGHT", "projection": "PERSP"},
            {"preset": "BACK", "projection": "PERSP"},
            {"preset": "FRONT_3Q", "projection": "PERSP"},
        ],
    },
    "3x3": {
        "grid": (3, 3),
        "views": [
            {"preset": "TOP", "projection": "ORTHO"},
            {"preset": "FRONT", "projection": "ORTHO"},
            {"preset": "RIGHT", "projection": "ORTHO"},
            {"preset": "LEFT", "projection": "ORTHO"},
            {"preset": "BACK", "projection": "ORTHO"},
            {"preset": "BOTTOM", "projection": "ORTHO"},
            {"preset": "FRONT_3Q", "projection": "PERSP"},
            {"preset": "REAR_3Q", "projection": "PERSP"},
            {"preset": "TOP_3Q", "projection": "PERSP"},
        ],
    },
    "single": {
        "grid": (1, 1),
        "views": [
            {"preset": "FRONT_3Q", "projection": "PERSP"},
        ],
    },
}


def parse_view_token(token: str, default_projection: str | None = None) -> dict[str, Any]:
    """Parse one view specification such as 'front', 'front_3q:persp', or 'camera:MainCam'."""
    raw = token.strip()
    if not raw:
        raise ValueError("Empty view token")
    if raw.lower().startswith("camera:"):
        cam_name = raw.split(":", 1)[1].strip()
        if not cam_name:
            raise ValueError("camera:<name> requires a non-empty camera object name")
        return {"camera": cam_name, "projection": "CAMERA", "label": f"CAM: {cam_name}"}

    proj_override = None
    if ":" in raw:
        name_part, proj_part = raw.split(":", 1)
        proj_upper = proj_part.strip().upper()
        if proj_upper not in ("ORTHO", "PERSP"):
            raise ValueError(f"Unsupported projection modifier {proj_part!r} in view {token!r}")
        raw = name_part.strip()
        proj_override = proj_upper

    key = VIEW_ALIASES.get(raw.upper(), raw.upper())
    if key not in DIRECTION_VECTORS:
        raise ValueError(
            f"Unknown view preset {token!r}; expected one of "
            f"{sorted(DIRECTION_VECTORS)} or camera:<Name>"
        )
    if proj_override is not None:
        proj = proj_override
    elif default_projection is not None:
        proj = default_projection.upper()
    else:
        proj = "PERSP" if key.endswith("_3Q") or key.endswith("_3Q_L") or key.endswith("_3Q_R") else "ORTHO"
    return {
        "preset": key,
        "projection": proj,
        "label": VIEW_LABELS.get(key, key),
    }


def resolve_views(
    layout: str,
    custom_views: list[str] | None = None,
    projection_override: str | None = None,
) -> tuple[tuple[int, int], list[dict[str, Any]]]:
    """Resolve grid dimensions (rows, cols) and tile view specs."""
    layout_key = (layout or "quad").lower()
    if layout_key == "2x2":
        layout_key = "quad"
    if layout_key not in LAYOUT_PRESETS:
        raise ValueError(f"Unsupported layout {layout!r}; use quad, orbit, 3x3, or single")

    proj_norm = projection_override.upper() if projection_override else None
    if proj_norm is not None and proj_norm not in ("ORTHO", "PERSP"):
        raise ValueError(f"Unsupported projection {projection_override!r}; use ortho or persp")

    preset = LAYOUT_PRESETS[layout_key]
    rows, cols = preset["grid"]
    expected = rows * cols

    if custom_views:
        if len(custom_views) == 1 and layout_key == "single":
            parsed = [parse_view_token(custom_views[0], proj_norm)]
        elif len(custom_views) != expected:
            raise ValueError(
                f"Layout {layout_key!r} requires {expected} views, got {len(custom_views)}"
            )
        else:
            parsed = [parse_view_token(v, proj_norm) for v in custom_views]
    else:
        parsed = []
        for entry in preset["views"]:
            proj = proj_norm or entry["projection"]
            key = entry["preset"]
            parsed.append({
                "preset": key,
                "projection": proj,
                "label": VIEW_LABELS.get(key, key),
            })
    return (rows, cols), parsed


def rotation_for_direction(direction: tuple[float, float, float]):
    """Build a right-handed camera-to-world rotation matrix (+X right, +Y up, +Z toward eye)."""
    from mathutils import Matrix, Vector

    back = Vector(direction).normalized()
    if abs(back.z) > 0.999:
        # TOP (+Z eye) has screen-up = +Y and screen-right = +X (aligns with -Y front).
        # BOTTOM (-Z eye) has screen-up = -Y and screen-right = +X.
        up_hint = Vector((0.0, 1.0 if back.z > 0.0 else -1.0, 0.0))
    else:
        up_hint = Vector((0.0, 0.0, 1.0))
    right = up_hint.cross(back).normalized()
    up = back.cross(right).normalized()
    return Matrix((right, up, back)).transposed()


def collect_target_points(objects, depsgraph, max_points_per_obj: int = 8192):
    """Sample evaluated world-space surface vertices for tight, reliable framing."""
    from mathutils import Vector

    points: list[Vector] = []
    for obj in objects:
        ev = obj.evaluated_get(depsgraph)
        mesh = None
        added = 0
        try:
            mesh = ev.to_mesh(preserve_all_data_layers=False, depsgraph=depsgraph)
            if mesh is not None and len(mesh.vertices) > 0:
                verts = mesh.vertices
                step = max(1, len(verts) // max_points_per_obj)
                mat = ev.matrix_world
                for idx in range(0, len(verts), step):
                    co = mat @ verts[idx].co
                    if math.isfinite(co.x) and math.isfinite(co.y) and math.isfinite(co.z):
                        points.append(co)
                        added += 1
        except Exception:
            pass
        finally:
            if mesh is not None:
                try:
                    ev.to_mesh_clear()
                except Exception:
                    pass
        if added == 0:
            mat = ev.matrix_world
            for corner in ev.bound_box:
                co = mat @ Vector(corner)
                if math.isfinite(co.x) and math.isfinite(co.y) and math.isfinite(co.z):
                    points.append(co)

    if not points:
        raise ValueError("Target objects produced no valid finite geometry bounds")

    lo = Vector((
        min(p.x for p in points),
        min(p.y for p in points),
        min(p.z for p in points),
    ))
    hi = Vector((
        max(p.x for p in points),
        max(p.y for p in points),
        max(p.z for p in points),
    ))
    span = (hi - lo).length
    if not math.isfinite(span) or span <= 1e-7:
        # Degenerate single point or tiny geometry: pad slightly so view distance is sane
        pad = Vector((0.05, 0.05, 0.05))
        lo = lo - pad
        hi = hi + pad
        span = (hi - lo).length
    return points, lo, hi, span


def compute_shared_ortho_extent(points, center, views) -> float:
    """Compute one common orthographic half-extent across all ORTHO preset views."""
    max_half = 0.0
    for view in views:
        if view.get("projection") != "ORTHO":
            continue
        direction = view.get("direction") or DIRECTION_VECTORS[view["preset"]]
        rot = rotation_for_direction(direction)
        right = rot.col[0].to_3d()
        up = rot.col[1].to_3d()
        for pt in points:
            rel = pt - center
            max_half = max(max_half, abs(rel.dot(right)), abs(rel.dot(up)))
    return max(max_half, 1e-4)


def apply_viewport_view(
    space,
    region_3d,
    view_spec: dict[str, Any],
    points,
    center,
    span: float,
    margin: float = 0.10,
    lens: float = 50.0,
    shared_ortho_half: float | None = None,
    fixed_framing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Position SpaceView3D region_3d accurately using Blender's live perspective_matrix."""
    from mathutils import Vector

    if fixed_framing:
        center = Vector(fixed_framing["center"])
        if fixed_framing.get("span"):
            span = float(fixed_framing["span"])
        if fixed_framing.get("ortho_half_extent"):
            shared_ortho_half = float(fixed_framing["ortho_half_extent"])

    if "camera" in view_spec:
        import bpy

        cam = bpy.data.objects.get(view_spec["camera"])
        if cam is None or cam.type != "CAMERA":
            raise ValueError(f"Scene camera {view_spec['camera']!r} not found")
        space.use_local_camera = True
        space.camera = cam
        region_3d.view_perspective = "CAMERA"
        region_3d.view_camera_offset = (0.0, 0.0)
        region_3d.view_camera_zoom = 0.0
        return {
            "label": view_spec["label"],
            "camera": cam.name,
            "projection": "CAMERA",
            "lens": float(cam.data.lens),
        }

    preset = view_spec["preset"]
    proj = view_spec["projection"]
    rot = rotation_for_direction(view_spec.get("direction") or DIRECTION_VECTORS[preset])
    quat = rot.to_quaternion()

    space.use_local_camera = False
    space.lens = float(lens)
    space.clip_start = max(1e-4, span / 2000.0)
    space.clip_end = max(100.0, span * 40.0)

    region_3d.view_perspective = proj
    region_3d.view_location = center.copy()
    region_3d.view_rotation = quat

    target_ndc = max(0.2, min(0.95, 1.0 - 2.0 * margin))

    if proj == "ORTHO":
        # Initial guess then exact closed-form calibration using Blender's own projection matrix.
        half_extent = shared_ortho_half if shared_ortho_half is not None else max(span * 0.5, 1e-4)
        region_3d.view_distance = max(half_extent * 2.5, 1e-3)
        region_3d.update()

        if fixed_framing and fixed_framing.get("ortho_view_distance"):
            region_3d.view_distance = float(fixed_framing["ortho_view_distance"])
            region_3d.update()
        else:
            # Measure actual projected max(|ndc.x|, |ndc.y|) and scale linearly.
            for _ in range(3):
                pmat = region_3d.perspective_matrix
                max_xy = 0.0
                if shared_ortho_half is not None:
                    # Use a uniform virtual cube of half-extent so all ORTHO tiles share world scale.
                    right = rot.col[0].to_3d()
                    up = rot.col[1].to_3d()
                    test_pts = (
                        center + right * shared_ortho_half,
                        center - right * shared_ortho_half,
                        center + up * shared_ortho_half,
                        center - up * shared_ortho_half,
                    )
                else:
                    test_pts = points
                for pt in test_pts:
                    clip = pmat @ pt.to_4d()
                    if abs(clip.w) > 1e-9:
                        max_xy = max(max_xy, abs(clip.x / clip.w), abs(clip.y / clip.w))
                if max_xy > 1e-6:
                    ratio = max_xy / target_ndc
                    region_3d.view_distance = max(1e-4, region_3d.view_distance * ratio)
                    region_3d.update()
                    if abs(ratio - 1.0) < 1e-3:
                        break
    else:
        # Perspective mode: iteratively solve exact view_distance via Blender's live perspective_matrix.
        if fixed_framing and fixed_framing.get("persp_view_distance"):
            region_3d.view_distance = float(fixed_framing["persp_view_distance"])
            region_3d.update()
        else:
            back = rot.col[2].to_3d()
            front_depth = max((pt - center).dot(back) for pt in points)
            min_dist = max(front_depth * 1.15 + span * 0.15, span * 0.25, 1e-3)
            dist = max(min_dist, span * 1.8)
            region_3d.view_distance = dist
            region_3d.update()

            for _ in range(8):
                pmat = region_3d.perspective_matrix
                max_xy = 0.0
                behind_camera = False
                for pt in points:
                    clip = pmat @ pt.to_4d()
                    if clip.w <= 1e-5:
                        behind_camera = True
                        break
                    max_xy = max(max_xy, abs(clip.x / clip.w), abs(clip.y / clip.w))
                if behind_camera:
                    dist *= 1.8
                elif max_xy > 1e-6:
                    ratio = max_xy / target_ndc
                    if abs(ratio - 1.0) < 0.008:
                        break
                    # Damped multiplicative update accounts for perspective foreshortening.
                    step = 1.0 + (ratio - 1.0) * 0.85
                    dist = max(min_dist, dist * max(0.35, min(3.0, step)))
                region_3d.view_distance = dist
                region_3d.update()

    # Keep clip planes tight and safe around the final eye distance.
    final_dist = float(region_3d.view_distance)
    space.clip_start = max(1e-4, min(span / 1000.0, final_dist * 0.01))
    space.clip_end = max(100.0, final_dist + span * 20.0)
    region_3d.update()

    return {
        "label": view_spec["label"],
        "preset": preset,
        "projection": proj,
        "view_distance": round(final_dist, 6),
        "lens": float(space.lens),
    }
