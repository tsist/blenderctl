# SPDX-License-Identifier: GPL-3.0-or-later
"""Deterministic rectangular UDIM packing with exact triangle verification.

Only base-mesh UV corners change. No unwrap, arbitrary fit scaling or texture edit.
"""
import math
import copy
import bpy
from mathutils import Vector
from protocol import Failure
from uv_contract import UV_EPS,AREA_EPS,DENSITY_REL_EPS

def reject(message,code='VALIDATION_FAILED'):
    raise Failure(code,message)

def bounds(points):
    return [min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)]

def origin(tile):return ((tile-1001)%10,(tile-1001)//10)

def tile_of(box):
    # A tile is selected by interior centroid; boundaries may lie on grid lines.
    u=math.floor((box[0]+box[2])/2);v=math.floor((box[1]+box[3])/2)
    if 0<=u<=9 and 0<=v<=9 and box[0]>=u-UV_EPS and box[1]>=v-UV_EPS and box[2]<=u+1+UV_EPS and box[3]<=v+1+UV_EPS:
        return 1001+u+10*v
    return None

def islands(mesh,layer):
    """Exact float UV continuity on the same topological edge, stable min-face seed."""
    parent=list(range(len(mesh.polygons)));edge_users={}
    def find(i):
        while parent[i]!=i:parent[i]=parent[parent[i]];i=parent[i]
        return i
    for face in mesh.polygons:
        loops=list(face.loop_indices)
        for a,b in zip(loops,loops[1:]+loops[:1]):
            va=mesh.loops[a].vertex_index;vb=mesh.loops[b].vertex_index
            key=(min(va,vb),max(va,vb))
            coords=(tuple(layer.data[a].uv),tuple(layer.data[b].uv))
            if va>vb:coords=coords[::-1]
            signature=(key,coords)
            if signature in edge_users:parent[find(face.index)]=find(edge_users[signature])
            else:edge_users[signature]=face.index
    groups={}
    for f in mesh.polygons:groups.setdefault(find(f.index),[]).append(f.index)
    return sorted(groups.values(),key=lambda x:x[0])

def inventory(mesh,layer):
    result=[]
    for faces in islands(mesh,layer):
        loops=[i for f in faces for i in mesh.polygons[f].loop_indices]
        points=[tuple(layer.data[i].uv) for i in loops]
        finite=all(math.isfinite(v) for p in points for v in p)
        box=bounds(points) if finite else None
        result.append({'seed_face':faces[0],'faces':faces,'loops':len(loops),'bounds':box,
            'tile':tile_of(box) if box else None,'pinned':any(layer.data[i].pin_uv for i in loops)})
    return result

def cross(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
def area(poly):return abs(sum(a[0]*b[1]-a[1]*b[0] for a,b in zip(poly,poly[1:]+poly[:1])))*.5

def overlap(a,b):
    polygon=list(a)
    if cross(*b)<0:b=b[::-1]
    for p,q in zip(b,b[1:]+b[:1]):
        out=[]
        for s,t in zip(polygon,polygon[1:]+polygon[:1]):
            cs=cross(p,q,s);ct=cross(p,q,t)
            if cs>=0:out.append(s)
            if (cs<0)!=(ct<0):
                ratio=cs/(cs-ct);out.append((s[0]+ratio*(t[0]-s[0]),s[1]+ratio*(t[1]-s[1])))
        polygon=out
        if not polygon:return 0.0
    return area(polygon)

def point_segment(p,a,b):
    dx=b[0]-a[0];dy=b[1]-a[1];norm=dx*dx+dy*dy
    t=max(0,min(1,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/norm)) if norm else 0
    return math.hypot(p[0]-a[0]-t*dx,p[1]-a[1]-t*dy)

def distance(a,b):
    if overlap(a,b)>AREA_EPS:return 0.0
    best=float('inf')
    for p,q in zip(a,a[1:]+a[:1]):
        for r,s in zip(b,b[1:]+b[:1]):
            if cross(p,q,r)*cross(p,q,s)<0 and cross(r,s,p)*cross(r,s,q)<0:return 0.0
            best=min(best,point_segment(p,r,s),point_segment(q,r,s),point_segment(r,p,q),point_segment(s,p,q))
    return best

def box_distance(a,b):return math.hypot(max(a[0]-b[2],b[0]-a[2],0),max(a[1]-b[3],b[1]-a[3],0))

def triangles_check(records,margin,internal=True):
    triangles=[]
    for i,r in enumerate(records):
        for tri in r['triangles']:
            p=[r['points'][k] for k in tri]
            if area(p)<=AREA_EPS:reject('Degenerate UV triangle: '+r['key'])
            triangles.append((bounds(p),i,p))
    triangles.sort(key=lambda x:x[0][0]);pairs=0;max_overlap=0.0;min_gap=None
    for n,(a,i,p) in enumerate(triangles):
        for following in range(n+1,len(triangles)):
            b,j,q=triangles[following]
            if b[0]>a[2]+margin+UV_EPS:break
            if box_distance(a,b)>margin+UV_EPS:continue
            if i==j and not internal:continue
            pairs+=1
            if pairs>2000000:reject('UV triangle pair budget exceeded','UNSUPPORTED')
            ov=overlap(p,q);max_overlap=max(max_overlap,ov)
            if ov>AREA_EPS:reject(f'Overlapping UV triangles ({ov:.12g} UV^2): '+records[i]['key']+' / '+records[j]['key'])
            if i!=j:
                gap=distance(p,q);min_gap=gap if min_gap is None else min(min_gap,gap)
                if gap+UV_EPS<margin:reject(f'UV island margin violated (gap {gap:.12g}, required {margin:.12g} UV): '+records[i]['key']+' / '+records[j]['key'])
    return {'triangle_pairs':pairs,'maximum_overlap_area':max_overlap,'minimum_checked_gap_uv':min_gap}

def protected(mesh,layer_name):
    from inspection import mesh_content,properties
    content=mesh_content(mesh)
    content['hashes'].pop('uv:'+layer_name,None)
    content['attributes']=[a for a in content['attributes'] if a['name']!=layer_name]
    return {'content':content,'properties':properties(mesh),
        'uv_flags':[(l.name,l.active_render,l.active_clone,[x.pin_uv for x in l.data]) for l in mesh.uv_layers],
        'active_uv':mesh.uv_layers.active_index,
        'shape_settings':[(k.name,properties(k)) for k in mesh.shape_keys.key_blocks] if mesh.shape_keys else []}

def compare_evaluation_reopen(expected,observed):
    """UV-only candidate: exact saved data plus full evaluated coordinate comparison.

    Catmull-Clark after Armature can differ by one float32 ULP on identical files.
    Never discard a geometry hash without checking every coordinate independently.
    """
    import scenes
    tolerance=1e-6  # fixed base-local Blender units; not relative to measured error
    report={'version':'1.0','scope':'UV_ONLY_SAVE_REOPEN_EVALUATED_COORDINATES',
        'absolute_tolerance':tolerance,'objects':[],'pass':False}
    left=copy.deepcopy(expected);right=copy.deepcopy(observed)
    if len(left['objects'])!=len(right['objects']):return '$/objects/length',report
    for a,b in zip(left['objects'],right['objects']):
        if a['name']!=b['name']:return '$/objects/name',report
        x=a['evaluated'];y=b['evaluated']
        if x is None or y is None:continue
        p=x.pop('coordinates');q=y.pop('coordinates')
        if len(p)!=len(q):return a['name']+'/evaluated/vertex_count',report
        delta=max((abs(v-w) for av,bv in zip(p,q) for v,w in zip(av,bv)),default=0)
        finite=all(math.isfinite(v) for row in p+q for v in row)
        report['objects'].append({'object':a['name'],'vertices':len(p),'maximum_coordinate_error':delta,
            'expected_sha256':x['positions_sha256'],'observed_sha256':y['positions_sha256'],'finite':finite})
        if not finite or delta>tolerance:return a['name']+'/evaluated/coordinates',report
        # Other geometry fields, UVs, topology and all saved/base content stay strict.
        y['positions_sha256']=x['positions_sha256']
    mismatch=scenes.compare(left,right);report['pass']=mismatch is None
    return mismatch,report

def run(op):
    import modeling,scenes
    resolutions={t['number']:t['resolution'] for t in op['tiles']}
    if len(resolutions)!=len(op['tiles']):reject('Duplicate UDIM number','INVALID_REQUEST')
    if len({t['object'] for t in op['targets']})!=len(op['targets']):reject('Duplicate UV target object','INVALID_REQUEST')
    records=[];targets=[];used=set()
    for target in op['targets']:
        item=scenes.find(bpy.data.objects,target['object'],True)
        if item.type!='MESH' or item.library or item.override_library or item.data.library or item.data.override_library:reject('UV layout requires a local mesh','UNSUPPORTED')
        mesh=item.data;modeling.bounded(mesh)
        if mesh.animation_data:reject('Animated mesh attributes unsupported','UNSUPPORTED')
        if mesh.as_pointer() in used:reject('Each shared mesh must appear once per layout','INVALID_REQUEST')
        used.add(mesh.as_pointer());layer=scenes.find(mesh.uv_layers,target['layer'])
        if modeling.topology(mesh)!=target['topology_sha256'] or modeling.sha([list(x.uv) for x in layer.data])!=target['uv_sha256']:reject('Stale topology or UV hash','CONFLICT')
        matrix=item.matrix_world.copy()
        if not all(math.isfinite(v) for row in matrix for v in row) or abs(matrix.to_3x3().determinant())<1e-12:reject('Non-finite or singular world transform')
        users=[x for x in bpy.data.objects if x.data==mesh]
        if mesh.users>1:
            if target['data_scope']=='reject_shared':reject('Shared mesh requires explicit policy','CONFLICT')
            if target['data_scope']=='shared':
                inverse=matrix.to_3x3().inverted()
                for user in users:
                    if not all(math.isfinite(v) for row in user.matrix_world for v in row):reject('Non-finite shared-user world transform','UNSUPPORTED')
                    relative=user.matrix_world.to_3x3()@inverse;metric=relative.transposed()@relative
                    if user.library or user.override_library or any(not math.isfinite(metric[i][j]) or abs(metric[i][j]-(1 if i==j else 0))>1e-6 for i in range(3) for j in range(3)):reject('Shared users must be local with identical world metric','UNSUPPORTED')
            else:
                if mesh.asset_data or mesh.get('asset_id') or (mesh.shape_keys and (mesh.shape_keys.asset_data or mesh.shape_keys.get('asset_id'))):reject('Cannot duplicate asset identity','UNSUPPORTED')
                name=item.name+'.UVLayout';scenes.fresh(bpy.data.meshes,name)
                item.data=scenes.named(mesh.copy(),name);mesh=item.data;layer=mesh.uv_layers[target['layer']]
        groups=islands(mesh,layer);selections={x['seed_face']:x for x in target['islands']}
        if len(selections)!=len(target['islands']) or not set(selections)<=set(x[0] for x in groups):reject('Island selection must use unique current minimum-face seeds','INVALID_REQUEST')
        baseline=protected(mesh,layer.name);mesh.calc_loop_triangles()
        byface={}
        for tri in mesh.loop_triangles:byface.setdefault(tri.polygon_index,[]).append(tuple(tri.loops))
        original={i:tuple(x.uv) for i,x in enumerate(layer.data)}
        if not all(math.isfinite(v) for p in original.values() for v in p):reject('Non-finite UV coordinates')
        for faces in groups:
            loop_ids=[i for f in faces for i in mesh.polygons[f].loop_indices];points={i:original[i] for i in loop_ids};box=bounds(list(points.values()));current=tile_of(box)
            selection=selections.get(faces[0]);tile=selection.get('tile',current) if selection else current
            if selection and tile not in resolutions:reject('Selected island needs an explicit allowed tile; implicit tile must already be valid','INVALID_REQUEST')
            # Unselected islands outside the requested atlas remain untouched and unvalidated.
            if not selection and tile not in resolutions:
                if any(not(box[2]<=origin(t)[0] or box[0]>=origin(t)[0]+1 or box[3]<=origin(t)[1] or box[1]>=origin(t)[1]+1) for t in resolutions):reject('Unselected island straddles requested atlas boundary')
                continue
            tris=[tri for f in faces for tri in byface.get(f,[])]
            if not tris:reject('Island has no triangles')
            world_area=0.0;triangle_world_areas=[]
            for tri in tris:
                p=[matrix@mesh.vertices[mesh.loops[i].vertex_index].co for i in tri]
                a=(p[1]-p[0]).cross(p[2]-p[0]).length*.5*op['meters_per_unit']**2
                if not math.isfinite(a) or a<=1e-18:reject('Degenerate base-mesh world triangle')
                world_area+=a
                triangle_world_areas.append(a)
            uv_area=sum(area([points[i] for i in tri]) for tri in tris)
            if uv_area<=AREA_EPS:reject('Degenerate UV island')
            locked=not selection or selection['locked']
            if any(layer.data[i].pin_uv for i in loop_ids) and not locked:reject('Pinned island must be explicitly locked','CONFLICT')
            initial=target['source_resolution']*math.sqrt(uv_area/world_area)
            desired=op['density'].get('pixels_per_meter',initial) if selection else resolutions[tile]*math.sqrt(uv_area/world_area)
            scale=desired/(resolutions[tile]*math.sqrt(uv_area/world_area))
            if selection and not op['allow_scale'] and abs(scale-1)>DENSITY_REL_EPS:reject('Density requires forbidden scaling','CONFLICT')
            if locked and (tile!=current or abs(scale-1)>DENSITY_REL_EPS):reject('Locked island conflicts with tile/density','CONFLICT')
            if locked or not op['allow_scale']:scale=1.0
            record={'key':item.name+':'+str(faces[0]),'object':item.name,'layer':layer.name,'seed_face':faces[0],
                'faces':faces,'tile':tile,'points':points,'triangles':tris,'selected':bool(selection),'locked':locked,
                'world_area_m2':world_area,'initial_density':initial,'target_density':desired,'scale':scale,
                'bounds_before':box,'rotation_degrees':0,'mesh':mesh,'triangle_world_areas':triangle_world_areas}
            records.append(record)
        targets.append({'object':item.name,'mesh':mesh,'layer':layer.name,'baseline':baseline,'original':original,'selected_loops':{i for r in records if r['mesh']==mesh and r['selected'] for i in r['points']},'data_users':[u.name for u in users]})
    if len(records)>2048 or sum(len(r['triangles']) for r in records)>100000:reject('UV layout exceeds 2048 islands / 100000 triangles','UNSUPPORTED')
    # Check each source island internally, but permit overlapping distinct moving islands.
    for r in records:triangles_check([r],0)
    tile_checks={}
    for tile,resolution in sorted(resolutions.items()):
        margin=op['margin_pixels']/resolution
        if margin*2>=1:reject('Margin leaves no tile capacity','INVALID_REQUEST')
        u,v=origin(tile);members=[r for r in records if r['tile']==tile];fixed=[r for r in members if r['locked']]
        occupied=[]
        for r in fixed:
            box=bounds(list(r['points'].values()))
            if box[0]<u+margin-UV_EPS or box[1]<v+margin-UV_EPS or box[2]>u+1-margin+UV_EPS or box[3]>v+1-margin+UV_EPS:reject('Fixed island violates tile edge margin')
            occupied.append(box)
        triangles_check(fixed,margin)
        moving=[r for r in members if not r['locked']]
        moving.sort(key=lambda r:(-((r['bounds_before'][2]-r['bounds_before'][0])*(r['bounds_before'][3]-r['bounds_before'][1])*r['scale']**2),r['key']))
        for r in moving:
            b=r['bounds_before'];base={i:((p[0]-b[0])*r['scale'],(p[1]-b[1])*r['scale']) for i,p in r['points'].items()};chosen=None
            options=[]
            for rotated in ([False,True] if op['allow_rotate'] else [False]):
                pts={i:(-p[1],p[0]) if rotated else p for i,p in base.items()};bb=bounds(list(pts.values()));w=bb[2]-bb[0];h=bb[3]-bb[1]
                xs=sorted({u+margin,*[b[2]+margin for b in occupied]});ys=sorted({v+margin,*[b[3]+margin for b in occupied]})
                attempts=0;found=False
                for y in ys:
                    for x in xs:
                        attempts+=1
                        if attempts>200000:reject('Rectangle search budget exceeded','UNSUPPORTED')
                        box=[x,y,x+w,y+h]
                        if box[2]>u+1-margin+UV_EPS or box[3]>v+1-margin+UV_EPS:continue
                        if any(not(box[2]+margin<=a[0]+1e-12 or box[0]>=a[2]+margin-1e-12 or box[3]+margin<=a[1]+1e-12 or box[1]>=a[3]+margin-1e-12) for a in occupied):continue
                        options.append((y,x,int(rotated),box,{i:(p[0]-bb[0]+x,p[1]-bb[1]+y) for i,p in pts.items()}))
                        found=True;break
                    if found:break
            if not options:reject('No rectangle placement at requested density (no fit scaling): '+r['key'],'CONFLICT')
            chosen=min(options,key=lambda x:x[:3]);r['rotation_degrees']=90*chosen[2];r['points']=chosen[4];occupied.append(chosen[3])
        for r in members:
            if r['selected'] and not r['locked']:
                layer=r['mesh'].uv_layers[r['layer']]
                for i,p in r['points'].items():layer.data[i].uv=p
                r['points']={i:tuple(layer.data[i].uv) for i in r['points']}
            r['bounds_after']=bounds(list(r['points'].values()))
            b=r['bounds_after']
            if b[0]<u+margin-UV_EPS or b[1]<v+margin-UV_EPS or b[2]>u+1-margin+UV_EPS or b[3]>v+1-margin+UV_EPS:reject('Float32 UV storage violated tile bounds')
            r['uv_area']=sum(area([r['points'][i] for i in tri]) for tri in r['triangles'])
            r['observed_density']=resolution*math.sqrt(r['uv_area']/r['world_area_m2'])
            densities=[resolution*math.sqrt(area([r['points'][i] for i in tri])/a) for tri,a in zip(r['triangles'],r['triangle_world_areas'])]
            r['triangle_density_range']=[min(densities),max(densities)]
            r['density_relative_error']=abs(r['observed_density']/r['target_density']-1)
            if r['density_relative_error']>DENSITY_REL_EPS:reject('Float32 UV storage violated density tolerance')
        tile_checks[str(tile)]=triangles_check(members,margin)
    for target in targets:
        mesh=target['mesh'];mesh.update()
        if protected(mesh,target['layer'])!=target['baseline']:reject('Non-UV mesh content changed')
        layer=mesh.uv_layers[target['layer']]
        if any(tuple(layer.data[i].uv)!=p for i,p in target['original'].items() if i not in target['selected_loops']):reject('Unselected UV corners changed')
    public=[{k:v for k,v in r.items() if k not in ('mesh','points','triangles','triangle_world_areas')} for r in records]
    return {'uv_layout_report_version':'1.0','adapter':op['adapter'],'density_scope':'BASE_MESH_WORLD_METRIC_AT_CONTEXT_FRAME',
        'tolerances':{'uv':UV_EPS,'overlap_area':AREA_EPS,'density_relative':DENSITY_REL_EPS},
        'islands':public,'tiles':tile_checks,'preservation':'pass','unselected_outside_tiles':'unchanged_not_validated',
        'packing':'deterministic_conservative_rectangles; failure_is_not_proof_of_geometric_infeasibility'}
