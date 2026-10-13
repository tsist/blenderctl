"""静态原生合成核心。只操作已加载场景，不保存、不渲染、不修改全局偏好。"""
import hashlib
import json
import math
from contextlib import contextmanager
from pathlib import Path
from copy import deepcopy
import bpy
from .contract import (WorkflowError, normalize_manifest, NODE_TYPES, COMPONENT_DEFINITIONS, BINDING_SCHEMA,
    validate, media_sources, ANIMATION_TRACK_SCHEMA, TRACKING_DOCUMENT_SCHEMA, _check_track, _target_key)

OWNER='_cwf_owner'
ROLE='_cwf_role'
NODE_ID='_cwf_node_id'
SCENE_TAG='_cwf_scene'
STATE='_cwf_state_v1'
BINDING='_cwf_binding_v1_1'
BOUND_IMAGE='_cwf_binding_image_id'
MEDIA='_cwf_media_v1_2'
ANIM='_cwf_animation_v1_2'
ARCHIVE='_cwf_retained_actions_v1_2'
UI={'location','location_absolute','width','height','dimensions','label','select','show_options',
    'show_preview','hide','use_custom_color','color','show_texture','bl_label','bl_description',
    'bl_icon','bl_width_default','bl_width_min','bl_width_max','bl_height_default','bl_height_min',
    'bl_height_max','rna_type','type','inputs','outputs','internal_links','parent','name','bl_idname',
    'warning_propagation','color_tag'}
FORBIDDEN={'CompositorNodeOutputFile','ShaderNodeScript'}
COMPONENTS={kind:(definition['node_type'],{key: spec['socket'] for key,spec in definition['parameters'].items()})
  for kind,definition in COMPONENT_DEFINITIONS.items()}

def _fail(code,message,details=None): raise WorkflowError(code,message,details)
def _dump(value): return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)
def _hash(value): return hashlib.sha256(_dump(value).encode()).hexdigest()

_immutable_media_provider = None

@contextmanager
def immutable_media_digest_scope(provider):
    """Internal, process-local content proof only; caller manifests have no seam."""
    global _immutable_media_provider
    if _immutable_media_provider is not None:
        _fail('CONFLICT','An immutable media digest scope is already active')
    if not callable(provider):_fail('INVALID_REQUEST','Internal immutable digest provider must be callable')
    _immutable_media_provider=provider
    try:yield
    finally:_immutable_media_provider=None

def _media_content_sha256(path):
    # Only sequence members and tracking provenance call this function.
    # Legacy image/Scene and Action definition checks retain their full paths.
    if _immutable_media_provider is not None:return _immutable_media_provider(Path(path))
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def _scene(name):
    scene=bpy.data.scenes.get(name)
    if scene is None: _fail('NOT_FOUND','Scene missing: '+name)
    if scene.library or scene.override_library: _fail('UNSUPPORTED','Scene must be local and editable')
    return scene

def _plain(value,depth=0):
    if depth>16: _fail('UNSUPPORTED','RNA nesting exceeds M1 proof budget')
    if value is None or isinstance(value,(str,bool,int)): return value
    if isinstance(value,float):
        if math.isfinite(value): return value
        return {'nonfinite':repr(value)}
    if isinstance(value,bpy.types.ID): return {'id_type':value.bl_rna.identifier,'name':value.name,'library':value.library.filepath if value.library else None}
    if isinstance(value,dict) or hasattr(value,'keys'):
        return {str(k):_plain(value[k],depth+1) for k in sorted(value.keys())}
    try: return [_plain(v,depth+1) for v in value]
    except TypeError: _fail('UNSUPPORTED','Unserializable value '+type(value).__name__)

def _custom(item):
    return {str(k):_plain(item[k]) for k in sorted(item.keys()) if k not in {STATE,ARCHIVE}}
def _id_name(item):
    return item.name+('|'+item.library.filepath if item.library else '')
def _owned(item,scene,wid):
    return item.get(OWNER)==wid and item.get(SCENE_TAG)==scene.name
def _mark(item,scene,wid,role,node_id=None):
    item[OWNER]=wid;item[SCENE_TAG]=scene.name;item[ROLE]=role
    if node_id is not None:item[NODE_ID]=node_id
def _state(scene):
    raw=scene.get(STATE)
    if raw is None:return None
    try: value=json.loads(raw)
    except (TypeError,ValueError):_fail('CONFLICT','Managed state is not valid JSON')
    if value.get('version')!='1.0':_fail('UNSUPPORTED','Unknown managed state version')
    return value

def _image(image):
    return {'id':_plain(image),'source':image.source,'filepath':image.filepath,
      'colorspace':image.colorspace_settings.name,'alpha_mode':image.alpha_mode,
      'size':list(image.size),'packed':[hashlib.sha256(p.packed_file.data).hexdigest() for p in image.packed_files if p.packed_file],
      'tiles':[t.number for t in image.tiles],'custom':_custom(image)}

def _pass_settings(layer):
    """Only actual Boolean pass switches; reading never enables a Pass."""
    result={p.identifier:bool(getattr(layer,p.identifier)) for p in layer.bl_rna.properties
      if p.type=='BOOLEAN' and p.identifier.startswith('use_pass_')}
    cycles=getattr(layer,'cycles',None)
    if cycles is not None:
        result['cycles']={p.identifier:bool(getattr(cycles,p.identifier)) for p in cycles.bl_rna.properties
          if p.type=='BOOLEAN' and ('pass' in p.identifier or 'denoising' in p.identifier)}
    return result

def _binding_spec(node):
    try:spec=json.loads(node[BINDING])
    except (KeyError,TypeError,ValueError):_fail('CONFLICT','Input binding metadata missing or invalid: '+node.name)
    if not isinstance(spec,dict):_fail('CONFLICT','Input binding metadata must be an object')
    try:
        from .contract import M3B_BINDING_SCHEMA
        validate(spec,M3B_BINDING_SCHEMA if spec.get('kind')=='movie' else BINDING_SCHEMA)
    except WorkflowError as exc:_fail('CONFLICT','Input binding metadata is malformed',{'node':node.name,'error':str(exc)})
    return spec

def _binding_observation(node):
    if node.bl_idname=='CompositorNodeRLayers':
        scene=node.scene
        layer=scene.view_layers.get(node.layer) if scene else None
        return {'kind':'render_layers','scene':_plain(scene),'view_layer':node.layer,
          'passes':_pass_settings(layer) if layer else None}
    if node.bl_idname=='CompositorNodeImage':
        return {'kind':'image','layer':node.layer,'has_layers':node.has_layers,'view':node.view,
          'has_views':node.has_views,'image':_image(node.image) if node.image else None}
    _fail('UNSUPPORTED','Binding on unsupported node type: '+node.bl_idname)

def _curves(mapping):
    attrs=('use_clip','clip_min_x','clip_min_y','clip_max_x','clip_max_y','black_level','white_level','tone','extend')
    return {'type':mapping.bl_rna.identifier,
      'settings':{k:_plain(getattr(mapping,k)) for k in attrs if hasattr(mapping,k)},
      'curves':[{'points':[{'location':list(p.location),'handle_type':p.handle_type} for p in c.points]} for c in mapping.curves]}

def _rna(obj,depth=0):
    """可读写语义属性及已知结构容器；派生只读标量不算源改动。"""
    if depth>12:_fail('UNSUPPORTED','Unproven RNA recursion budget')
    if obj is None:return None
    if isinstance(obj,bpy.types.ID):return _plain(obj)
    if isinstance(obj,bpy.types.CurveMapping):return _curves(obj)
    out={'rna':obj.bl_rna.identifier}
    for prop in obj.bl_rna.properties:
        key=prop.identifier
        if key=='rna_type':continue
        if prop.is_readonly and prop.type not in ('POINTER','COLLECTION'):continue
        try:value=getattr(obj,key)
        except Exception as exc:_fail('UNSUPPORTED','Cannot read RNA semantic '+key,{'error':str(exc)})
        if prop.type=='POINTER':
            out[key]=_plain(value) if isinstance(value,bpy.types.ID) or value is None else _rna(value,depth+1)
        elif prop.type=='COLLECTION':
            if len(value)>1000000:_fail('UNSUPPORTED','RNA collection beyond proof budget')
            out[key]=[_rna(v,depth+1) if hasattr(v,'bl_rna') and not isinstance(v,bpy.types.ID) else _plain(v) for v in value]
        else:out[key]=_plain(value)
    return out

def _fcurve(f):
    return {'data_path':f.data_path,'array_index':f.array_index,'extrapolation':f.extrapolation,
      'mute':f.mute,'lock':f.lock,
      'points':[{'co':list(p.co),'left':list(p.handle_left),'right':list(p.handle_right),
        'left_type':p.handle_left_type,'right_type':p.handle_right_type,'interpolation':p.interpolation,
        'easing':p.easing,'amplitude':p.amplitude,'back':p.back,'period':p.period} for p in f.keyframe_points],
      'samples':[list(p.co) for p in f.sampled_points],
      'modifiers':[_rna(m) for m in f.modifiers],
      'driver':({'type':f.driver.type,'expression':f.driver.expression,
        'use_self':f.driver.use_self,'variables':[_rna(v) for v in f.driver.variables]} if f.driver else None)}
def _action(action):
    if action is None:return None
    layers=[]
    for layer in action.layers:
        strips=[]
        for strip in layer.strips:
            if strip.type!='KEYFRAME':_fail('UNSUPPORTED','Unproven action strip type')
            strip_properties={p.identifier:_plain(getattr(strip,p.identifier)) for p in strip.bl_rna.properties
                              if not p.is_readonly and p.type in ('BOOLEAN','INT','FLOAT','STRING','ENUM')}
            strips.append({'type':strip.type,'properties':strip_properties,
                           'bags':[{'slot':bag.slot_handle,'curves':[_fcurve(f) for f in bag.fcurves]} for bag in strip.channelbags]})
        properties={p.identifier:_plain(getattr(layer,p.identifier)) for p in layer.bl_rna.properties
                    if not p.is_readonly and p.type in ('BOOLEAN','INT','FLOAT','STRING','ENUM')}
        layers.append({'name':layer.name,'properties':properties,'strips':strips})
    properties={p.identifier:_plain(getattr(action,p.identifier)) for p in action.bl_rna.properties
                if not p.is_readonly and p.type in ('BOOLEAN','INT','FLOAT','STRING','ENUM') and p.identifier!='use_fake_user'}
    return {'id':_plain(action),'properties':properties,'layers':layers,
            'slots':[{'handle':s.handle,'identifier':s.identifier,'target_id_type':s.target_id_type} for s in action.slots]}
def _animation(item):
    data=getattr(item,'animation_data',None)
    if not data:return None
    return {'action':_action(data.action),'slot':getattr(data,'action_slot_handle',None),
      'drivers':[_fcurve(f) for f in data.drivers],
      'nla':[_rna(track) for track in data.nla_tracks]}

def _node_key(node):
    return 'owned:'+node[NODE_ID] if node.get(NODE_ID) else 'native:'+node.name
def _socket_state(socket):
    return {'identifier':socket.identifier,'name':socket.name,'rna':socket.bl_rna.identifier,
      'enabled':socket.enabled,'default':_plain(socket.default_value) if hasattr(socket,'default_value') else None}
def _node_state(node,trail,enforce_safety):
    if enforce_safety and node.bl_idname in FORBIDDEN:_fail('UNSUPPORTED','External write/script node is outside M1: '+node.bl_idname)
    props={}
    for prop in node.bl_rna.properties:
        key=prop.identifier
        if key in UI or (prop.is_readonly and prop.type not in ('POINTER','COLLECTION')):continue
        try:value=getattr(node,key)
        except Exception as exc:_fail('UNSUPPORTED','Node semantic cannot be read: '+key,{'error':str(exc)})
        if key=='node_tree':
            props[key]=_tree_state(value,trail,enforce_safety) if value else None
        elif isinstance(value,bpy.types.Image):props[key]=_image(value)
        elif prop.type=='POINTER':props[key]=_plain(value) if isinstance(value,bpy.types.ID) or value is None else _rna(value)
        elif prop.type=='COLLECTION':props[key]=[_rna(v) if hasattr(v,'bl_rna') else _plain(v) for v in value]
        else:props[key]=_plain(value)
    result={'id':_node_key(node),'type':node.bl_idname,'mute':node.mute,'props':props,
      'inputs':[_socket_state(s) for s in node.inputs], 'outputs':[_socket_state(s) for s in node.outputs],
      'custom':_custom(node)}
    if BINDING in node:result['binding_observation']=_binding_observation(node)
    return result

def _interface(tree):
    rows=[]
    for item in tree.interface.items_tree:
        if item.item_type=='SOCKET':
            rows.append({'identifier':item.identifier,'name':item.name,'direction':item.in_out,'socket_type':item.socket_type,
             'default':_plain(item.default_value) if hasattr(item,'default_value') else None,
             'hide_value':getattr(item,'hide_value',False),'attribute_domain':getattr(item,'attribute_domain',None)})
        else:rows.append({'panel':item.name})
    return rows

def _tree_state(tree,trail=(),enforce_safety=True,exclude_nodes=(),exclude_link_target=None):
    if tree is None:return None
    if tree.as_pointer() in trail:_fail('UNSUPPORTED','Recursive node groups are outside M1')
    if len(trail)>16 or len(tree.nodes)>512:_fail('UNSUPPORTED','Node graph exceeds M1 fingerprint budget')
    if enforce_safety and tree.animation_data and ANIM not in tree:_fail('UNSUPPORTED','Unregistered compositor animation/drivers')
    trail=(*trail,tree.as_pointer())
    excluded=set(exclude_nodes)
    nodes=[n for n in tree.nodes if n.name not in excluded]
    links=[]
    for link in tree.links:
        if link.from_node.name in excluded or link.to_node.name in excluded:continue
        if exclude_link_target and (link.to_node.name,link.to_socket.identifier)==exclude_link_target:continue
        links.append({'from_node':_node_key(link.from_node),'from_socket':link.from_socket.identifier,
          'to_node':_node_key(link.to_node),'to_socket':link.to_socket.identifier})
    return {'tree_type':tree.bl_idname,'interface':_interface(tree),
      'nodes':sorted([_node_state(n,trail,enforce_safety) for n in nodes],key=lambda n:n['id']),
      'links':sorted(links,key=_dump),'animation':_animation(tree) if not enforce_safety else None,
      'custom':{k:v for k,v in _custom(tree).items() if not k.startswith('_cwf_')}}

def tree_fingerprint(tree):
    return _hash(_tree_state(tree))

def _output(tree):
    outputs=[n for n in tree.nodes if n.bl_idname=='NodeGroupOutput' and n.is_active_output]
    if len(outputs)!=1:_fail('UNSUPPORTED','Root needs exactly one active Group Output',{'count':len(outputs)})
    colors=[s for s in outputs[0].inputs if s.type=='RGBA']
    image=[s for s in colors if s.name=='Image']
    if len(image)==1 and colors and colors[0]==image[0]:return outputs[0],image[0]
    if len(colors)==1:return outputs[0],colors[0]
    _fail('UNSUPPORTED','Root must have an unambiguous first image output')

def _exact(sockets,identifier):
    found=[s for s in sockets if s.identifier==identifier]
    if len(found)!=1:_fail('NOT_FOUND','Socket identifier must resolve uniquely: '+identifier,
      {'observed':[{'name':s.name,'identifier':s.identifier,'type':s.bl_rna.identifier} for s in sockets]})
    return found[0]

def _set_socket(socket,value):
    if not hasattr(socket,'default_value'):_fail('UNSUPPORTED','Socket has no writable default: '+socket.identifier)
    if socket.type in ('OBJECT','IMAGE','MATERIAL','COLLECTION','TEXTURE','GEOMETRY'):
        _fail('UNSUPPORTED','ID/geometry default assignment is outside M1')
    try:socket.default_value=value
    except (TypeError,ValueError,RuntimeError) as exc:_fail('INVALID_REQUEST','Invalid socket default '+socket.identifier,{'error':str(exc)})
    expected=_plain(value);actual=_plain(socket.default_value)
    if isinstance(expected,list) and isinstance(actual,list):
        if len(expected)!=len(actual):_fail('INVALID_REQUEST','Socket array size changed')
        if any((not math.isclose(a,b,rel_tol=1e-6,abs_tol=1e-7)) if isinstance(a,(int,float)) and isinstance(b,(int,float)) else a!=b
               for a,b in zip(expected,actual)):
            _fail('INVALID_REQUEST','RNA clamped a requested vector value',{'expected':expected,'observed':actual})
    elif isinstance(expected,(int,float)) and not isinstance(expected,bool) and isinstance(actual,(int,float)):
        if not math.isclose(expected,actual,rel_tol=1e-6,abs_tol=1e-7):_fail('INVALID_REQUEST','RNA clamped a requested value',{'expected':expected,'observed':actual})
    elif expected!=actual:_fail('INVALID_REQUEST','RNA changed the requested default',{'expected':expected,'observed':actual})

def _patch(node,document,undo=None):
    if node.id_data and ANIM in node.id_data:
        declared=json.loads(node.id_data[ANIM])['targets']
        for direction in ('inputs','outputs','properties'):
            for identifier in document.get(direction,{}):
                if any(t=={'node':node.get(NODE_ID),'domain':direction,'identifier':identifier} for t in declared):
                    _fail('CONFLICT','Static patch cannot overwrite an animated target; remove or replace the complete track')
    for key,value in document.get('properties',{}).items():
        prop=node.bl_rna.properties.get(key)
        if key in UI or not prop or prop.is_readonly or prop.type not in ('BOOLEAN','INT','FLOAT','STRING','ENUM'):
            _fail('UNSUPPORTED','Property is not a supported writable semantic scalar: '+key)
        if 'path' in key.lower() or key.startswith('bl_'):_fail('UNSUPPORTED','External path/type mutation is outside M1')
        if BINDING in node and key in {'layer','view','frame_duration','frame_start','frame_offset','use_cyclic','use_auto_refresh'}:
            _fail('UNSUPPORTED','Input selection/time is binding-owned; use explicit replace_graph')
        if undo is not None:undo.append(('property',node,key,deepcopy(_plain(getattr(node,key)))))
        try:setattr(node,key,value)
        except Exception as exc:_fail('INVALID_REQUEST','Invalid node property '+key,{'error':str(exc)})
        observed=_plain(getattr(node,key))
        if isinstance(value,(int,float)) and not isinstance(value,bool) and isinstance(observed,(int,float)):
            if not math.isclose(value,observed,rel_tol=1e-6,abs_tol=1e-7):
                _fail('INVALID_REQUEST','RNA clamped a node property',{'expected':value,'observed':observed})
        elif _plain(value)!=observed:_fail('INVALID_REQUEST','RNA changed the requested property')
    for direction in ('inputs','outputs'):
        for identifier,value in document.get(direction,{}).items():
            socket=_exact(getattr(node,direction),identifier)
            if undo is not None:undo.append(('socket',socket,deepcopy(_plain(socket.default_value))))
            _set_socket(socket,value)

def _load_bound_image(spec,scene,wid,role):
    path=Path(spec['file']).resolve()
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=spec['expected_sha256']:
        _fail('CONFLICT','Bound image file/SHA changed',{'file':str(path)})
    image=bpy.data.images.load(str(path),check_existing=False);_mark(image,scene,wid,role)
    if image.source!='FILE':
        _fail('UNSUPPORTED','Bound image must be a decoded still file')
    try:image.colorspace_settings.name=spec['colorspace'];image.alpha_mode=spec['alpha_mode']
    except Exception as exc:_fail('INVALID_REQUEST','Bound image interpretation unavailable',{'error':str(exc)})
    if image.colorspace_settings.name!=spec['colorspace'] or image.alpha_mode!=spec['alpha_mode']:
        _fail('INVALID_REQUEST','Bound image interpretation differs from request')
    return image

def _bind_node(node,spec,manifest,scene,wid,resolved_media=None):
    if spec['kind'] in {'image_sequence','movie'}:
        return _bind_sequence(node,spec,manifest,scene,wid,resolved_media)
    if node.bl_idname=='CompositorNodeImage':
        image=_load_bound_image(spec,scene,wid,'binding_image');node.image=image;node[BOUND_IMAGE]=image.name
        # Assigning Image to the node resolves lazy multilayer loading in 5.2.1.
        if image.type not in {'IMAGE','MULTILAYER'} or (image.type!='MULTILAYER' and min(image.size)<1):
            _fail('UNSUPPORTED','Bound image did not decode as a single still or multilayer EXR')
        if image.type=='MULTILAYER' and 'layer' not in spec:
            _fail('INVALID_REQUEST','Multilayer EXR requires an explicit actual layer identifier')
        if 'layer' in spec:
            if image.type!='MULTILAYER' or not node.has_layers:_fail('INVALID_REQUEST','Layer selection requires multilayer EXR')
            try:node.layer=spec['layer']
            except (TypeError,ValueError,RuntimeError) as exc:_fail('NOT_FOUND','EXR layer identifier unavailable',{'requested':spec['layer'],'error':str(exc)})
            if node.layer!=spec['layer']:_fail('CONFLICT','EXR layer selection differs from request')
        if node.has_views:_fail('UNSUPPORTED','Multiview image binding is outside static M2')
    elif node.bl_idname=='CompositorNodeRLayers':
        if spec['scene']!=scene.name or spec['view_layer']!=manifest['view_layer']:
            _fail('UNSUPPORTED','Render Layers binding must match manifest context')
        node.scene=scene;node.layer=manifest['view_layer']
        if node.scene!=scene or node.layer!=manifest['view_layer']:_fail('CONFLICT','Render Layers binding did not persist')
    else:_fail('UNSUPPORTED','Unsupported bound input node')
    node[BINDING]=_dump(spec)

def _available_socket(socket):
    if not socket.enabled or getattr(socket,'is_unavailable',False):
        _fail('UNSUPPORTED','Required socket/Pass is unavailable: '+socket.identifier)

def _required_render_pass(node,identifier):
    """Finite actual 5.2.1 Pass switches, independently of socket presence."""
    layer=node.scene.view_layers.get(node.layer) if node.scene else None
    if layer is None:_fail('CONFLICT','Render Layers view layer disappeared')
    switches={'Image':'use_pass_combined','Alpha':'use_pass_combined','Depth':'use_pass_z',
      'Normal':'use_pass_normal','Noisy Image':'use_pass_combined'}
    denoising={'Denoising Albedo','Denoising Specular Albedo','Denoising Normal','Denoising Roughness','Denoising Depth'}
    if identifier in switches:
        key=switches[identifier]
        if not getattr(layer,key,False):_fail('UNSUPPORTED','Required render Pass is not enabled',{'identifier':identifier,'required_switch':key})
        if identifier=='Noisy Image' and node.scene.render.engine!='CYCLES':
            _fail('UNSUPPORTED','Noisy Image requires the source Cycles engine')
    elif identifier in denoising:
        cycles=getattr(layer,'cycles',None)
        if node.scene.render.engine!='CYCLES' or not cycles or not getattr(cycles,'denoising_store_passes',False):
            _fail('UNSUPPORTED','Denoising auxiliary Pass is not enabled',{'identifier':identifier,'required_switch':'cycles.denoising_store_passes'})
    else:_fail('UNSUPPORTED','Render Pass is outside the finite M2 binding scope',{'identifier':identifier})

def _validate_bound_input(node,scene,view_layer,wid):
    if not _owned(node,scene,wid):_fail('CONFLICT','Managed input node ownership changed')
    spec=_binding_spec(node)
    if spec['kind'] in {'image_sequence','movie'}:return _validate_sequence_node(node,spec,scene,wid)
    if node.bl_idname=='CompositorNodeImage':
        image=node.image
        if spec.get('kind')!='image' or not image or image.library or image.override_library or not _owned(image,scene,wid):
            _fail('CONFLICT','Bound Image reference/ownership changed')
        if image.name!=node.get(BOUND_IMAGE):_fail('CONFLICT','Bound Image ID changed; replace the graph explicitly')
        if image.source!='FILE' or image.type not in {'IMAGE','MULTILAYER'}:
            _fail('UNSUPPORTED','Bound media must remain a single still image')
        actual=Path(bpy.path.abspath(image.filepath)).resolve()
        if actual!=Path(spec['file']).resolve() or image.colorspace_settings.name!=spec['colorspace'] or image.alpha_mode!=spec['alpha_mode']:
            _fail('CONFLICT','Bound Image interpretation/path changed; replace the graph explicitly')
        if image.packed_files:_fail('UNSUPPORTED','Bound file images must keep their declared external dependency')
        if not actual.is_file() or hashlib.sha256(actual.read_bytes()).hexdigest()!=spec['expected_sha256']:
            _fail('CONFLICT','Bound Image dependency SHA changed')
        if image.type=='MULTILAYER' and not node.has_layers:
            # Cold open_mainfile keeps the saved layer index but its RNA enum is empty.
            # Reading the declared Image header and evaluating reveals the saved choice;
            # never assign node.image/node.layer or restore a requested value here.
            tuple(image.size)
            scene.view_layers[view_layer].update()
        if node.has_views:_fail('UNSUPPORTED','Multiview input is outside static M2')
        if image.type=='MULTILAYER' and node.layer!=spec.get('layer'):
            _fail('CONFLICT','Bound EXR layer changed; replace the graph explicitly')
        return image
    if spec.get('kind')!='render_layers' or node.scene!=scene or node.layer!=view_layer:
        _fail('CONFLICT','Bound Render Layers context changed')
    return None

def _image_consumers(images,allowed_tree):
    # Bound file inputs cannot silently become reusable global Image data.
    trees=list(bpy.data.node_groups)+[x.node_tree for x in [*bpy.data.materials,*bpy.data.worlds] if x.node_tree]
    for image in images:
        for tree in trees:
            if tree==allowed_tree:continue
            if any(getattr(node,'image',None)==image for node in tree.nodes):
                _fail('CONFLICT','Bound image acquired an outside consumer',{'image':image.name,'tree':tree.name})

def _validate_bound_graph(group,scene,view_layer,wid):
    """M2 ownership/context check also runs before adoption of a hand-edited graph."""
    images=[]
    for node in group.nodes:
        if node.bl_idname not in {'CompositorNodeImage','CompositorNodeRLayers'}:continue
        image=_validate_bound_input(node,scene,view_layer,wid)
        if image:images.append(image)
    for link in group.links:
        _available_socket(link.from_socket);_available_socket(link.to_socket)
        if link.from_node.bl_idname=='CompositorNodeRLayers':_required_render_pass(link.from_node,link.from_socket.identifier)
    _image_consumers(images,group)
    for node in group.nodes:
        if node.bl_idname=='CompositorNodeDefocus':
            if node.scene!=scene:_fail('CONFLICT','Defocus camera context must remain the manifest scene')
            if node.use_zbuffer:
                if scene.camera is None:_fail('UNSUPPORTED','Depth Defocus requires an actual scene Camera')
                if not _exact(node.inputs,'Z').is_linked:_fail('UNSUPPORTED','Depth Defocus requires an explicit linked Z input')

def _build_graph(manifest,scene,wid,resolved_media=None):
    group=bpy.data.node_groups.new('CWF '+wid+' Managed','CompositorNodeTree');_mark(group,scene,wid,'managed')
    try:
        si=group.interface.new_socket(name='Image',in_out='INPUT',socket_type='NodeSocketColor')
        so=group.interface.new_socket(name='Image',in_out='OUTPUT',socket_type='NodeSocketColor')
        ni=group.nodes.new('NodeGroupInput');no=group.nodes.new('NodeGroupOutput')
        _mark(ni,scene,wid,'managed_node','$input');_mark(no,scene,wid,'managed_node','$output')
        mapping={'$input':ni,'$output':no}
        if 'components' in manifest:
            previous=_exact(ni.outputs,si.identifier)
            for index,c in enumerate(manifest['components']):
                node_type,fields=COMPONENTS[c['kind']];n=group.nodes.new(node_type);n.name=c['id'];_mark(n,scene,wid,'managed_node',c['id'])
                _patch(n,{'inputs':{fields[k]:v for k,v in c['params'].items()}})
                group.links.new(previous,_exact(n.inputs,'Image'));previous=_exact(n.outputs,'Image')
                n.location=(index*220,0)
            group.links.new(previous,_exact(no.inputs,so.identifier))
        else:
            graph=manifest['graph']
            for spec in graph['nodes']:
                n=group.nodes.new(spec['type']);n.name=spec['id'];_mark(n,scene,wid,'managed_node',spec['id']);mapping[spec['id']]=n
                if 'binding' in spec:_bind_node(n,spec['binding'],manifest,scene,wid,resolved_media)
                if n.bl_idname=='CompositorNodeDefocus':n.scene=scene
                _patch(n,spec)
            occupied=set()
            for link in graph['links']:
                fn=mapping[link['from_node']];tn=mapping[link['to_node']]
                fi=si.identifier if link['from_node']=='$input' and link['from_socket']=='Image' else link['from_socket']
                ti=so.identifier if link['to_node']=='$output' and link['to_socket']=='Image' else link['to_socket']
                from_socket=_exact(fn.outputs,fi);to_socket=_exact(tn.inputs,ti)
                target=(link['to_node'],ti)
                if target in occupied:_fail('INVALID_REQUEST','Multiple links to a single input')
                occupied.add(target);group.links.new(from_socket,to_socket)
            # Native graph containment does not prove computational acyclicity.
            edges=[(l['from_node'],l['to_node']) for l in graph['links']]
            visiting=set();done=set()
            def walk(name):
                if name in visiting:_fail('UNSUPPORTED','Cyclic processing graph')
                if name in done:return
                visiting.add(name)
                for a,b in edges:
                    if a==name:walk(b)
                visiting.remove(name);done.add(name)
            for name in mapping:walk(name)
        if manifest['workflow_version'] in {'1.1','1.2','1.3'}:_validate_bound_graph(group,scene,manifest['view_layer'],wid)
        if manifest['workflow_version'] in {'1.2','1.3'}:
            _install_tracks(group,_manifest_tracks(manifest),scene,wid,manifest.get('tracking_imports',[]))
        _tree_state(group)
        return group
    except Exception:
        bpy.data.node_groups.remove(group)
        raise

def _bridge(root,scene,wid):
    rows=[n for n in root.nodes if _owned(n,scene,wid) and n.get(ROLE)=='bridge' and n.get(NODE_ID)=='$bridge']
    if len(rows)!=1:_fail('CONFLICT','Tagged managed bridge is missing or duplicated',{'count':len(rows)})
    return rows[0]

def _groups(scene,state):
    root=scene.compositing_node_group
    if not root or root.name!=state['root_group']:_fail('CONFLICT','Scene root binding changed',{'expected':state['root_group'],'observed':root.name if root else None})
    bridge=_bridge(root,scene,state['workflow_id'])
    group=bridge.node_tree
    if not group or group.name!=state['managed_group'] or not _owned(group,scene,state['workflow_id']):
        _fail('CONFLICT','Managed group reference/ownership changed')
    if group.library or group.override_library:_fail('CONFLICT','Managed group became linked/overridden')
    if state.get('workflow_version','1.0') in {'1.1','1.2','1.3'}:_validate_bound_graph(group,scene,state['view_layer'],state['workflow_id'])
    if state.get('workflow_version','1.0') in {'1.2','1.3'}:_validate_history(scene,state)
    if state.get('workflow_version') in {'1.2','1.3'} and 'bound_definitions' in state:
        if state['bound_definitions']!=_binding_identity(root,group):
            _fail('CONFLICT','Declared media/tracking provenance changed; use an explicit graph replacement')
        targets=json.loads(group[ANIM])['targets'] if ANIM in group else []
        if targets!=state['animation_targets']:_fail('CONFLICT','Declared animation targets changed; use explicit complete track patches')
    if state.get('workflow_version','1.0') in {'1.1','1.2','1.3'}:
        images=[]
        for node in root.nodes:
            if _owned(node,scene,state['workflow_id']) and node.get(ROLE)=='source' and BINDING in node:
                image=_validate_bound_input(node,scene,state['view_layer'],state['workflow_id'])
                if image:images.append(image)
                for link in node.outputs:
                    if link.is_linked:
                        _available_socket(link)
                        if node.bl_idname=='CompositorNodeRLayers':_required_render_pass(node,link.identifier)
        _image_consumers(images,root)
    consumers=[]
    all_trees=list(bpy.data.node_groups)+[m.node_tree for m in [*bpy.data.materials,*bpy.data.worlds] if m.node_tree]
    for tree in all_trees:
        for node in tree.nodes:
            if getattr(node,'node_tree',None)==group and node!=bridge:consumers.append({'tree':tree.name,'node':node.name})
    for other in bpy.data.scenes:
        if other.compositing_node_group==group:consumers.append({'scene':other.name})
        if other!=scene and other.compositing_node_group==root:consumers.append({'shared_root_scene':other.name})
    if consumers:_fail('CONFLICT','Managed graph acquired outside consumers; isolate before editing',{'consumers':consumers})
    return root,bridge,group

def _observed(scene,state):
    root,bridge,group=_groups(scene,state)
    return {'root':_tree_state(root),'enabled':scene.render.use_compositing,
      'scene':scene.name,'view_layer':state['view_layer'],'bridge':_node_key(bridge),
      'managed_interface':_interface(group)}

def _diff(before,after,path='$',rows=None):
    rows=[] if rows is None else rows
    if len(rows)>=64:return rows
    if type(before)!=type(after):rows.append({'path':path,'expected':before,'observed':after});return rows
    if isinstance(before,dict):
        for key in sorted(set(before)|set(after)):
            if key not in before or key not in after:rows.append({'path':path+'/'+key,'expected':before.get(key),'observed':after.get(key)})
            else:_diff(before[key],after[key],path+'/'+key,rows)
            if len(rows)>=64:break
    elif isinstance(before,list):
        if len(before)!=len(after):rows.append({'path':path+'/length','expected':len(before),'observed':len(after)})
        for i,(a,b) in enumerate(zip(before,after)):_diff(a,b,path+'/'+str(i),rows)
    elif before!=after:rows.append({'path':path,'expected':before,'observed':after})
    return rows

def _baseline(scene,state):
    if state.get('workflow_version') in {'1.2','1.3'} and 'bound_definitions' not in state:
        root=scene.compositing_node_group;group=bpy.data.node_groups[state['managed_group']]
        _refresh_declarations(root,group,state)
    observed=_observed(scene,state)
    state['baseline']={'fingerprint':_hash(observed),'state':observed}
    scene[STATE]=_dump(state)
    return observed

def _boundaries(scene,state=None):
    names={_id_name(scene.compositing_node_group)} if scene.compositing_node_group else set()
    if state and state['original']['root']:
        names.add(state['original']['root']+('|'+state['original']['root_library'] if state['original'].get('root_library') else ''))
    return names

def _mesh(mesh):
    return {'vertices':[list(v.co) for v in mesh.vertices],'edges':[list(e.vertices) for e in mesh.edges],
      'polygons':[{'vertices':list(p.vertices),'material':p.material_index,'smooth':p.use_smooth} for p in mesh.polygons],
      'attributes':{a.name:{'domain':a.domain,'type':a.data_type,'data':[_rna(v) for v in a.data]} for a in mesh.attributes},
      'vertex_weights':[[{'group':g.group,'weight':g.weight} for g in v.groups] for v in mesh.vertices],
      'materials':[_plain(m) for m in mesh.materials],
      'shape_keys':({'id':_plain(mesh.shape_keys),'use_relative':mesh.shape_keys.use_relative,
        'blocks':[{'name':k.name,'value':k.value,'mute':k.mute,'relative':k.relative_key.name if k.relative_key else None,
          'vertex_group':k.vertex_group,'interpolation':k.interpolation,'data':[list(v.co) for v in k.data]} for k in mesh.shape_keys.key_blocks],
        'animation':_animation(mesh.shape_keys)} if mesh.shape_keys else None),
      'animation':_animation(mesh),'custom':_custom(mesh)}

def _protection(scene,wid,boundaries,workflow_version='1.0'):
    ids={}
    def add(label,value):ids[label]=_hash(value)
    for obj in bpy.data.objects:
        add('Object:'+_id_name(obj),{'id':_plain(obj),'data':_plain(obj.data),'parent':_plain(obj.parent),
          'parent_type':obj.parent_type,'parent_bone':obj.parent_bone,
          'matrix_basis':[list(r) for r in obj.matrix_basis],'matrix_parent_inverse':[list(r) for r in obj.matrix_parent_inverse],
          'matrix_world':[list(r) for r in obj.matrix_world],'rotation_mode':obj.rotation_mode,
          'hide_render':obj.hide_render,'hide_viewport':obj.hide_viewport,
          'constraints':[_rna(c) for c in obj.constraints],'modifiers':[_rna(m) for m in obj.modifiers],
          'materials':[{'material':_plain(s.material),'link':s.link} for s in obj.material_slots],
          'vertex_groups':[{'name':g.name,'lock_weight':g.lock_weight} for g in obj.vertex_groups],
          'animation':_animation(obj),'custom':_custom(obj)})
    for mesh in bpy.data.meshes:add('Mesh:'+_id_name(mesh),_mesh(mesh))
    for label,group in [('Material',bpy.data.materials),('World',bpy.data.worlds)]:
        for item in group:
            props={p.identifier:_plain(getattr(item,p.identifier)) for p in item.bl_rna.properties
              if not p.is_readonly and p.type in ('BOOLEAN','INT','FLOAT','STRING','ENUM') and p.identifier not in ('name','use_fake_user')}
            add(label+':'+_id_name(item),{'props':props,'tree':_tree_state(item.node_tree,enforce_safety=False) if item.node_tree else None,'animation':_animation(item),'custom':_custom(item)})
    for label,group in [('Camera',bpy.data.cameras),('Light',bpy.data.lights),('Curve',bpy.data.curves),('Armature',bpy.data.armatures)]:
        for item in group:
            props={p.identifier:_plain(getattr(item,p.identifier)) for p in item.bl_rna.properties
              if not p.is_readonly and p.type in ('BOOLEAN','INT','FLOAT','STRING','ENUM') and p.identifier not in ('name','use_fake_user')}
            structure={}
            if label=='Camera':structure['dof']=_rna(item.dof)
            if label=='Curve':structure['splines']=[_rna(s) for s in item.splines]
            if label=='Armature':
                structure['bones']=[{'name':b.name,'parent':b.parent.name if b.parent else None,
                  'head':list(b.head_local),'tail':list(b.tail_local),'matrix':[list(r) for r in b.matrix_local],
                  'use_deform':b.use_deform,'use_connect':b.use_connect,'custom':_custom(b)} for b in item.bones]
            if label=='Light':structure['tree']=_tree_state(item.node_tree,enforce_safety=False) if item.node_tree else None
            add(label+':'+_id_name(item),{'props':props,'structure':structure,'animation':_animation(item),'custom':_custom(item)})
    registered=_registered_actions(scene,wid) if workflow_version in {'1.2','1.3'} else set()
    for action in bpy.data.actions:
        if action.name not in registered:add('Action:'+_id_name(action),_action(action))
    for collection in bpy.data.collections:
        add('Collection:'+_id_name(collection),{'objects':sorted(_id_name(o) for o in collection.objects),
          'children':sorted(_id_name(c) for c in collection.children),'hide_render':collection.hide_render,
          'hide_viewport':collection.hide_viewport,'hide_select':collection.hide_select,
          'instance_offset':list(collection.instance_offset),'custom':_custom(collection)})
    for image in bpy.data.images:
        if _owned(image,scene,wid) or image.type in ('RENDER_RESULT','COMPOSITING'):continue
        add('Image:'+_id_name(image),_image(image))
    for tree in bpy.data.node_groups:
        if _owned(tree,scene,wid):continue
        if _id_name(tree) in boundaries:
            output,socket=_output(tree)
            excluded=[n.name for n in tree.nodes if _owned(n,scene,wid)]
            value=_tree_state(tree,exclude_nodes=excluded,exclude_link_target=(output.name,socket.identifier))
        else:value=_tree_state(tree,enforce_safety=tree.bl_idname=='CompositorNodeTree')
        add('NodeTree:'+_id_name(tree),value)
    for sc in bpy.data.scenes:
        add('Scene:'+_id_name(sc),{'camera':_plain(sc.camera),'world':_plain(sc.world),
          'objects':sorted(o.name for o in sc.objects),'view_layers':[{'name':v.name,'use':v.use,
            **({'passes':_pass_settings(v)} if workflow_version in {'1.1','1.2','1.3'} else {})} for v in sc.view_layers],
          'frame_start':sc.frame_start,'frame_end':sc.frame_end,
          'root':None if sc==scene else _plain(sc.compositing_node_group),
          'compositing':None if sc==scene else sc.render.use_compositing,
          'frame_current':None if sc==scene else sc.frame_current,
          'render':{'engine':sc.render.engine,'resolution':[sc.render.resolution_x,sc.render.resolution_y,sc.render.resolution_percentage],'fps':sc.render.fps,'fps_base':sc.render.fps_base},
          'custom':_custom(sc)})
    if workflow_version in {'1.2','1.3'}:
        for item in _id_owners():
            if isinstance(item,bpy.types.NodeTree) and _owned(item,scene,wid):continue
            data=getattr(item,'animation_data',None)
            if data:add('AnimationControls:'+item.bl_rna.identifier+':'+_id_name(item),
                {key:_plain(getattr(data,key)) for key in ('action_blend_type','action_influence','action_extrapolation','use_nla','use_tweak_mode')})
    return {'version':'1.0','scene':scene.name,'workflow_id':wid,'ids':ids}

def snapshot_protection(scene_name,workflow_id=None):
    scene=_scene(scene_name);state=_state(scene)
    wid=workflow_id or (state['workflow_id'] if state else '')
    if state and state.get('workflow_version') in {'1.2','1.3'}:scene.frame_set(state['reference_frame'])
    return _protection(scene,wid,_boundaries(scene,state),state.get('workflow_version','1.0') if state else ('1.2' if ARCHIVE in scene else '1.0'))

def _input_bindings(root,group):
    rows=[]
    for tree in (root,group):
        for node in tree.nodes:
            if BINDING not in node:continue
            outputs=[{'identifier':s.identifier,'type':s.type,'enabled':s.enabled,
              'is_unavailable':getattr(s,'is_unavailable',False)} for s in node.outputs]
            if node.bl_idname=='CompositorNodeImage':
                image=node.image
                observed={'name':image.name,'file':str(Path(bpy.path.abspath(image.filepath)).resolve()),
                  'source':image.source,'type':image.type,'size':list(image.size),'colorspace':image.colorspace_settings.name,
                  'alpha_mode':image.alpha_mode,'layer':node.layer,'outputs':outputs}
            else:
                observed={'scene':node.scene.name,'view_layer':node.layer,
                  'passes':_pass_settings(node.scene.view_layers[node.layer]),'outputs':outputs}
            rows.append({'node_id':node.get(NODE_ID),'node':node.name,'tree':tree.name,'type':node.bl_idname,
              'binding':_binding_spec(node),'observed':observed,
              'required_outputs':sorted({link.from_socket.identifier for link in tree.links if link.from_node==node})})
    return sorted(rows,key=lambda row:(row['tree'],row['node_id']))

def inspect(scene_name,workflow_id):
    scene=_scene(scene_name);state=_state(scene)
    if state is None:
        return {'status':'detached','scene':scene.name,'workflow_id':workflow_id,'root_group':scene.compositing_node_group.name if scene.compositing_node_group else None,
          'managed_group':None,'bridge_node':None,'bridge':None,'source':None,'fingerprint':None,'semantic_fingerprint':None,'baseline':None,'conflicts':[]}
    if state['workflow_id']!=workflow_id:_fail('CONFLICT','This scene is owned by another workflow')
    root,bridge,group=_groups(scene,state);observed=_observed(scene,state);fp=_hash(observed)
    conflicts=_diff(state['baseline']['state'],observed)
    source=deepcopy(state['source']);input_socket=next(s for s in bridge.inputs if s.type=='RGBA')
    incoming=list(input_socket.links)
    source['actual_upstream']=[{'node':l.from_node.name,'socket':l.from_socket.identifier} for l in incoming]
    result={'status':'conflict' if conflicts else 'managed','scene':scene.name,'workflow_id':workflow_id,
      'root_group':root.name,'managed_group':group.name,'bridge_node':bridge.name,
      'bridge':{'node':bridge.name,'id':'$bridge','type':bridge.bl_idname,'input':input_socket.identifier,
                'output':next(s.identifier for s in bridge.outputs if s.type=='RGBA')},
      'source':source,'original':state['original'],'fingerprint':fp,'semantic_fingerprint':fp,
      'baseline':state['baseline'],'observed':{'state':observed},'conflicts':conflicts}
    if state.get('workflow_version','1.0') in {'1.1','1.2','1.3'}:
        result['workflow_version']=state['workflow_version'];result['input_bindings']=_input_bindings(root,group)
        result['baseline_definition']=state['baseline_definition']
    if state.get('workflow_version') in {'1.2','1.3'}:
        result['reference_frame']=state['reference_frame']
        result['animation']=_animation_inspection(group)
        result['temporal_bindings']=_temporal_inspection(root,group,scene)
        result['action_history']=deepcopy(state.get('action_history',[]))
    return result

def _source(root,scene,manifest,original,resolved_media=None):
    spec=manifest['source'];wid=manifest['workflow_id']
    if spec['kind'] in {'image_sequence','movie'}:
        node=root.nodes.new('CompositorNodeImage');_mark(node,scene,wid,'source','$source')
        _bind_sequence(node,spec,manifest,scene,wid,resolved_media)
        return _exact(node.outputs,'Image'),{'kind':spec['kind'],'node':node.name,'output':'Image'}
    if spec['kind']=='existing':
        if not original['source']:_fail('UNSUPPORTED','Existing source needs a connected root image output')
        node=root.nodes.get(original['source']['node'])
        return _exact(node.outputs,original['source']['socket']),{'kind':'existing','node':node.name,'output':original['source']['socket']}
    if spec['kind']=='render_layers':
        node=root.nodes.new('CompositorNodeRLayers');node.scene=scene;node.layer=manifest['view_layer']
        _mark(node,scene,wid,'source','$source')
        if manifest['workflow_version'] in {'1.1','1.2','1.3'}:
            node[BINDING]=_dump({'kind':'render_layers','scene':scene.name,'view_layer':manifest['view_layer']})
            _required_render_pass(node,'Image')
        socket=_exact(node.outputs,'Image')
        return socket,{'kind':'render_layers','node':node.name,'output':socket.identifier}
    path=Path(spec['file']).resolve()
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=spec['expected_sha256']:
        _fail('CONFLICT','Image source file/SHA changed')
    image=bpy.data.images.load(str(path),check_existing=False);_mark(image,scene,wid,'source_image')
    if image.source!='FILE' or min(image.size)<1:_fail('UNSUPPORTED','M1 image source must decode as a single still')
    try:image.colorspace_settings.name=spec['colorspace'];image.alpha_mode=spec['alpha_mode']
    except Exception as exc:_fail('INVALID_REQUEST','Image interpretation unavailable',{'error':str(exc)})
    node=root.nodes.new('CompositorNodeImage');node.image=image;_mark(node,scene,wid,'source','$source')
    if manifest['workflow_version'] in {'1.1','1.2','1.3'}:
        if image.type!='IMAGE':_fail('UNSUPPORTED','Primary source must remain a single flat still image; use explicit graph binding for multilayer EXR')
        node[BINDING]=_dump(spec);node[BOUND_IMAGE]=image.name
    return _exact(node.outputs,'Image'),{'kind':'image','node':node.name,'output':'Image','image':{'name':image.name,'file':str(path),'colorspace':image.colorspace_settings.name,'alpha_mode':image.alpha_mode}}

def apply(manifest,resolved_media=None):
    if bpy.app.version!=(5,2,1):_fail('UNSUPPORTED','M1 apply is verified only for Blender 5.2.1')
    manifest=normalize_manifest(manifest);scene=_scene(manifest['scene']);wid=manifest['workflow_id'];op=manifest['operation']
    if scene.view_layers.get(manifest['view_layer']) is None:_fail('NOT_FOUND','View Layer missing')
    state=_state(scene)
    if state and (state['workflow_id']!=wid or state['view_layer']!=manifest['view_layer']):
        _fail('CONFLICT','Workflow ownership/context differs')
    if state and state.get('workflow_version','1.0')!=manifest['workflow_version']:
        _fail('CONFLICT','Workflow version differs; snapshots are not automatically migrated')
    if op=='create' and state:_fail('CONFLICT','Scene already has one managed workflow')
    if op!='create' and not state:_fail('NOT_FOUND','No managed workflow in this scene')
    if manifest['workflow_version'] in {'1.2','1.3'}:
        if state:
            if 'reference_frame' in manifest and manifest['reference_frame']!=state['reference_frame']:
                _fail('CONFLICT','reference_frame cannot change in an existing snapshot')
            manifest['reference_frame']=state['reference_frame']
        if 'frame' in manifest and manifest['frame']!=manifest['reference_frame']:
            _fail('INVALID_REQUEST','M3 frame must equal the persisted reference_frame')
        _validate_resolved_set(manifest,resolved_media)
        _validate_scene_fps(scene,manifest)
        scene.frame_set(manifest['reference_frame'])
    if 'frame' in manifest:scene.frame_set(manifest['frame'])
    boundaries=_boundaries(scene,state);before=_protection(scene,wid,boundaries,manifest['workflow_version'])
    if op=='create':
        original_root=scene.compositing_node_group
        if original_root:_tree_state(original_root)
        if original_root and manifest['workflow_version'] in {'1.2','1.3'} and manifest['source']['kind']=='existing':
            _reject_unbound_temporal(original_root)
        original={'root':original_root.name if original_root else None,
                  'root_library':original_root.library.filepath if original_root and original_root.library else None,
                  'source':None,'output':None,'enabled':scene.render.use_compositing}
        if original_root:
            out,port=_output(original_root)
            if len(port.links)>1:_fail('UNSUPPORTED','Root image port has multiple sources')
            original['output']={'node':out.name,'socket':port.identifier}
            if port.links:original['source']={'node':port.links[0].from_node.name,'socket':port.links[0].from_socket.identifier}
        if manifest['source']['kind']=='existing' and (not original_root or not original['source']):
            _fail('UNSUPPORTED','Existing source requires a connected original root')
        group=_build_graph(manifest,scene,wid,resolved_media)
        # COW is deliberately conservative. A fake user may make it over-copy, never under-copy.
        cow=bool(original_root and (original_root.library or original_root.override_library or original_root.users>1))
        root=original_root.copy() if cow else original_root
        new_root=root is None
        if new_root:
            root=bpy.data.node_groups.new('CWF '+wid+' Root','CompositorNodeTree')
            root.interface.new_socket(name='Image',in_out='OUTPUT',socket_type='NodeSocketColor')
            out=root.nodes.new('NodeGroupOutput');_mark(out,scene,wid,'root_output','$root_output')
        if cow or new_root:_mark(root,scene,wid,'root_copy' if cow else 'root')
        try:
            output,port=_output(root)
            source_socket,source=_source(root,scene,manifest,original,resolved_media)
            bridge=root.nodes.new('CompositorNodeGroup');bridge.node_tree=group;bridge.name='CWF '+wid+' Bridge';_mark(bridge,scene,wid,'bridge','$bridge')
            si=next(s for s in bridge.inputs if s.type=='RGBA');so=next(s for s in bridge.outputs if s.type=='RGBA')
            root.links.new(source_socket,si)
            for link in list(port.links):root.links.remove(link)
            root.links.new(so,port)
            scene.compositing_node_group=root;scene.render.use_compositing=True
            state={'version':'1.0','workflow_id':wid,'scene':scene.name,'view_layer':manifest['view_layer'],
              'root_group':root.name,'managed_group':group.name,'original':original,'source':source,'cow':cow,'new_root':new_root}
            if manifest['workflow_version'] in {'1.1','1.2','1.3'}:
                state['workflow_version']=manifest['workflow_version']
                state['baseline_definition']='main bridge input only; extra managed inputs are not part of bypass baseline'
            if manifest['workflow_version'] in {'1.2','1.3'}:
                state['reference_frame']=manifest['reference_frame'];state['action_history']=[]
            _baseline(scene,state)
        except Exception:
            # Roll back only nodes allocated by this create; retained source data is never deleted.
            if not cow and not new_root:
                for n in list(root.nodes):
                    if _owned(n,scene,wid):root.nodes.remove(n)
                if original['source']:
                    root.links.new(_exact(root.nodes[original['source']['node']].outputs,original['source']['socket']),
                      _exact(root.nodes[original['output']['node']].inputs,original['output']['socket']))
            scene.compositing_node_group=original_root;scene.render.use_compositing=original['enabled']
            if STATE in scene:del scene[STATE]
            raise
    else:
        current=inspect(scene.name,wid)
        if current['conflicts'] and op!='adopt':
            _fail('CONFLICT','Managed or external graph semantics changed',{'expected':state['baseline']['fingerprint'],
              'observed':current['fingerprint'],'differences':current['conflicts']})
        root,bridge,group=_groups(scene,state)
        if op=='adopt':
            # Safety/ownership checks occur in inspect/_observed before accepting a new baseline.
            _baseline(scene,state)
        elif op=='update':
            if manifest.get('replace_graph'):
                replacement=_build_graph(manifest,scene,wid,resolved_media)
                if manifest['workflow_version'] in {'1.2','1.3'}:_remember_action(group,state)
                bridge.node_tree=replacement;state['managed_group']=replacement.name
                if manifest['workflow_version'] in {'1.2','1.3'}:_refresh_declarations(root,replacement,state)
            else:
                undo=[]
                try:
                    for patch in manifest.get('patches',[]):
                        rows=[n for n in group.nodes if _owned(n,scene,wid) and n.get(NODE_ID)==patch['node']]
                        if len(rows)!=1:_fail('NOT_FOUND','Managed patch node must resolve uniquely: '+patch['node'])
                        if patch['node'] in ('$input','$output'):_fail('UNSUPPORTED','Public interface patch is outside M1')
                        _patch(rows[0],patch,undo)
                    if state.get('workflow_version','1.0') in {'1.1','1.2','1.3'}:_validate_bound_graph(group,scene,state['view_layer'],wid)
                except Exception:
                    for entry in reversed(undo):
                        if entry[0]=='socket':entry[1].default_value=entry[2]
                        else:setattr(entry[1],entry[2],entry[3])
                    raise
            if manifest['workflow_version'] in {'1.2','1.3'} and manifest.get('animation_patches'):
                _update_tracks(group,manifest['animation_patches'],scene,wid,state)
                _refresh_declarations(root,group,state)
            _baseline(scene,state)
        elif op=='detach':
            if manifest['workflow_version'] in {'1.2','1.3'}:_remember_action(group,state)
            original=state['original']
            if state['cow'] or state['new_root']:
                found=[g for g in bpy.data.node_groups if g.name==original['root'] and
                       (g.library.filepath if g.library else None)==original.get('root_library')] if original['root'] else []
                restored=found[0] if len(found)==1 else None
                if original['root'] and restored is None:_fail('CONFLICT','Original root identity disappeared or is ambiguous')
                scene.compositing_node_group=restored
            else:
                output=root.nodes.get(original['output']['node']) if original['output'] else None
                if not output:_fail('CONFLICT','Original output endpoint disappeared')
                port=_exact(output.inputs,original['output']['socket'])
                from_socket=None
                if original['source']:
                    node=root.nodes.get(original['source']['node'])
                    if not node:_fail('CONFLICT','Original upstream endpoint disappeared')
                    from_socket=_exact(node.outputs,original['source']['socket'])
                for n in list(root.nodes):
                    if _owned(n,scene,wid):root.nodes.remove(n)
                if from_socket:root.links.new(from_socket,port)
            scene.render.use_compositing=original['enabled']
            if manifest['workflow_version'] in {'1.2','1.3'}:
                retained=json.loads(scene.get(ARCHIVE,'[]'))
                for entry in state['action_history']:
                    row={**entry,'workflow_id':wid}
                    if all(r['action']!=row['action'] for r in retained):retained.append(row)
                scene[ARCHIVE]=_dump(retained)
            del scene[STATE]
    if manifest['workflow_version'] in {'1.2','1.3'}:scene.frame_set(manifest['reference_frame'])
    after=_protection(scene,wid,boundaries|_boundaries(scene,_state(scene)),manifest['workflow_version'])
    differences=_diff(before,after)
    protection={'status':'pass' if not differences else 'fail','before':before,'after':after,'differences':differences}
    if differences:_fail('VALIDATION_FAILED','Non-target semantic protection changed',protection)
    inspection=inspect(scene.name,wid)
    return {'operation':op,'scene':scene.name,'workflow_id':wid,'status':inspection['status'],
      'managed_group':inspection['managed_group'],'root_group':inspection['root_group'],'bridge_node':inspection['bridge_node'],
      'fingerprint':inspection['fingerprint'],'baseline':inspection['baseline'],'protection':protection,'inspection':inspection}

# M3 helpers keep the legacy serializers unchanged unless a tree has explicit
# registered M3 metadata. Public declarations never contain resolved host paths.
def _validate_scene_fps(scene,manifest):
    for _,spec in media_sources(manifest):
        if spec['kind'] not in {'image_sequence','movie'}:continue
        fps=spec['mapping']['scene_fps']
        if not math.isclose(scene.render.fps/scene.render.fps_base,fps['numerator']/fps['denominator'],rel_tol=1e-7,abs_tol=1e-7):
            _fail('CONFLICT','Scene rational FPS differs from temporal binding; core does not modify source FPS')

def _validate_resolved_set(manifest,resolved):
    keys={key for key,spec in media_sources(manifest) if spec['kind'] in {'image_sequence','movie'}}
    if resolved is None:resolved={}
    if not isinstance(resolved,dict) or set(resolved)!=keys:
        _fail('INVALID_REQUEST','Internal resolved_media keys must exactly match declared new temporal bindings')
    for key,spec in media_sources(manifest):
        if spec['kind'] in {'image_sequence','movie'}:_validate_resolved(spec,resolved[key])

def _validate_resolved(spec,resolved):
    if spec['kind']=='movie':
        from .movie_contract import validate_resolved_movie
        return validate_resolved_movie(spec,resolved,digest=_media_content_sha256)
    required={'kind','version','declaration_sha256','owner_job_directory','staging_directory','anchor','frames','mapping'}
    if not isinstance(resolved,dict) or set(resolved)!=required or resolved['kind']!='image_sequence' or type(resolved['version']) is not int or resolved['version']!=1:
        _fail('INVALID_REQUEST','Malformed internal resolved sequence envelope')
    if resolved['declaration_sha256']!=_hash(spec) or resolved['mapping']!=spec['mapping']:
        _fail('CONFLICT','Resolved sequence does not match original declaration digest/mapping')
    owner=Path(resolved['owner_job_directory']);staging=Path(resolved['staging_directory'])
    if not owner.is_absolute() or not staging.is_absolute() or not staging.resolve().is_relative_to(owner.resolve()/'media'):
        _fail('CONFLICT','Sequence staging must remain in its owned job/media namespace')
    if staging.is_symlink() or not staging.is_dir():_fail('CONFLICT','Owned sequence staging directory unavailable')
    frames=resolved['frames'];mapping=spec['mapping']
    if not isinstance(frames,list) or len(frames)!=mapping['frame_count']:_fail('CONFLICT','Resolved sequence count differs')
    files=[];extension=None
    for index,(original,frame) in enumerate(zip(spec['frames'],frames),1):
        if set(frame)!={'source_frame','stage_index','scene_frame','original_file','original_sha256','file','sha256'}:
            _fail('INVALID_REQUEST','Malformed internal sequence member')
        if (frame['source_frame']!=original['source_frame'] or frame['stage_index']!=index or
            frame['scene_frame']!=mapping['scene_start']+index-1 or
            Path(frame['original_file']).resolve()!=Path(original['file']).resolve() or
            frame['original_sha256']!=original['expected_sha256'] or frame['sha256']!=original['expected_sha256']):
            _fail('CONFLICT','Resolved sequence original/stage/scene mapping differs')
        path=Path(frame['file'])
        if extension is None:extension=path.suffix.lower()
        if not extension or path.suffix.lower()!=extension or path.name!=f'frame-{index:06d}{extension}':
            _fail('CONFLICT','Sequence staging requires uniform canonical numbered container names')
        if not path.is_absolute() or path.resolve().parent!=staging.resolve() or path.is_symlink():
            _fail('CONFLICT','Sequence member escaped canonical owned staging namespace')
        if not path.is_file() or _media_content_sha256(path)!=frame['sha256']:
            _fail('CONFLICT','Sequence staged member missing or SHA changed',{'file':str(path),'source_frame':frame['source_frame']})
        original_path=Path(original['file'])
        if not original_path.is_file() or _media_content_sha256(original_path)!=original['expected_sha256']:
            _fail('CONFLICT','Original sequence member missing or SHA changed',{'file':str(original_path)})
        files.append(path.name)
    if sorted(p.name for p in staging.iterdir())!=sorted(files):_fail('CONFLICT','Owned staging namespace contains undeclared entries')
    anchor=resolved['anchor']
    if anchor!={'file':frames[0]['file'],'sha256':frames[0]['sha256'],'stage_index':1}:
        _fail('CONFLICT','Resolved anchor must be exact staged member one')
    return resolved

def _reject_unbound_temporal(tree,seen=None):
    seen=set() if seen is None else seen
    if tree.as_pointer() in seen:return
    seen.add(tree.as_pointer())
    for node in tree.nodes:
        image=getattr(node,'image',None)
        if node.bl_idname=='CompositorNodeMovieClip' or (image and image.source in {'SEQUENCE','MOVIE'}):
            _fail('UNSUPPORTED','M3 existing source cannot hide undeclared movie/sequence members; use explicit temporal binding')
        nested=getattr(node,'node_tree',None)
        if nested:_reject_unbound_temporal(nested,seen)

def _sequence_interpretation(spec):
    return ('Linear Rec.709','PREMUL') if spec['kind']=='movie' else (spec['colorspace'],spec['alpha_mode'])

def _bind_sequence(node,spec,manifest,scene,wid,resolved_media):
    key=node.get(NODE_ID)
    if not resolved_media or key not in resolved_media:_fail('INVALID_REQUEST','Sequence requires host-only resolved staging',{'node_id':key})
    resolved=_validate_resolved(spec,resolved_media[key]);_validate_scene_fps(scene,manifest)
    image=bpy.data.images.load(resolved['anchor']['file'],check_existing=False);_mark(image,scene,wid,'binding_image')
    if image.type!='IMAGE' or min(image.size)<1:_fail('UNSUPPORTED','M3 sequence supports decoded flat images only')
    dimensions=tuple(image.size)
    for member in resolved['frames'][1:]:
        decoded=bpy.data.images.load(member['file'],check_existing=False)
        try:
            if decoded.source!='FILE' or decoded.type!='IMAGE' or tuple(decoded.size)!=dimensions:
                _fail('UNSUPPORTED','Sequence members require uniformly decoded flat image dimensions')
        finally:bpy.data.images.remove(decoded)
    colorspace,alpha=_sequence_interpretation(spec)
    image.source='SEQUENCE';image.colorspace_settings.name=colorspace;image.alpha_mode=alpha
    node.image=image;node[BOUND_IMAGE]=image.name;node[BINDING]=_dump(spec);node[MEDIA]=_dump(resolved)
    node.frame_start=spec['mapping']['scene_start'];node.frame_duration=spec['mapping']['frame_count'];node.frame_offset=0
    node.use_cyclic=False;node.use_auto_refresh=True
    _validate_sequence_node(node,spec,scene,wid)

def _validate_sequence_node(node,spec,scene,wid):
    try:resolved=json.loads(node[MEDIA])
    except (KeyError,ValueError,TypeError):_fail('CONFLICT','Sequence resolved metadata missing')
    _validate_resolved(spec,resolved);image=node.image;mapping=spec['mapping']
    colorspace,alpha=_sequence_interpretation(spec)
    if not image or image.name!=node.get(BOUND_IMAGE) or not _owned(image,scene,wid) or image.library or image.override_library:
        _fail('CONFLICT','Sequence Image identity/ownership changed')
    if image.source!='SEQUENCE' or image.type!='IMAGE' or image.packed_files or node.has_views or node.has_layers:
        _fail('UNSUPPORTED','Sequence became packed/multiview/multilayer/nonsequence')
    if (Path(bpy.path.abspath(image.filepath)).resolve()!=Path(resolved['anchor']['file']).resolve() or
        image.colorspace_settings.name!=colorspace or image.alpha_mode!=alpha or
        node.frame_start!=mapping['scene_start'] or node.frame_duration!=mapping['frame_count'] or node.frame_offset!=0 or
        node.use_cyclic or not node.use_auto_refresh):_fail('CONFLICT','Sequence interpretation/native time mapping changed')
    if not mapping['scene_start']<=scene.frame_current<mapping['scene_start']+mapping['frame_count']:
        _fail('CONFLICT','Scene frame is outside declared temporal media domain')
    fps=mapping['scene_fps']
    if not math.isclose(scene.render.fps/scene.render.fps_base,fps['numerator']/fps['denominator'],rel_tol=1e-7):
        _fail('CONFLICT','Sequence scene FPS changed')
    return image

def _temporal_inspection(root,group,scene):
    rows=[]
    for tree in (root,group):
        for node in tree.nodes:
            if MEDIA not in node:continue
            spec=_binding_spec(node);resolved=json.loads(node[MEDIA]);index=scene.frame_current-spec['mapping']['scene_start']
            rows.append({'node_id':node.get(NODE_ID),'declaration':spec,'resolved_media':resolved,
                'active':resolved['frames'][index],'scene_frame':scene.frame_current})
    return sorted(rows,key=lambda x:x['node_id'])

def _binding_identity(root,group):
    rows={}
    for tree in (root,group):
        for node in tree.nodes:
            if BINDING in node:
                key=('root:' if tree==root else 'graph:')+node.get(NODE_ID,node.name)
                rows[key]={'binding':_binding_spec(node),'resolved':json.loads(node[MEDIA]) if MEDIA in node else None}
    rows['tracking_imports']=json.loads(group[ANIM]).get('tracking_imports',[]) if ANIM in group else []
    return rows

def _refresh_declarations(root,group,state):
    state['bound_definitions']=_binding_identity(root,group)
    state['animation_targets']=json.loads(group[ANIM])['targets'] if ANIM in group else []

def _id_owners():
    seen=set()
    for prop in bpy.data.bl_rna.properties:
        if prop.type!='COLLECTION':continue
        for item in getattr(bpy.data,prop.identifier):
            if isinstance(item,bpy.types.ID) and item.as_pointer() not in seen:
                seen.add(item.as_pointer());yield item
            for embedded in (getattr(item,'node_tree',None),getattr(item,'shape_keys',None)):
                if embedded and embedded.as_pointer() not in seen:
                    seen.add(embedded.as_pointer());yield embedded

def _action_consumers(action):
    rows=[]
    for item in _id_owners():
        data=getattr(item,'animation_data',None)
        if not data:continue
        if data.action==action:rows.append({'id':_plain(item),'slot':data.action_slot_handle,'kind':'direct',
            'controls':{key:_plain(getattr(data,key)) for key in ('action_blend_type','action_influence','action_extrapolation','use_nla','use_tweak_mode')}})
        for track in data.nla_tracks:
            for strip in track.strips:
                if strip.action==action:rows.append({'id':_plain(item),'kind':'NLA','track':track.name,'strip':strip.name})
    return sorted(rows,key=_dump)

def _target(tree,target):
    matches=[n for n in tree.nodes if n.get(NODE_ID)==target['node']]
    if len(matches)!=1 or target['node'] in {'$input','$output'}:_fail('NOT_FOUND','Animation target node must be a unique owned managed node')
    node=matches[0];domain=target['domain'];identifier=target['identifier']
    if domain=='properties':
        value_owner=node;field=identifier
        if identifier in UI or identifier in {'layer','view','frame_duration','frame_start','frame_offset','use_cyclic','use_auto_refresh'} or 'path' in identifier.lower() or identifier.startswith('bl_'):
            _fail('UNSUPPORTED','Animation target is UI/binding/time/type metadata')
    else:
        value_owner=_exact(getattr(node,domain),identifier);field='default_value';_available_socket(value_owner)
        if domain=='inputs' and getattr(value_owner,'is_linked',False):_fail('UNSUPPORTED','Linked input defaults are not active parameter targets')
        if domain=='outputs' and (node.bl_idname,identifier) not in {('ShaderNodeValue','Value'),('CompositorNodeRGB','Color')}:
            _fail('UNSUPPORTED','Computed outputs are not authored constant parameter targets')
    prop=value_owner.bl_rna.properties.get(field)
    if not prop or prop.is_readonly or not prop.is_animatable or prop.type not in {'FLOAT','INT'}:
        _fail('UNSUPPORTED','Only animatable numeric scalar/vector RNA targets are supported')
    path=value_owner.path_from_id(field)
    try:actual=tree.path_resolve(path)
    except Exception:_fail('CONFLICT','Internally resolved animation path no longer exists')
    value=_plain(getattr(value_owner,field));size=len(value) if isinstance(value,list) else 1
    if size>4:_fail('UNSUPPORTED','Animation target exceeds vector budget')
    return value_owner,field,path,size,prop

def _value_check(value,size,prop):
    values=value if isinstance(value,list) else [value]
    if len(values)!=size or (size>1 and not isinstance(value,list)) or (size==1 and isinstance(value,list)):
        _fail('INVALID_REQUEST','Animation value shape differs from actual RNA target')
    for item in values:
        if type(item) not in (int,float) or not math.isfinite(item) or not prop.hard_min<=item<=prop.hard_max:
            _fail('INVALID_REQUEST','Animation value outside finite actual RNA bounds')
        if prop.type=='INT' and item!=int(item):_fail('INVALID_REQUEST','Integer RNA target requires integral keys')

def _manifest_tracks(manifest):
    tracks=deepcopy(manifest.get('animation',{}).get('tracks',[]));sources=dict(media_sources(manifest))
    for imported in manifest.get('tracking_imports',[]):
        path=Path(imported['file'])
        if not path.is_file() or _media_content_sha256(path)!=imported['expected_sha256']:
            _fail('CONFLICT','Tracking input file/SHA changed')
        def unique_pairs(pairs):
            result={}
            for key,value in pairs:
                if key in result:raise ValueError('Duplicate tracking JSON field')
                result[key]=value
            return result
        try:doc=json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=unique_pairs)
        except (ValueError,UnicodeError):_fail('INVALID_REQUEST','Tracking document must be strict UTF-8 JSON')
        validate(doc,TRACKING_DOCUMENT_SCHEMA)
        source=sources.get(imported['source_binding'])
        if doc['source_binding']!=imported['source_binding'] or not source or source['kind'] not in {'image_sequence','movie'}:
            _fail('INVALID_REQUEST','Tracking source_binding must identify an explicitly declared temporal source')
        mapping=source['mapping'];samples=doc['samples']
        if [s['source_frame'] for s in samples]!=list(range(mapping['source_start'],mapping['source_start']+mapping['frame_count'])):
            _fail('INVALID_REQUEST','Tracking samples must densely cover the complete declared source domain')
        for field,target in imported['targets'].items():
            declared=next((n for n in manifest.get('graph',{}).get('nodes',[]) if n['id']==target['node']),None)
            if not declared or declared['type']!='CompositorNodeBoxMask' or target!={'node':declared['id'],'domain':'inputs',
                'identifier':{'position':'Position','size':'Size','rotation':'Rotation'}[field]}:
                _fail('UNSUPPORTED','Normalized tracking imports currently require exact BoxMask Position/Size/Rotation targets')
            tracks.append({'target':target,'keys':[{'frame':mapping['scene_start']+sample['source_frame']-mapping['source_start'],
                'value':sample[field],'interpolation':'LINEAR'} for sample in samples],'extrapolation':'CONSTANT'})
    keys=[_target_key(t['target']) for t in tracks]
    if len(keys)!=len(set(keys)):_fail('INVALID_REQUEST','Authored/tracking tracks overlap targets')
    return tracks

def _install_tracks(tree,tracks,scene,wid,provenance=None):
    if not tracks:return
    for track in tracks:
        _check_track(track);owner,field,path,size,prop=_target(tree,track['target'])
        for key in track['keys']:
            _value_check(key['value'],size,prop);setattr(owner,field,key['value'])
            if not owner.keyframe_insert(data_path=field,frame=key['frame']):_fail('VALIDATION_FAILED','Native keyframe insertion failed')
    data=tree.animation_data;action=data.action;_mark(action,scene,wid,'parameter_action');action.use_fake_user=True
    data.action_blend_type='REPLACE';data.action_influence=1;data.action_extrapolation='HOLD';data.use_nla=False
    tree[ANIM]=_dump({'action':action.name,'slot':data.action_slot_handle,'slot_identifier':data.action_slot.identifier,'targets':[t['target'] for t in tracks],
        'tracking_imports':deepcopy(provenance or [])})
    curves=_active_curves(tree)
    definitions={_target(tree,t['target'])[2]:t for t in tracks}
    for curve in curves:
        curve.extrapolation='CONSTANT'
        for point,key in zip(curve.keyframe_points,definitions[curve.data_path]['keys']):point.interpolation=key['interpolation']
    scene.frame_set(scene.frame_current);_animation_proof(tree)

def _active_curves(tree):
    data=tree.animation_data;action=data.action if data else None
    if not action or len(action.slots)!=1 or len(action.layers)!=1 or len(action.layers[0].strips)!=1:
        _fail('CONFLICT','Managed animation requires one exclusive Action/slot/layer/KEYFRAME strip')
    strip=action.layers[0].strips[0]
    if strip.type!='KEYFRAME' or len(strip.channelbags)!=1 or strip.channelbags[0].slot_handle!=data.action_slot_handle:
        _fail('CONFLICT','Managed Action active channelbag/slot changed')
    return list(strip.channelbags[0].fcurves)

def _animation_proof(tree):
    try:metadata=json.loads(tree[ANIM])
    except (ValueError,TypeError,KeyError):_fail('CONFLICT','Managed animation registration missing or invalid')
    data=tree.animation_data;action=data.action if data else None
    scene=_scene(tree[SCENE_TAG]);wid=tree[OWNER]
    if (not action or action.name!=metadata['action'] or not _owned(action,scene,wid) or action.get(ROLE)!='parameter_action' or
        action.library or action.override_library or data.action_slot_handle!=metadata['slot']):
        _fail('CONFLICT','Managed Action ownership/identity/slot changed')
    if (data.action_blend_type!='REPLACE' or data.action_influence!=1 or data.action_extrapolation!='HOLD' or
        data.drivers or data.nla_tracks or data.use_nla or data.use_tweak_mode):_fail('CONFLICT','Managed direct-action controls/drivers/NLA/tweak changed')
    if action.use_frame_range or action.use_cyclic:_fail('CONFLICT','Managed Action frame-range/cyclic controls changed')
    curves=_active_curves(tree)
    if data.action_slot.target_id_type!='NODETREE' or data.action_slot.identifier!=metadata['slot_identifier']:
        _fail('CONFLICT','Managed Action slot target type/identifier changed')
    controls={key:_plain(getattr(data,key)) for key in ('action_blend_type','action_influence','action_extrapolation','use_nla','use_tweak_mode')}
    if _action_consumers(action)!=[{'id':_plain(tree),'slot':data.action_slot_handle,'kind':'direct','controls':controls}]:
        _fail('CONFLICT','Managed Action acquired outside consumers')
    expected={};tracks=[]
    for target in metadata['targets']:
        owner,field,path,size,prop=_target(tree,target)
        for channel in range(size):expected[(path,channel)]=(target,size,prop)
        selected=sorted([f for f in curves if f.data_path==path],key=lambda f:f.array_index)
        if len(selected)!=size:_fail('CONFLICT','Managed animation target channels missing')
        frames=[p.co[0] for p in selected[0].keyframe_points]
        if not frames or frames!=sorted(set(frames)) or any(f!=int(f) or not -100000<=f<=100000 for f in frames):
            _fail('CONFLICT','Managed keys must be bounded sorted integer scene frames')
        if len(frames)>300:_fail('CONFLICT','Managed key budget exceeded')
        keys=[]
        for index,frame in enumerate(frames):
            if any([p.co[0] for p in f.keyframe_points]!=frames for f in selected):_fail('CONFLICT','Vector component key domains differ')
            interps={f.keyframe_points[index].interpolation for f in selected}
            if len(interps)!=1 or not interps<= {'LINEAR','CONSTANT'}:_fail('CONFLICT','Unsupported/differing key interpolation')
            values=[f.keyframe_points[index].co[1] for f in selected];value=values if size>1 else values[0]
            _value_check(value,size,prop)
            keys.append({'frame':int(frame),'value':value,'interpolation':next(iter(interps))})
        tracks.append({'target':target,'keys':keys,'extrapolation':'CONSTANT'})
    if len(curves)!=len(expected) or {(f.data_path,f.array_index) for f in curves}!=set(expected):
        _fail('CONFLICT','Managed Action has extra or duplicate undeclared channels')
    for curve in curves:
        if curve.extrapolation!='CONSTANT' or curve.mute or curve.modifiers or curve.sampled_points or curve.driver:
            _fail('CONFLICT','Unsupported managed FCurve controls/modifiers/samples/driver')
        for point in curve.keyframe_points:
            if not all(math.isfinite(v) for v in [*point.co,*point.handle_left,*point.handle_right]):
                _fail('CONFLICT','Nonfinite managed key/handle values')
    canonical=_action(action)
    for layer in canonical['layers']:
        for strip in layer['strips']:
            for bag in strip['bags']:
                for curve in bag['curves']:
                    target,_,_=expected[(curve['data_path'],curve['array_index'])];curve['data_path']=target
    for imported in metadata.get('tracking_imports',[]):
        path=Path(imported['file'])
        if not path.is_file() or _media_content_sha256(path)!=imported['expected_sha256']:
            _fail('CONFLICT','Tracking provenance file/SHA changed')
    return {'action':canonical,'slot':data.action_slot_handle,'tracking_imports':metadata.get('tracking_imports',[]),'controls':{'action_blend_type':data.action_blend_type,
        'action_influence':data.action_influence,'action_extrapolation':data.action_extrapolation,'use_nla':data.use_nla,
        'use_tweak_mode':data.use_tweak_mode},'consumers':_action_consumers(action),'tracks':sorted(tracks,key=lambda t:_target_key(t['target']))}

_legacy_tree_state=_tree_state
def _tree_state(tree,trail=(),enforce_safety=True,exclude_nodes=(),exclude_link_target=None):
    result=_legacy_tree_state(tree,trail,enforce_safety,exclude_nodes,exclude_link_target)
    if tree and ANIM in tree:
        proof=_animation_proof(tree);result['animation']=proof
        for track in proof['tracks']:
            target=track['target'];row=next(n for n in result['nodes'] if n['id']=='owned:'+target['node'])
            symbol={'animated_target':target}
            if target['domain']=='properties':row['props'][target['identifier']]=symbol
            else:next(s for s in row[target['domain']] if s['identifier']==target['identifier'])['default']=symbol
    return result

def _animation_inspection(group):
    if ANIM not in group:return {'tracks':[],'action':None}
    proof=_animation_proof(group)
    proof['evaluated']=[{'target':t['target'],'value':_plain(getattr(_target(group,t['target'])[0],_target(group,t['target'])[1]))} for t in proof['tracks']]
    return proof

def _registered_actions(scene,wid):
    retained=json.loads(scene.get(ARCHIVE,'[]'))
    for row in retained:
        action=bpy.data.actions.get(row['action'])
        if not action or not _owned(action,scene,row['workflow_id']) or _hash(_action(action))!=row['sha256'] or _action_consumers(action)!=row['consumers']:
            _fail('CONFLICT','Archived retained Action definition/consumers changed')
        for imported in row.get('tracking_imports',[]):
            path=Path(imported['file'])
            if not path.is_file() or _media_content_sha256(path)!=imported['expected_sha256']:
                _fail('CONFLICT','Archived historical tracking provenance missing or changed')
    state=_state(scene)
    names={entry['action'] for entry in retained}
    if not state or state.get('workflow_version') not in {'1.2','1.3'} or state['workflow_id']!=wid:return names
    _validate_history(scene,state)
    names.update(entry['action'] for entry in state.get('action_history',[]))
    group=bpy.data.node_groups.get(state['managed_group'])
    if group and ANIM in group:
        _animation_proof(group);names.add(json.loads(group[ANIM])['action'])
    return names

def _remember_action(group,state):
    if ANIM not in group:return
    action=group.animation_data.action
    entry={'action':action.name,'sha256':_hash(_action(action)),'consumers':_action_consumers(action),
        'tracking_imports':json.loads(group[ANIM]).get('tracking_imports',[])}
    if all(h['action']!=entry['action'] for h in state['action_history']):state['action_history'].append(entry)

def _validate_history(scene,state):
    for entry in state.get('action_history',[]):
        action=bpy.data.actions.get(entry['action'])
        if not action or not _owned(action,scene,state['workflow_id']) or _hash(_action(action))!=entry['sha256'] or _action_consumers(action)!=entry['consumers']:
            _fail('CONFLICT','Retained historical Action definition/consumers changed')
        for imported in entry.get('tracking_imports',[]):
            path=Path(imported['file'])
            if not path.is_file() or _media_content_sha256(path)!=imported['expected_sha256']:
                _fail('CONFLICT','Retained historical tracking provenance missing or changed')

def _update_tracks(group,patches,scene,wid,state):
    tracks=_animation_proof(group)['tracks'] if ANIM in group else []
    definitions={_target_key(t['target']):t for t in tracks};static=[]
    for patch in patches:
        target=patch['track']['target'] if patch['operation']=='replace' else patch['target'];key=_target_key(target)
        if patch['operation']=='replace':definitions[key]=patch['track']
        else:
            if key not in definitions:_fail('NOT_FOUND','Cannot remove an undeclared animation target')
            owner,field,path,size,prop=_target(group,target);_value_check(patch['static_value'],size,prop)
            del definitions[key];static.append((owner,field,patch['static_value']))
    old=group.animation_data.action if group.animation_data else None
    old_meta=group.get(ANIM);old_slot=group.animation_data.action_slot if group.animation_data else None
    if old:_remember_action(group,state)
    group.animation_data_clear()
    if ANIM in group:del group[ANIM]
    try:
        for owner,field,value in static:setattr(owner,field,value)
        _install_tracks(group,list(definitions.values()),scene,wid,json.loads(old_meta).get('tracking_imports',[]) if old_meta else [])
    except Exception:
        group.animation_data_clear()
        if ANIM in group:del group[ANIM]
        if old:
            data=group.animation_data_create();data.action=old;data.action_slot=old_slot
            data.action_blend_type='REPLACE';data.action_influence=1;data.action_extrapolation='HOLD';data.use_nla=False;group[ANIM]=old_meta
            state['action_history']=[h for h in state['action_history'] if h['action']!=old.name]
        scene.frame_set(scene.frame_current);raise
    if old:
        next(h for h in state['action_history'] if h['action']==old.name)['consumers']=_action_consumers(old)

def temporal_evaluation(scene_name,workflow_id,frame):
    scene=_scene(scene_name);state=_state(scene)
    if not state or state.get('workflow_version') not in {'1.2','1.3'} or state['workflow_id']!=workflow_id:
        _fail('NOT_FOUND','Temporal evaluation requires a managed workflow 1.2')
    if type(frame) is not int or not -100000<=frame<=100000:_fail('INVALID_REQUEST','Invalid temporal scene frame')
    reference=state['reference_frame'];scene.frame_set(reference);before=inspect(scene_name,workflow_id)
    if before['conflicts']:_fail('CONFLICT','Temporal evaluation requires the declared managed baseline')
    protection=snapshot_protection(scene_name,workflow_id)
    try:
        scene.frame_set(frame);root,bridge,group=_groups(scene,state)
        return {'scene_frame':frame,'reference_frame':reference,'animation':_animation_inspection(group),
            'temporal_bindings':_temporal_inspection(root,group,scene)}
    finally:
        scene.frame_set(reference)
        after=inspect(scene_name,workflow_id)
        if after['conflicts'] or before['fingerprint']!=after['fingerprint'] or protection!=snapshot_protection(scene_name,workflow_id):
            _fail('VALIDATION_FAILED','Reference-frame proof changed after temporary temporal evaluation')

