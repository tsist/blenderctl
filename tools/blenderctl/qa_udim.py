"""Read UV islands and measure coverage in the declared UDIM tiles."""

from collections import defaultdict
import math
from pathlib import Path
import struct

import bpy
import numpy as np


TILE_TOLERANCE = 1e-6
AREA_TOLERANCE = 1e-10


def tile_number(column, row):
    number = 1001 + column + 10 * row
    return number if 0 <= column < 10 and row >= 0 and number <= 1099 else None


def tiled_files(image):
    pattern = bpy.path.abspath(image.filepath, library=image.library)
    for tile in image.tiles:
        column, row = (tile.number - 1001) % 10, (tile.number - 1001) // 10
        yield tile.number, Path(
            pattern.replace("<UDIM>", str(tile.number)).replace("<UVTILE>", f"u{column + 1}_v{row + 1}")
        )


def tiled_images(obj):
    seen = set()
    trees = [
        slot.material.node_tree for slot in obj.material_slots
        if slot.material and slot.material.use_nodes and slot.material.node_tree
    ]
    images = {}
    while trees:
        tree = trees.pop()
        if tree.as_pointer() in seen:
            continue
        seen.add(tree.as_pointer())
        for node in tree.nodes:
            if node.type == "GROUP" and node.node_tree:
                trees.append(node.node_tree)
            if node.type == "TEX_IMAGE" and node.image and node.image.source == "TILED":
                images[node.image.as_pointer()] = node.image
    return list(images.values())


def image_sizes(image):
    import imbuf

    packed = {Path(entry.filepath).name: entry.packed_file for entry in image.packed_files}
    for tile, path in tiled_files(image):
        if path.name in packed:
            png_header = packed[path.name].data[:24] if path.suffix.lower() == ".png" else b""
        elif path.is_file() and path.suffix.lower() == ".png":
            with path.open("rb") as stream:
                png_header = stream.read(24)
        else:
            png_header = b""
        if png_header[:8] == b"\x89PNG\r\n\x1a\n" and len(png_header) == 24:
            yield tile, struct.unpack(">2I", png_header[16:24])
        elif path.is_file() and path.name not in packed:
            try:
                image_buffer = imbuf.load(str(path))
            except (OSError, ValueError, RuntimeError):
                continue
            try:
                yield tile, tuple(image_buffer.size)
            finally:
                image_buffer.free()


def _tile_bounds(uvs):
    low = np.floor(uvs.min(axis=0) + TILE_TOLERANCE).astype(int)
    high = np.ceil(uvs.max(axis=0) - TILE_TOLERANCE).astype(int) - 1
    high = np.maximum(low, high)
    return low, high


def _face_witness(index, uvs, allowed, island):
    if not np.isfinite(uvs).all():
        return None, {"polygon_index": index, "island": island, "reason": "non_finite"}
    if (uvs < -1).any() or (uvs > 11).any():
        return None, {"polygon_index": index, "island": island, "reason": "undeclared_tile"}
    low, high = _tile_bounds(uvs)
    if not np.array_equal(low, high):
        crossed = {
            f"crossed_{axis}": int(low[i]) + 1
            for i, axis in enumerate(("u", "v")) if high[i] > low[i]
        }
        tiles = [
            tile_number(x, y) for x in range(max(0, low[0]), min(9, high[0]) + 1)
            for y in range(max(0, low[1]), min(9, high[1]) + 1)
        ]
        return None, {"polygon_index": index, "island": island, "reason": "island_crosses_tile_boundary",
                      "tiles": sorted(t for t in tiles if t is not None), **crossed}
    tile = tile_number(*map(int, low))
    if tile not in allowed:
        return None, {"polygon_index": index, "island": island, "reason": "undeclared_tile",
                      "tile": tile, "tile_coordinates": low.tolist()}
    return tile, None


def _measure_tiles(faces, triangles, allowed, resolution, witness_limit):
    """Use UV-continuous mesh edges to identify islands across faces."""
    parents = {idx: idx for idx, _, _ in faces}

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    edge_faces = {}
    for index, vertices, uvs in faces:
        corners = tuple(zip(vertices, map(tuple, uvs)))
        for first, second in zip(corners, (*corners[1:], corners[0])):
            edge = tuple(sorted((first, second)))
            previous = edge_faces.setdefault(edge, index)
            if previous != index:
                parents[root(index)] = root(previous)

    island_faces = defaultdict(list)
    tiles_for_face = {}
    violations = []
    invalid_faces = set()
    loop_count = sum(len(uvs) for _, _, uvs in faces)
    invalid_loops = 0
    for index, _, uvs in faces:
        island = root(index)
        tile, finding = _face_witness(index, uvs, allowed, island)
        island_faces[island].append((index, tile, uvs))
        if finding:
            violations.append(finding)
            invalid_faces.add(index)
            invalid_loops += len(uvs)
        else:
            tiles_for_face[index] = tile

    tile_records = {}
    for island, records in island_faces.items():
        tiles = sorted({tile for _, tile, _ in records if tile is not None})
        if len(tiles) > 1:
            violations.append({"island": island, "reason": "island_crosses_tile_boundary", "tiles": tiles,
                               "polygon_indices": [index for index, _, _ in records][:witness_limit]})
            for index, _, uvs in records:
                if index not in invalid_faces:
                    invalid_faces.add(index)
                    invalid_loops += len(uvs)
                    tiles_for_face.pop(index)
            continue
        for index, tile, uvs in records:
            if tile is None:
                continue
            origin = ((tile - 1001) % 10, (tile - 1001) // 10)
            local = uvs - origin
            edge_margins = np.maximum(0.0, np.minimum(local, 1 - local).min(axis=0))
            record = tile_records.setdefault(tile, {
                "tile": tile, "face_count": 0, "islands": set(), "margin_axes": np.ones(2), "tris": [],
            })
            record["face_count"] += 1
            record["islands"].add(island)
            record["margin_axes"] = np.minimum(record["margin_axes"], edge_margins)

    for index, uvs in triangles:
        tile = tiles_for_face.get(index)
        if tile is not None:
            origin = ((tile - 1001) % 10, (tile - 1001) // 10)
            tile_records[tile]["tris"].append((index, uvs - origin))

    overlap_cells = covered_cells = 0
    for tile, record in tile_records.items():
        record["island_count"] = len(record.pop("islands"))
        known_resolution = resolution.get(tile)
        axes = record.pop("margin_axes")
        record["min_margin_uv"] = float(axes.min())
        record["margin_uv"] = axes.tolist()
        record["resolution"] = known_resolution[0] if known_resolution else None
        record["tile_size_px"] = list(known_resolution) if known_resolution else None
        record["min_margin_px"] = round(float((axes * known_resolution).min()), 6) if known_resolution else None
        coverage = _coverage(record.pop("tris"), witness_limit)
        record.update(coverage)
        overlap_cells += coverage["overlap_cells"]
        covered_cells += coverage["covered_cells"]
    return {
        "uv_oob_pct": round(100 * invalid_loops / max(1, loop_count), 3),
        "uv_overlap_pct": round(100 * overlap_cells / max(1, covered_cells), 3),
        "udim": {
            "tiles": [tile_records[tile] for tile in sorted(tile_records)],
            "allowed_tiles": sorted(allowed),
            "invalid_face_count": len(invalid_faces),
            "violations": violations[:witness_limit],
            "violations_truncated": len(violations) > witness_limit,
            "overlap_measurement": "per_tile_128px_mask_and_triangle_intersections",
        },
    }


def _signed_twice_area(points):
    return sum(a[0] * b[1] - a[1] * b[0] for a, b in zip(points, (*points[1:], points[0])))


def _intersection_area(first, second):
    polygon = list(first)
    second = list(second)
    if _signed_twice_area(second) < 0:
        second.reverse()
    for a, b in zip(second, (*second[1:], second[0])):
        if not polygon:
            break
        start = polygon[-1]
        dx, dy = b[0] - a[0], b[1] - a[1]
        start_side = dx * (start[1] - a[1]) - dy * (start[0] - a[0])
        clipped = []
        for end in polygon:
            end_side = dx * (end[1] - a[1]) - dy * (end[0] - a[0])
            if (start_side >= 0) != (end_side >= 0):
                clipped.append(start + (end - start) * (start_side / (start_side - end_side)))
            if end_side >= 0:
                clipped.append(end)
            start, start_side = end, end_side
        polygon = clipped
    return abs(_signed_twice_area(polygon)) * 0.5 if len(polygon) >= 3 else 0.0


def _coverage(triangles, witness_limit):
    grid = 128
    owner = np.full((grid, grid), -1, dtype=np.int32)
    overlaps = np.zeros_like(owner, dtype=np.bool_)
    entries = []
    for face, tri in triangles:
        denominator = _signed_twice_area(tri)
        if abs(denominator) <= AREA_TOLERANCE:
            continue
        low, high = tri.min(axis=0), tri.max(axis=0)
        entries.append((low, high, face, tri))
        x0, y0 = np.maximum(0, np.floor(low * grid).astype(int))
        x1, y1 = np.minimum(grid, np.ceil(high * grid).astype(int))
        x = (np.arange(x0, x1)[None, :] + 0.5) / grid
        y = (np.arange(y0, y1)[:, None] + 0.5) / grid
        first, second, third = tri
        u = ((second[0] - x) * (third[1] - y) - (second[1] - y) * (third[0] - x)) / denominator
        v = ((third[0] - x) * (first[1] - y) - (third[1] - y) * (first[0] - x)) / denominator
        inside = (u > 0) & (v > 0) & (u + v < 1)
        cells = owner[y0:y1, x0:x1]
        overlaps[y0:y1, x0:x1] |= inside & (cells >= 0) & (cells != face)
        cells[inside & (cells == -1)] = face

    pair_areas = defaultdict(float)
    active = []
    comparisons = 0
    complete = True
    for low, high, face, tri in sorted(entries, key=lambda entry: entry[0][0]):
        active = [item for item in active if item[1][0] > low[0]]
        for old_low, old_high, old_face, old_tri in active:
            comparisons += 1
            if comparisons > 200_000:
                complete = False
                break
            if old_face != face and old_high[1] > low[1] and high[1] > old_low[1]:
                area = _intersection_area(tri, old_tri)
                if area > AREA_TOLERANCE:
                    pair_areas[tuple(sorted((face, old_face)))] += area
        if not complete:
            break
        active.append((low, high, face, tri))
    covered_count = int((owner >= 0).sum())
    overlap_count = int(overlaps.sum())
    pairs = sorted(pair_areas.items(), key=lambda item: (-item[1], item[0]))
    return {
        "covered_cells": covered_count, "overlap_cells": overlap_count,
        "overlap_pct": round(100 * overlap_count / max(1, covered_count), 3),
        "intersection_check_complete": complete,
        "overlap_pairs": [
            {"polygon_indices": list(faces), "intersection_uv_area": round(area, 10)}
            for faces, area in pairs[:witness_limit]
        ],
        "overlap_pair_count": len(pairs),
    }


def inspect_mesh(mesh, uv_layer, *, allowed, resolution, witness_limit):
    uv_data = np.empty(len(mesh.loops) * 2, dtype=np.float32)
    uv_layer.data.foreach_get("uv", uv_data)
    uvs = uv_data.reshape((-1, 2)).astype(np.float64)
    faces = [(poly.index, tuple(poly.vertices), uvs[list(poly.loop_indices)]) for poly in mesh.polygons]
    triangles = [(tri.polygon_index, uvs[list(tri.loops)]) for tri in mesh.loop_triangles]
    return _measure_tiles(faces, triangles, allowed, resolution, witness_limit)


def inspect_bmesh(bm, uv_layer, *, allowed, resolution, witness_limit):
    def uv_array(loops):
        return np.asarray([tuple(loop[uv_layer].uv) for loop in loops], dtype=np.float64)

    faces = [(face.index, tuple(v.index for v in face.verts), uv_array(face.loops)) for face in bm.faces]
    triangles = [(tri[0].face.index, uv_array(tri)) for tri in bm.calc_loop_triangles()]
    return _measure_tiles(faces, triangles, allowed, resolution, witness_limit)
