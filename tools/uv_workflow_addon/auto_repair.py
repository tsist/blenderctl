"""Bounded post-SMART repair of supported boundary-fan folds.

Only the UV layer of the supplied disposable object may be changed. Source
geometry, seams, other UV layers and consumers are never edited. This is not a
general topology repair or a replacement for global density/packing/validation.
No asset-specific face/edge/chart identifiers are used.
"""
from collections import deque
import math

import bpy
import numpy as np

from . import core, metrics
from .contract import fail


def _edge_rows(mesh):
    rows = {edge.index: [] for edge in mesh.edges}
    for face in mesh.polygons:
        ids = list(face.loop_indices)
        for a, b in zip(ids, ids[1:] + ids[:1]):
            edge = mesh.loops[a].edge_index
            if mesh.loops[a].vertex_index > mesh.loops[b].vertex_index:
                a, b = b, a
            rows[edge].append((face.index, a, b))
    return rows


def _topology(mesh, uv, rows):
    parent = list(range(len(mesh.polygons)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cuts = set()
    for edge, sides in rows.items():
        if len(sides) != 2:
            continue
        fa, a, b = sides[0]
        fb, c, d = sides[1]
        if np.array_equal(uv[a], uv[c]) and np.array_equal(uv[b], uv[d]):
            parent[find(fa)] = find(fb)
        else:
            cuts.add(edge)
    return [find(i) for i in range(len(parent))], cuts


def _census(obj, uv, faces, positions, triangles, tri_faces, tri_loops, budget, progress=None):
    indices = np.flatnonzero(np.isin(tri_faces, np.asarray(faces, dtype=np.int64)))
    if not len(indices):
        fail('Boundary-fan repair found an empty chart', 'VALIDATION_FAILED')
    row = dict(name=obj.name, positions=positions, triangles=triangles[indices],
               uv=uv[tri_loops[indices]], islands=np.zeros(len(indices), dtype=np.int64))
    q = metrics.measure([row], width=2048, height=2048, pair_budget=budget,
                        cancel=(lambda: progress('repair_checking')) if progress else None)
    if not q['overlap']['complete']:
        fail('Boundary-fan repair chart overlap check is incomplete: ' +
             str(q['overlap'].get('reason')), 'RESOURCE_LIMIT')
    if (q['invalid_geometry_triangles'] or q['invalid_uv_triangles'] or
            q['unmeasurable_jacobian_triangles']):
        fail('Boundary-fan repair does not support invalid chart geometry/UV', 'UNSUPPORTED')
    return q, indices


def _fan_cut(mesh, uv, faces, rows, cuts, first_face, second_face):
    """Choose one middle edge of the seed pair's open same-alias vertex fan."""
    a, b = mesh.polygons[first_face], mesh.polygons[second_face]
    shared = sorted(set(a.vertices) & set(b.vertices))
    for vertex in shared:
        ia = next(i for i in a.loop_indices if mesh.loops[i].vertex_index == vertex)
        ib = next(i for i in b.loop_indices if mesh.loops[i].vertex_index == vertex)
        if not np.array_equal(uv[ia], uv[ib]):
            continue
        fan_faces = set()
        for f in faces:
            face = mesh.polygons[f]
            hits = [i for i in face.loop_indices if mesh.loops[i].vertex_index == vertex]
            if len(hits) == 1 and np.array_equal(uv[hits[0]], uv[ia]):
                fan_faces.add(f)
        links = {f: [] for f in fan_faces}
        for edge, sides in rows.items():
            if vertex not in mesh.edges[edge].vertices or edge in cuts:
                continue
            local = [f for f, _, _ in sides if f in fan_faces]
            if len(local) > 2:
                fail('Nonmanifold boundary fan is unsupported', 'UNSUPPORTED')
            if len(local) == 2:
                f, g = local
                links[f].append((g, edge))
                links[g].append((f, edge))
        queue = deque([first_face]); previous = {first_face: None}
        while queue:
            f = queue.popleft()
            for g, edge in sorted(links.get(f, [])):
                if g not in previous:
                    previous[g] = (f, edge)
                    queue.append(g)
        if second_face not in previous:
            continue
        component = set(previous)
        degrees = [len(links[f]) for f in component]
        if degrees.count(1) != 2 or any(d not in (1, 2) for d in degrees):
            # Existing boundary chain is required. A closed fan is a different
            # topology problem and is never cut speculatively by this method.
            continue
        path_faces = [second_face]; path_edges = []
        at = second_face
        while previous[at] is not None:
            before, edge = previous[at]
            path_faces.append(before); path_edges.append(edge); at = before
        path_faces.reverse(); path_edges.reverse()
        if not path_edges:
            continue
        # A local fan edge can be bypassed elsewhere in the complete chart.
        # Require an actual dual-graph bridge separating the overlap seed;
        # otherwise the short slit can leave a globally invalid chart behind.
        chart_faces = set(faces)
        chart_links = {f: [] for f in faces}
        for edge, sides in rows.items():
            if edge in cuts or len(sides) != 2:
                continue
            local = [f for f, _, _ in sides if f in chart_faces]
            if len(local) == 2:
                f, g = local
                chart_links[f].append((g, edge)); chart_links[g].append((f, edge))
        chosen = None
        order = sorted(range(len(path_edges)),
                       key=lambda i: (abs(i - (len(path_edges) - 1) / 2), -i))
        for i in order:
            candidate = path_edges[i]
            seen = {first_face}; queue = deque([first_face])
            while queue:
                f = queue.popleft()
                for g, edge in chart_links[f]:
                    if edge != candidate and g not in seen:
                        seen.add(g); queue.append(g)
            if second_face not in seen:
                chosen = candidate
                break
        if chosen is None:
            continue
        return chosen, dict(vertex=int(vertex), fan_faces=len(component),
                            path_faces=path_faces, path_edges=path_edges,
                            selected_edge=int(chosen), existing_boundary_chain=True,
                            separates_seed_in_complete_chart=True)
    fail('Overlap seed is not a supported shared-vertex boundary fan: ' +
         str([first_face, second_face]), 'UNSUPPORTED')


def _solve_chart(obj, uv, faces, cut_edges, positions):
    """Independent subset, explicit source face/corner bijection, free outline."""
    mesh = obj.data
    source_loops = [i for f in faces for i in mesh.polygons[f].loop_indices]
    vertices = sorted({v for f in faces for v in mesh.polygons[f].vertices})
    vmap = {v: i for i, v in enumerate(vertices)}
    coords = positions[vertices].copy()
    coords -= coords.min(axis=0) + (coords.max(axis=0) - coords.min(axis=0)) * .5
    with np.errstate(over='ignore', invalid='ignore'):
        coords = coords.astype(np.float32)
    if not np.isfinite(coords).all():
        fail('Chart geometry is not representable by native solver', 'UNSUPPORTED')
    sm = bpy.data.meshes.new('BoundaryFanChartScratch')
    so = bpy.data.objects.new('BoundaryFanChartScratch', sm)
    selected = [o for o in bpy.context.view_layer.objects if o.select_get()]
    old_active = bpy.context.view_layer.objects.active
    try:
        sm.from_pydata(coords.tolist(), [], [[vmap[v] for v in mesh.polygons[f].vertices] for f in faces])
        sm.update(); bpy.context.scene.collection.objects.link(so)
        for sf, f in enumerate(faces):
            expected = [vmap[v] for v in mesh.polygons[f].vertices]
            if list(sm.polygons[sf].vertices) != expected:
                fail('Boundary repair subset face/corner mapping differs', 'VALIDATION_FAILED')
        scratch_layer = 'RepairUV'
        sm.uv_layers.new(name=scratch_layer, do_init=False)
        core._write_uv(sm, scratch_layer, uv[source_loops])
        source_pairs = {tuple(sorted(map(int, e.vertices))): e.index for e in mesh.edges}
        subset_rows = _edge_rows(sm)
        required = set()
        for edge in sm.edges:
            source_pair = tuple(sorted(vertices[int(v)] for v in edge.vertices))
            ei = source_pairs.get(source_pair)
            if ei is None:
                fail('Boundary repair subset edge mapping differs', 'VALIDATION_FAILED')
            edge.use_seam = ei in cut_edges
            # Original chart-boundary cuts become one-face mesh boundaries in
            # the subset; only internal two-face slits have a numeric UV cut.
            if edge.use_seam and len(subset_rows[edge.index]) == 2:
                required.add(edge.index)
        for o in bpy.context.view_layer.objects:
            o.select_set(False)
        so.select_set(True); bpy.context.view_layer.objects.active = so
        try:
            bpy.ops.object.mode_set(mode='EDIT'); bpy.ops.mesh.select_all(action='SELECT')
            result = bpy.ops.uv.unwrap(method='ANGLE_BASED', correct_aspect=False, margin=0.)
            if result != {'FINISHED'}:
                fail('Boundary chart unwrap did not finish', 'VALIDATION_FAILED')
        finally:
            if so.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT')
        # Re-resolve by the stable string after Edit Mode; RNA layer handles
        # may have been invalidated when selection attributes were created.
        solved = core.coordinates(sm, scratch_layer)
        if solved.shape != (len(source_loops), 2) or not np.isfinite(solved).all():
            fail('Boundary repair returned invalid corner coordinates', 'VALIDATION_FAILED')
        sr = _edge_rows(sm)
        roots, actual_cuts = _topology(sm, solved, sr)
        if required != actual_cuts:
            fail('Native chart solve numeric cuts differ from exact slit plan', 'VALIDATION_FAILED')
        # A whole-chart reflection is harmless; mixed winding inside a chart is
        # unsupported. Canonicalize each actual numeric component explicitly.
        for root in sorted(set(roots)):
            fids = [f for f, r in enumerate(roots) if r == root]
            li = [i for f in fids for i in sm.polygons[f].loop_indices]
            tris = solved[li].astype(np.float64).reshape(-1, 3, 2)
            x, y = tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]
            signed = x[:, 0] * y[:, 1] - x[:, 1] * y[:, 0]
            if np.all(signed < 0):
                center = float(solved[li, 0].astype(np.float64).mean())
                solved[li, 0] = (2 * center - solved[li, 0].astype(np.float64)).astype(np.float32)
            elif not np.all(signed > 0):
                fail('Native boundary chart has mixed/degenerate UV winding', 'VALIDATION_FAILED')
        return solved, source_loops
    finally:
        if so.name in bpy.data.objects:
            bpy.data.objects.remove(so, do_unlink=True)
        if sm.name in bpy.data.meshes:
            bpy.data.meshes.remove(sm)
        for o in selected:
            if o.name in bpy.context.view_layer.objects:
                o.select_set(True)
        if old_active and old_active.name in bpy.context.view_layer.objects:
            bpy.context.view_layer.objects.active = old_active


def _area(values):
    t = values.astype(np.float64).reshape(-1, 3, 2)
    a, b = t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]
    return float(np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]).sum() * .5)


def repair(obj, layer, progress=None, pair_budget=2000000):
    """Repair supported open-fan folds, atomically writing only this UV layer.

    Fixed UV or authored cut/join constraints are intentionally not accepted by
    this helper. Its public caller must reject them before invoking it.
    """
    progress = progress or (lambda *a, **k: None)
    if obj.mode != 'OBJECT' or layer not in obj.data.uv_layers:
        fail('Boundary repair requires an Object Mode scratch UV layer', 'CONFLICT')
    if type(pair_budget) is not int or not 1 <= pair_budget <= 10000000:
        fail('Invalid boundary repair pair budget')
    mesh = obj.data
    if any(len(p.vertices) != 3 for p in mesh.polygons):
        fail('SMART_REPAIR currently requires explicit triangular source faces', 'UNSUPPORTED')
    if core._pins(mesh, layer):
        fail('SMART_REPAIR cannot honor fixed UV pins', 'UNSUPPORTED')
    original = core.coordinates(mesh, layer)
    working = original.copy()
    mesh.calc_loop_triangles()
    triangles = np.asarray([list(t.vertices) for t in mesh.loop_triangles], dtype=np.int64)
    tri_faces = np.asarray([t.polygon_index for t in mesh.loop_triangles], dtype=np.int64)
    tri_loops = np.asarray([list(t.loops) for t in mesh.loop_triangles], dtype=np.int64)
    positions = core.world_positions(obj, 1.)
    roots, _ = core.islands(obj, layer)
    charts = {}
    for face, root in enumerate(roots):
        charts.setdefault(root, []).append(face)
    rows = _edge_rows(mesh)
    _, original_cuts = _topology(mesh, original, rows)
    bad = []
    # Inspect every complete chart. The display's 16-example cap is never used
    # to decide how many charts need repair.
    for root, faces in charts.items():
        progress('repair_check_chart', chart=int(root), faces=len(faces))
        q, indices = _census(obj, working, faces, positions, triangles, tri_faces, tri_loops, pair_budget, progress)
        if q['overlap']['positive_pairs']:
            bad.append((root, faces, q, indices))
    changes = []; touched = set()
    for root, faces, initial, _ in bad:
        old_indices = [i for f in faces for i in mesh.polygons[f].loop_indices]
        original_area = _area(original[old_indices])
        original_center = original[old_indices].astype(np.float64).mean(axis=0)
        added = set(); attempts = []
        for iteration in range(4):
            q, tri_indices = _census(obj, working, faces, positions, triangles, tri_faces, tri_loops, pair_budget, progress)
            if not q['overlap']['positive_pairs']:
                break
            example = q['overlap']['examples'][0]
            ia = int(tri_indices[example['first']['triangle']])
            ib = int(tri_indices[example['second']['triangle']])
            fa, fb = int(tri_faces[ia]), int(tri_faces[ib])
            _, current_cuts = _topology(mesh, working, rows)
            edge, fan = _fan_cut(mesh, working, faces, rows, current_cuts, fa, fb)
            if edge in added or edge in original_cuts:
                fail('Boundary repair did not find a new slit', 'VALIDATION_FAILED')
            added.add(edge)
            progress('repair_chart_seed', chart=int(root), seed_faces=[fa, fb],
                     added_edge=int(edge), fan=fan)
            solved, source_loops = _solve_chart(obj, original, faces, original_cuts | added, positions)
            new_area = _area(solved)
            if not math.isfinite(new_area) or new_area <= 0:
                fail('Boundary repair chart has zero area', 'VALIDATION_FAILED')
            factor = math.sqrt(original_area / new_area)
            values = ((solved.astype(np.float64) - solved.astype(np.float64).mean(axis=0)) * factor + original_center).astype(np.float32)
            candidate = working.copy(); candidate[source_loops] = values
            after, _ = _census(obj, candidate, faces, positions, triangles, tri_faces, tri_loops, pair_budget, progress)
            attempt = dict(iteration=iteration + 1, seed_triangles=[ia, ib], seed_faces=[fa, fb],
                           before_pairs=int(q['overlap']['positive_pairs']),
                           after_pairs=int(after['overlap']['positive_pairs']),
                           added_edge=int(edge), fan=fan, original_area=original_area,
                           repaired_area=_area(values), scale=factor)
            attempts.append(attempt)
            progress('repair_chart_attempt', chart=int(root), **attempt)
            if after['overlap']['positive_pairs']:
                # Do not chase ABF's newly introduced overlaps by adding more
                # seams to an already failed solution. A broader segmentation
                # method is required and must be named/validated separately.
                fail('Bridge-slit reparameterization still folds in chart ' + str(root),
                     'UNSUPPORTED')
            working = candidate
            if not after['overlap']['positive_pairs']:
                break
        final, _ = _census(obj, working, faces, positions, triangles, tri_faces, tri_loops, pair_budget, progress)
        if final['overlap']['positive_pairs']:
            fail('Boundary repair exhausted four slits for chart ' + str(root), 'VALIDATION_FAILED')
        touched.update(old_indices)
        changes.append(dict(chart=int(root), faces=len(faces), loops=len(old_indices),
                            before_pairs=int(initial['overlap']['positive_pairs']), after_pairs=0,
                            new_cut_edges=sorted(added), attempts=attempts,
                            anisotropy=final['anisotropy']))
    untouched = sorted(set(range(len(original))) - touched)
    if not np.array_equal(working[untouched], original[untouched]):
        fail('Boundary repair moved an unaffected chart', 'VALIDATION_FAILED')
    final_roots, final_cuts = _topology(mesh, working, rows)
    if not original_cuts <= final_cuts:
        fail('Boundary repair removed an original numeric seam', 'VALIDATION_FAILED')
    core._write_uv(mesh, layer, working)
    return dict(method='SMART_REPAIR', scope='shared-vertex open boundary fan folds only',
                status='intrinsic_folds_repaired_global_pack_pending',
                source_charts=len(charts), repaired_charts=len(changes), result_charts=len(set(final_roots)),
                initial_pairs=sum(int(q['overlap']['positive_pairs']) for _, _, q, _ in bad),
                repaired_chart_pairs=0, changed_loops=int(np.any(working != original, axis=1).sum()),
                unaffected_loops_exact=True, original_numeric_cuts_preserved=True,
                new_cut_edges=sorted(final_cuts - original_cuts), charts=changes,
                total_triangles=len(triangles), pair_budget_per_chart=pair_budget,
                max_new_slits_per_chart=4, constraints='public caller must reject cut/join/fixed constraints')
