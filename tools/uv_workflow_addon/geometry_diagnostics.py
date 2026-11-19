"""Read-only base-mesh defect locations for a modeling handoff, never repair."""
import hashlib
import json
import math


def inspect(objects, unit, max_examples=1000, progress=None):
    from .core import geometry
    progress = progress or (lambda *args, **kwargs: None)
    output = []
    for obj in objects:
        progress('geometry_diagnostics', object=obj.name)
        mesh = obj.data
        mesh.calc_loop_triangles()
        issues = {}

        def add(kind, severity, item):
            row = issues.setdefault(kind, {'severity': severity, 'count': 0, 'examples': []})
            row['count'] += 1
            if len(row['examples']) < max_examples:
                row['examples'].append(item)

        positions = [tuple(obj.matrix_world @ v.co * unit) for v in mesh.vertices]
        if not len(mesh.loop_triangles):
            add('empty_surface', 'error', {'object': obj.name})
        finite = [all(math.isfinite(x) for x in p) for p in positions]
        for i, valid in enumerate(finite):
            if not valid:
                add('nonfinite_vertex', 'error', {'vertex': i})
        edge_faces = [[] for _ in mesh.edges]
        vertex_edges = [0] * len(mesh.vertices)
        for edge in mesh.edges:
            for i in edge.vertices:
                vertex_edges[i] += 1
            if all(finite[i] for i in edge.vertices) and positions[edge.vertices[0]] == positions[edge.vertices[1]]:
                add('zero_length_edge', 'warning', {'edge': edge.index, 'vertices': list(edge.vertices)})
        seen_faces = {}
        for face in mesh.polygons:
            for li in face.loop_indices:
                edge_faces[mesh.loops[li].edge_index].append(face.index)
            if all(finite[i] for i in face.vertices):
                footprint = tuple(sorted(positions[i] for i in face.vertices))
                if footprint in seen_faces:
                    add('coincident_face_vertices', 'review', {'face': face.index, 'other_face': seen_faces[footprint],
                        'vertices': list(face.vertices), 'loops': list(face.loop_indices)})
                else:
                    seen_faces[footprint] = face.index
        for i, tri in enumerate(mesh.loop_triangles):
            if all(finite[j] for j in tri.vertices):
                a, b, c = [obj.matrix_world @ mesh.vertices[j].co * unit for j in tri.vertices]
                area = (b-a).cross(c-a).length / 2
            else:
                area = float('nan')
            if not math.isfinite(area) or area <= 0:
                add('invalid_triangle', 'error', {'triangle': i, 'face': tri.polygon_index,
                    'vertices': list(tri.vertices), 'loops': list(tri.loops),
                    'face_edges': [mesh.loops[j].edge_index for j in mesh.polygons[tri.polygon_index].loop_indices],
                    'reason': 'nonfinite_area' if not math.isfinite(area) else 'zero_area'})
        for i, faces in enumerate(edge_faces):
            if len(faces) != 2:
                kind = 'loose_edge' if not faces else 'boundary_edge' if len(faces) == 1 else 'nonmanifold_edge'
                add(kind, 'info' if len(faces) <= 1 else 'review', {'edge': i, 'faces': sorted(set(faces))})
        for i, count in enumerate(vertex_edges):
            if not count:
                add('loose_vertex', 'info', {'vertex': i})
        for issue in issues.values():
            issue['examples_complete'] = len(issue['examples']) == issue['count']
        # hex preserves nonfinite diagnostic inputs while ordinary candidate identity
        # retains the existing, finite-only geometry contract.
        diagnostic_identity = hashlib.sha256(json.dumps({
            'positions_m': [[float(x).hex() for x in p] for p in positions],
            'edges': [list(e.vertices) for e in mesh.edges],
            'faces': [list(f.vertices) for f in mesh.polygons]}, sort_keys=True).encode()).hexdigest()
        try:
            ordinary_identity = geometry(obj)
        except ValueError:
            ordinary_identity = None
        status = 'fail' if any(r['severity'] == 'error' for r in issues.values()) else (
            'needs_modeling_review' if any(r['severity'] == 'review' for r in issues.values()) else 'no_blocker_found')
        output.append({'object': obj.name, 'mesh': mesh.name, 'geometry': ordinary_identity,
            'diagnostic_geometry_sha256': diagnostic_identity, 'status': status,
            'counts': {'vertices': len(mesh.vertices), 'edges': len(mesh.edges),
                       'faces': len(mesh.polygons), 'triangles': len(mesh.loop_triangles)},
            'issues': issues, 'scan_complete': True})
    statuses = [row['status'] for row in output]
    return {'version': '1.0', 'basis': 'BASE_MESH_WORLD_METERS', 'unit_scale': unit,
        'status': 'fail' if 'fail' in statuses else 'needs_modeling_review' if 'needs_modeling_review' in statuses else 'no_blocker_found',
        'objects': output, 'max_examples_per_issue': max_examples, 'mutation': 'none',
        'handoff': 'Use object and base-mesh face/edge/vertex/loop indices only while the geometry identity matches. Modeling owns repairs; after repair re-inspect and create new candidate/constraint bindings.',
        'limitations': ['Open boundaries and loose elements are reported, not automatically rejected.',
            'Coincident face detection compares exact world vertex sets; partial intersections and near-coincident surfaces are not certified.',
            'Counts scan all base elements; examples may be truncated. No manifold/retopology repair or evaluated-modifier diagnosis is performed.']}
