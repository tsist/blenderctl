# SPDX-License-Identifier: GPL-3.0-or-later
"""Auditable native expression evaluation with unchanged default script refusal."""
import hashlib,json,math
import bpy
from inspection import all_ids,identity,properties,curve_content
from protocol import Failure,atomic_json,digest
from driver_contract import normalize
from driver_math import prove,interval

def sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def owners():
    result=[];seen=set()
    for owner in all_ids():
        if owner.as_pointer() not in seen:
            seen.add(owner.as_pointer());result.append((owner,identity(owner)))
        tree=getattr(owner,'node_tree',None)
        if tree and tree.as_pointer() not in seen:
            seen.add(tree.as_pointer());result.append((tree,{'embedded_in':identity(owner),'property':'node_tree','id':identity(tree)}))
    return result

def inventory():
    rows=[];handles={}
    owner_list=owners();identities={o.as_pointer():i for o,i in owner_list}
    for owner,owner_id in owner_list:
        ad=getattr(owner,'animation_data',None)
        if not ad:continue
        for ordinal,f in enumerate(ad.drivers):
            ident={'owner':owner_id,'path':f.data_path,'index':f.array_index,'ordinal':ordinal}
            driver_id=sha(ident)
            if driver_id in handles:raise Failure('CONFLICT','Duplicate driver identity')
            try:owner.path_resolve(f.data_path);resolved=True
            except (ValueError,KeyError,AttributeError):resolved=False
            d=f.driver
            record={**ident,'driver_id':driver_id,'type':d.type,'expression':d.expression,'use_self':d.use_self,
                    'mute':f.mute,'curve':curve_content(f),'variables':[{'name':v.name,'type':v.type,'targets':[{**properties(t),'id':identities.get(t.id.as_pointer(),identity(t.id)) if t.id else None} for t in v.targets]} for v in d.variables],
                    'driven_path_resolved':resolved,'simple_expression':d.is_simple_expression if d.type=='SCRIPTED' else None}
            rows.append(record);handles[driver_id]=(owner,f)
    if len(rows)>4096:raise Failure('UNSUPPORTED','Driver inventory exceeds 4096')
    rows.sort(key=lambda r:r['driver_id'])
    return rows,handles

def audit(source):
    rows,_=inventory();fingerprint=sha(rows);unresolved=[r['driver_id'] for r in rows if not r['driven_path_resolved']]
    return {'version':'1.0','source_sha256':digest(source),'inventory_sha256':fingerprint,'driver_count':len(rows),'drivers':rows,
            'profile_template':{'mode':'NATIVE_SIMPLE_V1','source_sha256':digest(source),'inventory_sha256':fingerprint,'driver_count':len(rows),'acknowledged_unresolved':unresolved},
            'scope':'Template requires explicit use. Unresolved driven paths are preserved, not repaired. Only native simple expressions can be admitted; no embedded text or Python execution.'}

def check(profile,source,job=None):
    normalize(profile)
    if not source or digest(source)!=profile['source_sha256']:raise Failure('CONFLICT','Driver profile source SHA256 mismatch')
    if bpy.context.preferences.filepaths.use_scripts_auto_execute:raise Failure('UNSUPPORTED','Driver profile requires automatic Python execution disabled')
    if tuple(bpy.app.version)!=(5,2,1):raise Failure('UNSUPPORTED','NATIVE_SIMPLE_V1 is validated for Blender 5.2.1')
    rows,handles=inventory()
    if len(rows)!=profile['driver_count'] or sha(rows)!=profile['inventory_sha256']:raise Failure('CONFLICT','Driver inventory differs from profile')
    unresolved={r['driver_id'] for r in rows if not r['driven_path_resolved']}
    if set(profile['acknowledged_unresolved'])!=unresolved:raise Failure('CONFLICT','Explicit unresolved-driver acknowledgement differs from inventory')
    for r in rows:
        owner,f=handles[r['driver_id']];d=f.driver
        if r['driven_path_resolved']:driven_value(owner,f)
        if owner.library:raise Failure('UNSUPPORTED','Linked drivers are outside native profile')
        # use_self can be enabled but unused. The native expression parser below
        # rejects actual Python self/attribute access, independently of this flag.
        if len(d.expression)>4096 or len(d.variables)>64:raise Failure('UNSUPPORTED','Driver expression or variables exceed profile bound')
        if d.type not in ('SCRIPTED','AVERAGE','SUM','MIN','MAX'):raise Failure('UNSUPPORTED','Driver type outside native profile')
        if d.type=='SCRIPTED' and not d.is_simple_expression:raise Failure('UNSUPPORTED','Driver is not a native simple expression')
        if f.modifiers or f.sampled_points:raise Failure('UNSUPPORTED','Driver curve modifiers/sampled points need separate validation')
        for v in d.variables:
            if v.type not in ('SINGLE_PROP','TRANSFORMS','ROTATION_DIFF','LOC_DIFF'):raise Failure('UNSUPPORTED','Driver variable type outside native profile')
            for t in v.targets:
                if not t.id or t.id.library:raise Failure('UNSUPPORTED','Missing or linked driver variable target: '+r['driver_id'])
                if v.type!='SINGLE_PROP' and not isinstance(t.id,bpy.types.Object):raise Failure('UNSUPPORTED','Transform driver target must be an Object')
                if v.type=='SINGLE_PROP':
                    try:value=t.id.path_resolve(t.data_path)
                    except (ValueError,KeyError,AttributeError) as exc:raise Failure('UNSUPPORTED','Unresolved driver variable target') from exc
                    if not isinstance(value,(int,float,bool)) or not math.isfinite(value):raise Failure('UNSUPPORTED','Driver variable must resolve to finite scalar')
                elif t.bone_target and (not getattr(t.id,'pose',None) or t.bone_target not in t.id.pose.bones):raise Failure('UNSUPPORTED','Missing driver target bone')
    report={'mode':profile['mode'],'source_sha256':profile['source_sha256'],'inventory_sha256':profile['inventory_sha256'],'driver_count':len(rows),'acknowledged_unresolved':sorted(unresolved),'python_auto_execution':False,'embedded_text_execution':False}
    if job:atomic_json(job/'driver-policy.json',report)
    return report

def driven_value(owner,f):
    try:
        value=owner.path_resolve(f.data_path)
        if hasattr(value,'__len__') and not isinstance(value,str):
            if f.array_index<0 or f.array_index>=len(value):raise ValueError('Array index out of range')
            value=value[f.array_index]
        elif f.array_index!=0:raise ValueError('Scalar driver requires index zero')
    except (ValueError,KeyError,AttributeError,IndexError,TypeError) as exc:
        raise Failure('VALIDATION_FAILED','Invalid driven property or array index: '+f.data_path) from exc
    if not isinstance(value,(int,float,bool)) or not math.isfinite(value):raise Failure('VALIDATION_FAILED','Native driver output is not finite scalar')
    return value

def evaluated(profile):
    if profile is None:return
    rows,handles=inventory()
    for r in rows:
        if not r['driven_path_resolved'] or r['mute']:continue
        owner,f=handles[r['driver_id']]
        if not f.is_valid or not f.driver.is_valid:raise Failure('VALIDATION_FAILED','Active native driver failed evaluation: '+r['driver_id'])
        values={v.name:variable_bounds(v) for v in f.driver.variables}
        if f.driver.type=='SCRIPTED':prove(f.driver.expression,values,bpy.context.scene.frame_current_final)
        elif values:
            pairs=list(values.values())
            if f.driver.type=='SUM':interval(sum(p[0] for p in pairs),sum(p[1] for p in pairs))
            else:interval(*(x for p in pairs for x in p))
        driven_value(owner,f)

def variable_bounds(variable):
    if variable.type=='SINGLE_PROP':
        t=variable.targets[0]
        try:return interval(t.id.path_resolve(t.data_path))
        except (ValueError,TypeError,AttributeError) as exc:raise Failure('VALIDATION_FAILED','Driver input is no longer a finite scalar') from exc
    graph=bpy.context.evaluated_depsgraph_get();bounds=[];positions=[]
    for t in variable.targets:
        obj=t.id.evaluated_get(graph);pose=obj.pose.bones.get(t.bone_target) if t.bone_target and obj.pose else None
        matrices=[obj.matrix_world,obj.matrix_local,obj.matrix_basis]
        if pose:
            matrices.extend([pose.matrix,pose.matrix_basis,obj.matrix_world@pose.matrix])
            for space in ('LOCAL','LOCAL_WITH_PARENT','WORLD'):
                matrices.append(obj.convert_space(pose_bone=pose,matrix=pose.matrix,from_space='POSE',to_space=space))
        positions.append((obj.matrix_world@pose.matrix if pose else obj.matrix_world).translation)
        raw=[x for m in matrices for row in m for x in row]
        for target in (obj,pose):
            if target:
                for name in ('location','scale','rotation_euler','rotation_quaternion','rotation_axis_angle'):raw.extend(getattr(target,name))
        interval(*raw)
        bounds.append(10*max(1,math.tau,*(abs(v) for v in raw)))
    if variable.type=='LOC_DIFF':return interval((positions[0]-positions[1]).length)
    if variable.type=='ROTATION_DIFF':return (0,math.pi)
    return interval(-max(bounds),max(bounds))
