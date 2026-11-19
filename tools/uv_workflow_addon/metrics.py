"""Independent NumPy UV measurements; no bpy or audit/runtime dependencies.

Positions are WORLD metres; UV is per triangle corner. Names must be unique.
All triangle counts include invalid input. Incomplete/invalid aggregates are None,
never a statistic over a silently reduced denominator. Negative UV winding alone
is diagnostic, not failure. The default usage=None measures exclusive single-tile
UV exactly as version 0.2.3 did, including its output schema.

The keyword-only usage argument adds explicit intent semantics:
  None
      Legacy EXCLUSIVE single-tile behaviour and output, unchanged.
  {"mode": "EXCLUSIVE", "reuse_groups": [...]}
      Different numeric islands inside one declared reuse group form one layout
      unit; positive intersections between them are declared. A single island
      folding onto itself is always undeclared. Intersections between different
      layout units are undeclared. Unit-tile out-of-bounds still fails.
  {"mode": "TILED"}
      Every positive intersection between different numeric islands is declared
      (global repeated-tile usage); a single island folding onto itself is still
      undeclared. Out-of-bounds corners are counted but legal tiling does not
      fail; tile boundary/gap are not_applicable. boundary_margin_px remains the
      original signed unit-tile diagnostic and may be negative.

Reuse groups are validated strictly: nonempty unique string ids, at least two
existing (object, island) members, no repeated member, and each island in at
most one group. TILED does not accept nonempty reuse_groups (an empty list is
equivalent to omission).

No fixed overlap-area epsilon is used: strictly positive intersections count.
An adaptive orient2d predicate falls back to exact rational input arithmetic near
round-off; small intersection areas also use exact clipping. Pixel margins/gaps
are Euclidean pixels after scaling U by width and V by height. Boundary margin is
signed (negative outside the unit tile). Pairwise intersection SUM is not UNION;
raw positive counts/areas are partitioned into declared and undeclared over the
full positive-pair loop, never by filtering the 16 display examples.

Gap is measured between distinct layout units. Members of one declared reuse
group are one unit and are never measured against each other. A declared
within-group overlap never proves an external gap of zero; only an unexpected
cross-layout positive intersection proves that external gap is zero. Per-island
area/density records remain unmerged.

pair_budget bounds spatial primitive-pair candidates separately for overlap and gap.
For explicit EXCLUSIVE reuse with identical triangle coordinates, the overlap
budget counts distinct geometric class-pair tests; original pair counts/area SUM
are recovered with integer multiplicities. The report distinguishes both counts.
Legacy usage=None, TILED and empty reuse-group paths retain their prior counters.
Index node-pair visits have an additional max(64, pair_budget) guard.
Gap uses topology/UV boundary segments and requires containment to be resolved.
Cancellation is polled during the linear input census and spatial work. A truthy
return stops spatial work as unknown after completing required input counts;
callback exceptions propagate unchanged. No input array is modified.
"""
from __future__ import annotations

from bisect import bisect_right
from fractions import Fraction
import math
import operator

import numpy as np

_EPS = np.finfo(np.float64).eps


class _Cancel:
    def __init__(self, callback):
        if callback is not None and not callable(callback):
            raise TypeError("cancel must be callable or None")
        self.callback = callback
        self.requested = False

    def poll(self):
        if not self.requested and self.callback is not None:
            self.requested = bool(self.callback())
        return self.requested


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _exact_cross(a, b, c):
    ax, ay = (Fraction(float(x)) for x in a)
    bx, by = (Fraction(float(x)) for x in b)
    cx, cy = (Fraction(float(x)) for x in c)
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _orient(a, b, c):
    """Sign of orient2d on represented input floats, without a distance epsilon."""
    if tuple(c) == tuple(a) or tuple(c) == tuple(b):
        return 0
    left = (b[0] - a[0]) * (c[1] - a[1])
    right = (b[1] - a[1]) * (c[0] - a[0])
    det = left - right
    bound = 8 * _EPS * (abs(left) + abs(right))
    if math.isfinite(det) and abs(det) > bound:
        return 1 if det > 0 else -1
    exact = _exact_cross(a, b, c)
    return (exact > 0) - (exact < 0)


def _ccw(points):
    p = [tuple(float(v) for v in row) for row in points]
    return p if _orient(*p) > 0 else [p[0], p[2], p[1]]


def _positive_intersection(a, b):
    # Strict separating-axis test: a shared edge/vertex has zero area.
    for triangle, other in ((a, b), (b, a)):
        for p, q in zip(triangle, triangle[1:] + triangle[:1]):
            if not any(_orient(p, q, r) > 0 for r in other):
                return False
    return True


def _clip_area_exact(a, b):
    polygon = [tuple(Fraction(float(v)) for v in p) for p in a]
    clip = [tuple(Fraction(float(v)) for v in p) for p in b]
    for x, y in zip(clip, clip[1:] + clip[:1]):
        result = []
        for p, q in zip(polygon, polygon[1:] + polygon[:1]):
            dp, dq = _cross(x, y, p), _cross(x, y, q)
            if dp >= 0:
                result.append(p)
            if (dp >= 0) != (dq >= 0):
                t = dp / (dp - dq)
                result.append(tuple(p[k] + t * (q[k] - p[k]) for k in (0, 1)))
        polygon = result
        if len(polygon) < 3:
            return 0.0
    area = abs(sum((_cross(polygon[0], polygon[i], polygon[i + 1])
                    for i in range(1, len(polygon) - 1)), Fraction(0))) / 2
    return float(area)


def _intersection_area(a, b):
    polygon = list(a)
    for x, y in zip(b, b[1:] + b[:1]):
        result = []
        for p, q in zip(polygon, polygon[1:] + polygon[:1]):
            sp, sq = _orient(x, y, p), _orient(x, y, q)
            if sp >= 0:
                result.append(p)
            if (sp >= 0) != (sq >= 0):
                dp, dq = _cross(x, y, p), _cross(x, y, q)
                if dp == dq or (dp == 0 and sp != 0) or (dq == 0 and sq != 0):
                    return _clip_area_exact(a, b)
                t = dp / (dp - dq)
                result.append(tuple(p[k] + t * (q[k] - p[k]) for k in (0, 1)))
        polygon = result
        if len(polygon) < 3:
            return _clip_area_exact(a, b)
    area = abs(math.fsum(_cross(polygon[0], polygon[i], polygon[i + 1])
                         for i in range(1, len(polygon) - 1))) / 2
    extent = max(max(p[k] for p in a + b) - min(p[k] for p in a + b)
                 for k in (0, 1))
    # This bound chooses exact computation, NEVER discards an intersection.
    if not math.isfinite(area) or area <= 64 * _EPS * extent * extent:
        return _clip_area_exact(a, b)
    return area


def _point_segment(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return math.dist(p, a)
    ux, uy = dx / length, dy / length
    t = min(length, max(0.0, (p[0] - a[0]) * ux + (p[1] - a[1]) * uy))
    return math.hypot(p[0] - a[0] - t * ux, p[1] - a[1] - t * uy)


def _segment_gap(a, b, c, d):
    signs = _orient(a, b, c), _orient(a, b, d), _orient(c, d, a), _orient(c, d, b)
    if signs[0] * signs[1] < 0 and signs[2] * signs[3] < 0:
        return 0.0
    # Endpoint-on-segment and collinear contact need no length tolerance.
    for sign, p, q, r in ((signs[0], c, a, b), (signs[1], d, a, b),
                          (signs[2], a, c, d), (signs[3], b, c, d)):
        if sign == 0 and all(min(q[k], r[k]) <= p[k] <= max(q[k], r[k]) for k in (0, 1)):
            return 0.0
    return min(_point_segment(a, c, d), _point_segment(b, c, d),
               _point_segment(c, a, b), _point_segment(d, a, b))


def _triangle_gap(a, b):
    for p, triangle in ((a[0], b), (b[0], a)):
        if all(_orient(x, y, p) >= 0 for x, y in zip(triangle, triangle[1:] + triangle[:1])):
            return 0.0  # Containment: boundary-only distance would be misleading.
    return min(_segment_gap(p, q, r, s)
               for p, q in zip(a, a[1:] + a[:1])
               for r, s in zip(b, b[1:] + b[:1]))


def _validate(mesh):
    name = mesh["name"]
    if not isinstance(name, str) or not name:
        raise ValueError("mesh name must be a nonempty unique string")
    positions = np.asarray(mesh["positions"], dtype=np.float64)
    triangles = np.asarray(mesh["triangles"])
    uv = np.asarray(mesh["uv"], dtype=np.float64)
    islands = np.asarray(mesh["islands"])
    if not positions.size:
        positions = positions.reshape(0, 3)
    if not triangles.size:
        triangles = np.empty((0, 3), dtype=np.int64)
    if not uv.size:
        uv = uv.reshape(0, 3, 2)
    if not islands.size:
        islands = np.empty(0, dtype=np.int64)
    if positions.ndim != 2 or positions.shape[1:] != (3,):
        raise ValueError("positions must be Nx3")
    if triangles.ndim != 2 or triangles.shape[1:] != (3,) or triangles.dtype.kind not in "iu":
        raise ValueError("triangles must be Tx3 integer indices")
    if uv.shape != (len(triangles), 3, 2):
        raise ValueError("uv must be Tx3x2")
    if islands.shape != (len(triangles),) or islands.dtype.kind not in "iu":
        raise ValueError("islands must be T integers")
    if triangles.size and (triangles.min() < 0 or triangles.max() >= len(positions)):
        raise ValueError("triangle index outside positions")
    return name, positions, triangles.astype(np.int64, copy=False), uv, islands


_USAGE_MODES = ("EXCLUSIVE", "TILED")


def _validate_usage(usage):
    """Validate usage structure; membership existence is checked after census."""
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise ValueError("usage must be None or a dict")
    if "mode" not in usage:
        raise ValueError("usage.mode is required")
    mode = usage["mode"]
    if not isinstance(mode, str) or mode not in _USAGE_MODES:
        raise ValueError("usage.mode must be 'EXCLUSIVE' or 'TILED'")
    groups = usage.get("reuse_groups", [])
    if not isinstance(groups, list):
        raise ValueError("usage.reuse_groups must be a list")
    if mode == "TILED" and groups:
        raise ValueError("TILED usage does not allow reuse_groups")
    normalized = []
    seen_ids = set()
    seen_members = set()
    for position, group in enumerate(groups):
        label = "usage.reuse_groups[%d]" % position
        if not isinstance(group, dict):
            raise ValueError(label + " must be a dict")
        if "id" not in group or "members" not in group:
            raise ValueError(label + " requires id and members")
        group_id = group["id"]
        if not isinstance(group_id, str) or not group_id:
            raise ValueError(label + ".id must be a nonempty string")
        if group_id in seen_ids:
            raise ValueError("reuse_group ids must be unique: " + group_id)
        seen_ids.add(group_id)
        members = group["members"]
        if not isinstance(members, list):
            raise ValueError(label + ".members must be a list")
        if len(members) < 2:
            raise ValueError(label + " must contain at least 2 members")
        resolved_members = []
        for member_position, member in enumerate(members):
            member_label = label + ".members[%d]" % member_position
            if not isinstance(member, dict):
                raise ValueError(member_label + " must be a dict")
            if "object" not in member or "island" not in member:
                raise ValueError(member_label + " requires object and island")
            object_name = member["object"]
            if not isinstance(object_name, str) or not object_name:
                raise ValueError(member_label + ".object must be a nonempty string")
            island_value = member["island"]
            if isinstance(island_value, (bool, np.bool_)):
                raise ValueError(member_label + ".island must be an integer")
            try:
                island_index = operator.index(island_value)
            except TypeError as exc:
                raise ValueError(member_label + ".island must be an integer") from exc
            key = (object_name, int(island_index))
            if key in seen_members:
                raise ValueError(
                    "each (object, island) may appear only once across reuse_groups: "
                    + object_name + "[" + str(int(island_index)) + "]"
                )
            seen_members.add(key)
            resolved_members.append({"object": object_name, "island": int(island_index)})
        normalized.append({"id": group_id, "members": resolved_members})
    return {"mode": mode, "reuse_groups": normalized}


def _resolve_usage(usage, group_records):
    """Check member existence and assign layout ids independent of island stats."""
    if usage is None:
        return None
    lookup = {(record["object"], record["island"]): index
              for index, record in enumerate(group_records)}
    layout_ids = np.arange(len(group_records), dtype=np.int64)
    next_layout_id = len(group_records)
    resolved_groups = []
    for group in usage["reuse_groups"]:
        numeric_ids = []
        for member in group["members"]:
            key = (member["object"], member["island"])
            if key not in lookup:
                raise ValueError(
                    "reuse_group %r member does not exist: %s[%d]"
                    % (group["id"], member["object"], member["island"])
                )
            numeric_ids.append(lookup[key])
        for numeric_id in numeric_ids:
            layout_ids[numeric_id] = next_layout_id
        next_layout_id += 1
        resolved_groups.append({
            "id": group["id"],
            "members": [dict(member) for member in group["members"]],
            "layout_id": next_layout_id - 1,
        })
    layout_unit_count = len({int(value) for value in layout_ids})
    return {"mode": usage["mode"], "reuse_groups": resolved_groups,
            "layout_ids": layout_ids, "layout_unit_count": layout_unit_count}


def _usage_report(usage):
    if usage["mode"] == "TILED":
        return {
            "mode": "TILED",
            "reuse_groups": [],
            "layout_unit_count": None,
            "layout_units": "not_applicable",
            "tile_boundary": "not_applicable",
            "gap": "not_applicable",
            "declared_rule": (
                "all positive intersections between different numeric islands are "
                "declared; same-island self intersections remain undeclared"
            ),
        }
    return {
        "mode": "EXCLUSIVE",
        "reuse_groups": [
            {"id": group["id"], "members": [dict(member) for member in group["members"]]}
            for group in usage["reuse_groups"]
        ],
        "layout_unit_count": usage["layout_unit_count"],
        "layout_units": "declared reuse groups plus one unit per ungrouped numeric island",
        "tile_boundary": "unit_tile_out_of_bounds_fails",
        "gap": "measured_between_distinct_layout_units",
        "declared_rule": (
            "different numeric islands in the same declared reuse group are "
            "declared; same-island self intersections and all cross-layout "
            "intersections are undeclared"
        ),
    }


def _measure_chunk(points, pixels):
    with np.errstate(all="ignore"):
        e1, e2 = points[:, 1] - points[:, 0], points[:, 2] - points[:, 0]
        scale = np.maximum(np.abs(e1).max(axis=1), np.abs(e2).max(axis=1))
        safe = np.where((scale > 0) & np.isfinite(scale), scale, 1)
        p, q = e1 / safe[:, None], e2 / safe[:, None]
        cross = np.linalg.norm(np.cross(p, q), axis=1)
        norm = np.linalg.norm(p, axis=1)
        area = (cross * scale) * (scale / 2)
        x1 = norm * scale
        x2 = (p * q).sum(axis=1) / norm * scale
        y2 = cross / norm * scale
        good_g = (np.isfinite(points).all(axis=(1, 2)) & np.isfinite(e1).all(axis=1)
                  & np.isfinite(e2).all(axis=1) & np.isfinite(area) & (area > 0)
                  & np.isfinite(x1) & (x1 > 0) & np.isfinite(x2)
                  & np.isfinite(y2) & (y2 > 0))
        a, b = pixels[:, 1] - pixels[:, 0], pixels[:, 2] - pixels[:, 0]
        left, right = a[:, 0] * b[:, 1], a[:, 1] * b[:, 0]
        det = left - right
    finite_uv = np.isfinite(pixels).all(axis=(1, 2))
    uncertain = np.flatnonzero(finite_uv & (np.abs(det) <= 8 * _EPS * (np.abs(left) + np.abs(right))))
    for i in uncertain:
        det[i] = float(_exact_cross(*pixels[i]))
    uv_area = np.abs(det) / 2
    good_u = finite_uv & np.isfinite(uv_area) & (uv_area > 0)
    ratios = np.full(len(points), np.nan)
    valid = np.flatnonzero(good_g & good_u)
    if len(valid):
        with np.errstate(all="ignore"):
            first = a[valid] / x1[valid, None]
            second = (b[valid] - first * x2[valid, None]) / y2[valid, None]
            matrices = np.stack((first, second), axis=2)
        finite_j = np.isfinite(matrices).all(axis=(1, 2))
        if finite_j.any():
            singular = np.linalg.svd(matrices[finite_j], compute_uv=False)
            with np.errstate(all="ignore"):
                values = singular[:, 0] / singular[:, 1]
            ratios[valid[finite_j]] = values
    good_j = np.isfinite(ratios) & (ratios >= 1)
    return area, uv_area, good_g, good_u, ratios, good_j, det


class _SpatialCancelled(Exception):
    pass


class _Node:
    __slots__ = ("box", "ids", "left", "right", "count", "group")

    def __init__(self, box, ids, left=None, right=None, group=None):
        self.box, self.ids = box, ids
        self.left, self.right = left, right
        self.count = len(ids) if ids is not None else left.count + right.count
        self.group = group


def _boxes(primitives):
    return np.concatenate((primitives.min(axis=1), primitives.max(axis=1)), axis=1)


def _tree(boxes, ids, groups, cancel):
    """Balanced AABB hierarchy: wide primitives stay whole, never clipped/dropped."""
    built = 0

    def build(indices):
        nonlocal built
        built += 1
        if built % 128 == 1 and cancel.poll():
            raise _SpatialCancelled()
        selected = boxes[indices]
        box = tuple(selected[:, :2].min(axis=0)) + tuple(selected[:, 2:].max(axis=0))
        labels = groups[indices]
        group = int(labels[0]) if np.all(labels == labels[0]) else None
        if len(indices) <= 8:
            return _Node(box, tuple(int(i) for i in indices), group=group)
        centers = selected[:, :2] * .5 + selected[:, 2:] * .5
        axis = int(np.argmax(np.ptp(centers, axis=0)))
        order = np.argsort(centers[:, axis], kind="stable")
        middle = len(order) // 2
        left = build(indices[order[:middle]])
        right = build(indices[order[middle:]])
        return _Node(box, None, left, right, group)

    return build(np.asarray(ids, dtype=np.int64))


def _box_intersects(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _box_gap(a, b):
    return math.hypot(max(a[0] - b[2], b[0] - a[2], 0.0),
                      max(a[1] - b[3], b[1] - a[3], 0.0))


def _leaf_pairs(a, b):
    if a is b:
        for k, i in enumerate(a.ids):
            for j in a.ids[:k]:
                yield j, i
    else:
        for i in a.ids:
            for j in b.ids:
                yield i, j


def _classification_fields(usage, *, declared, undeclared, declared_area, undeclared_area,
                           declared_examples, undeclared_examples, cross_layout, incomplete):
    if usage["mode"] == "TILED":
        declared_rule = (
            "TILED: positive intersections between different numeric islands are "
            "declared; same-island self intersections are undeclared"
        )
        cross_layout_pairs = None
    else:
        declared_rule = (
            "EXCLUSIVE: positive intersections between different numeric islands "
            "in the same declared reuse group are declared; same-island self "
            "intersections and all cross-layout intersections are undeclared"
        )
        cross_layout_pairs = cross_layout
    return {
        "declared_positive_pairs": declared,
        "undeclared_positive_pairs": undeclared,
        "declared_pairwise_area_px2": declared_area,
        "undeclared_pairwise_area_px2": undeclared_area,
        "declared_examples": declared_examples,
        "undeclared_examples": undeclared_examples,
        "cross_layout_positive_pairs": cross_layout_pairs,
        "classification_complete": not incomplete,
        "classification_area_is_partial": incomplete,
        "classification_basis": (
            "all positive pairs encountered by the candidate loop, not the "
            "examples; the examples lists are capped for display and are never "
            "the classification input; classification_complete=false means the "
            "loop stopped early"
        ),
        "declared_rule": declared_rule,
        "area_semantics": (
            "pairwise SUM, not UNION; positive_pairs/pairwise_area_px2 are the raw "
            "total and are partitioned into declared and undeclared sums"
        ),
    }


def _overlap_uncompressed(pixels, valid_uv, locations, groups, budget, cancel, usage=None,
             layout_groups=None):
    ids = np.flatnonzero(valid_uv)
    partial_input = not bool(valid_uv.all())
    checked = positive = cross_island = visits = 0
    areas, examples = [], []
    classified = usage is not None
    if classified:
        declared = undeclared = cross_layout = 0
        declared_areas, undeclared_areas = [], []
        declared_examples, undeclared_examples = [], []
    reason = "invalid_uv_triangles" if partial_input else None
    interrupted = False
    if len(ids):
        boxes = _boxes(pixels)
        try:
            root = _tree(boxes, ids, groups, cancel)
        except _SpatialCancelled:
            root = None; interrupted = True; reason = "cancelled"
        stack = [(root, root)] if root else []
        while stack:
            a, b = stack.pop()
            visits += 1
            if visits % 256 == 1 and cancel.poll():
                interrupted = True; reason = "cancelled"; break
            if visits > max(64, budget):
                interrupted = True; reason = "index_visit_budget"; break
            if not _box_intersects(a.box, b.box):
                continue
            if a.ids is not None and b.ids is not None:
                for i, j in _leaf_pairs(a, b):
                    # Hierarchy/AABB separation is a proof, not an omitted face.
                    if not _box_intersects(boxes[i], boxes[j]):
                        continue
                    if checked >= budget:
                        interrupted = True; reason = "pair_budget"; break
                    checked += 1
                    if checked % 256 == 0 and cancel.poll():
                        interrupted = True; reason = "cancelled"; break
                    first, second = _ccw(pixels[i]), _ccw(pixels[j])
                    if _positive_intersection(first, second):
                        positive += 1
                        cross_island += int(groups[i] != groups[j])
                        value = _intersection_area(first, second)
                        areas.append(value)
                        if len(examples) < 16:
                            examples.append({"first": locations(i), "second": locations(j), "area_px2": value})
                        if classified:
                            if usage["mode"] == "TILED":
                                is_declared = groups[i] != groups[j]
                                is_cross_layout = None
                            else:
                                same_layout = layout_groups[i] == layout_groups[j]
                                is_declared = groups[i] != groups[j] and same_layout
                                is_cross_layout = not same_layout
                            example = {"first": locations(i), "second": locations(j), "area_px2": value}
                            if is_declared:
                                declared += 1
                                declared_areas.append(value)
                                if len(declared_examples) < 16:
                                    declared_examples.append(example)
                            else:
                                undeclared += 1
                                undeclared_areas.append(value)
                                if len(undeclared_examples) < 16:
                                    undeclared_examples.append(example)
                            if is_cross_layout:
                                cross_layout += 1
                if interrupted:
                    break
            elif a is b:
                stack.extend(((a.right, a.right), (a.left, a.right), (a.left, a.left)))
            elif b.ids is not None or (a.ids is None and a.count >= b.count):
                stack.extend(((a.right, b), (a.left, b)))
            else:
                stack.extend(((a, b.right), (a, b.left)))
    incomplete = partial_input or interrupted
    failed_pairs = undeclared if classified else positive
    if incomplete:
        status = "unknown"
    else:
        status = "fail" if failed_pairs else "pass"
    if classified and not incomplete and failed_pairs:
        reason = "undeclared_positive_pairs"
    result = {"status": status, "reason": reason, "positive_pairs": positive,
              "cross_island_positive_pairs": cross_island,
              "pairwise_area_px2": math.fsum(areas), "checked_pairs": checked,
              "exact_clips": checked, "index_node_pair_visits": visits,
              "examples": examples, "complete": not incomplete,
              "area_is_partial": incomplete,
              "candidate_semantics": "unique primitive pairs with positive-area AABB intersection; balanced hierarchy excludes proven-separated pairs",
              "semantics": "sum of pairwise positive intersections, not union; same-island and adjacent pairs included"}
    if not classified:
        return result
    result.update(_classification_fields(
        usage, declared=declared, undeclared=undeclared,
        declared_area=math.fsum(declared_areas),
        undeclared_area=math.fsum(undeclared_areas),
        declared_examples=declared_examples,
        undeclared_examples=undeclared_examples,
        cross_layout=cross_layout, incomplete=incomplete,
    ))
    result["semantics"] = (
        "sum of pairwise positive intersections, not union; same-island and "
        "adjacent pairs included; raw positive_pairs/pairwise_area_px2 are "
        "partitioned into declared and undeclared over the full loop"
    )
    return result


def _overlap(pixels, valid_uv, locations, groups, budget, cancel, usage=None,
             layout_groups=None):
    """Count identical UV primitives once, retaining original pair multiplicity.

    Only explicit EXCLUSIVE reuse enables this path. Keys contain the layout
    identity and all three exact represented points: chart similarity, equal
    area, vertex count, or approximate coordinates never establish equivalence.
    Numeric-island multiplicities preserve same-island self-fold failures.
    """
    if usage is None or usage['mode'] != 'EXCLUSIVE' or not usage['reuse_groups']:
        return _overlap_uncompressed(pixels, valid_uv, locations, groups, budget,
                                     cancel, usage, layout_groups)
    classes = []
    lookup = {}
    ids = np.flatnonzero(valid_uv)
    for ordinal, index in enumerate(ids):
        if ordinal % 2048 == 0 and cancel.poll():
            return _overlap_uncompressed(pixels, valid_uv, locations, groups,
                                         budget, cancel, usage, layout_groups)
        # tuple equality intentionally treats +0/-0 alike, like the predicates.
        points = tuple(sorted(tuple(map(float, p)) for p in pixels[index]))
        key = (int(layout_groups[index]), points)
        ci = lookup.get(key)
        if ci is None:
            ci = len(classes); lookup[key] = ci
            classes.append({'points': points, 'layout': key[0], 'size': 0,
                            'members': {}})
        row = classes[ci]; row['size'] += 1
        member = row['members'].get(int(groups[index]))
        if member is None:
            row['members'][int(groups[index])] = [1, int(index), None]
        else:
            member[0] += 1
            if member[2] is None: member[2] = int(index)
    if len(classes) == len(ids):
        return _overlap_uncompressed(pixels, valid_uv, locations, groups, budget,
                                     cancel, usage, layout_groups)

    positive = cross_island = declared = undeclared = cross_layout = 0
    checked = represented = visits = area_evaluations = 0
    raw_area = declared_area = undeclared_area = Fraction(0)
    examples, declared_examples, undeclared_examples = [], [], []
    partial_input = not bool(valid_uv.all())
    reason = 'invalid_uv_triangles' if partial_input else None
    interrupted = False

    class Stop(Exception): pass

    def example_pair(a, b, same_numeric):
        """One concrete original pair, without expanding the alias product."""
        if same_numeric:
            for group, left in a['members'].items():
                right = b['members'].get(group)
                if right is not None and (a is not b or left[0] > 1):
                    return left[1], left[2] if a is b else right[1]
        else:
            ag, left = next(iter(a['members'].items()))
            for bg, right in b['members'].items():
                if ag != bg: return left[1], right[1]
            bg, right = next(iter(b['members'].items()))
            for ag, left in a['members'].items():
                if ag != bg: return left[1], right[1]
        raise AssertionError('Positive multiplicity has no concrete pair')

    def append_example(target, pair, value):
        if len(target) < 16:
            i, j = pair
            target.append({'first': locations(i), 'second': locations(j),
                           'area_px2': value})

    def process(i, j):
        nonlocal checked, represented, positive, cross_island, declared
        nonlocal undeclared, cross_layout, raw_area, declared_area, undeclared_area
        nonlocal area_evaluations
        if checked >= budget: raise Stop('pair_budget')
        checked += 1
        if checked % 256 == 0 and cancel.poll(): raise Stop('cancelled')
        a, b = classes[i], classes[j]
        if i == j:
            count = a['size'] * (a['size'] - 1) // 2
            same_numeric = sum(m[0] * (m[0] - 1) // 2 for m in a['members'].values())
        else:
            count = a['size'] * b['size']
            small, large = (a, b) if len(a['members']) < len(b['members']) else (b, a)
            same_numeric = sum(m[0] * large['members'][g][0]
                               for g, m in small['members'].items() if g in large['members'])
        represented += count
        first, second = _ccw(a['points']), _ccw(b['points'])
        if not _positive_intersection(first, second): return
        value = _intersection_area(first, second)
        area_evaluations += 1
        between_islands = count - same_numeric
        same_layout = a['layout'] == b['layout']
        allowed = between_islands if same_layout else 0
        forbidden = count - allowed
        positive += count; cross_island += between_islands
        declared += allowed; undeclared += forbidden
        cross_layout += 0 if same_layout else count
        # Exact weighted sum of the computed binary64 clipping areas avoids
        # rounding a large count*area subtotal before combining class pairs.
        exact_value = Fraction.from_float(value)
        raw_area += exact_value * count
        declared_area += exact_value * allowed
        undeclared_area += exact_value * forbidden
        pair = example_pair(a, b, same_numeric > 0)
        append_example(examples, pair, value)
        if allowed:
            append_example(declared_examples, example_pair(a, b, False), value)
        if forbidden:
            pair = example_pair(a, b, same_layout)
            append_example(undeclared_examples, pair, value)

    primitive_pixels = np.asarray([row['points'] for row in classes], dtype=np.float64).reshape(-1,3,2)
    boxes = _boxes(primitive_pixels)
    try:
        # A class represents distinct original pairs even when the hierarchy
        # contains only one geometric primitive. These pairs consume real work.
        for i, row in enumerate(classes):
            if row['size'] > 1: process(i, i)
        if classes:
            labels = np.asarray([row['layout'] for row in classes], dtype=np.int64)
            root = _tree(boxes, np.arange(len(classes)), labels, cancel)
            stack = [(root, root)]
            while stack:
                a, b = stack.pop(); visits += 1
                if visits % 256 == 1 and cancel.poll(): raise Stop('cancelled')
                if visits > max(64, budget): raise Stop('index_visit_budget')
                if not _box_intersects(a.box, b.box): continue
                if a.ids is not None and b.ids is not None:
                    for i, j in _leaf_pairs(a, b):
                        if _box_intersects(boxes[i], boxes[j]): process(i, j)
                elif a is b:
                    stack.extend(((a.right,a.right),(a.left,a.right),(a.left,a.left)))
                elif b.ids is not None or (a.ids is None and a.count >= b.count):
                    stack.extend(((a.right,b),(a.left,b)))
                else:
                    stack.extend(((a,b.right),(a,b.left)))
    except _SpatialCancelled:
        interrupted = True; reason = 'cancelled'
    except Stop as exc:
        interrupted = True; reason = str(exc)
    incomplete = partial_input or interrupted
    if not incomplete and undeclared: reason = 'undeclared_positive_pairs'
    result = dict(status='unknown' if incomplete else 'fail' if undeclared else 'pass',
        reason=reason,positive_pairs=positive,cross_island_positive_pairs=cross_island,
        pairwise_area_px2=float(raw_area),checked_pairs=checked,exact_clips=checked,
        index_node_pair_visits=visits,examples=examples,complete=not incomplete,
        area_is_partial=incomplete,
        candidate_semantics='exact triangle-coordinate classes within explicit layout identities; original pair multiplicities retained',
        semantics='pairwise SUM, not UNION; original same-island folds and all cross-layout intersections remain undeclared',
        duplicate_primitive_compression=dict(valid_input_triangles=len(ids),spatial_classes=len(classes),
            represented_candidate_pairs=represented,area_evaluations=area_evaluations,
            exact_clips_field='legacy alias for candidate tests, not the number of exact-rational fallbacks',
            work_budget_counts='geometrically distinct candidate class pairs, including duplicate-class self pairs'))
    result.update(_classification_fields(usage,declared=declared,undeclared=undeclared,
        declared_area=float(declared_area),undeclared_area=float(undeclared_area),
        declared_examples=declared_examples,undeclared_examples=undeclared_examples,
        cross_layout=cross_layout,incomplete=incomplete))
    result['classification_basis']='all original primitive pairs represented by tested exact-coordinate class pairs and numeric-island multiplicities; incomplete results remain partial'
    return result


def _boundary_segments(pixels, valid_uv, groups, topology, cancel):
    """Remove only proven internal edges, using topology AND exact UV endpoints.

    A two-face edge is internal only when its two third vertices lie on opposite
    UV sides. Folded same-side edges and nonmanifold incidence are retained.
    Keeping additional seams is conservative for filled-island minimum distance.
    """
    incidences = {}
    for n, i in enumerate(np.flatnonzero(valid_uv)):
        if n % 2048 == 0 and cancel.poll():
            raise _SpatialCancelled()
        points = pixels[i]
        orientation = _orient(*points)
        group = int(groups[i])
        for k in range(3):
            other = (k + 1) % 3
            va, vb = int(topology[i, k]), int(topology[i, other])
            a, b = tuple(points[k]), tuple(points[other])
            side = orientation
            if va > vb or (va == vb and a > b):
                va, vb, a, b, side = vb, va, b, a, -side
            key = (group, va, vb, a, b)
            if key not in incidences:
                incidences[key] = [1, side, False]
            else:
                row = incidences[key]
                row[0] += 1
                row[2] = row[0] == 2 and side * row[1] < 0
    unique = {}
    for n, ((group, _, _, a, b), row) in enumerate(incidences.items()):
        if n % 4096 == 0 and cancel.poll():
            raise _SpatialCancelled()
        if row[2]:
            continue
        first, second = (a, b) if a <= b else (b, a)
        unique[(group, first, second)] = None
    segments = np.array([[a, b] for group, a, b in unique], dtype=np.float64).reshape(-1, 2, 2)
    labels = np.array([group for group, a, b in unique], dtype=np.int64)
    return segments, labels


def _gap(pixels, valid_uv, groups, layout_ids_by_group, topology, overlap, budget,
         cancel, usage=None):
    import heapq
    import itertools

    ids = np.flatnonzero(valid_uv)
    bad_input = not bool(valid_uv.all())
    base = {"checked_pairs": 0, "exact_distances": 0, "index_node_pair_visits": 0,
            "boundary_segments": 0, "triangle_edges_before_boundary_filter": int(len(ids) * 3)}
    classified = usage is not None

    def finish(result, unit_count):
        if not classified:
            return result
        result = dict(result)
        result.update({
            "applicable": True,
            "layout_unit_count": unit_count,
            "within_group_gap": "not_measured",
            "layout_semantics": (
                "minimum boundary distance between distinct declared layout units; "
                "members of one reuse group are one unit and never measured against "
                "each other; declared internal overlap does not prove external gap 0"
            ),
        })
        return result

    if classified and usage["mode"] == "TILED":
        return {
            **base, "status": "not_applicable", "reason": "tiled_usage",
            "min_px": None, "complete": False, "minimum_proven": False,
            "applicable": False, "layout_unit_count": None,
            "within_group_gap": "not_measured",
            "layout_semantics": (
                "tile boundary and inter-tile gap are not_applicable for TILED; "
                "boundary_margin_px remains the original signed unit-tile diagnostic"
            ),
        }
    layout_groups = layout_ids_by_group[groups]
    layout_ids = layout_groups[ids]
    layout_units = len({int(value) for value in layout_ids})
    if cancel.requested:
        return finish({**base, "status": "unknown", "reason": "cancelled",
                       "min_px": None, "complete": False}, layout_units)
    if layout_units <= 1:
        reason = ("invalid_uv_triangles" if bad_input
                  else "no_distinct_layout_units" if classified else "no_distinct_islands")
        return finish({**base, "status": "unknown" if bad_input else "pass",
                       "reason": reason, "min_px": None,
                       "complete": not bad_input}, layout_units)
    cross_layout_key = "cross_layout_positive_pairs" if classified else "cross_island_positive_pairs"
    if overlap.get(cross_layout_key, 0):
        return finish({**base, "status": "unknown" if bad_input else "pass",
                       "reason": "cross_layout_overlap_proves_zero" if classified
                       else "cross_island_overlap_proves_zero",
                       "min_px": 0.0, "complete": not bad_input,
                       "minimum_proven": not bad_input}, layout_units)
    try:
        segments, labels = _boundary_segments(pixels, valid_uv, groups, topology, cancel)
        base["boundary_segments"] = len(segments)
        if len(set(labels.tolist())) < len(set(groups[ids].tolist())):
            return finish({**base, "status": "unknown",
                           "reason": "unresolved_island_boundary",
                           "min_px": None, "complete": False}, layout_units)
        # Boundary labels are numeric group ids; map through the per-group
        # layout table, not through per-triangle layout ids.
        labels = layout_ids_by_group[labels]
        if classified and usage['reuse_groups']:
            # Validate every numeric island's boundary above before removing
            # exact duplicates inside a layout. Different layouts stay distinct.
            unique = {}
            for i, (label, segment) in enumerate(zip(labels, segments)):
                if i % 4096 == 0 and cancel.poll(): raise _SpatialCancelled()
                unique.setdefault((int(label), tuple(segment[0]), tuple(segment[1])), i)
            indices = np.fromiter(unique.values(), dtype=np.int64)
            segments, labels = segments[indices], labels[indices]
            base['spatial_boundary_segments'] = len(segments)
        boxes = _boxes(segments)
        root = _tree(boxes, np.arange(len(segments)), labels, cancel)
    except _SpatialCancelled:
        return finish({**base, "status": "unknown", "reason": "cancelled",
                       "min_px": None, "complete": False}, layout_units)
    best = math.inf
    checked = visits = 0
    reason = "invalid_uv_triangles" if bad_input else None
    interrupted = False
    heap = []
    sequence = itertools.count()

    def push(a, b):
        if a.group is not None and a.group == b.group:
            return
        lower = _box_gap(a.box, b.box)
        if lower < best:
            heapq.heappush(heap, (lower, next(sequence), a, b))

    push(root, root)
    while heap:
        lower, _, a, b = heapq.heappop(heap)
        if lower >= best:
            break  # AABB lower bounds prove every remaining pair cannot improve.
        visits += 1
        if visits % 256 == 1 and cancel.poll():
            interrupted = True; reason = "cancelled"; break
        if visits > max(64, budget):
            interrupted = True; reason = "index_visit_budget"; break
        if a.ids is not None and b.ids is not None:
            for i, j in _leaf_pairs(a, b):
                if labels[i] == labels[j] or _box_gap(boxes[i], boxes[j]) >= best:
                    continue
                if checked >= budget:
                    interrupted = True; reason = "pair_budget"; break
                checked += 1
                if checked % 256 == 0 and cancel.poll():
                    interrupted = True; reason = "cancelled"; break
                best = min(best, _segment_gap(*segments[i], *segments[j]))
                if best == 0:
                    break
            if interrupted or best == 0:
                break
        elif a is b:
            push(a.left, a.left); push(a.left, a.right); push(a.right, a.right)
        elif b.ids is not None or (a.ids is None and a.count >= b.count):
            push(a.left, b); push(a.right, b)
        else:
            push(a, b.left); push(a, b.right)
    # Positive boundary distance alone cannot exclude one layout unit lying
    # inside another when the full cross-layout intersection survey was incomplete.
    containment_unresolved = best != 0 and not overlap["complete"]
    incomplete = bad_input or interrupted or containment_unresolved
    if containment_unresolved and reason is None:
        reason = "overlap_incomplete_for_containment"
    return finish({**base, "status": "unknown" if incomplete else "pass", "reason": reason,
                   "min_px": best if math.isfinite(best) else None,
                   "checked_pairs": checked, "exact_distances": checked,
                   "index_node_pair_visits": visits, "complete": not incomplete,
                   "minimum_proven": not incomplete,
                   "min_is_upper_bound": incomplete and math.isfinite(best),
                   "candidate_semantics": "different-layout-unit boundary segments only; heap-ordered AABB lower bounds" if classified
                   else "different-island boundary segments only; heap-ordered AABB lower bounds",
                   }, layout_units)


def measure(meshes, width=2048, height=2048, pair_budget=2000000, cancel=None,
            *, usage=None):
    """Return JSON-safe measurements; malformed array contracts raise ValueError."""
    width, height = float(width), float(height)
    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        raise ValueError("positive finite width and height required")
    try:
        pair_budget = operator.index(pair_budget)
    except TypeError as exc:
        raise ValueError("pair_budget must be a nonnegative integer") from exc
    if pair_budget < 0:
        raise ValueError("pair_budget must be nonnegative")
    validated_usage = _validate_usage(usage)
    cancellation = _Cancel(cancel)
    cancellation.poll()
    pixel_parts, valid_parts, group_parts, topology_parts = [], [], [], []
    areas, ratios = [], []
    group_records = []
    starts, names = [], []
    total = bad_g = bad_u = bad_j = outside = nonfinite_corners = negative = 0
    margin = math.inf
    margin_complete = True
    seen = set()
    for mesh in meshes:
        name, positions, triangles, uv, islands = _validate(mesh)
        if name in seen:
            raise ValueError("mesh names must be unique: " + name)
        seen.add(name)
        starts.append(total); names.append(name)
        count = len(triangles)
        local_g = np.zeros(count, dtype=bool)
        local_u = np.zeros(count, dtype=bool)
        local_j = np.zeros(count, dtype=bool)
        local_a = np.zeros(count)
        local_p = np.zeros(count)
        local_r = np.full(count, np.nan)
        with np.errstate(all="ignore"):
            pixels = uv * np.array([width, height])
        for first in range(0, count, 4096):
            last = min(count, first + 4096)
            a, p, gg, gu, r, gj, det = _measure_chunk(positions[triangles[first:last]], pixels[first:last])
            local_a[first:last], local_p[first:last] = a, p
            local_g[first:last], local_u[first:last] = gg, gu
            local_j[first:last], local_r[first:last] = gj, r
            negative += int(np.count_nonzero(det < 0))
            cancellation.poll()
        bad_g += int(np.count_nonzero(~local_g))
        bad_u += int(np.count_nonzero(~local_u))
        bad_j += int(np.count_nonzero(local_g & local_u & ~local_j))
        outside += int(np.count_nonzero(np.any((uv < 0) | (uv > 1), axis=2)))
        finite_corners = np.isfinite(pixels).all(axis=2)
        nonfinite_corners += int(np.count_nonzero(~finite_corners))
        if count and finite_corners.all():
            with np.errstate(all="ignore"):
                candidate_margin = np.min(np.stack((pixels[:, :, 0], width - pixels[:, :, 0],
                                                   pixels[:, :, 1], height - pixels[:, :, 1])))
            if math.isfinite(float(candidate_margin)):
                margin = min(margin, float(candidate_margin))
            else:
                margin_complete = False
        elif count:
            margin_complete = False
        unique_islands, inverse = np.unique(islands, return_inverse=True)
        group_ids = inverse.astype(np.int64, copy=False) + len(group_records)
        group_counts = np.bincount(inverse, minlength=len(unique_islands))
        group_bad_g = np.bincount(inverse, weights=(~local_g).astype(np.int64), minlength=len(unique_islands))
        group_bad_u = np.bincount(inverse, weights=(~local_u).astype(np.int64), minlength=len(unique_islands))
        with np.errstate(all="ignore"):
            group_area = np.bincount(inverse, weights=np.where(local_g, local_a, 0), minlength=len(unique_islands))
            group_pixels = np.bincount(inverse, weights=np.where(local_u, local_p, 0), minlength=len(unique_islands))
        for k, island in enumerate(unique_islands):
            if k % 256 == 0:
                cancellation.poll()
            ng, nu = int(group_bad_g[k]), int(group_bad_u[k])
            geometry_area, pixel_area = float(group_area[k]), float(group_pixels[k])
            area_ok, pixels_ok = math.isfinite(geometry_area), math.isfinite(pixel_area)
            density = None
            if not ng and not nu and area_ok and pixels_ok and geometry_area > 0:
                density = math.sqrt(pixel_area / geometry_area)
                if not math.isfinite(density):
                    density = None
            group_records.append({"object": name, "island": int(island), "px_per_m": density,
                                  "area_m2": geometry_area if not ng and area_ok else None,
                                  "pixel_area": pixel_area if not nu and pixels_ok else None,
                                  "triangles": int(group_counts[k]),
                                  "invalid_geometry_triangles": ng, "invalid_uv_triangles": nu})
        pixel_parts.append(pixels); valid_parts.append(local_u); group_parts.append(group_ids); topology_parts.append(triangles)
        areas.extend(float(x) for x in local_a[local_g & local_u & local_j])
        ratios.extend(float(x) for x in local_r[local_g & local_u & local_j])
        total += count
    resolved_usage = _resolve_usage(validated_usage, group_records)
    complete_numeric = total > 0 and not (bad_g or bad_u or bad_j)
    p95 = maximum = None
    if complete_numeric:
        ordered = sorted(zip(ratios, areas))
        mass = math.fsum(areas)
        accumulated = 0.0
        for value, weight in ordered:
            accumulated += weight
            if accumulated >= 0.95 * mass:
                p95 = value; break
        maximum = max(ratios)
    density_complete = bool(group_records) and all(r["px_per_m"] is not None for r in group_records)
    reference_density = deviation = None
    if density_complete:
        reference_density = math.sqrt(math.fsum(r["pixel_area"] for r in group_records)
                                      / math.fsum(r["area_m2"] for r in group_records))
        if math.isfinite(reference_density) and reference_density > 0:
            deviation = max(abs(r["px_per_m"] / reference_density - 1) for r in group_records)
        else:
            reference_density = None
            density_complete = False
    pixels = np.concatenate(pixel_parts) if pixel_parts else np.empty((0, 3, 2))
    valid_uv = np.concatenate(valid_parts) if valid_parts else np.empty(0, dtype=bool)
    groups = np.concatenate(group_parts) if group_parts else np.empty(0, dtype=np.int64)
    if resolved_usage is not None:
        layout_ids_by_group = resolved_usage["layout_ids"]
    else:
        layout_ids_by_group = np.arange(len(group_records), dtype=np.int64)
    layout_groups = layout_ids_by_group[groups]

    topology = np.concatenate(topology_parts) if topology_parts else np.empty((0, 3), dtype=np.int64)

    def location(i):
        mesh_index = bisect_right(starts, i) - 1
        return {"object": names[mesh_index], "triangle": int(i - starts[mesh_index])}
    if cancellation.requested:
        overlap = {"status": "unknown", "reason": "cancelled", "positive_pairs": 0, "pairwise_area_px2": 0.0,
                   "checked_pairs": 0, "exact_clips": 0, "examples": [], "complete": False, "area_is_partial": True,
                   "semantics": "pairwise sum, not union"}
        if resolved_usage is not None:
            overlap.update({
                "cross_island_positive_pairs": 0,
                "index_node_pair_visits": 0,
                "candidate_semantics": "cancelled before spatial work",
            })
            overlap.update(_classification_fields(
                resolved_usage, declared=0, undeclared=0, declared_area=0.0,
                undeclared_area=0.0, declared_examples=[], undeclared_examples=[],
                cross_layout=0, incomplete=True,
            ))
            overlap["classification_basis"] = (
                "cancelled before the positive-pair loop; declared and undeclared "
                "counts/areas are partial zeros"
            )
            overlap["semantics"] = (
                "pairwise sum, not union; cancelled before the positive-pair loop"
            )
    else:
        overlap = _overlap(pixels, valid_uv, location, groups, pair_budget, cancellation,
                           usage=resolved_usage, layout_groups=layout_groups)
    gap = _gap(pixels, valid_uv, groups, layout_ids_by_group, topology, overlap,
               pair_budget, cancellation, usage=resolved_usage)
    if resolved_usage is None:
        failure = bad_g or bad_u or bad_j or outside or overlap["positive_pairs"]
    else:
        outside_failure = outside if resolved_usage["mode"] == "EXCLUSIVE" else 0
        failure = (bad_g or bad_u or bad_j or outside_failure
                   or overlap["undeclared_positive_pairs"])
    unknown = (not total or cancellation.requested or overlap["status"] == "unknown" or gap["status"] == "unknown"
               or not density_complete or not complete_numeric)
    result = {"status": "fail" if failure else "unknown" if unknown else "pass",
              "total_triangles": total, "invalid_geometry_triangles": bad_g, "invalid_uv_triangles": bad_u,
              "unmeasurable_jacobian_triangles": bad_j, "out_of_bounds_corners": outside,
              "nonfinite_uv_corners": nonfinite_corners, "negative_uv_triangles": negative,
              "anisotropy": {"area_p95": p95, "max": maximum, "complete": complete_numeric,
                             "triangles_in_denominator": total, "measurable_triangles": len(ratios)},
              "density": {"islands": group_records, "relative_max_deviation": deviation,
                          "reference_px_per_m": reference_density, "complete": density_complete,
                          "reference_definition": "sqrt(total absolute pixel area / total world area)"},
              "boundary_margin_px": margin if margin_complete and math.isfinite(margin) else None,
              "overlap": overlap, "gap": gap, "cancelled": cancellation.requested,
              "numeric_policy": {"overlap_area_epsilon_px2": 0.0, "out_of_bounds_epsilon_uv": 0.0,
                                 "degenerate_area_epsilon": 0.0, "gap_length_epsilon_px": 0.0,
                                 "predicates": "adaptive float64 orient2d with exact-rational fallback",
                                 "range": "unrepresentable float64 geometry/UV/Jacobians are invalid, never pass"},
              "coverage": {"all_input_triangles_counted": True, "numeric_complete": complete_numeric,
                           "pair_budget_per_phase": pair_budget}}
    if resolved_usage is not None:
        result["usage"] = _usage_report(resolved_usage)
    return result
