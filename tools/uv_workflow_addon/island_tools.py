"""Deterministic whole-island orientation and explicitly mapped UV reuse.

Functions return arrays without writing Blender data. The caller owns pins,
constraints, declaration persistence and full-candidate quality decisions.
"""
import math
import numpy as np
from .contract import fail
from .editing_math import apply_coordinates


def _input(obj,uv,width,height):
    if not isinstance(uv,np.ndarray) or uv.dtype!=np.float32 or uv.shape!=(len(obj.data.loops),2) or not np.isfinite(uv).all():
        fail('Island tools require complete finite float32 UV arrays: '+obj.name)
    if any(type(x) is not int or x<=0 for x in (width,height)):
        fail('Island tools require positive integer tile dimensions')


def _topology(obj,uv):
    mesh=obj.data;rows={};parent=list(range(len(mesh.polygons)));cuts=set()
    def find(x):
        while parent[x]!=x:parent[x]=parent[parent[x]];x=parent[x]
        return x
    for face in mesh.polygons:
        ids=list(face.loop_indices)
        for a,b in zip(ids,ids[1:]+ids[:1]):
            edge=mesh.loops[a].edge_index
            if mesh.loops[a].vertex_index>mesh.loops[b].vertex_index:a,b=b,a
            rows.setdefault(edge,[]).append((face.index,a,b))
    for edge,sides in rows.items():
        if len(sides)!=2:continue
        fa,a,b=sides[0];fb,c,d=sides[1]
        if np.array_equal(uv[a],uv[c]) and np.array_equal(uv[b],uv[d]):parent[find(fa)]=find(fb)
        else:cuts.add(edge)
    islands={}
    for face in mesh.polygons:islands.setdefault(find(face.index),[]).append(face.index)
    return sorted(islands.values(),key=lambda f:min(f)),cuts


def _faces(obj,faces):
    if not isinstance(faces,(list,tuple)) or not faces or len(set(faces))!=len(faces) or any(type(f) is not int or not 0<=f<len(obj.data.polygons) for f in faces):
        fail('Whole-island faces must be explicit unique face indices: '+obj.name)
    return set(faces)


def _whole_island(obj,uv,faces):
    chosen=_faces(obj,faces);islands,_=_topology(obj,uv)
    if not any(chosen==set(group) for group in islands):
        fail('Operation requires exactly one complete numeric UV island: '+obj.name)
    return sorted(chosen)


def _loops(obj,faces):return sorted({i for f in faces for i in obj.data.polygons[f].loop_indices})


def _selection(objects,old_uv,selection,width,height):
    by_name={o.name:o for o in objects}
    if not isinstance(selection,dict) or not selection or set(selection)-set(by_name):fail('Island selection names explicit target objects')
    resolved=[]
    for obj in objects:
        _input(obj,old_uv[obj.name],width,height)
        if obj.name not in selection:continue
        row=selection[obj.name]
        if not isinstance(row,dict) or set(row)!={'domain','indices'} or row['domain']!='ISLANDS':
            fail('Island tools require ISLANDS face seeds')
        seeds=_faces(obj,row['indices']);islands,_=_topology(obj,old_uv[obj.name])
        resolved.extend((obj,faces) for faces in islands if seeds&set(faces))
    return sorted(resolved,key=lambda item:(item[0].name,min(item[1])))


def _preserve_cuts(obj,before,after):
    if _topology(obj,before)[1]!=_topology(obj,after)[1]:
        fail('Island operation would change numeric UV cuts: '+obj.name,'CONFLICT')


def orient(obj,uv,faces,edge_loops,angle_degrees,width,height):
    _input(obj,uv,width,height);faces=_whole_island(obj,uv,faces);indices=_loops(obj,faces)
    if not isinstance(edge_loops,(list,tuple)) or len(edge_loops)!=2 or any(type(i) is not int or i not in indices for i in edge_loops):
        fail('Orientation requires two explicit edge corners in the selected island')
    a,b=edge_loops
    if not any((a,b) in list(zip(ids,ids[1:]+ids[:1])) or (b,a) in list(zip(ids,ids[1:]+ids[:1]))
               for ids in (list(obj.data.polygons[f].loop_indices) for f in faces)):
        fail('Orientation corners must be adjacent corners of the same face')
    if type(angle_degrees) not in (int,float) or not math.isfinite(angle_degrees):fail('Orientation angle must be finite')
    vector=(uv[b].astype(float)-uv[a])*[width,height]
    if not np.any(vector):fail('Orientation edge has zero pixel length')
    before=math.degrees(math.atan2(vector[1],vector[0]));rotation=(float(angle_degrees)-before)%360
    try:out,details=apply_coordinates(uv,indices,{'kind':'rotate','angle_degrees':rotation},width,height)
    except ValueError as exc:fail(str(exc))
    _preserve_cuts(obj,uv,out)
    vector=(out[b].astype(float)-out[a])*[width,height]
    if not np.any(vector):fail('Orientation edge collapsed after float32 conversion','CONFLICT')
    details.update(kind='orient',faces=faces,edge_loops=list(edge_loops),before_degrees=before,
                   target_degrees=float(angle_degrees)%360,result_degrees=math.degrees(math.atan2(vector[1],vector[0]))%360,
                   pixel_aspect=[width,height],numeric_cuts='preserved')
    return out,details


def _mapping(obj,faces,ref,ref_faces,pairs,mirrored):
    indices=_loops(obj,faces);ref_indices=set(_loops(ref,ref_faces))
    if not isinstance(pairs,list) or any(not isinstance(p,list) or len(p)!=2 or any(type(i) is not int for i in p) for p in pairs):
        fail('Stack pairs require explicit [moving_loop,reference_loop] integers')
    mapping=dict(pairs)
    if len(mapping)!=len(pairs) or set(mapping)!=set(indices):fail('Stack pairs must cover every moving island corner exactly once')
    if set(mapping.values())!=ref_indices or len(mapping)!=len(set(mapping.values())):
        fail('Each moving island must map bijectively onto all reference island corners')
    ref_face={i:f for f in ref_faces for i in ref.data.polygons[f].loop_indices};used=set();vertices={};inverse={}
    for f in faces:
        ids=list(obj.data.polygons[f].loop_indices);mapped=[mapping[i] for i in ids]
        targets={ref_face[i] for i in mapped}
        if len(targets)!=1:fail('Stack face corners must map to one complete reference face')
        target=targets.pop();expected=list(ref.data.polygons[target].loop_indices)
        if target in used or len(mapped)!=len(expected) or set(mapped)!=set(expected):fail('Stack faces must map one-to-one to reference faces')
        used.add(target);start=expected.index(mapped[0]);step=-1 if mirrored else 1
        if mapped!=[expected[(start+step*i)%len(expected)] for i in range(len(expected))]:
            fail('Stack face corner winding disagrees with mirrored mode')
        for i,j in zip(ids,mapped):
            a=obj.data.loops[i].vertex_index;b=ref.data.loops[j].vertex_index
            if vertices.setdefault(a,b)!=b or inverse.setdefault(b,a)!=a:
                fail('Stack corner pairs do not preserve mesh vertex correspondence')
    if used!=set(ref_faces):fail('Stack must cover the complete reference face topology')
    return mapping


def stack(objects,layer,old_uv,selection,action,width,height):
    resolved=_selection(objects,old_uv,selection,width,height);by_name={o.name:o for o in objects}
    reference=action.get('reference');pairs=action.get('pairs');mirrored=action.get('mirrored')
    if not isinstance(reference,dict) or set(reference)!={'object','face'} or reference['object'] not in by_name or type(reference['face']) is not int:
        fail('Stack requires an explicit reference object and face seed')
    if type(mirrored) is not bool or not isinstance(pairs,dict) or set(pairs)!=set(selection):fail('Stack pairs must name exact moving objects and mirrored must be boolean')
    ref=by_name[reference['object']];seed=reference['face']
    if not 0<=seed<len(ref.data.polygons):fail('Stack reference face is out of range')
    ref_faces=next(faces for faces in _topology(ref,old_uv[ref.name])[0] if seed in faces)
    if any(obj.name==ref.name and set(faces)&set(ref_faces) for obj,faces in resolved):fail('Static reference island cannot be in the moving selection')
    per_object={}
    for name,rows in pairs.items():
        if not isinstance(rows,list) or any(not isinstance(p,list) or len(p)!=2 or any(type(i) is not int for i in p) for p in rows):fail('Invalid stack pair rows')
        if len({p[0] for p in rows})!=len(rows):fail('A moving corner cannot have multiple stack pairs')
        per_object[name]=rows
    outputs={o.name:old_uv[o.name].copy() for o in objects};details={};members=[{'object':ref.name,'faces':sorted(ref_faces)}]
    covered={name:set() for name in selection}
    for obj,faces in resolved:
        ids=set(_loops(obj,faces));rows=[p for p in per_object[obj.name] if p[0] in ids]
        mapping=_mapping(obj,faces,ref,ref_faces,rows,mirrored)
        for i,j in mapping.items():outputs[obj.name][i]=old_uv[ref.name][j]
        covered[obj.name].update(mapping);members.append({'object':obj.name,'faces':faces})
        details.setdefault(obj.name,{'kind':'stack','mirrored':mirrored,'islands':[]})['islands'].append({'faces':faces,'copied_corners':len(mapping)})
    for name,rows in per_object.items():
        if {p[0] for p in rows}!=covered[name]:fail('Stack pairs contain corners outside selected islands')
    for obj in objects:_preserve_cuts(obj,old_uv[obj.name],outputs[obj.name])
    declaration={'reference':{'object':ref.name,'faces':sorted(ref_faces)},'members':members,'mirrored':mirrored}
    return outputs,details,declaration


def unstack(objects,layer,old_uv,selection,offset_px,width,height):
    resolved=_selection(objects,old_uv,selection,width,height)
    if not isinstance(offset_px,(list,tuple)) or len(offset_px)!=2 or any(type(x) not in (int,float) or not math.isfinite(x) for x in offset_px) or not any(offset_px):
        fail('Unstack requires a nonzero finite pixel offset')
    outputs={o.name:old_uv[o.name].copy() for o in objects};details={}
    for ordinal,(obj,faces) in enumerate(resolved,1):
        delta=[float(x)*ordinal for x in offset_px]
        try:out,_=apply_coordinates(outputs[obj.name],_loops(obj,faces),{'kind':'translate','delta_px':delta},width,height)
        except ValueError as exc:fail(str(exc))
        outputs[obj.name]=out
        details.setdefault(obj.name,{'kind':'unstack','islands':[]})['islands'].append({'faces':faces,'ordinal':ordinal,'delta_px':delta})
    for obj in objects:_preserve_cuts(obj,old_uv[obj.name],outputs[obj.name])
    return outputs,details
