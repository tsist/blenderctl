"""Explicit edge-pair stitching: the static side never moves."""
import numpy as np
from .contract import fail


def stitch(obj, uv, indices, pairs, limit_px, width, height):
    mesh=obj.data; selected=set(indices); mapping=dict(pairs)
    if len(mapping)!=len(pairs) or set(mapping)!=selected:
        fail('Stitch pairs must cover every selected moving corner exactly once')
    if any(i>=len(mesh.loops) or j>=len(mesh.loops) for i,j in pairs):
        fail('Stitch corner out of range')
    if selected & set(mapping.values()):fail('Static stitch corners cannot also be moving corners')
    nodes={};keys={};edges={}
    for p in mesh.polygons:
        ls=list(p.loop_indices)
        for a,b in zip(ls,ls[1:]+ls[:1]):
            key=(mesh.loops[a].vertex_index,*map(float,uv[a]));keys[a]=key
            nodes.setdefault(key,[]).append(a)
            edges.setdefault(mesh.loops[a].edge_index,[]).append((a,b))
    for a,b in pairs:
        if mesh.loops[a].vertex_index!=mesh.loops[b].vertex_index:
            fail('Stitch endpoints must refer to the same mesh vertex')
        if keys[a]==keys[b]:fail('Stitch pair is already continuous')
        if not set(nodes[keys[a]])<=selected:
            fail('Stitch selection must include all continuous aliases on the moving side')
        if any(keys[mapping[i]]!=keys[b] for i in nodes[keys[a]]):
            fail('One moving UV vertex cannot have different static destinations')
    joined=[];covered=set()
    for ei,rows in edges.items():
        if len(rows)!=2:continue
        for (a,b),(c,d) in (rows,rows[::-1]):
            if a not in mapping or b not in mapping:continue
            by_vertex={mesh.loops[i].vertex_index:i for i in (c,d)}
            if c in selected or d in selected:continue
            if all(keys[mapping[i]]==keys[by_vertex[mesh.loops[i].vertex_index]] for i in (a,b)):
                joined.append(ei);covered.update((keys[a],keys[b]))
    if any(keys[i] not in covered for i in selected):
        fail('Stitch must pair both endpoints of complete two-face mesh edges')
    out=uv.copy()
    for a,b in pairs:
        distance=float(np.linalg.norm((uv[a].astype(float)-uv[b])*[width,height]))
        if not np.isfinite(distance) or distance>limit_px:
            fail('Stitch pair exceeds its explicit pixel limit','CONFLICT')
        out[a]=uv[b]
    return out,{'kind':'stitch','solver':'explicit_corner_pairs','static_side':'exact',
                'joined_edges':sorted(set(joined)),'moving_loops':len(selected),
                'snap_islands':False,'midpoint_snap':False,'limit_px':limit_px}
