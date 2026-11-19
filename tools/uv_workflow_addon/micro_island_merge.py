"""Bounded post-unwrap merge of tiny islands into adjacent triangle charts."""
import math

import bpy
import numpy as np

from . import core, metrics
from .contract import fail


_ATLAS_WIDTH = 2048
_POLICIES = (
    ("area", 0.30, 0.75),
    ("edge", 0.30, 0.75),
)


def _triangle_loops(mesh):
    if not mesh.polygons or any(poly.loop_total != 3 for poly in mesh.polygons):
        fail("Micro-island merge requires nonempty explicit triangles", "UNSUPPORTED")
    return np.asarray([tuple(poly.loop_indices) for poly in mesh.polygons], dtype=np.intp)


def _signed_double_area(uv_triangles):
    edge_a = uv_triangles[:, 1] - uv_triangles[:, 0]
    edge_b = uv_triangles[:, 2] - uv_triangles[:, 0]
    return edge_a[:, 0] * edge_b[:, 1] - edge_a[:, 1] * edge_b[:, 0]


def _chart_areas(uv, face_loops, groups):
    areas = np.abs(_signed_double_area(uv[face_loops].astype(np.float64))) * 0.5 * _ATLAS_WIDTH ** 2
    return {root: float(areas[faces].sum()) for root, faces in groups.items()}


def _summarize(areas, threshold):
    return {
        "islands": len(areas),
        "micro_islands": sum(area < threshold for area in areas.values()),
        "min_island_area_px2": min(areas.values()),
        "total_area_px2": math.fsum(areas.values()),
    }


def _groups(labels):
    groups = {}
    for face, root in enumerate(labels):
        groups.setdefault(root, []).append(face)
    return groups


def island_counts(obj, layer, threshold_px2):
    """Measure the represented layout at the supported 2048 pixel budget."""
    labels, _ = core.islands(obj, layer)
    areas = _chart_areas(core.coordinates(obj.data, layer), _triangle_loops(obj.data), _groups(labels))
    return _summarize(areas, threshold_px2)


def _solver_positions(obj):
    mesh = obj.data
    local = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", local)
    local = local.reshape((-1, 3))
    linear = np.asarray(obj.matrix_world, dtype=np.float64)[:3, :3]
    if not np.isfinite(local).all() or not np.isfinite(linear).all() or np.linalg.det(linear) <= 0:
        fail("Micro-island merge requires finite geometry and a positive transform", "UNSUPPORTED")
    gram = linear.T @ linear
    scale = np.trace(gram) / 3
    if np.allclose(gram / scale, np.eye(3), atol=1e-6, rtol=0.0):
        # A similarity has the same intrinsic metric. Preserve the stored
        # coordinates, including the validated native unwrap's rounding.
        return local
    positions = local.astype(np.float64) @ linear.T
    positions -= positions.min(axis=0) + (positions.max(axis=0) - positions.min(axis=0)) * 0.5
    with np.errstate(over="ignore", invalid="ignore"):
        positions = positions.astype(np.float32)
    if not np.isfinite(positions).all():
        fail("Micro-island solve positions exceed Blender float32 range", "UNSUPPORTED")
    return positions


def _manifold_edges(mesh, face_loops, loop_vertices):
    # Endpoint order is canonical per mesh edge, independent of face winding.
    rows = [[] for _ in mesh.edges]
    for face, loops in enumerate(face_loops):
        for first, second in zip(loops, np.roll(loops, -1)):
            edge = mesh.loops[int(first)].edge_index
            if loop_vertices[first] > loop_vertices[second]:
                first, second = second, first
            rows[edge].append((face, int(first), int(second)))
    if any(len(items) > 2 for items in rows):
        fail("Micro-island merge does not support edges with more than two incident faces", "UNSUPPORTED")
    return {edge: pair for edge, pair in enumerate(rows) if len(pair) == 2}


def _anchor_plan(mesh, positions, labels, groups, areas, micro, edges):
    micro = set(micro)
    borders = {root: {} for root in micro}
    for index, (a, b) in edges.items():
        ra, rb = labels[a[0]], labels[b[0]]
        if (ra in micro) == (rb in micro):
            continue
        small, anchor = (ra, rb) if ra in micro else (rb, ra)
        first, second = mesh.edges[index].vertices
        length = float(np.linalg.norm(positions[first] - positions[second]))
        count, total = borders[small].get(anchor, (0, 0.0))
        borders[small][anchor] = (count + 1, total + length)
    groups_by_anchor = {}
    unassigned = []
    for root, candidates in sorted(borders.items(), key=lambda item: (areas[item[0]], item[0])):
        if not candidates:
            unassigned.append(root)
            continue
        # Keep the proven min-union preference. Shared edges, length and
        # canonical face root only break ties between eligible anchors.
        anchor = min(candidates, key=lambda candidate: (
            len(groups[candidate]), -candidates[candidate][0],
            -candidates[candidate][1], candidate,
        ))
        groups_by_anchor.setdefault(anchor, []).append(root)
    return groups_by_anchor, unassigned


def _native_union(scene, positions, triangles, seed_uv, measure):
    used = sorted(set(triangles.ravel()))
    local_index = {vertex: i for i, vertex in enumerate(used)}
    local_faces = [tuple(local_index[vertex] for vertex in face) for face in triangles]
    mesh = bpy.data.meshes.new("UV_MicroIsland_Union")
    scratch = None
    try:
        mesh.from_pydata(positions[used].tolist(), [], local_faces)
        mesh.update()
        loop_ids = np.empty(len(mesh.loops), dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", loop_ids)
        if not np.array_equal(loop_ids, np.asarray(local_faces).ravel()):
            fail("Native micro-island scratch corner mapping differs from source", "VALIDATION_FAILED")
        scratch = bpy.data.objects.new("UV_MicroIsland_Union", mesh)
        scene.collection.objects.link(scratch)
        layer = mesh.uv_layers.new(name="UnionUV")
        mesh.uv_layers.active = layer
        # Edit-mode exit can recreate the Mesh UV layer and invalidate RNA handles.
        layer_name = layer.name
        core._write_uv(mesh, layer_name, seed_uv.reshape((-1, 2)))
        scratch.select_set(True)
        bpy.context.view_layer.objects.active = scratch
        for method in ("ANGLE_BASED", "MINIMUM_STRETCH"):
            if not measure.can_attempt():
                break
            # Reset the scratch layer from the same input for each solve.
            core._write_uv(mesh, layer_name, seed_uv.reshape((-1, 2)))
            try:
                bpy.ops.object.mode_set(mode="EDIT")
                bpy.ops.mesh.select_all(action="SELECT")
                outcome = bpy.ops.uv.unwrap(method=method, correct_aspect=False, margin=0.0)
            finally:
                if scratch.mode != "OBJECT":
                    bpy.ops.object.mode_set(mode="OBJECT")
            if outcome != {"FINISHED"}:
                measure.record(method, ["native_unwrap_failed"])
                continue
            labels, _ = core.islands(scratch, layer_name)
            if len(set(labels)) != 1:
                measure.record(method, ["multiple_components"])
                continue
            solved = core.coordinates(mesh, layer_name).astype(np.float64).reshape((-1, 3, 2))
            candidate = measure.inspect(method, solved)
            if candidate is not None:
                return candidate
        return None
    finally:
        if scratch is not None:
            bpy.data.objects.remove(scratch, do_unlink=True)
        bpy.data.meshes.remove(mesh)


def _edge_lengths(triangles):
    return np.linalg.norm(triangles - np.roll(triangles, -1, axis=1), axis=2)


class _UnionCheck:
    def __init__(self, seed_uv, face_indices, remaining, receipts, anchor, micro):
        self.seed = seed_uv.astype(np.float64)
        self.faces = face_indices
        self.remaining = remaining
        self.receipts = receipts
        self.anchor = anchor
        self.micro = micro

    def can_attempt(self):
        if self.remaining > 0:
            return True
        self.record(None, ["overlap_budget_exhausted"])
        return False

    def record(self, method, reasons, **checks):
        self.receipts.append({
            "status": "rejected" if reasons else "accepted",
            "method": method,
            "anchor_root": self.anchor,
            "micro_roots": self.micro,
            "union_faces": len(self.faces),
            "reasons": reasons,
            **checks,
        })

    def inspect(self, method, uv):
        area = _signed_double_area(uv)
        if not np.isfinite(uv).all() or not np.isfinite(area).all() or np.any(area == 0):
            self.record(method, ["nonfinite_or_zero_area"])
            return None
        center = uv.reshape((-1, 2)).mean(axis=0)
        reflected = bool(np.all(area < 0))
        if reflected:
            uv[:, :, 0] = 2 * center[0] - uv[:, :, 0]
            center = uv.reshape((-1, 2)).mean(axis=0)
        base_area = np.abs(_signed_double_area(self.seed))
        scale = math.sqrt(float(base_area.sum() / np.abs(_signed_double_area(uv)).sum()))
        with np.errstate(over="ignore", invalid="ignore"):
            represented = ((uv - center) * scale + center).astype(np.float32)
        candidate = represented.astype(np.float64)
        candidate_area = _signed_double_area(candidate)
        if not np.isfinite(candidate).all() or np.any(candidate_area <= 0):
            self.record(method, ["nonpositive_or_nonfinite_float32_output"], mirrored=reflected)
            return None

        reasons = []
        checks = {}
        ratios = (
            candidate_area / base_area,
            _edge_lengths(candidate) / _edge_lengths(self.seed),
        )
        for (kind, p95_limit, max_limit), ratio in zip(_POLICIES, ratios):
            error = np.abs(ratio - 1)
            p95, max_error = float(np.percentile(error, 95)), float(error.max())
            checks[kind + "_ratio"] = {"p95_abs_dev": p95, "max_abs_dev": max_error}
            if p95 > p95_limit or max_error > max_limit:
                reasons.append(kind + "_distortion")

        # Work at the real pixel scale using the production exact intersection
        # backend. Inspect the persisted float32 values, not the solver's
        # temporary float64 coordinates.
        overlap = metrics._overlap(
            candidate * _ATLAS_WIDTH,
            np.ones(len(self.faces), dtype=bool),
            lambda i: int(self.faces[i]),
            np.zeros(len(self.faces), dtype=np.intp),
            self.remaining,
            metrics._Cancel(None),
        )
        self.remaining = max(0, self.remaining - max(overlap["checked_pairs"], overlap["index_node_pair_visits"]))
        if overlap["status"] != "pass":
            reasons.append("self_overlap" if overlap["status"] == "fail" else overlap["reason"])
        self.record(method, reasons, mirrored=reflected, scale_to_original_area=scale, overlap=overlap, **checks)
        return None if reasons else represented


def _edge_changes(edges, before, after, labels):
    joined, cut = [], []
    for index, (a, b) in edges.items():
        first, second = list(a[1:]), list(b[1:])
        old_join = np.array_equal(before[first], before[second])
        new_join = np.array_equal(after[first], after[second])
        if old_join == new_join:
            continue
        if new_join and labels[a[0]] != labels[b[0]]:
            fail("Micro-island solve joined unrelated islands across edge %d" % index, "VALIDATION_FAILED")
        (joined if new_join else cut).append(index)
    return joined, cut


def merge_micro_islands(obj, layer, *, threshold_px2=64.0, pair_budget=2_000_000, progress=None):
    """Return an unpacked, staged UV buffer and receipts. Leave the source untouched."""
    mesh = obj.data
    face_loops = _triangle_loops(mesh)
    source = core.coordinates(mesh, layer)
    if not np.isfinite(source).all() or source.min() < 0 or source.max() > 1:
        fail("Micro-island merge requires finite UVs inside the 0–1 tile", "UNSUPPORTED")
    if np.any(_signed_double_area(source[face_loops].astype(np.float64)) == 0):
        fail("Micro-island merge requires nonzero-area source UV triangles", "VALIDATION_FAILED")
    labels, _ = core.islands(obj, layer)
    groups = _groups(labels)
    before = _chart_areas(source, face_loops, groups)
    micro = [root for root, area in before.items() if area < threshold_px2]
    staged = source.copy()
    island_labels = list(labels)
    receipts, unassigned, touched = [], [], []
    joined, new_cuts = [], []
    if micro:
        positions = _solver_positions(obj)
        loop_vertices = np.empty(len(mesh.loops), dtype=np.intp)
        mesh.loops.foreach_get("vertex_index", loop_vertices)
        edges = _manifold_edges(mesh, face_loops, loop_vertices)
        plan, unassigned = _anchor_plan(mesh, positions, labels, groups, before, micro, edges)
        if plan:
            remaining = pair_budget
            with core.scratch([]) as (scene, _):
                for group_index, (anchor, roots) in enumerate(sorted(plan.items()), 1):
                    if progress is not None:
                        progress("micro_island_merge", group=group_index, groups=len(plan))
                    roots = sorted(roots)
                    faces = sorted(list(groups[anchor]) + [face for root in roots for face in groups[root]])
                    loops = face_loops[faces]
                    check = _UnionCheck(source[loops], faces, remaining, receipts, anchor, roots)
                    candidate = _native_union(scene, positions, loop_vertices[loops], source[loops], check)
                    remaining = check.remaining
                    if candidate is not None:
                        staged[loops] = candidate
                        touched.extend(faces)
                        for face in faces:
                            island_labels[face] = anchor
            if touched:
                joined, new_cuts = _edge_changes(edges, source, staged, island_labels)

    old_counts = _summarize(before, threshold_px2)
    new_counts = _summarize(_chart_areas(staged, face_loops, _groups(island_labels)), threshold_px2)
    status = "no_micro_islands"
    if micro:
        status = "no_groups_merged" if not touched else (
            "partial_draft" if new_counts["micro_islands"] else "draft_requires_pack"
        )
    return staged, {
        "kind": "merge_micro_islands",
        "status": status,
        "threshold_px2": threshold_px2,
        "before": old_counts,
        "after_draft": new_counts,
        "islands_before": old_counts["islands"],
        "islands_after": new_counts["islands"],
        "micro_before_count": old_counts["micro_islands"],
        "micro_after_count": new_counts["micro_islands"],
        "unassigned_micro_roots": sorted(unassigned),
        "groups": receipts,
        "joined_edges": joined,
        "new_cut_edges": new_cuts,
        "touched_faces": sorted(touched),
        "requires_pack": bool(touched),
    }
