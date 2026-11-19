"""Explicit usage, density and direction rules bound to editable candidate data."""
import copy
import math
import numpy as np
from .contract import fail


def defaults(binding):
    return {'version':'1.0','binding':binding,'mode':'EXCLUSIVE','reference':'Explicit exclusive UV workflow',
            'reuse_groups':[],'density':{'default_px_m':None,'regions':[],'relative_tolerance':.05},'directions':[]}


def _ids(value,label):
    if not isinstance(value,list) or not value or any(type(i) is not int or i<0 for i in value) or len(set(value))!=len(value):
        fail(label+' requires unique nonnegative indices')


def normalize(value,names):
    if not isinstance(value,dict) or set(value)!={'version','binding','mode','reference','reuse_groups','density','directions'} or value['version']!='1.0':
        fail('Settings require version, binding, mode, reference, reuse_groups, density and directions')
    if value['mode'] not in ('EXCLUSIVE','TILED'):fail('Settings mode must be EXCLUSIVE or TILED')
    if not isinstance(value['reference'],str) or not value['reference'].strip() or len(value['reference'])>1024:fail('State the intended use of these settings')
    # Use the same exact identity representation as purpose recipes.
    from .recipes import normalize_recipe
    dummy={'version':'1.0','binding':value['binding'],'policy':{'name':'settings','purpose':'settings','reference':'settings',
        'density_basis':'ISLAND_AREA_REFERENCE','stretch_p95_max':1,'stretch_max':1,'density_relative_max':.05,'border_px_min':0,'gap_px_min':0}}
    normalize_recipe(dummy,names)
    groups=value['reuse_groups']
    if not isinstance(groups,list) or len(groups)>10000:fail('Invalid reuse groups')
    seen=set();members=set()
    for group in groups:
        if not isinstance(group,dict) or set(group)!={'id','members'} or not isinstance(group['id'],str) or not group['id'] or group['id'] in seen:fail('Reuse group requires unique id and members')
        seen.add(group['id'])
        if not isinstance(group['members'],list) or len(group['members'])<2:fail('Reuse requires at least two distinct complete islands')
        for member in group['members']:
            if not isinstance(member,dict) or set(member)!={'object','faces'} or member['object'] not in names:fail('Invalid reuse member')
            _ids(member['faces'],'Reuse faces');key=(member['object'],tuple(sorted(member['faces'])))
            if key in members:fail('An island can belong to only one reuse group')
            members.add(key)
    if value['mode']=='TILED' and groups:fail('TILED uses explicit repeating-island semantics, not atlas reuse groups')
    density=value['density']
    if not isinstance(density,dict) or set(density)!={'default_px_m','regions','relative_tolerance'}:fail('Invalid density policy')
    if type(density['relative_tolerance']) not in (int,float) or not 0<=density['relative_tolerance']<=1:fail('Invalid density tolerance')
    if density['default_px_m'] is not None and (type(density['default_px_m']) not in (int,float) or not math.isfinite(density['default_px_m']) or density['default_px_m']<=0):fail('Invalid default pixel density')
    if not isinstance(density['regions'],list):fail('Density regions must be a list')
    for region in density['regions']:
        if not isinstance(region,dict) or set(region)!={'object','faces','target_px_m'} or region['object'] not in names:fail('Invalid density region')
        _ids(region['faces'],'Density region')
        if type(region['target_px_m']) not in (int,float) or not math.isfinite(region['target_px_m']) or region['target_px_m']<=0:fail('Region target must be finite positive px/m')
    if not isinstance(value['directions'],list):fail('Directions must be a list')
    for direction in value['directions']:
        if not isinstance(direction,dict) or set(direction)!={'object','loops','angle_degrees','tolerance_degrees'} or direction['object'] not in names:fail('Invalid direction constraint')
        _ids(direction['loops'],'Direction loops')
        if len(direction['loops'])!=2:fail('Direction needs one oriented UV edge')
        if type(direction['angle_degrees']) not in (int,float) or not math.isfinite(direction['angle_degrees']):fail('Direction angle must be finite')
        if type(direction['tolerance_degrees']) not in (int,float) or not math.isfinite(direction['tolerance_degrees']) or not 0<=direction['tolerance_degrees']<90:fail('Direction tolerance must be in [0,90) degrees')
    return copy.deepcopy(value)


def _island_members(obj,layer):
    from . import core
    groups,_=core.islands(obj,layer);members={}
    for face,group in enumerate(groups):members.setdefault(group,set()).add(face)
    return groups,members


def _require_island_member(obj,faces,groups,members):
    if any(f>=len(obj.data.polygons) for f in faces):fail('Bound face no longer exists: '+obj.name,'CONFLICT')
    ids={groups[f] for f in faces}
    if len(ids)!=1 or members[next(iter(ids))]!=set(faces):
        fail('Bound region is no longer one complete numeric UV island: '+obj.name,'CONFLICT')
    return next(iter(ids))


def island_member(obj,layer,faces):
    return _require_island_member(obj,faces,*_island_members(obj,layer))


def resolve(objects,layer,manifest,unit,settings):
    """Resolve exact face membership at the measurement boundary, never guess IDs."""
    if settings is None:return None,{},[]
    from .recipes import binding,require_binding
    require_binding(settings,binding(objects,manifest,unit))
    by_name={o.name:o for o in objects};usage={'mode':settings['mode'],'reuse_groups':[]};targets={}
    # Current coordinates and numeric membership cannot change during this
    # read-only resolution. Reuse them only within this one call, never across
    # edits/objects/layers or independent validation calls.
    prepared={}
    def resolve_member(o,faces):
        if o.name not in prepared:
            from . import core
            prepared[o.name]=(*_island_members(o,layer),core.coordinates(o.data,layer))
        groups,members,uv=prepared[o.name]
        return _require_island_member(o,faces,groups,members),uv
    for group in settings['reuse_groups']:
        rows=[];shape=None
        for member in group['members']:
            o=by_name[member['object']];island,uv=resolve_member(o,member['faces'])
            faces=sorted(tuple(sorted(tuple(map(float,uv[i])) for i in o.data.polygons[f].loop_indices)) for f in member['faces'])
            if shape is None:shape=faces
            elif faces!=shape:fail('Declared reuse islands no longer share the same UV face footprint; unstack or explicitly redefine the group','CONFLICT')
            rows.append({'object':o.name,'island':island})
        usage['reuse_groups'].append({'id':group['id'],'members':rows})
    for region in settings['density']['regions']:
        o=by_name[region['object']];key=(o.name,resolve_member(o,region['faces'])[0])
        if key in targets:fail('Density regions overlap: '+o.name,'CONFLICT')
        targets[key]=region['target_px_m']
    directions=[]
    for d in settings['directions']:
        o=by_name[d['object']];a,b=d['loops'];m=o.data
        if max(a,b)>=len(m.loops):fail('Direction corner identity changed','CONFLICT')
        if not any(a in p.loop_indices and b in p.loop_indices and (abs(a-b)==1 or {a,b}=={p.loop_start,p.loop_start+p.loop_total-1}) for p in m.polygons):
            fail('Direction loops must be an oriented edge in one face','CONFLICT')
        directions.append(d)
    return usage,targets,directions


def evaluate(q,objects,layer,manifest,settings,targets,directions):
    if settings is None:return
    from . import core
    density=settings['density'];deviations=[];rows=[]
    has_targets=bool(targets) or density['default_px_m'] is not None or 'density_px_m' in manifest
    for row in q['density']['islands'] if has_targets else []:
        target=targets.get((row['object'],row['island']),density['default_px_m'])
        if target is None:target=manifest.get('density_px_m')
        actual=row['px_per_m'];deviation=abs(actual/target-1) if actual is not None and target is not None else None
        rows.append({'object':row['object'],'island':row['island'],'target_px_m':target,'actual_px_m':actual,'relative_deviation':deviation})
        deviations.append(deviation)
    if rows:
        complete=all(d is not None and math.isfinite(d) for d in deviations)
        maximum=max(deviations) if complete else None
        q['density']['configured']={'basis':'EXPLICIT_ISLAND_TARGETS','complete':complete,'relative_max_deviation':maximum,
                                   'relative_tolerance':density['relative_tolerance'],'regions':rows,
                                   'precedence':'explicit region, explicit default, original manifest target; missing target is unknown'}
        if not complete and q['status']!='fail':q['status']='unknown'
        elif complete and maximum>density['relative_tolerance']:q['status']='fail'
    by_name={o.name:o for o in objects};violations=[]
    for d in directions:
        uv=core.coordinates(by_name[d['object']].data,layer);a,b=d['loops'];edge=(uv[b].astype(float)-uv[a])*[manifest['width'],manifest['height']]
        angle=math.degrees(math.atan2(edge[1],edge[0]));error=abs((angle-d['angle_degrees']+180)%360-180)
        if not np.isfinite(edge).all() or np.linalg.norm(edge)==0 or error>d['tolerance_degrees']:
            violations.append([d['object'],'direction',d['loops'],error])
    q['direction_constraints']={'status':'fail' if violations else 'pass','violations':violations}
    if violations:
        q['constraints']['violations'].extend(violations);q['constraints']['count']+=len(violations);q['constraints']['status']='fail';q['status']='fail'
    q['usage_policy']={'mode':settings['mode'],'reference':settings['reference'],'reuse_groups':len(settings['reuse_groups']),
                       'overlap_permission':'different explicitly declared islands only; intra-island folds remain invalid',
                       'unique_baking':'requires EXCLUSIVE mode without reuse groups'}


def density_value(q,basis=None):
    configured=q['density'].get('configured')
    if basis=='ISLAND_AREA_REFERENCE':return q['density']['relative_max_deviation'],q['density']['complete']
    if basis=='CONFIGURED_TARGETS' and not configured:return None,False
    if configured:return configured['relative_max_deviation'],configured['complete']
    return q['density']['relative_max_deviation'],q['density']['complete']
