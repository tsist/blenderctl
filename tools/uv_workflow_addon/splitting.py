"""Explicit face-side UV splits; coordinates are returned without mesh writes."""
import numpy as np
from .contract import fail
from .editing_math import apply_coordinates


def _edge_rows(mesh):
    rows={}
    for face in mesh.polygons:
        loops=list(face.loop_indices)
        for a,b in zip(loops,loops[1:]+loops[:1]):
            edge=mesh.loops[a].edge_index
            # Canonical vertex order also handles faces with matching winding.
            if mesh.loops[a].vertex_index>mesh.loops[b].vertex_index:a,b=b,a
            rows.setdefault(edge,[]).append((face.index,a,b))
    return rows


def _cuts(rows,uv):
    return {edge for edge,sides in rows.items() if len(sides)==2
            and (not np.array_equal(uv[sides[0][1]],uv[sides[1][1]])
                 or not np.array_equal(uv[sides[0][2]],uv[sides[1][2]]))}


def split(obj,uv,faces,delta_px,width,height):
    """Translate selected faces and require exactly their new boundary cuts.

    The caller owns managed-pin/lock checks, seam-plan updates, quality checks
    and transactional writes. A pixel displacement is not a guaranteed gap.
    """
    mesh=obj.data
    if not isinstance(uv,np.ndarray) or uv.dtype!=np.float32 or uv.shape!=(len(mesh.loops),2) or not np.isfinite(uv).all():
        fail('Split requires the complete finite float32 candidate UV array')
    if not isinstance(faces,(list,tuple)) or not faces or any(type(i) is not int or i<0 or i>=len(mesh.polygons) for i in faces) or len(set(faces))!=len(faces):
        fail('Split requires explicit unique face indices')
    selected=set(faces)
    try:
        delta=np.asarray(delta_px)
        if delta.shape!=(2,) or delta.dtype.kind not in 'iuf' or not np.isfinite(delta).all() or not np.any(delta!=0):
            fail('Split requires a nonzero finite pixel displacement')
    except (TypeError,ValueError):
        fail('Split requires a nonzero finite pixel displacement')
    rows=_edge_rows(mesh);before=_cuts(rows,uv);boundary=set()
    for edge,sides in rows.items():
        memberships=[face in selected for face,_,_ in sides]
        if any(memberships) and not all(memberships):
            if len(sides)!=2:fail('Split crosses a non-two-face mesh edge','UNSUPPORTED')
            if edge not in before:boundary.add(edge)
    if not boundary:fail('Selected faces have no continuous two-face boundary to split')
    indices=sorted({i for face in faces for i in mesh.polygons[face].loop_indices})
    try:out,_=apply_coordinates(uv,indices,{'kind':'translate','delta_px':delta_px},width,height)
    except ValueError as exc:fail(str(exc))
    after=_cuts(rows,out)
    if after!=before|boundary:
        fail('Split displacement did not create exactly the requested cuts or welded an existing cut','CONFLICT')
    unchanged=np.ones(len(uv),dtype=bool);unchanged[indices]=False
    if not np.array_equal(out[unchanged],uv[unchanged]):fail('Split moved unselected corners','VALIDATION_FAILED')
    return out,{'kind':'split','solver':'explicit_face_translation','selected_faces':len(selected),
                'selected_loops':len(indices),'new_cut_edges':sorted(boundary),
                'previous_cut_edges':sorted(before),'result_cut_edges':sorted(after),
                'delta_px':list(map(float,delta)),'unselected_uv':'exact',
                'internal_continuity':'preserved','existing_cuts':'preserved',
                'displacement_semantics':'pixel displacement, not minimum edge separation'}
