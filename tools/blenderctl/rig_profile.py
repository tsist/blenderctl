# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only, unevaluated rig facts and conservative map suggestions.

No frame changes, depsgraph updates, operators, path evaluation or Python driver
execution occur here. Constraint cycles are structural candidates, not a claim
that Blender's solver cannot solve a rig.
"""
import ast,math,re,unicodedata
import bpy

def finite(value):
    if isinstance(value,(int,float)):return value if math.isfinite(value) else None
    return [finite(v) for v in value]

def node(obj,bone=None):return obj.name_full+('::'+bone if bone else '')

def path_bone(path):
    match=re.match(r'''^pose\.bones\[((?:"(?:[^"\\]|\\.)*")|(?:'(?:[^'\\]|\\.)*'))\]''',path)
    if not match:return None
    try:return ast.literal_eval(match.group(1))
    except (ValueError,SyntaxError):return None

def cycles(graph):
    """Iterative SCCs avoid recursion failure on production skeletons."""
    keys=set(graph)
    for values in graph.values():keys.update(values)
    visited=set();order=[]
    for root in sorted(keys):
        if root in visited:continue
        stack=[(root,False)]
        while stack:
            key,done=stack.pop()
            if done:order.append(key);continue
            if key in visited:continue
            visited.add(key);stack.append((key,True))
            stack.extend((v,False) for v in sorted(graph.get(key,[]),reverse=True) if v not in visited)
    reverse={key:[] for key in keys}
    for key,values in graph.items():
        for value in values:reverse[value].append(key)
    visited=set();result=[]
    for root in reversed(order):
        if root in visited:continue
        component=[];stack=[root];visited.add(root)
        while stack:
            key=stack.pop();component.append(key)
            for value in reverse[key]:
                if value not in visited:visited.add(value);stack.append(value)
        if len(component)>1 or root in graph.get(root,[]):result.append(sorted(component))
    return sorted(result)

def weights(mesh,rig):
    deform={bone.name for bone in rig.data.bones if bone.use_deform}
    groups={g.index:g.name for g in mesh.vertex_groups}
    invalid={'non_finite':[],'negative':[],'above_one':[],'unweighted':[],'not_normalized':[],'unknown_group_index':[]}
    sums=[];max_influences=0
    for vertex in mesh.data.vertices:
        total=0.;influences=0;bad=False
        for weight in vertex.groups:
            name=groups.get(weight.group)
            if name is None:invalid['unknown_group_index'].append(vertex.index)
            w=weight.weight
            if not math.isfinite(w):invalid['non_finite'].append(vertex.index);bad=True;continue
            if w<0:invalid['negative'].append(vertex.index)
            if w>1:invalid['above_one'].append(vertex.index)
            if name in deform:total+=w;influences+=w>0
        if not bad:
            sums.append(total)
            if total<=1e-8:invalid['unweighted'].append(vertex.index)
            if abs(total-1)>1e-4:invalid['not_normalized'].append(vertex.index)
        max_influences=max(max_influences,influences)
    return {'object':mesh.name_full,'rig':rig.name_full,'vertices':len(mesh.data.vertices),
            'scope':'deform-group sum; individual weight validity checks include all groups',
            'issues':{key:{'count':len(set(v)),'vertex_indices':sorted(set(v))} for key,v in invalid.items()},
            'orphan_groups':sorted(set(groups.values())-set(rig.data.bones.keys())),
            'non_deform_groups':sorted(set(groups.values()) & (set(rig.data.bones.keys())-deform)),
            'sum_min':min(sums,default=0.),'sum_max':max(sums,default=0.),'max_influences':max_influences}

def animation(owner):
    ad=getattr(owner,'animation_data',None)
    if not ad:return {'active_action':None,'active_slot':None,'nla':[],'drivers':[]}
    strips=[]
    def visit(items,track):
        for strip in items:
            slot=getattr(strip,'action_slot',None)
            strips.append({'track':track,'name':strip.name,'type':strip.type,'action':strip.action.name_full if strip.action else None,
                           'slot':slot.identifier if slot else None,'frame_start':finite(strip.frame_start),'frame_end':finite(strip.frame_end),'mute':strip.mute})
            if strip.type=='META':visit(strip.strips,track)
    for track in ad.nla_tracks:visit(track.strips,track.name)
    drivers=[]
    for curve in ad.drivers:
        targets=[]
        for variable in curve.driver.variables:
            for target in variable.targets:
                targets.append({'variable':variable.name,'type':variable.type,'id':target.id.name_full if target.id else None,
                                'bone':target.bone_target,'data_path':target.data_path})
        drivers.append({'data_path':curve.data_path,'array_index':curve.array_index,'type':curve.driver.type,
                        'expression':curve.driver.expression,'mute':curve.mute,'targets':targets})
    return {'active_action':ad.action.name_full if ad.action else None,'active_slot':ad.action_slot.identifier if ad.action_slot else None,'nla':strips,'drivers':drivers}

def profile():
    graph={};missing=[];nonfinite=[];rigs=[]
    def reference(origin,target,bone,kind):
        if target is None:
            missing.append({'owner':origin,'kind':kind,'target':None,'bone':bone});return
        if bone and (target.type!='ARMATURE' or bone not in target.data.bones):
            missing.append({'owner':origin,'kind':kind,'target':target.name_full,'bone':bone});return
        graph.setdefault(origin,set()).add(node(target,bone or None))
    def constraints(owner,obj,bone=None):
        result=[];origin=node(obj,bone)
        for c in owner.constraints:
            row={'name':c.name,'type':c.type,'mute':c.mute,'influence':finite(c.influence),'targets':[]}
            if row['influence'] is None:nonfinite.append({'owner':origin,'property':'constraint:'+c.name+':influence'})
            for prop,sub in [('target','subtarget'),('pole_target','pole_subtarget')]:
                if not hasattr(c,prop):continue
                target=getattr(c,prop);name=getattr(c,sub,'')
                row['targets'].append({'role':prop,'object':target.name_full if target else None,'bone':name})
                # Pole targets are optional; ordinary target properties are not.
                if target or name or prop=='target':reference(origin,target,name,c.type+':'+prop)
            for target in getattr(c,'targets',[]):
                row['targets'].append({'role':'targets','object':target.target.name_full if target.target else None,'bone':target.subtarget})
                reference(origin,target.target,target.subtarget,c.type+':targets')
            if c.type=='IK':
                chain=[];p=owner if bone else None
                while p and (c.chain_count==0 or len(chain)<c.chain_count):chain.append(p.name);p=p.parent
                row['ik']={'chain_count':c.chain_count,'affected_chain':chain,'use_tail':c.use_tail,'use_stretch':c.use_stretch,'iterations':c.iterations}
                for ancestor in chain[1:]:
                    for target in (c.target,c.pole_target):
                        if target:graph.setdefault(node(obj,ancestor),set()).add(node(target,c.subtarget if target==c.target else c.pole_subtarget or None))
            result.append(row)
        return result
    for obj in sorted(bpy.data.objects,key=lambda o:o.name_full):
        graph.setdefault(node(obj),set())
        if obj.parent:reference(node(obj),obj.parent,obj.parent_bone if obj.parent_type=='BONE' else '', 'parent')
        object_constraints=constraints(obj,obj)
        for owner in [obj,obj.data] if obj.data else [obj]:
            ad=getattr(owner,'animation_data',None)
            if not ad:continue
            for curve in ad.drivers:
                origin=node(obj)
                driven_bone=path_bone(curve.data_path)
                if driven_bone and (not obj.pose or driven_bone not in obj.pose.bones):
                    missing.append({'owner':origin,'kind':'driver:driven_bone','target':obj.name_full,'bone':driven_bone})
                if obj.pose:
                    for p in obj.pose.bones:
                        if curve.data_path.startswith(p.path_from_id()+'.'):origin=node(obj,p.name);break
                for variable in curve.driver.variables:
                    for target in variable.targets:
                        if isinstance(target.id,bpy.types.Object):reference(origin,target.id,target.bone_target or path_bone(target.data_path),'driver:'+variable.type)
                        elif target.id is None:missing.append({'owner':origin,'kind':'driver:'+variable.type,'target':None,'bone':target.bone_target})
        if obj.type!='ARMATURE':continue
        bones=[]
        for b in obj.data.bones:
            key=node(obj,b.name);graph.setdefault(key,set()).add(node(obj))
            if b.parent:graph[key].add(node(obj,b.parent.name))
            rest=finite(b.matrix_local)
            if any(x is None for row in rest for x in row):nonfinite.append({'owner':key,'property':'matrix_local'})
            for prop in ('head_local','tail_local','x_axis','y_axis','z_axis'):
                if any(x is None for x in finite(getattr(b,prop))):nonfinite.append({'owner':key,'property':prop})
            if not math.isfinite(b.length):nonfinite.append({'owner':key,'property':'length'})
            bones.append({'name':b.name,'parent':b.parent.name if b.parent else None,'use_deform':b.use_deform,
                          'role':'deform' if b.use_deform else 'non_deform_unspecified','head':finite(b.head_local),'tail':finite(b.tail_local),
                          'length':finite(b.length),'rest_matrix':rest,'axes':{'x':finite(b.x_axis),'y':finite(b.y_axis),'z':finite(b.z_axis)},
                          'connected':b.use_connect,'inherit_scale':b.inherit_scale,'constraints':constraints(obj.pose.bones[b.name],obj,b.name)})
        world=finite(obj.matrix_world)
        if any(x is None for row in world for x in row):nonfinite.append({'owner':node(obj),'property':'matrix_world'})
        rigs.append({'object':obj.name_full,'data':obj.data.name_full,'linked':bool(obj.library or obj.data.library),'bone_count':len(bones),
                     'deform_bone_count':sum(b['use_deform'] for b in bones),'scale':finite(obj.scale),'world_matrix':world,'bones':bones,
                     'constraints':object_constraints,'animation':animation(obj),'data_animation':animation(obj.data),'ik_fk_semantics':'unknown; no name-based control classification'})
    mesh_weights=[]
    for mesh in sorted((o for o in bpy.data.objects if o.type=='MESH'),key=lambda o:o.name_full):
        targets={m.object for m in mesh.modifiers if m.type=='ARMATURE' and m.object and m.object.type=='ARMATURE'}
        if mesh.parent and mesh.parent.type=='ARMATURE':targets.add(mesh.parent)
        for rig in sorted(targets,key=lambda o:o.name_full):mesh_weights.append(weights(mesh,rig))
    actions=[{'name':a.name_full,'slots':[{'identifier':s.identifier,'target_id_type':s.target_id_type} for s in a.slots]} for a in bpy.data.actions]
    return {'version':'1.0','mode':'UNEVALUATED_READ_ONLY','rigs':rigs,'actions':actions,'mesh_weights':mesh_weights,
            'diagnostics':{'missing_references':missing,'non_finite':nonfinite,'structural_cycles':cycles(graph)},
            'limits':['Constraint/driver graph cycles are structural candidates, not solver validation.','No evaluation, motion quality, IK/FK semantics, or scripted execution is certified.','Weights are reported for meshes with an armature modifier or armature parent.']}

def normalized_name(name):
    value=unicodedata.normalize('NFKC',name).casefold()
    value=value.rsplit(':',1)[-1]
    return re.sub(r'[^\w]+','',value)

def suggest(source,target):
    """Return ranked facts, never an executable or confirmed map."""
    if source.type!='ARMATURE' or target.type!='ARMATURE':raise ValueError('Armature objects required')
    def geometry(rig):
        bones=list(rig.data.bones);coords=[list(b.head_local) for b in bones]+[list(b.tail_local) for b in bones]
        if not coords:return {}
        if not all(math.isfinite(x) for c in coords for x in c):raise ValueError('Non-finite rest geometry')
        low=[min(c[i] for c in coords) for i in range(3)];high=[max(c[i] for c in coords) for i in range(3)]
        scale=max(math.dist(low,high),1e-12)
        return {b.name:([(b.head_local[i]-low[i])/scale for i in range(3)],b.length/scale) for b in bones}
    sg,tg=geometry(source),geometry(target);rows=[]
    for t in target.data.bones:
        candidates=[]
        for s in source.data.bones:
            name_equal=normalized_name(s.name)==normalized_name(t.name)
            distance=math.dist(sg[s.name][0],tg[t.name][0]);length_error=abs(sg[s.name][1]-tg[t.name][1])
            score=(0.65 if name_equal else 0)+0.25/(1+distance*10)+0.10/(1+length_error*10)
            candidates.append({'source':s.name,'score':round(score,8),'normalized_name_match':name_equal,'rest_head_distance':distance,'normalized_length_error':length_error})
        candidates.sort(key=lambda c:(-c['score'],c['source']));candidates=candidates[:5]
        rows.append({'target':t.name,'candidates':candidates,'ambiguous':len(candidates)>1 and candidates[0]['score']-candidates[1]['score']<0.05,'confirmed':False})
    return {'version':'1.0','source':source.name_full,'target':target.name_full,'confirmed':False,'mapping_suggestions':rows,
            'scope':'Names and axis-aligned normalized rest positions only; no semantic or coordinate-axis equivalence guarantee. Explicit reviewed one-to-one mapping required.'}
