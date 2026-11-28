"""Pure read-only mesh, spatial, UV, material, and skinning QA core.

Designed for dual execution inside:
1. CLI worker (`blenderctl model inspect --qa-profile ...`)
2. Live Blender Python sessions (`Blender Lab MCP`) via direct import without CLI dependencies.
"""

from collections import deque
import math
from pathlib import Path

import bmesh
import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from mathutils.kdtree import KDTree
import numpy as np
import qa_udim


QA_REPORT_VERSION = "1.0"
QA_FIX_REPORT_VERSION = "1.0"
DEFAULT_MAX_TRIS = 300000
DEFAULT_WITNESS_LIMIT = 25
UV_AREA_THRESHOLD = 1e-10
DEFAULT_SAFE_FIX_ACTIONS = ("fix_data_texture_srgb", "remove_unused_empty_material_slots")
SAFE_FIX_ACTIONS = (*DEFAULT_SAFE_FIX_ACTIONS, "hide_boolean_cutters_in_render", "purge_local_orphans",
                    "align_subdivision_levels", "relativize_texture_paths")

# Data sockets where sRGB transfer distortion corrupts PBR shading math.
DATA_PRINCIPLED_INPUTS = frozenset(
    {
        "Metallic",
        "Roughness",
        "IOR",
        "Alpha",
        "Specular",
        "Specular IOR Level",
        "Specular Tint",
        "Anisotropic",
        "Anisotropic Rotation",
        "Transmission",
        "Transmission Weight",
        "Coat Weight",
        "Coat Roughness",
        "Coat IOR",
        "Sheen Weight",
        "Sheen Roughness",
        "Subsurface Weight",
        "Subsurface Radius",
        "Subsurface Scale",
    }
)
COLOR_PRINCIPLED_INPUTS = frozenset({"Base Color", "Emission Color", "Emission", "Coat Tint"})
DATA_NODE_TYPES = frozenset({"NORMAL_MAP", "BUMP", "DISPLACEMENT", "VECTOR_DISPLACEMENT"})
PASSTHROUGH_NODE_TYPES = frozenset(
    {
        "REROUTE",
        "MATH",
        "VECT_MATH",
        "MAP_RANGE",
        "VALTORGB",
        "SEPARATE_COLOR",
        "SEPRGB",
        "SEPXYZ",
        "COMBINE_COLOR",
        "COMBRGB",
        "COMBXYZ",
        "INVERT",
        "GAMMA",
        "BRIGHTCONTRAST",
        "MIX",
        "MIX_RGB",
        "CLAMP",
        "RGBTOBW",
    }
)

# Deterministic errors block in every profile; context-sensitive rules vary by profile table.
ALWAYS_BLOCKERS = frozenset(
    {
        "negative_scale",
        "non_finite",
        "data_texture_srgb",
        "missing_texture_file",
        "empty_material_slot_in_use",
    }
)

PROFILE_CONFIG = {
    "game": {
        "description": "Primary game-delivery profile (strict material/scale/file errors; soft review signals for overlap/ngons/islands).",
        "hard_blockers": set(ALWAYS_BLOCKERS),
        "disabled_checks": set(),
        "uv_overlap_warn_pct": 5.0,
        "ground_z_tol": 0.01,
    },
    "render": {
        "description": "Primary offline render / AI-import cleanup profile (relaxes ngons, UDIM bounds, and resting Z).",
        "hard_blockers": set(ALWAYS_BLOCKERS),
        "disabled_checks": {"ngons", "ground_z_offset", "exceed_bone_influences", "unweighted_verts", "unnormalized_weights", "root_bone_weights"},
        "uv_overlap_warn_pct": 25.0,
        "uv_oob_info_only": True,
        "ground_z_tol": 0.05,
    },
    "mobile_strict": {
        "description": "Strict mobile game slot (promotes UV missing/OOB, skin limits, unapplied scale, and z-fighting to blockers).",
        "hard_blockers": set(ALWAYS_BLOCKERS)
        | {
            "uv_missing",
            "uv_oob",
            "exceed_bone_influences",
            "unweighted_verts",
            "unapplied_scale",
            "zfighting",
        },
        "disabled_checks": set(),
        "uv_overlap_warn_pct": 5.0,
        "ground_z_tol": 0.01,
    },
    "print": {
        "description": "3D print / watertight slot (promotes manifold, boundary, wire, winding, and floating islands to blockers).",
        "hard_blockers": set(ALWAYS_BLOCKERS)
        | {
            "nonmanifold_edges",
            "boundary_edges",
            "wire_edges",
            "inside_out_islands",
            "inconsistent_normals",
            "floating_island",
        },
        "disabled_checks": {
            "uv_missing",
            "uv_zero_area",
            "uv_oob",
            "uv_high_overlap",
            "exceed_bone_influences",
            "unweighted_verts",
            "unnormalized_weights",
            "root_bone_weights",
            "base_color_non_color",
            "data_texture_srgb",
            "missing_texture_file",
            "empty_material_slot_in_use",
            "unused_material_slot",
            "no_materials",
        },
        "uv_overlap_warn_pct": 100.0,
        "ground_z_tol": 0.01,
    },
}


def _is_non_color_colorspace(name):
    low = (name or "").strip().lower()
    return (
        low in ("non-color", "raw", "linear", "linear rec.709", "linear rec.2020", "utility - raw", "acescg")
        or "non-color" in low
        or "raw" in low
    )


def _signed_volume_64(tris_coords):
    """Compute signed tetrahedral volume in Python float64 shifted to local island origin."""
    if not tris_coords:
        return 0.0
    ox, oy, oz = tris_coords[0][0]
    total = 0.0
    for a, b, c in tris_coords:
        ax, ay, az = a[0] - ox, a[1] - oy, a[2] - oz
        bx, by, bz = b[0] - ox, b[1] - oy, b[2] - oz
        cx, cy, cz = c[0] - ox, c[1] - oy, c[2] - oz
        # det([a-o, b-o, c-o]) / 6
        total += (
            ax * (by * cz - bz * cy)
            - ay * (bx * cz - bz * cx)
            + az * (bx * cy - by * cx)
        )
    return total / 6.0


def _world_pt_64(mat, co):
    """Transform 3D point by 4x4 matrix in Python float64 to avoid float32 translation loss before shifting."""
    x, y, z = float(co.x), float(co.y), float(co.z)
    m = mat
    return (
        float(m[0][0]) * x + float(m[0][1]) * y + float(m[0][2]) * z + float(m[0][3]),
        float(m[1][0]) * x + float(m[1][1]) * y + float(m[1][2]) * z + float(m[1][3]),
        float(m[2][0]) * x + float(m[2][1]) * y + float(m[2][2]) * z + float(m[2][3]),
    )


def _clip_polygon_halfplane_2d(poly, ax, ay, bx, by):
    """Clip 2D convex polygon against half-plane to the left of directed edge A->B."""
    if not poly:
        return []
    out = []
    ex, ey = bx - ax, by - ay
    prev = poly[-1]
    prev_inside = ex * (prev[1] - ay) - ey * (prev[0] - ax) >= -1e-12
    for curr in poly:
        curr_inside = ex * (curr[1] - ay) - ey * (curr[0] - ax) >= -1e-12
        if curr_inside != prev_inside:
            dx, dy = curr[0] - prev[0], curr[1] - prev[1]
            denom = ex * dy - ey * dx
            if abs(denom) > 1e-18:
                t = (ex * (prev[1] - ay) - ey * (prev[0] - ax)) / -denom
                t = max(0.0, min(1.0, t))
                out.append((prev[0] + t * dx, prev[1] + t * dy))
        if curr_inside:
            out.append(curr)
        prev, prev_inside = curr, curr_inside
    return out


def _tri_overlap_area_2d(t1_world, t2_world, normal):
    """Project two near-parallel triangles onto normal's plane and return their 2D intersection area."""
    u = normal.orthogonal().normalized()
    v = normal.cross(u).normalized()
    origin = t1_world[0]

    def proj(p):
        d = p - origin
        return (float(d.dot(u)), float(d.dot(v)))

    p1 = [proj(p) for p in t1_world]
    p2 = [proj(p) for p in t2_world]
    # Ensure p2 is CCW in (u, v) for left-of-edge clipping.
    area2_twice = (p2[1][0] - p2[0][0]) * (p2[2][1] - p2[0][1]) - (p2[1][1] - p2[0][1]) * (p2[2][0] - p2[0][0])
    if area2_twice < 0:
        p2 = [p2[0], p2[2], p2[1]]
    clipped = p1
    for k in range(3):
        clipped = _clip_polygon_halfplane_2d(clipped, p2[k][0], p2[k][1], p2[(k + 1) % 3][0], p2[(k + 1) % 3][1])
        if len(clipped) < 3:
            return 0.0
    area = 0.0
    for k in range(len(clipped)):
        x1, y1 = clipped[k]
        x2, y2 = clipped[(k + 1) % len(clipped)]
        area += x1 * y2 - x2 * y1
    return abs(area) * 0.5


def _resolve_target_objects(objects, scene, view_layer, include_hidden):
    """Resolve MESH target objects without mutating scene selection or mode."""
    active_scene = scene or bpy.context.scene
    active_layer = view_layer or bpy.context.view_layer
    if objects is None:
        pool = active_scene.objects if active_scene else bpy.data.objects
        result = []
        for obj in sorted(pool, key=lambda o: o.name):
            if obj.type != "MESH":
                continue
            if not include_hidden and (obj.hide_viewport or (hasattr(obj, "hide_get") and obj.hide_get())):
                continue
            if not include_hidden and active_layer and obj.name in active_layer.objects and hasattr(obj, "visible_get"):
                try:
                    if not obj.visible_get(view_layer=active_layer):
                        continue
                except RuntimeError:
                    pass
            result.append(obj)
        return result

    resolved = []
    for item in objects:
        if isinstance(item, bpy.types.Object):
            obj = item
        else:
            obj = bpy.data.objects.get(str(item))
        if obj is None:
            raise ValueError(f"QA target object not found: {item}")
        if obj.type != "MESH":
            raise ValueError(f"QA target object is not MESH: {obj.name} ({obj.type})")
        resolved.append(obj)
    return resolved


class UVLayerNotFound(ValueError):
    """An explicitly requested UV layer does not exist on the inspected geometry."""


def _acquire_object_mesh_data(obj, depsgraph, evaluated, preserve_layers=False):
    """Obtain temporary evaluated Mesh or Edit-Mode BMesh copy without altering scene state.

    Returns `(bm, tri_mesh_data, edit_unflushed, cleanup_fn)`.
    """
    if obj.mode == "EDIT":
        edit_bm = bmesh.from_edit_mesh(obj.data)
        bm = edit_bm.copy()
        return bm, None, True, bm.free

    if evaluated:
        ev = obj.evaluated_get(depsgraph)
        me = ev.to_mesh(preserve_all_data_layers=preserve_layers, depsgraph=depsgraph)
        bm = bmesh.new()
        bm.from_mesh(me)

        def _cleanup():
            bm.free()
            ev.to_mesh_clear()

        return bm, me, False, _cleanup

    me = obj.data
    bm = bmesh.new()
    bm.from_mesh(me)
    return bm, me, False, bm.free


def _uv_triangle_location(index, polygon, area):
    finite = math.isfinite(area)
    return {
        "triangle_index": index, "polygon_index": polygon,
        "uv_area": area if finite else None,
        "reason": ("non_finite" if not finite else "zero_area" if area == 0 else "below_threshold"),
    }


def _rasterize_uv_stats(me, uv_layer, max_tris_sample=30000, *, locations, location_limit, measure_overlap=True):
    """Compute UV missing/OOB/zero-area metrics and fixed 128x128 raster overlap estimate."""
    loops = me.loops
    uv_data = uv_layer.data
    n_loops = len(uv_data)
    if n_loops == 0 or len(me.polygons) == 0:
        return {"uv_oob_pct": 0.0, "uv_zero_area_count": 0, "uv_overlap_pct": 0.0, "sampled": False}

    uv_flat = np.empty(n_loops * 2, dtype=np.float32)
    uv_data.foreach_get("uv", uv_flat)
    uvs = uv_flat.reshape((-1, 2))

    finite_mask = np.isfinite(uvs).all(axis=1)
    oob_mask = (~finite_mask) | (uvs[:, 0] < -1e-4) | (uvs[:, 0] > 1.0001) | (uvs[:, 1] < -1e-4) | (uvs[:, 1] > 1.0001)
    uv_oob_pct = float(100.0 * oob_mask.sum() / max(1, n_loops))

    me.calc_loop_triangles()
    loop_tris = me.loop_triangles
    n_tris = len(loop_tris)
    if n_tris == 0:
        return {"uv_oob_pct": uv_oob_pct, "uv_zero_area_count": 0, "uv_overlap_pct": 0.0, "sampled": False}

    tri_loops = np.empty(n_tris * 3, dtype=np.int32)
    loop_tris.foreach_get("loops", tri_loops)
    tri_loops = tri_loops.reshape((-1, 3))
    pts = uvs[tri_loops]  # (n_tris, 3, 2)

    # Match BMesh arithmetic on the same stored float32 UVs, including near-collinear triangles.
    ab = np.subtract(pts[:, 1], pts[:, 0], dtype=np.float64)
    ac = np.subtract(pts[:, 2], pts[:, 0], dtype=np.float64)
    areas = 0.5 * np.abs(ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0])
    low_area = ~np.isfinite(areas) | (areas < UV_AREA_THRESHOLD)
    uv_zero_area_count = int(np.count_nonzero(low_area))
    if uv_zero_area_count and location_limit:
        for idx in np.flatnonzero(low_area)[:location_limit]:
            tri = loop_tris[int(idx)]
            locations.append({
                **_uv_triangle_location(int(idx), tri.polygon_index, float(areas[idx])),
                "loop_indices": list(tri.loops), "vertex_indices": list(tri.vertices),
            })

    if not measure_overlap:
        return {"uv_oob_pct": 0.0, "uv_zero_area_count": uv_zero_area_count, "uv_overlap_pct": 0.0, "sampled": False}

    # Fixed 128x128 integer owner buffer keeps memory under 100 KB even on huge meshes.
    grid = 128
    owner = np.full((grid, grid), -1, dtype=np.int32)
    overlap = np.zeros((grid, grid), dtype=bool)
    step = max(1, n_tris // max_tris_sample) if n_tris > max_tris_sample else 1

    poly_indices = np.empty(n_tris, dtype=np.int32)
    loop_tris.foreach_get("polygon_index", poly_indices)

    for idx in range(0, n_tris, step):
        tri_uv = pts[idx]
        if not np.isfinite(tri_uv).all():
            continue
        if (tri_uv[:, 0] < -1e-4).any() or (tri_uv[:, 0] > 1.0001).any():
            continue
        if (tri_uv[:, 1] < -1e-4).any() or (tri_uv[:, 1] > 1.0001).any():
            continue
        u0, v0 = float(tri_uv[0][0]), float(tri_uv[0][1])
        u1, v1 = float(tri_uv[1][0]), float(tri_uv[1][1])
        u2, v2 = float(tri_uv[2][0]), float(tri_uv[2][1])
        denom = (v1 - v2) * (u0 - u2) + (u2 - u1) * (v0 - v2)
        if abs(denom) < 1e-10:
            continue
        min_x = max(0, int(min(u0, u1, u2) * (grid - 1)))
        max_x = min(grid - 1, int(max(u0, u1, u2) * (grid - 1)))
        min_y = max(0, int(min(v0, v1, v2) * (grid - 1)))
        max_y = min(grid - 1, int(max(v0, v1, v2) * (grid - 1)))
        pid = int(poly_indices[idx])
        for gx in range(min_x, max_x + 1):
            px = (gx + 0.5) / grid
            for gy in range(min_y, max_y + 1):
                py = (gy + 0.5) / grid
                l0 = ((v1 - v2) * (px - u2) + (u2 - u1) * (py - v2)) / denom
                l1 = ((v2 - v0) * (px - u2) + (u0 - u2) * (py - v2)) / denom
                l2 = 1.0 - l0 - l1
                if l0 >= -1e-3 and l1 >= -1e-3 and l2 >= -1e-3:
                    prev_pid = owner[gy, gx]
                    if prev_pid == -1:
                        owner[gy, gx] = pid
                    elif prev_pid != pid:
                        overlap[gy, gx] = True

    covered_count = int((owner != -1).sum())
    uv_overlap_pct = float(100.0 * overlap.sum() / max(1, covered_count))
    return {
        "uv_oob_pct": round(uv_oob_pct, 3),
        "uv_zero_area_count": uv_zero_area_count,
        "uv_overlap_pct": round(uv_overlap_pct, 3),
        "sampled": step > 1,
    }


def _rasterize_uv_stats_from_bmesh(bm, layer, max_tris_sample=30000, *, locations, location_limit):
    """Fallback UV analysis directly from BMesh when inspecting unflushed Edit-Mode geometry."""
    # These indices belong to the private edit snapshot, including newly created elements.
    bm.faces.index_update()
    bm.verts.index_update()
    bm_tris = bm.calc_loop_triangles()
    if not bm_tris:
        return {"uv_oob_pct": 0.0, "uv_zero_area_count": 0, "uv_overlap_pct": 0.0, "sampled": False}
    oob = 0
    total_loops = 0
    zero_area = 0
    grid = 128
    owner = np.full((grid, grid), -1, dtype=np.int32)
    overlap = np.zeros((grid, grid), dtype=bool)
    step = max(1, len(bm_tris) // max_tris_sample) if len(bm_tris) > max_tris_sample else 1

    for idx, tri in enumerate(bm_tris):
        uvs_tri = []
        tri_oob = False
        for loop in tri:
            total_loops += 1
            uv = loop[layer].uv
            u, v = float(uv.x), float(uv.y)
            if not (math.isfinite(u) and math.isfinite(v)) or u < -1e-4 or u > 1.0001 or v < -1e-4 or v > 1.0001:
                oob += 1
                tri_oob = True
            uvs_tri.append((u, v))
        (u0, v0), (u1, v1), (u2, v2) = uvs_tri
        area = 0.5 * abs((u1 - u0) * (v2 - v0) - (u2 - u0) * (v1 - v0))
        if not math.isfinite(area) or area < UV_AREA_THRESHOLD:
            zero_area += 1
            if len(locations) < location_limit:
                face = tri[0].face
                corners = {loop: corner for corner, loop in enumerate(face.loops)}
                locations.append({
                    **_uv_triangle_location(idx, face.index, area),
                    "face_corner_indices": [corners[loop] for loop in tri],
                    "vertex_indices": [loop.vert.index for loop in tri],
                })
        if idx % step != 0 or tri_oob or area < UV_AREA_THRESHOLD:
            continue
        denom = (v1 - v2) * (u0 - u2) + (u2 - u1) * (v0 - v2)
        if abs(denom) < 1e-10:
            continue
        pid = tri[0].face.index
        min_x = max(0, int(min(u0, u1, u2) * (grid - 1)))
        max_x = min(grid - 1, int(max(u0, u1, u2) * (grid - 1)))
        min_y = max(0, int(min(v0, v1, v2) * (grid - 1)))
        max_y = min(grid - 1, int(max(v0, v1, v2) * (grid - 1)))
        for gx in range(min_x, max_x + 1):
            px = (gx + 0.5) / grid
            for gy in range(min_y, max_y + 1):
                py = (gy + 0.5) / grid
                l0 = ((v1 - v2) * (px - u2) + (u2 - u1) * (py - v2)) / denom
                l1 = ((v2 - v0) * (px - u2) + (u0 - u2) * (py - v2)) / denom
                l2 = 1.0 - l0 - l1
                if l0 >= -1e-3 and l1 >= -1e-3 and l2 >= -1e-3:
                    prev_pid = owner[gy, gx]
                    if prev_pid == -1:
                        owner[gy, gx] = pid
                    elif prev_pid != pid:
                        overlap[gy, gx] = True

    covered_count = int((owner != -1).sum())
    return {
        "uv_oob_pct": round(100.0 * oob / max(1, total_loops), 3),
        "uv_zero_area_count": zero_area,
        "uv_overlap_pct": round(100.0 * overlap.sum() / max(1, covered_count), 3),
        "sampled": step > 1,
    }


def _classify_image_roles(node_tree, tex_node):
    """Traverse downstream from TEX_IMAGE Color socket to detect data vs color targets.

    Links originating from the Alpha socket are ignored for Color-space enforcement so standard
    sRGB RGBA textures driving both Base Color and Alpha are never false-flagged.
    """
    roles = set()
    visited = set()
    queue = deque()
    for out_sock in tex_node.outputs:
        if out_sock.name == "Alpha":
            continue
        for link in out_sock.links:
            if link.is_valid:
                queue.append((link.to_node, link.to_socket))

    while queue:
        node, sock = queue.popleft()
        key = (node.as_pointer(), sock.name)
        if key in visited:
            continue
        visited.add(key)

        if node.type in DATA_NODE_TYPES:
            roles.add("data")
        elif node.type == "BSDF_PRINCIPLED":
            if sock.name in DATA_PRINCIPLED_INPUTS:
                roles.add("data")
            elif sock.name in COLOR_PRINCIPLED_INPUTS:
                roles.add("color")
        elif node.type in ("EMISSION", "BSDF_DIFFUSE", "BSDF_GLOSSY", "BSDF_REFRACTION", "SUBSURFACE_SCATTERING") and sock.name == "Color":
            roles.add("color")

        if node.type in PASSTHROUGH_NODE_TYPES:
            for out_sock in node.outputs:
                for link in out_sock.links:
                    if link.is_valid:
                        queue.append((link.to_node, link.to_socket))
    return roles


def _inspect_materials(objects, add_finding):
    """Inspect material slots, shader node colorspace wiring, and external texture existence."""
    checked_images = set()
    for obj in objects:
        me = obj.data
        slots = obj.material_slots
        polys = me.polygons
        referenced_indices = set()
        invalid_polys = []

        if len(polys) > 0:
            mat_indices = np.empty(len(polys), dtype=np.int32)
            polys.foreach_get("material_index", mat_indices)
            referenced_indices = set(int(x) for x in np.unique(mat_indices))
            for idx in sorted(referenced_indices):
                if idx < 0 or idx >= len(slots) or slots[idx].material is None:
                    invalid_polys.append(idx)

            if invalid_polys and len(slots) > 0:
                add_finding(
                    "empty_material_slot_in_use",
                    {
                        "object": obj.name,
                        "slot_indices": invalid_polys,
                        "detail": "Polygon(s) reference missing or empty material slot",
                    },
                )
            elif invalid_polys and len(slots) == 0:
                # Object has zero slots yet nonzero polys: treat empty material assignment as no_materials signal unless invalid index > 0.
                if any(i != 0 for i in invalid_polys):
                    add_finding(
                        "empty_material_slot_in_use",
                        {
                            "object": obj.name,
                            "slot_indices": invalid_polys,
                            "detail": "Polygon(s) reference out-of-range material slot index",
                        },
                    )
                else:
                    add_finding(
                        "no_materials",
                        {"object": obj.name, "faces": len(polys)},
                    )

        for idx, slot in enumerate(slots):
            if len(polys) > 0 and idx not in referenced_indices:
                add_finding(
                    "unused_material_slot",
                    {
                        "object": obj.name,
                        "slot_index": idx,
                        "material": slot.material.name if slot.material else None,
                    },
                )
            mat = slot.material
            if mat is None or not mat.use_nodes or mat.node_tree is None:
                continue
            for node in mat.node_tree.nodes:
                if node.type != "TEX_IMAGE" or node.image is None:
                    continue
                img = node.image
                roles = _classify_image_roles(mat.node_tree, node)
                cs_name = img.colorspace_settings.name if hasattr(img, "colorspace_settings") else ""
                non_color = _is_non_color_colorspace(cs_name)
                if "data" in roles and "color" not in roles and not non_color:
                    add_finding(
                        "data_texture_srgb",
                        {
                            "object": obj.name,
                            "material": mat.name,
                            "image": img.name,
                            "node": node.name,
                            "colorspace": cs_name,
                        },
                    )
                elif "color" in roles and "data" not in roles and non_color:
                    add_finding(
                        "base_color_non_color",
                        {
                            "object": obj.name,
                            "material": mat.name,
                            "image": img.name,
                            "node": node.name,
                            "colorspace": cs_name,
                        },
                    )

                img_key = (mat.name, node.name, img.name)
                if img_key not in checked_images:
                    checked_images.add(img_key)
                    if img.source == "TILED":
                        packed_paths = {
                            Path(entry.filepath).name for entry in img.packed_files
                        }
                        for tile, path in qa_udim.tiled_files(img):
                            if path.name not in packed_paths and not path.is_file():
                                add_finding("missing_texture_file", {
                                    "object": obj.name, "material": mat.name, "image": img.name,
                                    "tile": tile, "filepath": img.filepath, "resolved_path": str(path),
                                })
                    if not img.packed_file and img.source in ("FILE", "SEQUENCE", "MOVIE") and img.filepath:
                        abs_path = Path(bpy.path.abspath(img.filepath, library=img.library))
                        if not abs_path.is_file():
                            add_finding(
                                "missing_texture_file",
                                {
                                    "object": obj.name,
                                    "material": mat.name,
                                    "image": img.name,
                                    "filepath": img.filepath,
                                    "resolved_path": str(abs_path),
                                },
                            )


def _inspect_weights(objects, influence_limit, add_finding):
    """Inspect Armature skin weights on base meshes."""
    for obj in objects:
        arm_mods = [m for m in obj.modifiers if m.type == "ARMATURE" and m.object is not None and m.object.type == "ARMATURE"]
        if not arm_mods:
            continue
        arm_obj = arm_mods[0].object
        deform_bones = {b.name for b in arm_obj.data.bones if b.use_deform}
        root_bones = {b.name for b in arm_obj.data.bones if b.parent is None and "root" in b.name.lower()}

        group_idx_to_name = {vg.index: vg.name for vg in obj.vertex_groups}
        deform_group_indices = {idx for idx, name in group_idx_to_name.items() if name in deform_bones}
        root_group_indices = {idx for idx, name in group_idx_to_name.items() if name in root_bones}

        unweighted = 0
        over_limit = 0
        max_influences = 0
        unnormalized = 0
        root_weighted = 0

        for vert in obj.data.vertices:
            deform_w = []
            for g in vert.groups:
                if g.group in root_group_indices and g.weight > 1e-4:
                    root_weighted += 1
                if g.group in deform_group_indices and g.weight > 1e-4:
                    deform_w.append(g.weight)
            count = len(deform_w)
            if count == 0:
                unweighted += 1
                continue
            if count > max_influences:
                max_influences = count
            if count > influence_limit:
                over_limit += 1
            if abs(sum(deform_w) - 1.0) > 1e-3:
                unnormalized += 1

        if unweighted:
            add_finding("unweighted_verts", {"object": obj.name, "armature": arm_obj.name, "vertex_count": unweighted}, count=unweighted)
        if over_limit:
            add_finding(
                "exceed_bone_influences",
                {
                    "object": obj.name,
                    "armature": arm_obj.name,
                    "vertex_count": over_limit,
                    "max_influences_found": max_influences,
                    "limit": influence_limit,
                },
                count=over_limit,
            )
        if unnormalized:
            add_finding("unnormalized_weights", {"object": obj.name, "armature": arm_obj.name, "vertex_count": unnormalized}, count=unnormalized)
        if root_weighted:
            add_finding("root_bone_weights", {"object": obj.name, "armature": arm_obj.name, "vertex_count": root_weighted}, count=root_weighted)


def _extract_mesh_islands(bm):
    """Partition BMesh faces into edge-connected surface islands."""
    bm.faces.ensure_lookup_table()
    visited = set()
    islands = []
    for face in bm.faces:
        if face.index in visited:
            continue
        comp = []
        queue = deque([face])
        visited.add(face.index)
        while queue:
            cur = queue.popleft()
            comp.append(cur)
            for edge in cur.edges:
                for linked in edge.link_faces:
                    if linked.index not in visited:
                        visited.add(linked.index)
                        queue.append(linked)
        islands.append(comp)
    return islands


def _bmesh_triangulated_copy_tris(bm_island_faces, world_mat, island_meta):
    """Fan-triangulate faces in an island for fast world BVH and 64-bit origin-shifted signed volume."""
    tris_64 = []
    world_verts = []
    world_tris = []
    tri_meta = []
    vert_map = {}

    for face in bm_island_faces:
        if len(face.verts) < 3:
            continue
        f_vids = set()
        local_idx = []
        for v in face.verts:
            f_vids.add(v.index)
            vi = vert_map.get(v.index)
            if vi is None:
                vi = len(world_verts)
                vert_map[v.index] = vi
                wco = world_mat @ v.co
                world_verts.append(wco)
            local_idx.append(vi)
        wpts_64 = [_world_pt_64(world_mat, v.co) for v in face.verts]
        v0 = local_idx[0]
        p0_64 = wpts_64[0]
        for k in range(1, len(local_idx) - 1):
            v1, v2 = local_idx[k], local_idx[k + 1]
            tris_64.append((p0_64, wpts_64[k], wpts_64[k + 1]))
            tri_idx = len(world_tris)
            world_tris.append((v0, v1, v2))
            tri_meta.append((island_meta, face.index, f_vids, tri_idx))
    return world_verts, world_tris, tris_64, tri_meta


def _parity_ray_enclosed(point, other_bvhs):
    """Cast 3 skew rays; majority odd hit-count inside another BVH indicates an enclosed cavity part."""
    directions = (
        Vector((1.0, 0.131, 0.293)).normalized(),
        Vector((-0.317, 1.0, 0.179)).normalized(),
        Vector((0.233, -0.411, 1.0)).normalized(),
    )
    for host_name, host_bvh in other_bvhs:
        odd_votes = 0
        for direction in directions:
            origin = point.copy()
            hits = 0
            for _ in range(32):
                loc, _norm, _idx, _dist = host_bvh.ray_cast(origin, direction)
                if loc is None:
                    break
                hits += 1
                origin = loc + direction * 1e-4
            if hits % 2 == 1:
                odd_votes += 1
        if odd_votes >= 2:
            return host_name
    return None


def _run_spatial_bvh_checks(island_records, eps, tol, add_finding):
    """Run world-space BVH z-fighting, back-to-back, and floating vs enclosed island checks."""
    all_verts = []
    all_tris = []
    all_meta = []  # (obj_name, island_key, face_idx, orig_vert_ids, area, normal, centroid)
    island_bvhs = []

    for rec in island_records:
        offset = len(all_verts)
        all_verts.extend(rec["world_verts"])
        local_tris = rec["world_tris"]
        for local_i, (a, b, c) in enumerate(local_tris):
            va, vb, vc = all_verts[offset + a], all_verts[offset + b], all_verts[offset + c]
            cross = (vb - va).cross(vc - va)
            area2 = cross.length
            if area2 <= 1e-10:
                continue
            normal = cross / area2
            centroid = (va + vb + vc) / 3.0
            meta_entry = rec["tri_meta"][local_i]
            all_tris.append((offset + a, offset + b, offset + c))
            all_meta.append((rec["obj_name"], rec["island_key"], meta_entry[1], meta_entry[2], area2 * 0.5, normal, centroid, va, vb, vc))
        if rec["world_verts"] and rec["world_tris"]:
            island_bvhs.append((rec, BVHTree.FromPolygons(rec["world_verts"], rec["world_tris"])))

    if all_tris:
        scene_bvh = BVHTree.FromPolygons(all_verts, all_tris, epsilon=eps)
        candidate_pairs = set()
        for idx_a, idx_b in scene_bvh.overlap(scene_bvh):
            if idx_a != idx_b:
                candidate_pairs.add((idx_a, idx_b) if idx_a < idx_b else (idx_b, idx_a))

        # BVHTree.overlap() ignores epsilon for non-intersecting parallel triangles within eps;
        # find_nearest_range() on interior samples catches near-coplanar z-fighting and back-to-back slabs.
        probe_radius = eps * 1.05
        for idx_a, a_meta in enumerate(all_meta):
            obj_a, _isl_a, face_a, vids_a, _area_a, _norm_a, cen_a, va0, va1, va2 = a_meta
            samples = (
                cen_a,
                va0 * 0.7 + cen_a * 0.3,
                va1 * 0.7 + cen_a * 0.3,
                va2 * 0.7 + cen_a * 0.3,
            )
            for pt in samples:
                for _loc, _n, idx_b, _dist in scene_bvh.find_nearest_range(pt, probe_radius):
                    if idx_b == idx_a:
                        continue
                    pair = (idx_a, idx_b) if idx_a < idx_b else (idx_b, idx_a)
                    if pair not in candidate_pairs:
                        candidate_pairs.add(pair)

        for pair in sorted(candidate_pairs):
            a_meta = all_meta[pair[0]]
            b_meta = all_meta[pair[1]]
            obj_a, isl_a, face_a, vids_a, area_a, norm_a, cen_a, va0, va1, va2 = a_meta
            obj_b, isl_b, face_b, vids_b, area_b, norm_b, cen_b, vb0, vb1, vb2 = b_meta

            if obj_a == obj_b and (face_a == face_b or (vids_a & vids_b)):
                continue

            dot = float(norm_a.dot(norm_b))
            if abs(dot) < 0.98:
                continue
            plane_gap = abs(float((cen_b - cen_a).dot(norm_a)))
            if plane_gap > eps:
                continue

            overlap_area = _tri_overlap_area_2d((va0, va1, va2), (vb0, vb1, vb2), norm_a)
            if overlap_area <= max(1e-8, 0.01 * min(area_a, area_b)):
                continue

            hit_centroid = (cen_a + cen_b) * 0.5
            witness = {
                "objects": [obj_a, obj_b],
                "faces": [face_a, face_b],
                "plane_gap": round(plane_gap, 6),
                "overlap_area": round(overlap_area, 6),
                "centroid": [round(float(hit_centroid.x), 4), round(float(hit_centroid.y), 4), round(float(hit_centroid.z), 4)],
            }
            if dot > 0.0:
                add_finding("zfighting", witness)
            else:
                add_finding("back_to_back", witness)

    if len(island_bvhs) >= 2:
        # Largest-bbox-diagonal / tri-count island anchors multi-island scenes unless disconnected from everything else.
        anchor_idx = max(
            range(len(island_bvhs)),
            key=lambda i: (island_bvhs[i][0]["bbox_diag"], len(island_bvhs[i][0]["world_tris"])),
        )
        for i, (rec_i, bvh_i) in enumerate(island_bvhs):
            if i == anchor_idx or not rec_i["sample_verts"]:
                continue
            connected = False
            min_gap = float("inf")
            others = []
            for j, (rec_j, bvh_j) in enumerate(island_bvhs):
                if i == j:
                    continue
                if bvh_i.overlap(bvh_j):
                    connected = True
                    break
                others.append((rec_j["label"], bvh_j))
                for sv in rec_i["sample_verts"]:
                    _loc, _n, _idx, dist = bvh_j.find_nearest(sv)
                    if dist is not None and dist < min_gap:
                        min_gap = dist
                        if min_gap <= tol:
                            connected = True
                            break
                if connected:
                    break
            if connected:
                continue

            probe_point = rec_i["centroid"]
            host_label = _parity_ray_enclosed(probe_point, others)
            witness = {
                "object": rec_i["obj_name"],
                "island_index": rec_i["island_idx"],
                "tri_count": len(rec_i["world_tris"]),
                "min_gap": round(float(min_gap), 5) if math.isfinite(min_gap) else None,
                "centroid": [round(float(probe_point.x), 4), round(float(probe_point.y), 4), round(float(probe_point.z), 4)],
            }
            if host_label is not None:
                witness["enclosed_by"] = host_label
                add_finding("enclosed_island", witness)
            else:
                add_finding("floating_island", witness)


def run(
    objects=None,
    profile="game",
    eps=0.001,
    tol=0.002,
    evaluated=True,
    max_tris=300000,
    uv=True,
    influence_limit=4,
    check_materials=True,
    check_weights=True,
    check_ground=True,
    include_hidden=False,
    witness_limit=25,
    scene=None,
    view_layer=None,
    uv_layer=None,
    udim=None,
):
    """Run read-only geometry, spatial, UV, material, and skinning checks on Blender scene or selected objects."""
    if profile not in PROFILE_CONFIG:
        raise ValueError(f"Unsupported QA profile '{profile}'; expected one of {sorted(PROFILE_CONFIG)}")
    if eps <= 0 or tol <= 0 or max_tris <= 0 or influence_limit <= 0:
        raise ValueError("eps, tol, max_tris, and influence_limit must be positive")
    if uv_layer is not None and not uv:
        raise ValueError("A named UV layer requires UV inspection")
    if udim is not None and udim.get("enabled") and not uv:
        raise ValueError("Declared UDIM requires UV inspection")

    cfg = PROFILE_CONFIG[profile]
    disabled = cfg["disabled_checks"]
    blocker_codes = cfg["hard_blockers"]

    target_objs = _resolve_target_objects(objects, scene, view_layer, include_hidden)
    if uv_layer is not None and not target_objs:
        raise UVLayerNotFound(f"No target meshes on which to inspect UV layer {uv_layer!r}")
    depsgraph = bpy.context.evaluated_depsgraph_get() if bpy.context else None

    findings = {}
    skipped = []
    object_summaries = []
    island_records = []

    total_verts = 0
    total_faces = 0
    total_tris = 0
    scene_min_z = None
    has_edit_unflushed = False
    uv_locations_left = max(0, witness_limit)
    udim_sizes = {}

    def add_finding(code, witness, count=1):
        if code in disabled:
            return
        bucket = "hard_blocker" if code in blocker_codes else "review_signal"
        severity = "error" if bucket == "hard_blocker" else ("info" if code in ("ngons", "enclosed_island", "ground_z_offset", "no_materials", "unused_material_slot") or (code == "uv_oob" and cfg.get("uv_oob_info_only")) else "warning")
        entry = findings.setdefault(
            code,
            {
                "code": code,
                "bucket": bucket,
                "severity": severity,
                "status": "fail" if bucket == "hard_blocker" else severity,
                "count": 0,
                "witnesses": [],
                "truncated": False,
            },
        )
        entry["count"] += int(count)
        if len(entry["witnesses"]) < witness_limit:
            entry["witnesses"].append(witness)
        else:
            entry["truncated"] = True

    for obj in target_objs:
        sx, sy, sz = (float(obj.scale.x), float(obj.scale.y), float(obj.scale.z))
        det_mw = float(obj.matrix_world.determinant())
        has_neg_scale = sx < -1e-6 or sy < -1e-6 or sz < -1e-6 or det_mw < -1e-6
        if has_neg_scale:
            add_finding("negative_scale", {"object": obj.name, "scale": [round(sx, 5), round(sy, 5), round(sz, 5)], "matrix_det": round(det_mw, 5)})
        elif any(abs(s - 1.0) > 1e-4 for s in (sx, sy, sz)):
            add_finding("unapplied_scale", {"object": obj.name, "scale": [round(sx, 5), round(sy, 5), round(sz, 5)]})

        bm, tri_mesh_data, edit_unflushed, cleanup = _acquire_object_mesh_data(obj, depsgraph, evaluated, uv_layer is not None)
        try:
            if edit_unflushed:
                has_edit_unflushed = True
            bm.verts.ensure_lookup_table()
            bm.edges.ensure_lookup_table()
            bm.faces.ensure_lookup_table()

            v_count = len(bm.verts)
            f_count = len(bm.faces)
            tri_count = sum(max(0, len(f.verts) - 2) for f in bm.faces)
            total_verts += v_count
            total_faces += f_count
            total_tris += tri_count

            non_finite_verts = [v.index for v in bm.verts if not all(math.isfinite(c) for c in v.co)]
            if non_finite_verts:
                add_finding("non_finite", {"object": obj.name, "vertices": non_finite_verts[:10]}, count=len(non_finite_verts))

            degen_faces = [f.index for f in bm.faces if f.calc_area() <= 1e-10 or len({v.index for v in f.verts}) < 3]
            if degen_faces:
                add_finding("degenerate_faces", {"object": obj.name, "faces": degen_faces[:10]}, count=len(degen_faces))

            zero_edges = [e.index for e in bm.edges if e.calc_length() <= 1e-7]
            if zero_edges:
                add_finding("zero_length_edges", {"object": obj.name, "edges": zero_edges[:10]}, count=len(zero_edges))

            wire_edges = [e.index for e in bm.edges if len(e.link_faces) == 0]
            boundary_edges = [e.index for e in bm.edges if len(e.link_faces) == 1]
            nonmanifold_edges = [e.index for e in bm.edges if len(e.link_faces) > 2]
            inconsistent_edges = [e.index for e in bm.edges if len(e.link_faces) == 2 and not e.is_contiguous]
            ngons = [f.index for f in bm.faces if len(f.verts) > 4]

            if wire_edges:
                add_finding("wire_edges", {"object": obj.name, "edges": wire_edges[:10]}, count=len(wire_edges))
            if boundary_edges:
                add_finding("boundary_edges", {"object": obj.name, "edges": boundary_edges[:10]}, count=len(boundary_edges))
            if nonmanifold_edges:
                add_finding("nonmanifold_edges", {"object": obj.name, "edges": nonmanifold_edges[:10]}, count=len(nonmanifold_edges))
            if inconsistent_edges:
                add_finding("inconsistent_normals", {"object": obj.name, "edges": inconsistent_edges[:10]}, count=len(inconsistent_edges))
            if ngons:
                add_finding("ngons", {"object": obj.name, "faces": ngons[:10]}, count=len(ngons))

            # Duplicate vertex welding probe (capped to avoid slow KDTree construction on huge sculpts).
            if 0 < v_count <= 100000 and not non_finite_verts:
                kd = KDTree(v_count)
                for i, v in enumerate(bm.verts):
                    kd.insert(v.co, i)
                kd.balance()
                dup_verts = 0
                for v in bm.verts:
                    if len(kd.find_range(v.co, 1e-5)) > 1:
                        dup_verts += 1
                if dup_verts:
                    add_finding("duplicate_verts", {"object": obj.name, "vertex_count": dup_verts}, count=dup_verts)
            elif v_count > 100000:
                skipped.append({"check": "duplicate_verts", "object": obj.name, "reason": f"vertex_count ({v_count}) > 100000"})

            # World bounding box Z and per-island watertight volume check.
            obj_min_z = None
            for v in bm.verts:
                if non_finite_verts and not all(math.isfinite(c) for c in v.co):
                    continue
                wz = _world_pt_64(obj.matrix_world, v.co)[2]
                if obj_min_z is None or wz < obj_min_z:
                    obj_min_z = wz
            if obj_min_z is not None and (scene_min_z is None or obj_min_z < scene_min_z):
                scene_min_z = obj_min_z

            islands = _extract_mesh_islands(bm)
            for isl_idx, isl_faces in enumerate(islands):
                isl_edges = {e for f in isl_faces for e in f.edges}
                is_closed = bool(isl_faces) and all(len(e.link_faces) == 2 and e.is_contiguous for e in isl_edges)
                isl_verts_set = {v for f in isl_faces for v in f.verts}
                label = f"{obj.name}#island{isl_idx}"
                w_verts, w_tris, tris_64, tri_meta = _bmesh_triangulated_copy_tris(isl_faces, obj.matrix_world, (obj.name, isl_idx))

                centroid = Vector((0.0, 0.0, 0.0))
                if w_verts:
                    centroid = sum(w_verts, Vector((0.0, 0.0, 0.0))) / len(w_verts)
                    xs = [p.x for p in w_verts]
                    ys = [p.y for p in w_verts]
                    zs = [p.z for p in w_verts]
                    diag = Vector((max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))).length
                else:
                    diag = 0.0

                if is_closed and tris_64:
                    svol = _signed_volume_64(tris_64)
                    # If the object itself has negative_scale, world transform already mirrors winding intentionally or accidentally; report inside_out only on positive determinant or explicit local check.
                    if svol < -1e-9 and det_mw > 0:
                        add_finding(
                            "inside_out_islands",
                            {
                                "object": obj.name,
                                "island_index": isl_idx,
                                "faces": len(isl_faces),
                                "signed_volume": round(svol, 6),
                                "centroid": [round(float(centroid.x), 4), round(float(centroid.y), 4), round(float(centroid.z), 4)],
                            },
                        )

                stride = max(1, len(w_verts) // 128)
                sample_verts = w_verts[::stride]
                island_records.append(
                    {
                        "obj_name": obj.name,
                        "island_idx": isl_idx,
                        "island_key": (obj.name, isl_idx),
                        "label": label,
                        "world_verts": w_verts,
                        "world_tris": w_tris,
                        "tri_meta": tri_meta,
                        "sample_verts": sample_verts,
                        "centroid": centroid,
                        "bbox_diag": diag,
                    }
                )

            # Lightweight UV check
            uv_summary = None
            if uv and (f_count > 0 or uv_layer is not None):
                locations = []
                layers = tri_mesh_data.uv_layers if tri_mesh_data is not None else bm.loops.layers.uv
                layer = layers.get(uv_layer) if uv_layer is not None else layers.active
                if layer is None and uv_layer is not None:
                    raise UVLayerNotFound(f"UV layer {uv_layer!r} not found on {obj.name!r}")
                if layer is None and tri_mesh_data is not None and len(layers):
                    layer = next((item for item in layers if item.active_render), layers[0])
                if layer is None:
                    add_finding("uv_missing", {"object": obj.name, "faces": f_count})
                else:
                    layer_name = layer.name
                    tiled = qa_udim.tiled_images(obj)
                    assignments = udim.get("per_object_tiles", {}) if udim else {}
                    declared = bool(udim and udim["enabled"] and (tiled or not assignments or obj.name in assignments))
                    inspect_tiles = declared or (udim is None and bool(tiled))
                    if tri_mesh_data is not None:
                        uv_summary = _rasterize_uv_stats(
                            tri_mesh_data, layer, locations=locations, location_limit=uv_locations_left,
                            measure_overlap=not inspect_tiles,
                        )
                    else:
                        uv_summary = _rasterize_uv_stats_from_bmesh(bm, layer, locations=locations, location_limit=uv_locations_left)
                    if inspect_tiles:
                        used = {tile.number for image in tiled for tile in image.tiles}
                        if declared:
                            allowed = set(assignments.get(obj.name, udim["tiles"]))
                            expected_size = tuple(udim.get("tile_size_px", (udim["tile_resolution"],) * 2))
                            resolution = {tile: expected_size for tile in allowed}
                            for image in tiled:
                                if not any(token in image.filepath for token in ("<UDIM>", "<UVTILE>")):
                                    add_finding("udim_texture_token_missing", {
                                        "object": obj.name, "image": image.name, "filepath": image.filepath,
                                    })
                                actual = {tile.number for tile in image.tiles}
                                if not allowed <= actual or not actual <= set(udim["tiles"]):
                                    add_finding("udim_texture_tile_mismatch", {
                                        "object": obj.name, "image": image.name,
                                        "required_tiles": sorted(allowed), "actual_tiles": sorted(actual),
                                    })
                                key = image.as_pointer()
                                if key not in udim_sizes:
                                    udim_sizes[key] = dict(qa_udim.image_sizes(image))
                                for tile in allowed:
                                    actual_size = udim_sizes[key].get(tile)
                                    if actual_size and actual_size != expected_size:
                                        add_finding("udim_texture_resolution_mismatch", {
                                            "object": obj.name, "image": image.name, "tile": tile,
                                            "expected_size": list(expected_size), "actual_size": list(actual_size),
                                        })
                        else:
                            allowed = used
                            resolution = {}
                            for image in tiled:
                                key = image.as_pointer()
                                if key not in udim_sizes:
                                    udim_sizes[key] = dict(qa_udim.image_sizes(image))
                                for tile, size in udim_sizes[key].items():
                                    resolution[tile] = tuple(min(a, b) for a, b in zip(size, resolution.get(tile, size)))
                        kwargs = {"allowed": allowed, "resolution": resolution, "witness_limit": witness_limit}
                        if tri_mesh_data is not None:
                            detail = qa_udim.inspect_mesh(tri_mesh_data, layer, **kwargs)
                        else:
                            detail = qa_udim.inspect_bmesh(bm, layer, **kwargs)
                        uv_summary.update(detail)
                    if uv_layer is not None:
                        uv_summary["uv_layer"] = layer_name

                if uv_summary is not None:
                    tiled_summary = uv_summary.get("udim")
                    if uv_summary["uv_oob_pct"] > 0.0:
                        witness = {"object": obj.name, "oob_pct": uv_summary["uv_oob_pct"]}
                        if tiled_summary:
                            witness["violations"] = tiled_summary["violations"]
                        add_finding("uv_oob", witness)
                    if tiled_summary:
                        for tile in tiled_summary["tiles"]:
                            if tile["min_margin_px"] is not None and tile["min_margin_px"] < 4:
                                add_finding("udim_tile_margin", {
                                    "object": obj.name, "tile": tile["tile"],
                                    "min_margin_px": tile["min_margin_px"], "required_margin_px": 4,
                                })
                            if tile["overlap_pair_count"]:
                                add_finding("uv_overlap_within_tile", {
                                    "object": obj.name, "tile": tile["tile"], "overlap_pct": tile["overlap_pct"],
                                    "pairs": tile["overlap_pairs"], "pair_count": tile["overlap_pair_count"],
                                })
                            if not tile["intersection_check_complete"]:
                                skipped.append({"checks": ["udim_intersection_area"], "object": obj.name,
                                                "tile": tile["tile"], "reason": "comparison budget exceeded"})
                    low_count = uv_summary["uv_zero_area_count"]
                    if low_count:
                        uv_locations_left -= len(locations)
                        geometry_source = "EDIT_BMESH" if edit_unflushed else "EVALUATED_MESH" if evaluated else "BASE_MESH"
                        witness = {
                            "object": obj.name, "mesh": obj.data.name, "uv_layer": layer_name,
                            "geometry_source": geometry_source, "tri_count": low_count,
                            "area_threshold": UV_AREA_THRESHOLD, "triangles": locations,
                            "triangles_truncated": len(locations) < low_count,
                        }
                        if geometry_source == "BASE_MESH" and not witness["triangles_truncated"]:
                            face_ids = sorted({tri["polygon_index"] for tri in locations})
                            witness["uv_selection"] = {obj.name: {"domain": "FACES", "indices": face_ids}}
                        add_finding("uv_zero_area", witness, count=low_count)
                    if uv_summary["uv_overlap_pct"] > cfg["uv_overlap_warn_pct"]:
                        add_finding(
                            "uv_high_overlap",
                            {
                                "object": obj.name,
                                "overlap_pct": uv_summary["uv_overlap_pct"],
                                "threshold_pct": cfg["uv_overlap_warn_pct"],
                                "sampled": uv_summary["sampled"],
                            },
                        )

            object_summaries.append(
                {
                    "name": obj.name,
                    "vertices": v_count,
                    "faces": f_count,
                    "triangles": tri_count,
                    "islands": len(islands),
                    "min_z": round(obj_min_z, 5) if obj_min_z is not None else None,
                    "uv": uv_summary,
                }
            )
        finally:
            cleanup()

    if check_materials:
        _inspect_materials(target_objs, add_finding)
    if check_weights:
        _inspect_weights(target_objs, influence_limit, add_finding)
    if check_ground and scene_min_z is not None and abs(scene_min_z) > cfg["ground_z_tol"]:
        add_finding("ground_z_offset", {"scene_min_z": round(scene_min_z, 5), "tolerance": cfg["ground_z_tol"]})

    # Heavy spatial BVH checks gated by --max-tris
    spatial_checks = ["zfighting", "back_to_back", "floating_island", "enclosed_island"]
    if total_tris > max_tris:
        skipped.append(
            {
                "checks": spatial_checks,
                "reason": f"total_tris ({total_tris}) exceeded max_tris ({max_tris})",
                "total_tris": total_tris,
                "max_tris": max_tris,
            }
        )
    elif island_records:
        _run_spatial_bvh_checks(island_records, eps, tol, add_finding)

    hard_blockers = sorted((f for f in findings.values() if f["bucket"] == "hard_blocker"), key=lambda x: x["code"])
    review_signals = sorted((f for f in findings.values() if f["bucket"] == "review_signal"), key=lambda x: x["code"])
    status = "fail" if hard_blockers else ("review" if review_signals else "pass")

    return {
        "qa_report_version": QA_REPORT_VERSION,
        "read_only": True,
        "profile": profile,
        "status": status,
        "stats": {
            "objects_checked": len(target_objs),
            "total_vertices": total_verts,
            "total_faces": total_faces,
            "total_triangles": total_tris,
            "scene_min_z": round(scene_min_z, 5) if scene_min_z is not None else None,
            "evaluated": bool(evaluated),
            "max_tris": int(max_tris),
            "edit_mode_unflushed": has_edit_unflushed,
        },
        "counts": {
            "hard_blockers": len(hard_blockers),
            "review_signals": len(review_signals),
            "skipped": len(skipped),
        },
        "hard_blockers": hard_blockers,
        "review_signals": review_signals,
        "skipped": skipped,
        "objects": object_summaries,
    }



def _collect_global_image_roles():
    """Aggregate TEX_IMAGE semantic roles across all local node-based materials to prevent cross-material breakage."""
    image_roles = {}
    for mat in bpy.data.materials:
        if not getattr(mat, "use_nodes", True) or mat.node_tree is None:
            continue
        for node in mat.node_tree.nodes:
            if node.type != "TEX_IMAGE" or node.image is None:
                continue
            ptr = node.image.as_pointer()
            roles = image_roles.setdefault(ptr, set())
            roles.update(_classify_image_roles(mat.node_tree, node))
    return image_roles


def _pick_non_color_name(img):
    """Select an available Non-Color/Raw colorspace enum name on the image datablock."""
    prop = img.colorspace_settings.bl_rna.properties.get("name")
    items = [e.identifier for e in prop.enum_items] if prop else []
    for candidate in ("Non-Color", "Raw", "Utility - Raw", "Linear Rec.709", "Linear"):
        if not items or candidate in items:
            return candidate
    for ident in items:
        if _is_non_color_colorspace(ident):
            return ident
    return None


def _fix_data_texture_srgb(target_objs, dry_run):
    """Convert unambiguous sRGB data textures to Non-Color without touching geometry or mixed-role maps."""
    global_roles = _collect_global_image_roles()
    changes = []
    skipped_unfixable = []
    handled_images = set()

    for obj in target_objs:
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None or not getattr(mat, "use_nodes", True) or mat.node_tree is None:
                continue
            for node in mat.node_tree.nodes:
                if node.type != "TEX_IMAGE" or node.image is None:
                    continue
                img = node.image
                ptr = img.as_pointer()
                if ptr in handled_images:
                    continue
                local_roles = _classify_image_roles(mat.node_tree, node)
                if "data" not in local_roles:
                    continue
                handled_images.add(ptr)

                all_roles = global_roles.get(ptr, local_roles)
                cs_before = img.colorspace_settings.name if hasattr(img, "colorspace_settings") else ""
                if _is_non_color_colorspace(cs_before):
                    continue
                if "color" in local_roles:
                    skipped_unfixable.append(
                        {
                            "action": "fix_data_texture_srgb",
                            "object": obj.name,
                            "material": mat.name,
                            "image": img.name,
                            "reason": "image_drives_both_color_and_data_sockets",
                        }
                    )
                    continue
                if "color" in all_roles:
                    skipped_unfixable.append(
                        {
                            "action": "fix_data_texture_srgb",
                            "object": obj.name,
                            "material": mat.name,
                            "image": img.name,
                            "reason": "shared_image_has_conflicting_color_and_data_roles",
                        }
                    )
                    continue
                if img.library or img.override_library:
                    skipped_unfixable.append(
                        {
                            "action": "fix_data_texture_srgb",
                            "object": obj.name,
                            "material": mat.name,
                            "image": img.name,
                            "reason": "linked_or_override_image_readonly",
                        }
                    )
                    continue
                target_cs = _pick_non_color_name(img)
                if target_cs is None:
                    skipped_unfixable.append(
                        {
                            "action": "fix_data_texture_srgb",
                            "object": obj.name,
                            "material": mat.name,
                            "image": img.name,
                            "reason": "no_non_color_colorspace_available",
                        }
                    )
                    continue

                if not dry_run:
                    img.colorspace_settings.name = target_cs
                    cs_after = img.colorspace_settings.name
                else:
                    cs_after = target_cs
                changes.append(
                    {
                        "action": "fix_data_texture_srgb",
                        "object": obj.name,
                        "material": mat.name,
                        "image": img.name,
                        "before": cs_before,
                        "after": cs_after,
                        "dry_run": bool(dry_run),
                    }
                )
    return changes, skipped_unfixable


def _remove_unused_empty_material_slots(target_objs, dry_run):
    """Remove unused empty (None) DATA-linked material slots, preserving active/shared slot assignments."""
    changes = []
    skipped_unfixable = []
    handled_meshes = set()

    for obj in target_objs:
        me = obj.data
        if not isinstance(me, bpy.types.Mesh):
            continue
        mesh_ptr = me.as_pointer()
        if mesh_ptr in handled_meshes:
            continue
        handled_meshes.add(mesh_ptr)

        if obj.library or obj.override_library or me.library or me.override_library:
            if any(s.material is None for s in obj.material_slots):
                skipped_unfixable.append(
                    {
                        "action": "remove_unused_empty_material_slots",
                        "object": obj.name,
                        "reason": "linked_or_override_mesh_readonly",
                    }
                )
            continue

        users = [o for o in bpy.data.objects if o.data == me]
        if any(o.mode == "EDIT" for o in users):
            if any(s.material is None for s in obj.material_slots):
                skipped_unfixable.append(
                    {
                        "action": "remove_unused_empty_material_slots",
                        "object": obj.name,
                        "reason": "object_in_edit_mode_skip_material_slot_cleanup",
                    }
                )
            continue
        if any(o.library or o.override_library for o in users) or any(
            s.link != "DATA" for o in users for s in o.material_slots
        ):
            if any(s.material is None for o in users for s in o.material_slots):
                skipped_unfixable.append(
                    {
                        "action": "remove_unused_empty_material_slots",
                        "object": obj.name,
                        "reason": "shared_or_object_linked_material_slot_skipped",
                    }
                )
            continue

        slots = me.materials
        if len(slots) == 0:
            continue
        polys = me.polygons
        referenced = set()
        if len(polys) > 0:
            mat_indices = np.empty(len(polys), dtype=np.int32)
            polys.foreach_get("material_index", mat_indices)
            referenced = {int(x) for x in np.unique(mat_indices)}

        removable = [
            idx
            for idx, mat in enumerate(slots)
            if mat is None and idx not in referenced
        ]
        if not removable:
            continue

        user_names = sorted(o.name for o in users)
        for idx in reversed(removable):
            if not dry_run:
                # Mesh.materials.pop(index=idx) automatically decrements polygon material_index > idx in Blender C core.
                me.materials.pop(index=idx)
            changes.append(
                {
                    "action": "remove_unused_empty_material_slots",
                    "object": obj.name,
                    "mesh": me.name,
                    "affected_objects": user_names,
                    "removed_slot_index": idx,
                    "before": None,
                    "after": "removed",
                    "dry_run": bool(dry_run),
                }
            )
        if not dry_run:
            me.update()
    return changes, skipped_unfixable


def _fix_boolean_visibility(objects, dry_run):
    from qa import _boolean_cutters, _is_helper_cutter

    changes = []
    for cutter in sorted(_boolean_cutters(objects), key=lambda obj: obj.name):
        if not (cutter.library or cutter.override_library or cutter.hide_render) and _is_helper_cutter(cutter):
            changes.append({"action": "hide_boolean_cutters_in_render", "object": cutter.name,
                            "before": False, "after": True, "dry_run": dry_run})
            if not dry_run:
                cutter.hide_render = True
    return changes, []


def _fix_subdivision_levels(objects, dry_run):
    changes = []
    for obj in objects:
        if obj.library or obj.override_library:
            continue
        for mod in obj.modifiers:
            if mod.type not in {"SUBSURF", "MULTIRES"} or mod.levels == mod.render_levels:
                continue
            changes.append({"action": "align_subdivision_levels", "object": obj.name, "modifier": mod.name,
                            "before": mod.levels, "after": mod.render_levels, "dry_run": dry_run})
            if not dry_run:
                mod.levels = mod.render_levels
    return changes, []


def _fix_texture_paths(objects, dry_run):
    from qa import _linked_image_nodes

    materials = {slot.material for obj in objects for slot in obj.material_slots if slot.material}
    images = {image for material in materials for image in _linked_image_nodes(material)}
    base = Path(bpy.data.filepath).parent if bpy.data.filepath else None
    changes, skipped = [], []
    for image in sorted(images, key=lambda item: item.name):
        if (image.source not in {"FILE", "TILED", "SEQUENCE", "MOVIE"} or image.packed_file
                or image.library or image.override_library or not image.filepath or image.filepath.startswith("//")):
            continue
        if not base:
            skipped.append({"action": "relativize_texture_paths", "image": image.name, "reason": "save an input file first"})
            continue
        relative = bpy.path.relpath(image.filepath, start=str(base))
        if not relative.startswith("//"):
            skipped.append({"action": "relativize_texture_paths", "image": image.name, "reason": "different drive"})
            continue
        changes.append({"action": "relativize_texture_paths", "image": image.name,
                        "before": image.filepath, "after": relative, "dry_run": dry_run})
        if not dry_run:
            image.filepath = relative
    return changes, skipped


def _fix_local_orphans(dry_run):
    ids = bpy.data.user_map()
    candidates = {item for item in ids if not (item.library or item.use_fake_user or item.is_embedded_data)}
    details = {
        item.as_pointer(): (item.bl_rna.identifier, item.name)
        for item in candidates
    }
    if dry_run:
        # Follow chains made only of local unused IDs.
        orphans = {item for item in candidates if item.users == 0}
        while True:
            implicit = {item for item in candidates - orphans if ids[item] and ids[item] <= orphans}
            if not implicit:
                break
            orphans.update(implicit)
        removed = [item.as_pointer() for item in orphans]
    else:
        bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=False, do_recursive=True)
        remaining = {item.as_pointer() for item in bpy.data.user_map()}
        removed = details.keys() - remaining
    return [
        {"action": "purge_local_orphans", "type": kind, "name": name, "dry_run": dry_run}
        for kind, name in sorted(details[key] for key in removed)
    ], []


def fix_safe(
    objects=None,
    profile="game",
    actions=None,
    dry_run=False,
    eps=0.001,
    tol=0.002,
    evaluated=True,
    max_tris=DEFAULT_MAX_TRIS,
    uv=True,
    influence_limit=4,
    include_hidden=False,
    scene=None,
    view_layer=None,
):
    """Apply strictly non-destructive, uncontroversial metadata fixes on working scene or isolated candidate.

    The two compatibility fixes are default. The four broader actions are opt-in.
    Base vertex coordinates, topology, normals, UVs, and skin weights are preserved.
    """
    selected_actions = list(DEFAULT_SAFE_FIX_ACTIONS) if actions is None else list(actions)
    unknown = [a for a in selected_actions if a not in SAFE_FIX_ACTIONS]
    if unknown:
        raise ValueError(f"Unsupported fix_safe actions {unknown}; allowed: {SAFE_FIX_ACTIONS}")

    qa_kwargs = {
        "objects": objects,
        "profile": profile,
        "eps": eps,
        "tol": tol,
        "evaluated": evaluated,
        "max_tris": max_tris,
        "uv": uv,
        "influence_limit": influence_limit,
        "include_hidden": include_hidden,
        "scene": scene,
        "view_layer": view_layer,
    }
    qa_before = run(**qa_kwargs)
    target_objs = _resolve_target_objects(objects, scene, view_layer, include_hidden)

    changes = []
    skipped_unfixable = []
    for action in selected_actions:
        if action == "fix_data_texture_srgb":
            c, s = _fix_data_texture_srgb(target_objs, dry_run=dry_run)
        elif action == "remove_unused_empty_material_slots":
            c, s = _remove_unused_empty_material_slots(target_objs, dry_run=dry_run)
        elif action == "hide_boolean_cutters_in_render":
            c, s = _fix_boolean_visibility(target_objs, dry_run)
        elif action == "purge_local_orphans":
            c, s = _fix_local_orphans(dry_run)
        elif action == "align_subdivision_levels":
            c, s = _fix_subdivision_levels(target_objs, dry_run)
        elif action == "relativize_texture_paths":
            c, s = _fix_texture_paths(target_objs, dry_run)
        changes.extend(c)
        skipped_unfixable.extend(s)

    qa_after = qa_before if dry_run else run(**qa_kwargs)
    remaining = {
        "hard_blockers": [
            {"code": x["code"], "count": x["count"]}
            for x in qa_after["hard_blockers"]
        ],
        "review_signals": [
            {"code": x["code"], "count": x["count"]}
            for x in qa_after["review_signals"]
        ],
    }

    return {
        "qa_fix_report_version": QA_FIX_REPORT_VERSION,
        "profile": profile,
        "dry_run": bool(dry_run),
        "geometry_mutated": False,
        "evaluated_geometry_mutated": not dry_run and any(
            entry.get("action") == "align_subdivision_levels" for entry in changes
        ),
        "applied_actions": selected_actions,
        "changes": changes,
        "skipped_unfixable": skipped_unfixable,
        "remaining_non_auto_fixable": remaining,
        "qa_before": {
            "status": qa_before["status"],
            "counts": qa_before["counts"],
            "hard_blockers": qa_before["hard_blockers"],
            "review_signals": qa_before["review_signals"],
            "skipped": qa_before["skipped"],
        },
        "qa_after": {
            "status": qa_after["status"],
            "counts": qa_after["counts"],
            "hard_blockers": qa_after["hard_blockers"],
            "review_signals": qa_after["review_signals"],
            "skipped": qa_after["skipped"],
        },
    }
