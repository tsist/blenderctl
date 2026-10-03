# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit paired zones, stable state sockets and bounded region validation."""
import bpy
from protocol import Failure
from inspection import value
KINDS={'REPEAT':('GeometryNodeRepeatInput','GeometryNodeRepeatOutput','repeat_items'),'SIMULATION':('GeometryNodeSimulationInput','GeometryNodeSimulationOutput','state_items')}
INPUTS={v[0] for v in KINDS.values()};OUTPUTS={v[1] for v in KINDS.values()};TYPES=INPUTS|OUTPUTS
SUPPORTED={'GEOMETRY','FLOAT','VECTOR'}
def kind_for(output):
    for name,(_,typ,attr) in KINDS.items():
        if output.bl_idname==typ:return name,attr
    raise Failure('INVALID_REQUEST','Zone output endpoint required')
def state_items(output):
    kind,attr=kind_for(output);items=list(getattr(output,attr));sockets=[s for s in output.outputs if s.identifier.startswith('Item_')]
    if len(items)!=len(sockets):raise Failure('VALIDATION_FAILED','Zone state/socket count differs')
    return [(item,socket) for item,socket in zip(items,sockets)]
def item_records(output):
    return [{'name':i.name,'type':i.socket_type,'identifier':s.identifier,**({'attribute_domain':i.attribute_domain} if hasattr(i,'attribute_domain') else {})} for i,s in state_items(output)]
def pairs(tree):
    outputs=[n for n in tree.nodes if n.bl_idname in OUTPUTS];ins=[n for n in tree.nodes if n.bl_idname in INPUTS]
    for i in ins:
        o=i.paired_output
        if not o or o.id_data!=tree or o.bl_idname not in OUTPUTS or KINDS[kind_for(o)[0]][0]!=i.bl_idname:raise Failure('VALIDATION_FAILED','Missing or incompatible zone pair: '+i.name)
    result=[]
    for o in outputs:
        matching=[i for i in ins if i.paired_output==o]
        if len(matching)!=1:raise Failure('VALIDATION_FAILED','Zone output requires exactly one paired input: '+o.name)
        result.append((matching[0],o))
    return result
def reachable(start,graph,stop=None):
    visited=set();queue=[start]
    while queue:
        n=queue.pop()
        if n in visited or n==stop:continue
        visited.add(n);queue.extend(graph.get(n,()))
    return visited
def validate_tree(tree,strict=True):
    for n in tree.nodes:
        child=getattr(n,'node_tree',None)
        if child and any(x.bl_idname in TYPES for x in child.nodes):raise Failure('UNSUPPORTED','Zones inside nested node groups need a separate work and bake-ID adapter')
    found=pairs(tree)
    if not found:return
    if tree.bl_idname!='GeometryNodeTree' or len(found)>16:raise Failure('UNSUPPORTED','At most 16 zones in a geometry tree')
    graph={n.name:set() for n in tree.nodes};reverse={n.name:set() for n in tree.nodes}
    for link in tree.links:
        graph[link.from_node.name].add(link.to_node.name);reverse[link.to_node.name].add(link.from_node.name)
        if link.to_node.bl_idname=='GeometryNodeRepeatInput' and link.to_socket.identifier=='Iterations':raise Failure('UNSUPPORTED','Repeat iterations must be an explicit bounded constant')
    bodies={}
    for i,o in found:
        items=item_records(o)
        if not 1<=len(items)<=8 or len({x['name'] for x in items})!=len(items) or len({x['identifier'] for x in items})!=len(items):raise Failure('VALIDATION_FAILED','Zone state items require unique names/identifiers and count 1..8')
        if any(x['type'] not in SUPPORTED for x in items) or sum(x['type']=='GEOMETRY' for x in items)!=1:raise Failure('UNSUPPORTED','Zone states require exactly one geometry plus bounded float/vector items')
        if i.bl_idname=='GeometryNodeRepeatInput':
            s=i.inputs['Iterations']
            if s.is_linked or not 1<=s.default_value<=1000:raise Failure('UNSUPPORTED','Repeat count must be a constant 1..1000')
        forward=reachable(i.name,graph,o.name);backward=reachable(o.name,reverse,i.name);body=(forward&backward)-{i.name,o.name};bodies[o.name]=body|{i.name,o.name}
        if strict:
            for n in body|{i.name}:
                if graph[n]-body-{o.name}:raise Failure('CONFLICT','Link leaves zone without its output endpoint: '+tree.name+'/'+n)
    regions=list(bodies.values())
    for index,x in enumerate(regions):
        for y in regions[index+1:]:
            if x&y and not (x<=y or y<=x):raise Failure('CONFLICT','Zone regions overlap without proper nesting')
    for i,o in found:
        containing=[(a,b) for a,b in found if bodies[o.name]<=bodies[b.name]]
        if len(containing)>8:raise Failure('UNSUPPORTED','Zone nesting exceeds eight')
        product=1
        for a,b in containing:
            if a.bl_idname=='GeometryNodeRepeatInput':product*=a.inputs['Iterations'].default_value
        if product>1000:raise Failure('UNSUPPORTED','Nested repeat iteration product exceeds 1000')
def validate_all(strict=True):
    for tree in bpy.data.node_groups:
        if tree.bl_idname=='GeometryNodeTree':validate_tree(tree,strict)
def snapshot():
    result=[]
    for tree in sorted(bpy.data.node_groups,key=lambda t:t.name):
        if tree.bl_idname!='GeometryNodeTree':continue
        for i,o in pairs(tree):
            kind,_=kind_for(o)
            result.append({'tree':tree.name,'kind':kind,'input':i.name,'output':o.name,'items':item_records(o),'iterations':i.inputs['Iterations'].default_value if kind=='REPEAT' else None,'input_defaults':[{'identifier':s.identifier,'value':value(s.default_value)} for s in i.inputs if hasattr(s,'default_value')],'skip':bool(o.inputs['Skip'].default_value) if kind=='SIMULATION' else None})
    return sorted(result,key=lambda r:(r['tree'],r['output']))

def fresh_modifier(o,old):
    """Replace only the in-memory modifier, retaining graph, ID and VALUE inputs.

    Changing bake_directory does not detach Blender's internal PACKED history.
    A fresh modifier on the same tree preserves native node bake IDs.
    """
    group=old.node_group;name=old.name;index=list(o.modifiers).index(old)
    inputs=[]
    for s in group.interface.items_tree:
        if s.item_type!='SOCKET' or s.in_out!='INPUT':continue
        prop=getattr(old.properties.inputs,s.identifier,None)
        if not hasattr(prop,'value'):continue
        if hasattr(prop,'type') and prop.type!='VALUE':raise Failure('UNSUPPORTED','Fresh zone cache requires VALUE modifier inputs')
        raw=prop.value
        inputs.append((s.identifier,raw[:] if hasattr(raw,'to_list') or hasattr(raw,'to_tuple') else raw))
    ids=[(b.bake_id,b.node.name if b.node else None) for b in old.bakes]
    fresh=o.modifiers.new('__blenderctl_fresh_zone','NODES');fresh.node_group=group
    try:
        for p in old.bl_rna.properties:
            if p.is_readonly or p.identifier in ('name','bake_directory','bake_target'):continue
            if p.type in ('BOOLEAN','INT','FLOAT','STRING','ENUM'):setattr(fresh,p.identifier,getattr(old,p.identifier))
        for ident,raw in inputs:setattr(getattr(fresh.properties.inputs,ident),'value',raw)
        try:custom_keys=list(old.keys())
        except TypeError:custom_keys=[]  # Blender 5.2 NodesModifier has no IDProperties.
        for key in custom_keys:
            raw=old[key];fresh[key]=raw.to_dict() if hasattr(raw,'to_dict') else raw.to_list() if hasattr(raw,'to_list') else raw
        if ids!=[(b.bake_id,b.node.name if b.node else None) for b in fresh.bakes]:raise Failure('VALIDATION_FAILED','Fresh modifier changed native zone/bake identities')
        o.modifiers.move(len(o.modifiers)-1,index)
        o.modifiers.remove(old);fresh.name=name;o.update_tag();return fresh
    except BaseException:
        if fresh in o.modifiers[:]:o.modifiers.remove(fresh)
        raise

def detach_caches(job):
    """Node edits evaluate from an empty owned cache; never delete source bakes."""
    from pathlib import Path
    from protocol import atomic_json
    rows=[]
    for o in sorted(bpy.data.objects,key=lambda x:x.name):
        for m in list(o.modifiers):
            if m.type!='NODES' or not m.node_group or not m.bakes:continue
            old={'directory':m.bake_directory,'target':m.bake_target,'bakes':[{'id':b.bake_id,'directory':b.directory,'custom_path':b.use_custom_path,'target':b.bake_target} for b in m.bakes]}
            m=fresh_modifier(o,m)
            m.bake_target='DISK';m.bake_directory=str(Path(job)/'unbaked-node-cache'/str(len(rows)))
            for b in m.bakes:b.use_custom_path=False;b.directory='';b.bake_target='INHERIT'
            o.use_simulation_cache=True;o.update_tag()
            rows.append({'object':o.name,'modifier':m.name,'old':old,'new_directory':m.bake_directory,'receipt_invalidated':True,'source_cache_retained':True})
    if rows:atomic_json(Path(job)/'node-cache-detachment.json',rows)
def execute(op):
    import nodes,scenes
    t=nodes.tree_for(op['tree'])
    if t.bl_idname!='GeometryNodeTree' or op['tree']['kind']!='GROUP':raise Failure('INVALID_REQUEST','Zones require a standalone geometry group')
    action=op['op']
    if action=='zone.create':
        if op['input']==op['output']:raise Failure('INVALID_REQUEST','Zone endpoint names must differ')
        names=[x['name'] for x in op['items']]
        if len(set(names))!=len(names) or sum(x['type']=='GEOMETRY' for x in op['items'])!=1:raise Failure('INVALID_REQUEST','Unique state names and exactly one geometry state required')
        if (op['kind']=='REPEAT')!=('iterations' in op):raise Failure('INVALID_REQUEST','Only repeat requires iterations')
        for name in (op['input'],op['output']):scenes.fresh(t.nodes,name)
        it,ot,attr=KINDS[op['kind']];i=scenes.named(t.nodes.new(it),op['input']);o=scenes.named(t.nodes.new(ot),op['output']);getattr(o,attr).clear()
        for state in op['items']:getattr(o,attr).new(state['type'],state['name'])
        i.pair_with_output(o)
        if op['kind']=='REPEAT':i.inputs['Iterations'].default_value=op['iterations']
    elif action=='zone.iterations':
        i=scenes.find(t.nodes,op['input'])
        if i.bl_idname!='GeometryNodeRepeatInput':raise Failure('INVALID_REQUEST','Repeat input required')
        if i.inputs['Iterations'].is_linked:raise Failure('CONFLICT','Repeat iterations must be unlinked')
        i.inputs['Iterations'].default_value=op['value']
    elif action=='zone.remove':
        i=scenes.find(t.nodes,op['input']);o=scenes.find(t.nodes,op['output'])
        if i.bl_idname not in INPUTS or i.paired_output!=o:raise Failure('CONFLICT','Requested endpoints are not a pair')
        if any(l.from_node in (i,o) or l.to_node in (i,o) for l in t.links):raise Failure('CONFLICT','Unlink zone endpoints before paired removal')
        t.nodes.remove(i);t.nodes.remove(o)
    else:
        o=scenes.find(t.nodes,op['output']);kind,attr=kind_for(o);items=getattr(o,attr);current=state_items(o)
        if action=='zone.item.add':
            if len(current)>=8 or any(x.name==op['item']['name'] for x,s in current):raise Failure('CONFLICT','Duplicate state name or eight-item limit')
            if op['item']['type']=='GEOMETRY':raise Failure('UNSUPPORTED','A zone has exactly one geometry item')
            items.new(op['item']['type'],op['item']['name'])
        else:
            matches=[(idx,item,s) for idx,(item,s) in enumerate(current) if s.identifier==op['identifier']]
            if len(matches)!=1:raise Failure('NOT_FOUND','Zone state identifier not found')
            idx,item,s=matches[0]
            if action=='zone.item.rename':
                if any(x.name==op['name'] and x!=item for x,_ in current):raise Failure('CONFLICT','Duplicate zone state name')
                item.name=op['name']
            elif action=='zone.item.move':
                if op['index']>=len(current):raise Failure('INVALID_REQUEST','State destination index out of range')
                items.move(idx,op['index'])
            elif action=='zone.item.remove':
                if item.socket_type=='GEOMETRY':raise Failure('UNSUPPORTED','Cannot remove the required geometry state')
                i=next(i for i,p in pairs(t) if p==o)
                if any(l.from_node in (i,o) and l.from_socket.identifier==op['identifier'] or l.to_node in (i,o) and l.to_socket.identifier==op['identifier'] for l in t.links):raise Failure('CONFLICT','Unlink the state item before removal')
                items.remove(item)
            else:raise Failure('INVALID_REQUEST','Unknown zone operation')
    t.update_tag();validate_tree(t,False)
