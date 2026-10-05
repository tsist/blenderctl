# SPDX-License-Identifier: GPL-3.0-or-later
"""Versioned three-way graph reconciliation; no source saves or GUI operations."""
import copy
import hashlib
import json
import os
from .contract import WorkflowError
from . import core as legacy

KEY = 'mw_id'
STATE = 'mw_state_v2'
GLAZE_SOCKETS = {'ior':'IOR','coat_weight':'Coat Weight','coat_roughness':'Coat Roughness','coat_ior':'Coat IOR'}
GLAZE_DEFAULTS = {'ior':1.5,'coat_weight':0.0,'coat_roughness':0.03,'coat_ior':1.5}

def _digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def _flatten(value, prefix=''):
    result = {}
    if isinstance(value, dict):
        for key, child in value.items(): result.update(_flatten(child, prefix + '/' + key))
    else: result[prefix] = value
    return result

def _capture(material):
    result = legacy._capture_managed_state_v1(material)
    for nid, node in legacy._nodes(material).items():
        result['structure'][nid]['group'] = group_fingerprint(node.node_tree) if node.type == 'GROUP' else None
        for prop in ('blend_type','use_clamp','mute'):
            if hasattr(node,prop): result['structure'][nid][prop]=getattr(node,prop)
        if node.type == 'TEX_IMAGE' and node.image:
            path = os.path.abspath(__import__('bpy').path.abspath(node.image.filepath))
            image = result['fields'][nid]['image']
            if not os.path.isfile(path): raise WorkflowError('missing_resource', 'Managed texture missing: ' + path)
            image['sha256'] = _digest(path)
            image.pop('file', None); image.pop('id', None)
    return result

capture_managed_state = _capture

def verify_managed_state(material):
    current = _capture(material)
    baseline = json.loads(material[STATE])['snapshot']
    conflicts = [part for part in current if current[part] != baseline[part]]
    return {'ok': not conflicts, 'conflicts': conflicts, 'state': current}

def group_fingerprint(group, _stack=(), _budget=None):
    """Content identity for bounded local shader groups without external IDs."""
    import bpy
    if group is None or group.bl_idname != 'ShaderNodeTree' or group.library or group.override_library:
        raise WorkflowError('unsupported_group', 'A local ShaderNodeTree is required')
    if group in _stack or len(_stack) >= 8:
        raise WorkflowError('unsupported_group', 'Recursive or excessively deep node group')
    if group.animation_data:
        raise WorkflowError('unsupported_group', 'Animated or driven groups are unsupported')
    budget = _budget if _budget is not None else [0]
    budget[0] += len(group.nodes)
    if budget[0] > 256: raise WorkflowError('unsupported_group', 'Group exceeds 256 total nodes')
    interface = []
    for item in group.interface.items_tree:
        if item.item_type == 'SOCKET':
            interface.append({'name':item.name,'direction':item.in_out,'type':item.socket_type,
                              'default':legacy._plain(item.default_value) if hasattr(item,'default_value') else None})
    data = []
    indices = {n: i for i,n in enumerate(group.nodes)}
    for node in group.nodes:
        if node.bl_idname == 'ShaderNodeScript': raise WorkflowError('unsupported_group', 'Script nodes are unsupported')
        row = {'type':node.bl_idname,'mute':node.mute,'inputs':[(s.identifier,legacy._plain(s.default_value)) for s in node.inputs if hasattr(s,'default_value')], 'outputs':[(s.identifier,legacy._plain(s.default_value)) for s in node.outputs if hasattr(s,'default_value')]}
        for prop in node.bl_rna.properties:
            name = prop.identifier
            if prop.type == 'POINTER':
                value = getattr(node,name,None)
                if isinstance(value,bpy.types.ID):
                    if name == 'node_tree': row['group'] = group_fingerprint(value,_stack+(group,),budget)
                    else: raise WorkflowError('unsupported_group','External ID dependency: '+name)
                elif value is not None and name not in ('rna_type','parent','color_ramp'):
                    raise WorkflowError('unsupported_group','Unfingerprinted nested node data: '+name)
            elif not prop.is_readonly and prop.type in ('BOOLEAN','ENUM','INT','FLOAT','STRING') and name not in ('name','label','location','width','height','select','show_options','show_preview','show_texture','hide'):
                row[name] = legacy._plain(getattr(node,name))
        if hasattr(node,'color_ramp'):
            ramp=node.color_ramp
            ramp_values={}
            for prop in ramp.bl_rna.properties:
                if prop.identifier=='rna_type': continue
                if prop.identifier=='elements': continue
                if prop.type in ('BOOLEAN','ENUM','INT','FLOAT','STRING'):
                    ramp_values[prop.identifier]=legacy._plain(getattr(ramp,prop.identifier))
                else:
                    raise WorkflowError('unsupported_group','Unfingerprinted ColorRamp data: '+prop.identifier)
            # Includes interpolation, color_mode and hue_interpolation in the
            # actual runtime RNA; alpha is contained in each RGBA color.
            ramp_values['elements']=[{'position':e.position,'color':list(e.color)} for e in ramp.elements]
            row['ramp']=ramp_values
        if hasattr(node,'mapping'):
            raise WorkflowError('unsupported_group','Curve mapping nodes are not yet fingerprinted')
        data.append(row)
    links = sorted((indices[l.from_node],l.from_socket.identifier,indices[l.to_node],l.to_socket.identifier) for l in group.links)
    return hashlib.sha256(legacy._json({'interface':interface,'nodes':data,'links':links}).encode()).hexdigest()

def _effect(layer):
    import bpy
    spec = layer.get('effect')
    if not spec: return None
    group = bpy.data.node_groups.get(spec['group'])
    if group is None: raise WorkflowError('missing_group','Effect group missing: '+spec['group'])
    if group_fingerprint(group) != spec['expected_sha256']:
        raise WorkflowError('group_sha_mismatch','Effect group fingerprint differs: '+spec['group'])
    sockets = [x for x in group.interface.items_tree if x.item_type == 'SOCKET']
    for direction,key in [('INPUT','input_socket'),('OUTPUT','output_socket')]:
        matches = [s for s in sockets if s.in_out == direction and s.name == spec[key] and s.socket_type == 'NodeSocketShader']
        if len(matches) != 1: raise WorkflowError('unsupported_group','Effect requires unique Shader input/output')
    for parameter in spec['parameters']:
        matches = [s for s in sockets if s.in_out == 'INPUT' and s.name == parameter['socket'] and s.socket_type in ('NodeSocketFloat','NodeSocketVector','NodeSocketColor')]
        if len(matches) != 1: raise WorkflowError('unsupported_group','Unsupported or ambiguous effect parameter')
        value = parameter['value']; kind = matches[0].socket_type
        if (kind == 'NodeSocketFloat' and isinstance(value,list)) or (kind != 'NodeSocketFloat' and (not isinstance(value,list) or len(value) != (4 if kind == 'NodeSocketColor' else 3))):
            raise WorkflowError('unsupported_group','Effect parameter shape mismatch')
    return group

def _build(material, manifest, files, groups, reusable_images=()):
    import bpy
    tree = material.node_tree; tree.nodes.clear(); nodes = {}; image_pool=list(reusable_images)
    def node(nid,kind,parent=None,xy=(0,0)):
        n=tree.nodes.new(kind); n[KEY]=nid; n.name='MW/'+nid
        if parent: n.parent=parent
        n.location=xy; nodes[nid]=n; return n
    def link(a,out,b,inp): tree.links.new(a.outputs[out],b.inputs[inp])
    previous=None
    sockets={'base_color':'Base Color','roughness':'Roughness','metallic':'Metallic','alpha':'Alpha','emission_color':'Emission Color','emission_strength':'Emission Strength'}
    for index,layer in enumerate(manifest['layers']):
        lid=layer['id']; frame=node(lid+'/frame','NodeFrame',xy=(index*1700,0)); frame.label=layer['name']
        shader=node(lid+'/shader','ShaderNodeBsdfPrincipled',frame,(1100,0))
        for role,socket in sockets.items(): shader.inputs[socket].default_value=layer['values'][role]
        for role,socket in GLAZE_SOCKETS.items(): shader.inputs[socket].default_value=layer['values'].get(role,GLAZE_DEFAULTS[role])
        uv=node(lid+'/uv','ShaderNodeUVMap',frame); uv.uv_map=manifest['target']['uv_layer']
        mapping=node(lid+'/mapping','ShaderNodeMapping',frame,(200,0)); link(uv,'UV',mapping,'Vector')
        for role,socket in [('scale','Scale'),('rotation','Rotation'),('translation','Location')]: mapping.inputs[socket].default_value=layer['mapping'][role]
        textures={}
        for count,role in enumerate(list(layer['channels'])+(['mask'] if layer['mask'] else [])):
            tex=node(lid+'/texture/'+role,'ShaderNodeTexImage',frame,(450,-count*270)); textures[role]=tex
            path,spec=files[(lid,role)]; iid='mw_image_v2/'+spec['expected_sha256']+'/'+spec['color_space']
            candidates=[i for i in image_pool if i.get(KEY)==iid and i.source=='FILE' and i.colorspace_settings.name==spec['color_space'] and os.path.isfile(bpy.path.abspath(i.filepath)) and _digest(bpy.path.abspath(i.filepath))==spec['expected_sha256']]
            image=candidates[0] if candidates else bpy.data.images.load(path,check_existing=False)
            if not candidates: image[KEY]=iid; image.colorspace_settings.name=spec['color_space']
            if image not in image_pool: image_pool.append(image)
            tex.image=image; link(mapping,'Vector',tex,'Vector')
            destination=sockets.get('emission_color' if role=='emission' else role)
            if destination and role not in ('base_color','roughness'): link(tex,'Color',shader,destination)
        tint=node(lid+'/tint','ShaderNodeMixRGB',frame,(800,0)); tint.blend_type='MULTIPLY'; tint.inputs[0].default_value=1
        tint.inputs[1].default_value=layer['values']['base_color']; tint.inputs[2].default_value=layer['adjustments']['color_tint']
        if 'base_color' in textures: link(textures['base_color'],'Color',tint,1)
        link(tint,'Color',shader,'Base Color')
        scale=node(lid+'/roughness_scale','ShaderNodeMath',frame,(800,-250)); scale.operation='MULTIPLY'; scale.inputs[0].default_value=layer['values']['roughness']; scale.inputs[1].default_value=layer['adjustments']['roughness_scale']
        bias=node(lid+'/roughness_bias','ShaderNodeMath',frame,(950,-250)); bias.operation='ADD'; bias.use_clamp=True; bias.inputs[1].default_value=layer['adjustments']['roughness_bias']; link(scale,0,bias,0); link(bias,0,shader,'Roughness')
        if 'roughness' in textures: link(textures['roughness'],'Color',scale,0)
        normal=None
        if 'normal' in textures:
            # OpenGL tangent normals (+Y); image bytes are never converted here.
            normal=node(lid+'/normal','ShaderNodeNormalMap',frame,(800,-700)); normal.space='TANGENT'; normal.uv_map=manifest['target']['uv_layer']; normal.inputs['Strength'].default_value=layer['values']['normal_strength']; link(textures['normal'],'Color',normal,'Color')
        if 'height' in textures:
            bump=node(lid+'/bump','ShaderNodeBump',frame,(950,-900)); bump.inputs['Distance'].default_value=layer['values']['height_distance']; link(textures['height'],'Color',bump,'Height')
            if normal: link(normal,'Normal',bump,'Normal')
            link(bump,'Normal',shader,'Normal')
            normal=bump
        elif normal: link(normal,'Normal',shader,'Normal')
        # The bundled Principled OSL shader defaults CoatNormal independently to
        # global N. Explicitly share the final tangent/bump result for a coated
        # surface; zero-coat graphs retain their historical links unchanged.
        if normal and layer['values'].get('coat_weight',GLAZE_DEFAULTS['coat_weight'])>0:
            link(normal,'Normal',shader,'Coat Normal')
        surface=shader; surface_socket='BSDF'
        if groups[lid]:
            effect=node(lid+'/effect','ShaderNodeGroup',frame,(1300,-400)); effect.node_tree=groups[lid]; spec=layer['effect']
            for parameter in spec['parameters']: effect.inputs[parameter['socket']].default_value=parameter['value']
            link(shader,'BSDF',effect,spec['input_socket']); surface=effect; surface_socket=spec['output_socket']
        if index:
            factor=node(lid+'/factor','ShaderNodeMath',frame,(1000,300)); factor.operation='MULTIPLY'; factor.inputs[0].default_value=layer['opacity'] if layer['enabled'] else 0; factor.inputs[1].default_value=1
            if 'mask' in textures: link(textures['mask'],'Color',factor,1)
            mix=node(lid+'/mix','ShaderNodeMixShader',frame,(1450,200)); link(factor,0,mix,0); link(previous,previous_socket,mix,1); link(surface,surface_socket,mix,2); previous=mix; previous_socket=0
        else: previous=surface; previous_socket=surface_socket
    output=node('output','ShaderNodeOutputMaterial',xy=(len(manifest['layers'])*1700,0)); link(previous,previous_socket,output,'Surface')
    return nodes

def _set_fields(node, wanted, template):
    node.label=wanted['label']
    for prop in ('operation','uv_map','space','vector_type'):
        if prop in wanted: setattr(node,prop,wanted[prop])
    for socket in node.inputs:
        if socket.identifier in wanted['inputs']: socket.default_value=wanted['inputs'][socket.identifier]
    if node.type=='TEX_IMAGE': node.image=template.image

def _commit_checkpoint(stage):
    """Private fault-injection seam; never configurable by manifests or CLI."""
    pass

def _restore_managed(material, backup):
    """Restore owned nodes in the original data block; leave manual nodes intact."""
    tree=material.node_tree; originals=legacy._nodes(backup); nodes=legacy._nodes(material)
    for nid in list(nodes):
        if nid not in originals or nodes[nid].bl_idname!=originals[nid].bl_idname:
            tree.nodes.remove(nodes.pop(nid))
    for nid,template in originals.items():
        if nid not in nodes:
            nodes[nid]=tree.nodes.new(template.bl_idname); nodes[nid][KEY]=nid
        node=nodes[nid]
        if node.type=='GROUP': node.node_tree=template.node_tree
        for prop in ('operation','uv_map','space','vector_type','invert','interpolation','projection','extension','is_active_output','blend_type','use_clamp','mute'):
            if hasattr(template,prop): setattr(node,prop,getattr(template,prop))
        fields={'label':template.label,'inputs':{s.identifier:legacy._plain(s.default_value) for s in template.inputs if hasattr(s,'default_value')}}
        _set_fields(node,fields,template)
        node.name=template.name; node.width=template.width; node.hide=template.hide; node.select=template.select
        node.use_custom_color=template.use_custom_color; node.color=template.color
        for key in list(node.keys()): del node[key]
        for key in template.keys(): node[key]=template[key]
    # Copy parents before relative locations; manual children must also recover.
    lookup={n.name:n for n in tree.nodes}
    for template in backup.node_tree.nodes:
        node=nodes[template[KEY]] if KEY in template else lookup.get(template.name)
        if node:
            node.parent=nodes.get(template.parent.get(KEY)) if template.parent and KEY in template.parent else (lookup.get(template.parent.name) if template.parent else None)
            node.location=template.location
    for link in list(tree.links):
        if KEY in link.from_node or KEY in link.to_node: tree.links.remove(link)
    for link in backup.node_tree.links:
        if KEY not in link.from_node and KEY not in link.to_node: continue
        src=nodes[link.from_node[KEY]] if KEY in link.from_node else lookup[link.from_node.name]
        dst=nodes[link.to_node[KEY]] if KEY in link.to_node else lookup[link.to_node.name]
        tree.links.new(src.outputs[link.from_socket.identifier],dst.inputs[link.to_socket.identifier])
    for key in list(material.keys()): del material[key]
    for key in backup.keys(): material[key]=backup[key]

def apply_material(manifest,resource_paths=None):
    import bpy
    from .engine_compat import validate_manifest, validate_material
    compatibility=validate_manifest(manifest)
    scene=bpy.data.scenes.get(manifest['context']['scene']); target=manifest['target']; slot=target['material_slot']
    if not scene or manifest['context']['view_layer'] not in scene.view_layers: raise WorkflowError('invalid_context','Missing context')
    obj=scene.objects.get(target['object'])
    if obj is None or obj.type!='MESH' or obj.name not in scene.view_layers[manifest['context']['view_layer']].objects: raise WorkflowError('invalid_target','Explicit visible mesh required')
    if obj.library or obj.data.library or obj.override_library or obj.data.override_library or obj.mode!='OBJECT': raise WorkflowError('protected_target','Local non-override Object Mode target required')
    if target['uv_layer'] not in obj.data.uv_layers: raise WorkflowError('missing_uv','UV layer missing')
    if slot>=len(obj.material_slots) and obj.data.users>1: raise WorkflowError('shared_slot','Cannot add shared mesh slot')
    matches=[m for m in bpy.data.materials if m.get(KEY)==manifest['material']['id']]
    if len(matches)>1: raise WorkflowError('managed_conflict','Duplicate material identity')
    material=matches[0] if matches else None
    if material and (material.override_library or (material.node_tree and (material.node_tree.library or material.node_tree.override_library))):
        raise WorkflowError('protected_material','Library override material or node tree requires explicit isolation')
    if material and (material.animation_data or (material.node_tree and material.node_tree.animation_data)):
        raise WorkflowError('protected_material','Animated or driven materials are outside the static workflow')
    if material and (material.library or any(s.material==material and (other!=obj or i!=slot) for other in bpy.data.objects for i,s in enumerate(other.material_slots))): raise WorkflowError('shared_material','Explicit material isolation required')
    old=None; upgrading=False
    if material:
        if STATE in material: old=json.loads(material[STATE])
        elif legacy.STATE in material:
            old=json.loads(material[legacy.STATE]); upgrading=True
            raw=legacy.capture_managed_state(material)
            if raw!=old['snapshot']: raise WorkflowError('managed_conflict','V1 upgrade requires unchanged managed baseline')
            old['snapshot']=_capture(material)
        else: raise WorkflowError('managed_conflict','Missing baseline')
        if old['manifest']['target']!=target: raise WorkflowError('managed_conflict','Binding migration requires explicit isolation')
        if slot>=len(obj.material_slots) or obj.material_slots[slot].material!=material or obj.material_slots[slot].link!='OBJECT': raise WorkflowError('managed_conflict','Material slot was edited')
    files={}; groups={}; relocations=[]
    for layer in manifest['layers']:
        groups[layer['id']]=_effect(layer)
        for role,spec in list(layer['channels'].items())+([('mask',layer['mask'])] if layer['mask'] else []):
            path=(resource_paths or {}).get(spec['file'],spec['file'])
            if not os.path.isfile(path): raise WorkflowError('missing_resource','Missing texture: '+path)
            if _digest(path)!=spec['expected_sha256']: raise WorkflowError('resource_sha_mismatch','Texture hash mismatch: '+path)
            files[(layer['id'],role)]=(path,spec)
            nid=layer['id']+'/texture/'+role
            if material and nid in legacy._nodes(material):
                image=legacy._nodes(material)[nid].image
                previous_path=os.path.abspath(bpy.path.abspath(image.filepath)) if image else None
                if previous_path and os.path.normcase(previous_path)!=os.path.normcase(os.path.abspath(path)):
                    relocations.append({'node':nid,'from':previous_path,'to':os.path.abspath(path),'sha256':spec['expected_sha256']})
    legacy._preflight_image_files(files)
    before_images=set(bpy.data.images); desired_material=bpy.data.materials.new('__MW_DESIRED__'); desired_material.use_nodes=True
    backup=None; fresh=material is None; commit_started=False
    previous_slots=[(s.link,s.material) for s in obj.material_slots]
    try:
        reusable_images={node.image for node in legacy._nodes(material).values() if node.type=='TEX_IMAGE' and node.image} if material else ()
        desired_nodes=_build(desired_material,manifest,files,groups,reusable_images); desired=_capture(desired_material)
        baseline=old['snapshot'] if old else {'structure':{},'fields':{},'links':[]}; current=_capture(material) if material else baseline
        base_nodes=baseline['structure']; current_nodes=current['structure']; wanted_nodes=desired['structure']
        if current_nodes!=base_nodes: raise WorkflowError('managed_conflict','Managed structure was edited')
        if current['links']!=baseline['links']: raise WorkflowError('managed_conflict','Managed links were edited')
        for nid,data in current['fields'].items():
            if data.get('image') != baseline['fields'].get(nid,{}).get('image'):
                raise WorkflowError('managed_conflict','Managed image content or color space was edited: '+nid)
        removed=set(base_nodes)-set(wanted_nodes)
        if material:
            for node in material.node_tree.nodes:
                if KEY not in node and node.parent and node.parent.get(KEY) in removed:
                    raise WorkflowError('managed_conflict','Deleting frame would reparent a manual node: '+node.name)
        changed=['structure/'+nid for nid in sorted(set(base_nodes)|set(wanted_nodes)) if base_nodes.get(nid)!=wanted_nodes.get(nid)]
        if desired['links']!=baseline['links']: changed.append('links')
        effective=copy.deepcopy(desired['fields'])
        for nid in base_nodes:
            if nid in removed or base_nodes[nid]!=wanted_nodes.get(nid):
                if current['fields'][nid]!=baseline['fields'][nid]: raise WorkflowError('managed_conflict','Deleting/replacing edited node: '+nid)
                continue
            b=_flatten(baseline['fields'][nid]); c=_flatten(current['fields'][nid]); d=_flatten(desired['fields'][nid])
            for key,value in d.items():
                if b.get(key)!=value:
                    if c.get(key)!=b.get(key): raise WorkflowError('managed_conflict','Manual field overlap: '+nid+key)
                    changed.append(nid+key)
                elif c.get(key)!=b.get(key):
                    parts=key.strip('/').split('/'); dest=effective[nid]
                    for part in parts[:-1]: dest=dest[part]
                    dest[parts[-1]]=c[key]
        current_layout={nid:list(node.location) for nid,node in legacy._nodes(material).items()} if material else {}
        desired_layout={nid:list(node.location) for nid,node in desired_nodes.items()}
        baseline_layout=old.get('layout',{}) if old else {}
        if old and not baseline_layout:
            # Older v2 packages and v1 upgrades have deterministic frame presets.
            baseline_layout=copy.deepcopy(current_layout)
            for index,layer in enumerate(old['manifest']['layers']): baseline_layout[layer['id']+'/frame']=[index*1700.0,0.0]
        backup=material.copy() if material else None
        commit_started=True
        if fresh:
            material=bpy.data.materials.new(manifest['material']['name']); material.use_nodes=True; material.node_tree.nodes.clear(); material[KEY]=manifest['material']['id']
        nodes=legacy._nodes(material); tree=material.node_tree
        # All expected failures have been checked above. Commit only managed deltas.
        for nid in list(nodes):
            if nid not in wanted_nodes or base_nodes[nid]!=wanted_nodes[nid]: tree.nodes.remove(nodes.pop(nid))
        _commit_checkpoint('after_remove')
        for nid,template in desired_nodes.items():
            created=nid not in nodes
            if created:
                n=tree.nodes.new(template.bl_idname); n[KEY]=nid; n.name='MW/'+nid; n.location=template.location; nodes[nid]=n
            n=nodes[nid]
            if n.type=='GROUP': n.node_tree=template.node_tree
            for prop in ('operation','uv_map','space','vector_type','invert','interpolation','projection','extension','is_active_output','blend_type','use_clamp'):
                if hasattr(template,prop): setattr(n,prop,getattr(template,prop))
            _set_fields(n,effective[nid],template)
        for nid,n in nodes.items(): n.parent=nodes[wanted_nodes[nid]['parent']] if wanted_nodes[nid]['parent'] else None
        for nid,node in nodes.items():
            if nid not in current_layout or current_layout[nid]==baseline_layout.get(nid): node.location=desired_layout[nid]
        _commit_checkpoint('after_fields')
        if desired['links']!=current['links']:
            for l in list(tree.links):
                if KEY in l.from_node or KEY in l.to_node: tree.links.remove(l)
            for src,output,dst,inp in desired['links']: tree.links.new(nodes[src].outputs[output],nodes[dst].inputs[inp])
        compatibility['material']=validate_material(material,compatibility['engine'])
        while len(obj.material_slots)<=slot: obj.data.materials.append(None)
        obj.material_slots[slot].link='OBJECT'; obj.material_slots[slot].material=material
        _commit_checkpoint('after_bind')
        material[STATE]=legacy._json({'manifest':copy.deepcopy(manifest),'snapshot':desired,'layout':desired_layout})
        if legacy.STATE in material: del material[legacy.STATE]
        _commit_checkpoint('after_state')
        return {'material_name':material.name,'material_id':manifest['material']['id'],'target':dict(target),'counts':{'nodes':len(nodes),'layers':len(manifest['layers']),'images':len({n.image for n in nodes.values() if n.type=='TEX_IMAGE' and n.image})},'changed_fields':changed,'resource_relocations':relocations,'reused':not fresh,'upgraded':upgrading,'compatibility':compatibility,'limitations':['Local static shader groups only; shared managed material requires isolation','Texture constants are fallbacks; adjustments multiply color and scale/bias roughness','Normal textures use OpenGL +Y tangent convention; image bytes are not converted']}
    except Exception:
        if commit_started:
            if backup: _restore_managed(material,backup)
            while len(obj.material_slots)>len(previous_slots): obj.data.materials.pop(index=len(obj.material_slots)-1)
            for index,(link,bound) in enumerate(previous_slots):
                obj.material_slots[index].link=link; obj.material_slots[index].material=bound
            if fresh and material: bpy.data.materials.remove(material)
        raise
    finally:
        if backup: bpy.data.materials.remove(backup)
        bpy.data.materials.remove(desired_material)
        for image in list(bpy.data.images):
            if image.get(KEY) and image.users==0 and image not in before_images: bpy.data.images.remove(image)

def existing_resource_paths(material,manifest):
    if material is None or STATE not in material: return {}
    old=json.loads(material[STATE]); current=_capture(material); result={}
    for layer in manifest['layers']:
        for role,spec in list(layer['channels'].items())+([('mask',layer['mask'])] if layer['mask'] else []):
            nid=layer['id']+'/texture/'+role
            expected=old['snapshot']['fields'].get(nid,{}).get('image')
            if expected and expected['sha256']==spec['expected_sha256'] and expected['color_space']==spec['color_space']:
                if current['fields'].get(nid,{}).get('image')!=expected: raise WorkflowError('managed_conflict','Image binding edited: '+nid)
                n=legacy._nodes(material)[nid]; path=os.path.abspath(__import__('bpy').path.abspath(n.image.filepath))
                if spec['file'] in result and result[spec['file']]!=path: raise WorkflowError('ambiguous_resource','Ambiguous image location')
                result[spec['file']]=path
    return result

def get_stored_manifest(material):
    key=STATE if STATE in material else legacy.STATE if legacy.STATE in material else None
    if key is None: raise WorkflowError('managed_conflict','Missing managed manifest')
    from .contract import normalize_manifest
    # Normalize only the returned draft. Older v2 stored snapshots already contain
    # every shader socket; leave their baselines untouched for three-way merging.
    return normalize_manifest(json.loads(material[key])['manifest'])

def export_current_manifest(material):
    import bpy
    if material.override_library or material.animation_data or (material.node_tree and (material.node_tree.override_library or material.node_tree.animation_data)):
        raise WorkflowError('export_unrepresentable','Static templates cannot export animated, driven or library override materials')
    if STATE not in material:
        if legacy.STATE not in material: raise WorkflowError('managed_conflict','Missing managed manifest')
        manifest=copy.deepcopy(json.loads(material[legacy.STATE])['manifest'])
        snapshot=legacy.capture_managed_state(material)
    else:
        manifest=copy.deepcopy(json.loads(material[STATE])['manifest']); snapshot=_capture(material)
    baseline=json.loads(material[STATE] if STATE in material else material[legacy.STATE])['snapshot']
    if STATE not in material and snapshot!=baseline:
        raise WorkflowError('export_unrepresentable','V1 export requires an unchanged managed baseline; resolve manual edits before exporting or upgrading')
    if snapshot['structure']!=baseline['structure'] or snapshot['links']!=baseline['links']:
        raise WorkflowError('managed_conflict','Cannot export an edited managed graph as the original template')
    nodes=legacy._nodes(material)
    for layer in manifest['layers']:
        lid=layer['id']; shader=nodes[lid+'/shader']; layer['name']=nodes[lid+'/frame'].label
        for role,socket in {'base_color':'Base Color','roughness':'Roughness','metallic':'Metallic','alpha':'Alpha','emission_color':'Emission Color','emission_strength':'Emission Strength'}.items(): layer['values'][role]=legacy._plain(shader.inputs[socket].default_value)
        if STATE in material:
            for role,socket in GLAZE_SOCKETS.items(): layer['values'][role]=legacy._plain(shader.inputs[socket].default_value)
        for role,socket in [('scale','Scale'),('rotation','Rotation'),('translation','Location')]: layer['mapping'][role]=list(nodes[lid+'/mapping'].inputs[socket].default_value)
        for role,spec in list(layer['channels'].items())+([('mask',layer['mask'])] if layer['mask'] else []):
            image=nodes[lid+'/texture/'+role].image; path=os.path.abspath(bpy.path.abspath(image.filepath)); spec.update(file=path,expected_sha256=_digest(path),color_space=image.colorspace_settings.name)
        if lid+'/factor' in nodes:
            factor=nodes[lid+'/factor']; value=factor.inputs[0].default_value
            expected=baseline['fields'][lid+'/factor']['inputs'][factor.inputs[0].identifier]
            if value!=expected:
                if not 0<=value<=1: raise WorkflowError('export_unrepresentable','Layer factor lies outside opacity bounds')
                layer['enabled']=value!=0
                if value!=0: layer['opacity']=float(value)
        if lid+'/normal' in nodes: layer['values']['normal_strength']=nodes[lid+'/normal'].inputs['Strength'].default_value
        if lid+'/bump' in nodes: layer['values']['height_distance']=nodes[lid+'/bump'].inputs['Distance'].default_value
        if lid+'/tint' in nodes:
            layer['values']['base_color']=list(nodes[lid+'/tint'].inputs[1].default_value); layer['values']['roughness']=nodes[lid+'/roughness_scale'].inputs[0].default_value
            layer['adjustments']={'color_tint':list(nodes[lid+'/tint'].inputs[2].default_value),'roughness_scale':nodes[lid+'/roughness_scale'].inputs[1].default_value,'roughness_bias':nodes[lid+'/roughness_bias'].inputs[1].default_value}
        if layer.get('effect'):
            for p in layer['effect']['parameters']: p['value']=legacy._plain(nodes[lid+'/effect'].inputs[p['socket']].default_value)
    if STATE in material:
        before_images=set(bpy.data.images); temp=bpy.data.materials.new('__MW_EXPORT_CHECK__');temp.use_nodes=True
        try:
            files={};groups={}
            for layer in manifest['layers']:
                groups[layer['id']]=_effect(layer)
                for role,spec in list(layer['channels'].items())+([('mask',layer['mask'])] if layer['mask'] else []): files[(layer['id'],role)]=(spec['file'],spec)
            _build(temp,manifest,files,groups,{n.image for n in nodes.values() if n.type=='TEX_IMAGE' and n.image})
            if _capture(temp)!=snapshot:
                raise WorkflowError('export_unrepresentable','Current managed values cannot be represented by this template; use a controlled node group')
        finally:
            bpy.data.materials.remove(temp)
            for image in list(bpy.data.images):
                if image not in before_images and image.users==0: bpy.data.images.remove(image)
    from .engine_compat import validate_manifest
    validate_manifest(manifest)
    return manifest
