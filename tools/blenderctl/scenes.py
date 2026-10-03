# SPDX-License-Identifier: GPL-3.0-or-later
"""Scene candidates using explicit data APIs, bounded operations and reopen verification."""
import math
from pathlib import Path
import bpy,bmesh
from mathutils import Matrix,Vector,Euler
from protocol import Failure,atomic_json,digest
from inspection import all_ids,record,key,identity,value
from scene_contract import normalize

def find(collection,name,write=False):
    matches=[x for x in collection if x.name==name]
    if len(matches)!=1:raise Failure('CONFLICT' if matches else 'NOT_FOUND','Name must resolve uniquely: '+name)
    item=matches[0]
    if write and (item.library or item.override_library):raise Failure('UNSUPPORTED','Editing linked/override data is outside scene preparation: '+name)
    return item

def fresh(collection,name):
    if any(x.name.casefold()==name.casefold() for x in collection):raise Failure('CONFLICT','Name collision: '+name)

def named(item,name):
    item.name=name
    if item.name!=name:raise Failure('CONFLICT','Blender changed requested name: '+name)
    return item

def parent_collection(ref):
    return find(bpy.data.scenes,ref['scene'],True).collection if 'scene' in ref else find(bpy.data.collections,ref['collection'],True)

def update():
    for scene in bpy.data.scenes:
        for layer in scene.view_layers:layer.update()

def acyclic(parent_edge=None,object_edge=None,collection_edge=None):
    objects=list(bpy.data.objects)
    edges={}
    for o in objects:
        parent=parent_edge[1] if parent_edge and parent_edge[0]==o.as_pointer() else o.parent.as_pointer() if o.parent else None
        edges[o.as_pointer()]=([parent] if parent else [])+[c.target.as_pointer() for c in o.constraints if not c.mute and c.influence and getattr(c,'target',None)]
    collections=[*bpy.data.collections,*(s.collection for s in bpy.data.scenes)]
    groups={c.as_pointer():[x.as_pointer() for x in c.children]+[o.instance_collection.as_pointer() for o in c.objects if o.instance_type=='COLLECTION' and o.instance_collection] for c in collections}
    if object_edge:edges.setdefault(object_edge[0],[]).append(object_edge[1])
    if collection_edge:groups.setdefault(collection_edge[0],[]).append(collection_edge[1])
    for graph in (edges,groups):
        done=set()
        def walk(node,trail):
            if node in trail:raise Failure('CONFLICT','Parent/constraint or collection-instance cycle')
            if node in done:return
            if len(trail)>200:raise Failure('UNSUPPORTED','Hierarchy exceeds 200 levels')
            for target in graph.get(node,[]):walk(target,trail|{node})
            done.add(node)
        for node in graph:walk(node,set())

def static_transform(obj):
    if obj.animation_data or obj.rigid_body or obj.parent_type!='OBJECT':raise Failure('UNSUPPORTED','Transform editing requires a static object with object parenting')

def world_editable(obj):
    static_transform(obj)
    if any(not c.mute and c.influence for c in obj.constraints):raise Failure('UNSUPPORTED','World transform requires an unconstrained owner')

def matrix_close(a,b,tol=1e-5):return all(math.isclose(a[i][j],b[i][j],rel_tol=1e-6,abs_tol=tol) for i in range(4) for j in range(4))

def set_world(obj,matrix):
    # Blender decomposes matrices into local TRS; refuse silent shear loss.
    parent=obj.parent.matrix_world@obj.matrix_parent_inverse if obj.parent else Matrix.Identity(4)
    if abs(parent.determinant())<1e-12:raise Failure('CONFLICT','Singular parent transform')
    local=parent.inverted()@matrix
    loc,rot,scale=local.decompose()
    if not matrix_close(local,Matrix.LocRotScale(loc,rot,scale)):raise Failure('UNSUPPORTED','Requested transform introduces local shear')
    obj.matrix_world=matrix;update()
    if not matrix_close(obj.matrix_world,matrix):raise Failure('VALIDATION_FAILED','World transform did not evaluate as requested')

def create_object(op):
    name=op['name'];fresh(bpy.data.objects,name);collection=find(bpy.data.collections,op['collection'],True)
    kind=op['kind'];data=None
    if kind=='INSTANCE':acyclic(collection_edge=(collection.as_pointer(),find(bpy.data.collections,op['instance_collection']).as_pointer()))
    if kind in ('CUBE','PLANE','UV_SPHERE','CYLINDER','CONE'):
        fresh(bpy.data.meshes,name+'.Mesh');data=bpy.data.meshes.new(name+'.Mesh')
        if kind=='PLANE':
            h=op.get('size',2)/2;data.from_pydata([(-h,-h,0),(h,-h,0),(h,h,0),(-h,h,0)],[],[(0,1,2,3)])
        else:
            mesh=bmesh.new()
            try:
                if kind=='CUBE':bmesh.ops.create_cube(mesh,size=op.get('size',2))
                elif kind=='UV_SPHERE':bmesh.ops.create_uvsphere(mesh,u_segments=op.get('segments',32),v_segments=op.get('rings',16),radius=op.get('radius',1))
                else:bmesh.ops.create_cone(mesh,cap_ends=True,cap_tris=False,segments=op.get('segments',32),radius1=op.get('radius',1),radius2=0 if kind=='CONE' else op.get('radius',1),depth=op.get('depth',2))
                mesh.to_mesh(data)
            finally:mesh.free()
        data.update()
    elif kind=='CAMERA':fresh(bpy.data.cameras,name+'.Camera');data=bpy.data.cameras.new(name+'.Camera')
    elif kind=='LIGHT':fresh(bpy.data.lights,name+'.Light');data=bpy.data.lights.new(name+'.Light',op.get('light_type','AREA'))
    item=named(bpy.data.objects.new(name,data),name);collection.objects.link(item)
    if kind=='INSTANCE':item.instance_type='COLLECTION';item.instance_collection=find(bpy.data.collections,op['instance_collection'])

def configure_data(op):
    item=find(bpy.data.objects,op['object'],True)
    expected='CAMERA' if op['op']=='camera.configure' else 'LIGHT'
    if item.type!=expected:raise Failure('INVALID_REQUEST','Wrong object type for '+op['op'])
    data=item.data
    if data.library or data.override_library or data.animation_data:raise Failure('UNSUPPORTED','Data configuration requires local unanimated data')
    if op['data_scope']=='single_user' and data.users>1:
        group=bpy.data.cameras if expected=='CAMERA' else bpy.data.lights
        name=item.name+'.Private';fresh(group,name)
        if data.asset_data or data.get('asset_id'):raise Failure('UNSUPPORTED','Use asset workflow to copy asset data')
        item.data=named(data.copy(),name);data=item.data
    mapping={'radius':'shadow_soft_size','spot_size_deg':'spot_size','angle_deg':'angle'}
    if expected=='LIGHT':
        allowed={'POINT':{'radius'},'AREA':{'size'},'SPOT':{'radius','spot_size_deg','spot_blend'},'SUN':{'angle_deg'}}[data.type]|{'energy','color','op','object','data_scope'}
        if set(op)-allowed:raise Failure('INVALID_REQUEST','Light properties do not apply to '+data.type)
    if expected=='CAMERA' and op.get('clip_end',data.clip_end)<=op.get('clip_start',data.clip_start):raise Failure('INVALID_REQUEST','Camera clip_end must exceed clip_start')
    for name,v in op.items():
        if name in ('op','object','data_scope'):continue
        attr=mapping.get(name,name);v=math.radians(v) if name.endswith('_deg') else v
        setattr(data,attr,v)
        actual=getattr(data,attr)
        if isinstance(v,(int,float)) and not math.isclose(actual,v,rel_tol=1e-5,abs_tol=1e-6):raise Failure('INVALID_REQUEST','Blender clamped '+name)

def execute(op):
    kind=op['op']
    if kind=='scene.create':fresh(bpy.data.scenes,op['name']);named(bpy.data.scenes.new(op['name']),op['name'])
    elif kind=='scene.remove':
        scene=find(bpy.data.scenes,op['scene'],True)
        if len(bpy.data.scenes)==1:raise Failure('CONFLICT','Cannot remove the final scene')
        bpy.data.scenes.remove(scene)
    elif kind=='scene.configure':
        scene=find(bpy.data.scenes,op['scene'],True)
        if 'units' in op:
            units=op['units'];allowed={'NONE':{'ADAPTIVE'},'METRIC':{'ADAPTIVE','METERS','CENTIMETERS','MILLIMETERS','KILOMETERS'},'IMPERIAL':{'ADAPTIVE','MILES','FEET','INCHES'}}[units['system']]
            if units['length_unit'] not in allowed:raise Failure('INVALID_REQUEST','Length unit incompatible with unit system')
            for k,v in units.items():setattr(scene.unit_settings,k,v)
        if 'timeline' in op:
            t=op['timeline']
            if t['end']<t['start']:raise Failure('INVALID_REQUEST','Timeline end precedes start')
            scene.frame_start=t['start'];scene.frame_end=t['end'];scene.render.fps=t['fps'];scene.render.fps_base=t['fps_base']
        if 'camera' in op:
            camera=find(bpy.data.objects,op['camera']) if op['camera'] else None
            if camera and (camera.type!='CAMERA' or camera.name not in scene.objects):raise Failure('INVALID_REQUEST','Scene camera must be a camera linked into this scene')
            scene.camera=camera
    elif kind.startswith('view_layer.'):
        scene=find(bpy.data.scenes,op['scene'],True)
        if kind=='view_layer.create':fresh(scene.view_layers,op['name']);named(scene.view_layers.new(op['name']),op['name'])
        else:
            layer=find(scene.view_layers,op['view_layer'])
            if kind=='view_layer.remove':
                if len(scene.view_layers)==1:raise Failure('CONFLICT','Cannot remove final view layer')
                scene.view_layers.remove(layer)
            else:
                if 'use' in op:layer.use=op['use']
                if 'collection_path' in op:
                    target=layer.layer_collection
                    for name in op['collection_path']:target=find(target.children,name)
                    for name in ('exclude','holdout','indirect_only'):
                        if name in op:setattr(target,name,op[name])
    elif kind=='collection.create':
        fresh(bpy.data.collections,op['name']);parent=parent_collection(op['parent']);parent.children.link(named(bpy.data.collections.new(op['name']),op['name']))
    elif kind=='collection.rename':
        item=find(bpy.data.collections,op['collection'],True)
        if item.name!=op['name']:fresh(bpy.data.collections,op['name']);named(item,op['name'])
    elif kind=='collection.attach':
        item=find(bpy.data.collections,op['collection'],True);parent=parent_collection(op['parent'])
        if op['linked']:
            if item.name not in parent.children:
                acyclic(collection_edge=(parent.as_pointer(),item.as_pointer()));parent.children.link(item)
        else:
            if item.name not in parent.children:raise Failure('NOT_FOUND','Collection is not linked to this parent')
            parent.children.unlink(item)
    elif kind=='collection.member':
        collection=find(bpy.data.collections,op['collection'],True);item=find(bpy.data.objects,op['object'],True)
        if op['linked']:
            if item.name not in collection.objects:
                if item.instance_type=='COLLECTION' and item.instance_collection:acyclic(collection_edge=(collection.as_pointer(),item.instance_collection.as_pointer()))
                collection.objects.link(item)
        else:
            if item.name not in collection.objects:raise Failure('NOT_FOUND','Object is not linked to this collection')
            collection.objects.unlink(item)
    elif kind=='object.create':create_object(op)
    elif kind=='object.duplicate':
        source=find(bpy.data.objects,op['object'],True);fresh(bpy.data.objects,op['name']);collection=find(bpy.data.collections,op['collection'],True)
        if source.type not in ('MESH','EMPTY','CAMERA','LIGHT'):raise Failure('UNSUPPORTED','Object duplication supports mesh/empty/camera/light')
        if source.instance_type=='COLLECTION' and source.instance_collection:acyclic(collection_edge=(collection.as_pointer(),source.instance_collection.as_pointer()))
        if source.asset_data or source.get('asset_id') or source.data and (source.data.asset_data or source.data.get('asset_id')):raise Failure('UNSUPPORTED','Use asset workflow to duplicate marked/identified assets')
        item=named(source.copy(),op['name'])
        if source.data and op['data']=='copy':
            # Only explicitly supported geometry/camera/light copies; rigs and node duplication are later stages.
            if source.type not in ('MESH','CAMERA','LIGHT'):raise Failure('UNSUPPORTED','Independent data copy supports mesh/camera/light')
            group={'MESH':bpy.data.meshes,'CAMERA':bpy.data.cameras,'LIGHT':bpy.data.lights}[source.type]
            name=op['name']+'.Data';fresh(group,name);item.data=named(source.data.copy(),name)
        collection.objects.link(item)
    elif kind=='object.transform':
        item=find(bpy.data.objects,op['object'],True);static_transform(item)
        if op['space']=='local':
            if 'location' in op:item.location=op['location']
            if 'rotation_deg' in op:item.rotation_mode='XYZ';item.rotation_euler=[math.radians(v) for v in op['rotation_deg']]
            if 'scale' in op:item.scale=op['scale']
        else:
            world_editable(item);matrix=item.matrix_world.copy();loc,rot,scale=matrix.decompose()
            if not matrix_close(matrix,Matrix.LocRotScale(loc,rot,scale)):raise Failure('UNSUPPORTED','Existing world shear cannot be edited as TRS')
            loc=Vector(op.get('location',loc));rot=Euler([math.radians(v) for v in op['rotation_deg']],'XYZ').to_quaternion() if 'rotation_deg' in op else rot;scale=Vector(op.get('scale',scale))
            set_world(item,Matrix.LocRotScale(loc,rot,scale))
    elif kind=='object.parent':
        item=find(bpy.data.objects,op['object'],True);world_editable(item);parent=find(bpy.data.objects,op['parent']) if op['parent'] else None
        acyclic(parent_edge=(item.as_pointer(),parent.as_pointer() if parent else None))
        before=item.matrix_world.copy();item.parent=parent
        if item.parent!=parent:raise Failure('VALIDATION_FAILED','Blender refused requested parent')
        item.matrix_parent_inverse=Matrix.Identity(4);update()
        if op['keep_world']:set_world(item,before)
    elif kind=='object.visibility':
        item=find(bpy.data.objects,op['object'],True)
        for prop in ('hide_render','hide_viewport'):
            if prop in op:setattr(item,prop,op[prop])
    elif kind=='object.convert_axes':
        from bpy_extras.io_utils import axis_conversion
        conversion=axis_conversion(from_forward=op['from_forward'],from_up=op['from_up'],to_forward=op['to_forward'],to_up=op['to_up']).to_4x4()@Matrix.Scale(op['scale_factor'],4)
        items=[find(bpy.data.objects,n,True) for n in op['objects']]
        for item in items:world_editable(item)
        desired={o.name:conversion@o.matrix_world for o in items}
        def depth(o):return 0 if o.parent is None else 1+depth(o.parent)
        for item in sorted(items,key=depth):set_world(item,desired[item.name])
    elif kind=='constraint.add':
        item=find(bpy.data.objects,op['object'],True);target=find(bpy.data.objects,op['target']);fresh(item.constraints,op['name'])
        static_transform(item)
        acyclic(object_edge=(item.as_pointer(),target.as_pointer()))
        constraint=named(item.constraints.new(op['type']),op['name']);constraint.target=target
        for prop in ('influence','owner_space','target_space','track_axis','up_axis'):
            if prop in op:setattr(constraint,prop,op[prop])
        if op['type']=='TRACK_TO':constraint.track_axis=op.get('track_axis','TRACK_NEGATIVE_Z');constraint.up_axis=op.get('up_axis','UP_Y')
    elif kind=='constraint.remove':
        item=find(bpy.data.objects,op['object'],True);static_transform(item);item.constraints.remove(find(item.constraints,op['name']))
    elif kind in ('camera.configure','light.configure'):configure_data(op)
    elif kind=='world.create':
        scene=find(bpy.data.scenes,op['scene'],True);fresh(bpy.data.worlds,op['name']);world=named(bpy.data.worlds.new(op['name']),op['name']);world.use_nodes=True
        node=world.node_tree.nodes.get('Background');node.inputs['Color'].default_value=(*op['color'],1);node.inputs['Strength'].default_value=op['strength'];scene.world=world
    else:raise Failure('INVALID_REQUEST','Unsupported scene operation')
    acyclic();update()

def activate(context):
    scene=find(bpy.data.scenes,context['scene']);layer=find(scene.view_layers,context['view_layer']);bpy.context.window.scene=scene;bpy.context.window.view_layer=layer;scene.frame_set(context['frame']);layer.update()
    selected=context['selected_objects'];active=context['active_object']
    if active and active not in selected:raise Failure('INVALID_REQUEST','Active object must be explicitly selected')
    for name in selected:
        item=find(bpy.data.objects,name)
        if item.name not in layer.objects or not item.visible_get(view_layer=layer):raise Failure('CONFLICT','Selected object is not visible in the requested view layer: '+name)
    for item in layer.objects:item.select_set(item.name in selected,view_layer=layer)
    layer.objects.active=find(bpy.data.objects,active) if active else None
    if bpy.context.mode!='OBJECT':raise Failure('CONFLICT','Object mode required')
    return scene,layer

def prepare_context(context):
    # Names may be created by preceding operations. Never execute a transform at a stale frame.
    matches=[s for s in bpy.data.scenes if s.name==context['scene']]
    if len(matches)==1:
        bpy.context.window.scene=matches[0]
        layers=[l for l in matches[0].view_layers if l.name==context['view_layer']]
        if layers:bpy.context.window.view_layer=layers[0]
        matches[0].frame_set(context['frame'])

def state():
    update();blocks=sorted((record(i) for i in all_ids()),key=lambda r:key({k:r[k] for k in ('type','name','library')}))
    scenes=[]
    for scene in sorted(bpy.data.scenes,key=lambda s:s.name):
        layers=[]
        for layer in scene.view_layers:
            flags=[]
            def walk(node,path):
                for child in node.children:
                    path2=path+[child.name];flags.append({'path':path2,'exclude':child.exclude,'holdout':child.holdout,'indirect_only':child.indirect_only});walk(child,path2)
            walk(layer.layer_collection,[])
            layers.append({'name':layer.name,'use':layer.use,'active':layer.objects.active.name if layer.objects.active else None,'selected_objects':sorted(o.name for o in layer.objects if o.select_get(view_layer=layer)),'objects':sorted(o.name for o in layer.objects),'collections':flags})
        scenes.append({'name':scene.name,'root_collections':sorted(c.name for c in scene.collection.children),'root_objects':sorted(o.name for o in scene.collection.objects),'world':identity(scene.world) if scene.world else None,'camera':scene.camera.name if scene.camera else None,'frame':scene.frame_current,'timeline':[scene.frame_start,scene.frame_end,scene.render.fps,scene.render.fps_base],'units':{p:getattr(scene.unit_settings,p) for p in ('system','length_unit','scale_length')},'view_layers':layers})
    collections=[{'name':c.name,'library':c.library.filepath if c.library else None,'children':sorted(n.name for n in c.children),'objects':sorted(o.name for o in c.objects),'instance_offset':list(c.instance_offset)} for c in sorted(bpy.data.collections,key=lambda c:c.name)]
    layer=bpy.context.view_layer;layer.update();graph=bpy.context.evaluated_depsgraph_get()
    objects=[{'name':o.name,'library':o.library.filepath if o.library else None,'matrix_world':value(o.evaluated_get(graph).matrix_world if o.name in layer.objects else o.matrix_world),'evaluated_in_active_view_layer':o.name in layer.objects,'parent_type':o.parent_type,'parent_bone':o.parent_bone,'instance_type':o.instance_type,'instance_collection':identity(o.instance_collection) if o.instance_collection else None,'hide_viewport':o.hide_viewport,'collections':sorted(c.name for c in o.users_collection)} for o in sorted(bpy.data.objects,key=lambda o:o.name)]
    return {'scene_report_version':'1.0','scenes':scenes,'collections':collections,'objects':objects,'datablocks':blocks,'evaluation_context':{'scene':bpy.context.scene.name,'view_layer':layer.name,'frame':bpy.context.scene.frame_current,'mode':bpy.context.mode},'coverage':'declared_scene_fields_and_existing_content_snapshot; not_all_plugin_or_render_state'}

def compare(a,b,path='$'):
    if type(a) in (int,float) and type(b) in (int,float):
        if not math.isfinite(a) or not math.isfinite(b) or not math.isclose(a,b,rel_tol=1e-6,abs_tol=1e-5):return path
    elif isinstance(a,dict) and isinstance(b,dict):
        if a.keys()!=b.keys():return path+'/keys'
        for k in a:
            mismatch=compare(a[k],b[k],path+'/'+k)
            if mismatch:return mismatch
    elif isinstance(a,list) and isinstance(b,list):
        if len(a)!=len(b):return path+'/length'
        for i,(x,y) in enumerate(zip(a,b)):
            mismatch=compare(x,y,f'{path}/{i}')
            if mismatch:return mismatch
    elif a!=b:return path
    return None

def inspect(params,job):
    bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    if 'context' in params:activate(params['context'])
    result=state();atomic_json(job/'scene-report.json',result);return result

def prepare(params,job):
    manifest=normalize(params['manifest'])
    if params.get('file'):
        bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
        if tuple(bpy.data.version[:2])!=tuple(bpy.app.version[:2]):raise Failure('UNSUPPORTED','Scene edits require matching Blender major/minor')
    else:
        bpy.ops.wm.read_factory_settings(use_empty=True);named(bpy.context.scene,manifest.get('initial_scene','Scene'))
    if bpy.context.mode!='OBJECT':raise Failure('UNSUPPORTED','Source must be saved in object mode')
    before=state();atomic_json(job/'scene-before.json',before);acyclic()
    actions=[]
    for index,op in enumerate(manifest['operations']):
        prepare_context(manifest['context'])
        try:execute(op)
        except Failure as exc:raise Failure(exc.code,f'Operation {index} ({op["op"]}): {exc}') from exc
        actions.append({'index':index,'op':op['op'],'status':'applied_in_candidate_memory'})
        atomic_json(job/'scene-actions.json',actions)
    activate(manifest['context'])
    retained=[]
    for item in all_ids():
        if not item.library and not item.is_embedded_data and item.users==0 and not item.use_fake_user:
            item.use_fake_user=True;retained.append(identity(item))
    expected=state();atomic_json(job/'scene-expected.json',expected)
    candidate=job/'scene-candidate.blend'
    if candidate.exists():raise Failure('CONFLICT','Candidate already exists')
    bpy.context.preferences.filepaths.save_version=0
    bpy.ops.wm.save_as_mainfile(filepath=str(candidate),copy=True,relative_remap=True,check_existing=False)
    bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False);activate(manifest['context']);observed=state()
    atomic_json(job/'scene-report.json',observed)
    mismatch=compare(expected,observed)
    if mismatch:raise Failure('VALIDATION_FAILED','Candidate reopen mismatch at '+mismatch)
    atomic_json(job/'scene-change.json',{'operations':manifest['operations'],'context':manifest['context'],'retained_orphans':retained,'reopen':'pass'})
    return {'candidate':str(candidate),'candidate_sha256':digest(candidate),'operations':len(actions),'context':manifest['context'],'reopen':'pass','retained_orphans':retained,'report':str(job/'scene-report.json'),'next_step':'project plan-copy or plan-files; transaction apply; scene inspect/validate final path','publication':'working_candidate_only'}
