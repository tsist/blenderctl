"""Static evaluated asset previews; original scene is reopened before embedding."""
import copy,hashlib,json,time
from pathlib import Path
from contextlib import ExitStack
import bpy
from mathutils import Vector
from protocol import Failure,atomic_json,read_json,digest
from preview_contract import normalize,render_spec,receipt_inputs
from inspection import identity,key,all_ids
from verification import resolve
import rendering,scenes,assets,nodes,drivers,preview_framing

SURFACE_TYPES = {'MESH', 'CURVE', 'SURFACE', 'FONT'}
SCENE_OBJECT_TYPES = SURFACE_TYPES | {'ARMATURE', 'EMPTY', 'LIGHT', 'CAMERA'}

def fingerprint(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def implementation():return fingerprint({p.name:digest(p) for p in sorted(Path(__file__).parent.glob('*.py'))})
def recipe(params):return {k:v for k,v in params.items() if k!='reuse_receipt'}
def resolve_target(spec):
    target=resolve(list(all_ids()),spec['target'])
    if target.library or target.override_library:raise Failure('UNSUPPORTED','Preview target must be local and non-override')
    return target
def reuse(params,job):
    d=params['reuse_receipt']
    if digest(d['file'])!=d['expected_sha256']:raise Failure('CONFLICT','Preview receipt changed')
    r=read_json(d['file']);documents=receipt_inputs(r);expected=recipe(params)
    if r['recipe']!=expected or r['recipe_sha256']!=fingerprint(expected) or r['implementation_sha256']!=implementation() or r['blender_build']!=bpy.app.build_hash.decode():raise Failure('CONFLICT','Preview receipt invalidated by source, target, settings, dependencies or implementation')
    for item in documents:
        if digest(item['file'])!=item['expected_sha256']:raise Failure('CONFLICT','Preview output/candidate hash changed')
    return {'preview_report_version':'1.0','mode':'reused','outputs':r['outputs'],'candidate':r['candidate']['file'] if r['candidate'] else None,'candidate_sha256':r['candidate']['expected_sha256'] if r['candidate'] else None,'receipt':d['file'],'receipt_sha256':d['expected_sha256'],'embedded_preview':r['embedded_preview'],'framing':r['framing'],'source_saved':False}
def clay():
    m=bpy.data.materials.new('PreviewClay');m.use_nodes=True;b=m.node_tree.nodes.get('Principled BSDF');b.inputs['Base Color'].default_value=(.55,.57,.6,1);b.inputs['Roughness'].default_value=.68;return m
def shader_copy(material,memo):
    if material is None:return None
    if material.as_pointer() in memo:return memo[material.as_pointer()]
    copied=material.copy();copied.animation_data_clear();memo[material.as_pointer()]=copied
    def freeze(tree,seen):
        if tree.as_pointer() in seen:raise Failure('UNSUPPORTED','Recursive shader group')
        seen=seen|{tree.as_pointer()};tree.animation_data_clear()
        for node in tree.nodes:
            if node.bl_idname in ('ShaderNodeScript','ShaderNodeObjectInfo'):raise Failure('UNSUPPORTED','Object-context/OSL shader requires a dedicated preview fixture')
            if getattr(node,'object',None):raise Failure('UNSUPPORTED','Shader object coordinates require an explicit context fixture')
            if getattr(node,'node_tree',None):node.node_tree=node.node_tree.copy();freeze(node.node_tree,seen)
    if copied.node_tree:freeze(copied.node_tree,set())
    return copied
def fixture(spec,target):
    if spec['fixture']=='CUBE':bpy.ops.mesh.primitive_cube_add(size=2)
    else:bpy.ops.mesh.primitive_uv_sphere_add(segments=48,ring_count=24,radius=1)
    o=bpy.context.object;o.name='PreviewFixture'
    for p in o.data.polygons:p.use_smooth=spec['fixture']=='UV_SPHERE'
    if isinstance(target,bpy.types.Material):o.data.materials.append(target)
    elif target.bl_rna.identifier=='GeometryNodeTree':
        m=o.modifiers.new('PreviewGeometry','NODES');m.node_group=target
    else:
        material=bpy.data.materials.new('PreviewShaderFixture');material.use_nodes=True;tree=material.node_tree;tree.nodes.clear();group=tree.nodes.new('ShaderNodeGroup');group.node_tree=target;output=tree.nodes.new('ShaderNodeOutputMaterial');sockets=[s for s in group.outputs if s.type=='SHADER']
        if len(sockets)!=1:raise Failure('UNSUPPORTED','Shader group fixture requires exactly one shader output')
        tree.links.new(sockets[0],output.inputs['Surface']);o.data.materials.append(material)
    return [o]
def targets(spec,target,scene,layer):
    if spec['fixture']!='NONE':return fixture(spec,target)
    selected=[target] if isinstance(target,bpy.types.Object) else list(target.all_objects)
    if any(o.instance_type != 'NONE' and not (o.type == 'EMPTY' and o.instance_type == 'COLLECTION') for o in selected):
        raise Failure('UNSUPPORTED','Preview supports only native Empty collection instances')
    if any(o.type not in SCENE_OBJECT_TYPES for o in selected):raise Failure('UNSUPPORTED','Target contains an unsupported geometric object type')
    selected=[o for o in selected if o.type in SURFACE_TYPES or o.instance_type == 'COLLECTION']
    if not selected:raise Failure('VALIDATION_FAILED','Preview target contains no surface objects')
    if any(o.library for o in selected):
        raise Failure('UNSUPPORTED','Select the local Empty that places the packed asset')
    if spec['visibility']=='REQUIRE_VISIBLE':
        if any(o.name not in layer.objects or o.hide_viewport or o.hide_render or not o.visible_get(view_layer=layer) for o in selected):raise Failure('UNSUPPORTED','Hidden target requires explicit INCLUDE_HIDDEN preview policy')
    else:
        def show(lc):
            lc.exclude=False;lc.hide_viewport=False
            if not lc.collection.library:
                lc.collection.hide_viewport=False
            for child in lc.children:show(child)
        show(layer.layer_collection)
        for o in scene.objects:
            if not o.library:
                o.hide_viewport=False;o.hide_set(False)
        if any(o.name not in scene.objects for o in selected):raise Failure('UNSUPPORTED','Preview targets must belong to selected scene')
    return sorted(selected,key=lambda x:x.name)

def localize_archived_instances(selected):
    # Use the native localization contract in this disposable worker. It verifies
    # instance transforms and copies the modifier, material and packed image dependencies.
    from collection_local import realize

    surfaces = []
    for item in selected:
        backing = item.instance_collection if item.instance_type == 'COLLECTION' else None
        if backing is None or not backing.library:
            surfaces.append(item)
            continue
        if not backing.is_linked_packed or not backing.library.is_archive:
            raise Failure('UNSUPPORTED', f'Preview requires a native packed asset archive: {item.name}')
        result = realize(item, {'expected_collection': backing.name}, None)
        surfaces.extend(bpy.data.objects[part['object']] for part in result['parts'].values() if part['mesh'] is not None)
    return surfaces

def collection_instance_parts(root):
    # Match dependency-graph instances to a known collection membership.
    # Nested instancers can otherwise introduce geometry not attributable to this root.
    backing = root.instance_collection
    if backing is None or backing.library or backing.override_library:
        raise Failure('UNSUPPORTED', f'Preview needs a local backing collection: {root.name}')
    parts = list(backing.all_objects)
    for part in parts:
        if (part.library or part.override_library or part.instance_type != 'NONE' or
                part.type not in SCENE_OBJECT_TYPES or
                any(m.show_render and m.type in {'NODES', 'PARTICLE_SYSTEM'} for m in part.modifiers)):
            raise Failure('UNSUPPORTED', f'Preview cannot freeze nested or procedural instances: {root.name}/{part.name}')
    return {part.as_pointer(): part for part in parts}

def freeze_meshes(selected,spec):
    # Capture render-level geometry in the original scene. All edits are in the
    # disposable process; embedding later reopens the immutable original.
    roots = {o.as_pointer(): collection_instance_parts(o) for o in selected if o.instance_type == 'COLLECTION'}
    objects = {o.as_pointer(): o for o in bpy.context.scene.objects if not o.library}
    for parts in roots.values():
        objects.update(parts)
    for o in objects.values():
        for m in o.modifiers:
            if m.type!='CLOTH':m.show_viewport=m.show_render
            if m.type=='SUBSURF':m.levels=m.render_levels
    bpy.context.scene.frame_set(spec['frame']);nodes.sequence_bindings(spec['frame']);bpy.context.view_layer.update();graph=bpy.context.evaluated_depsgraph_get();frozen=[];points=[];materials={}
    selected_ids={o.as_pointer() for o in selected}
    neutral=clay() if spec['appearance']=='CLAY' else None

    def freeze(name, evaluated, matrix):
        mesh=bpy.data.meshes.new_from_object(evaluated,preserve_all_data_layers=True,depsgraph=graph)
        if not mesh.polygons:raise Failure('VALIDATION_FAILED','Target has no evaluated surface: '+name)
        if len(points)+len(mesh.vertices)>500000:raise Failure('UNSUPPORTED','Preview exceeds 500000 evaluated vertices')
        world=[matrix@v.co for v in mesh.vertices]
        preview_framing.bounds(world)
        points.extend(world)
        if neutral:
            mesh.materials.clear();mesh.materials.append(neutral)
            for p in mesh.polygons:p.material_index=0
        else:
            copied=[shader_copy(m,materials) for m in mesh.materials];mesh.materials.clear()
            for m in copied:mesh.materials.append(m)
        frozen.append((name,mesh,matrix))

    seen = set()
    for item in graph.object_instances:
        if not item.is_instance or not item.parent:
            continue
        parent = item.parent.original
        root_id = parent.as_pointer()
        if root_id not in selected_ids:
            continue
        evaluated = item.object
        part_id = evaluated.original.as_pointer()
        if root_id not in roots or part_id not in roots[root_id]:
            raise Failure('UNSUPPORTED','Evaluated instances require explicit realization before preview; mesh-only capture would omit geometry')
        if evaluated.type in SURFACE_TYPES and item.show_self and not evaluated.hide_render:
            freeze(f'{parent.name}/{evaluated.original.name}', evaluated, item.matrix_world.copy())
            seen.add(root_id)
    missing_roots = roots.keys() - seen
    if missing_roots:
        missing = next(o.name for o in selected if o.as_pointer() in missing_roots)
        raise Failure('VALIDATION_FAILED', f'Collection instance has no visible evaluated surface: {missing}')
    for o in selected:
        if o.as_pointer() not in roots:
            ev=o.evaluated_get(graph)
            freeze(o.name,ev,ev.matrix_world.copy())
    preview_framing.bounds(points);return frozen,points
def studio(frozen,points,spec):
    scene=bpy.data.scenes.new('AssetPreviewStudio');bpy.context.window.scene=scene;bpy.context.window.view_layer=scene.view_layers[0]
    scene.render.resolution_x=spec['width'];scene.render.resolution_y=spec['height'];scene.render.resolution_percentage=100;scene.render.pixel_aspect_x=scene.render.pixel_aspect_y=1
    lo,hi=preview_framing.bounds(points);center=(lo+hi)*.5;size=(hi-lo).length
    for name,mesh,matrix in frozen:
        o=bpy.data.objects.new('Preview_'+name,mesh);scene.collection.objects.link(o);o.matrix_world=matrix;o.location-=center
    points=[p-center for p in points]
    camera=bpy.data.objects.new('PreviewCamera',bpy.data.cameras.new('PreviewCamera'));scene.collection.objects.link(camera)
    world=bpy.data.worlds.new('PreviewWorld');world.use_nodes=True;world.node_tree.nodes['Background'].inputs['Color'].default_value=(*spec['background'],1);world.node_tree.nodes['Background'].inputs['Strength'].default_value=.5;scene.world=world
    for name,direction,power in [('Key',(-2,-3,4),100),('Fill',(3,-1,2),55),('Rim',(1,3,3),85)]:
        light=bpy.data.lights.new('Preview'+name,'AREA');light.energy=power*size*size;light.shape='DISK';light.size=2*size;o=bpy.data.objects.new('Preview'+name,light);scene.collection.objects.link(o);o.location=Vector(direction).normalized()*size*2;o.rotation_euler=(-o.location).to_track_quat('-Z','Y').to_euler()
    scene.display.shading.light='STUDIO';scene.display.shading.color_type='SINGLE';scene.display.shading.single_color=(.55,.57,.6);scene.display.shading.show_shadows=True;scene.display.shading.show_cavity=True
    return scene,camera,points,center
def alpha_bounds(path,spec):
    import OpenImageIO as oiio,numpy as np
    inp=oiio.ImageInput.open(str(path))
    try:data=np.asarray(inp.read_image(format=oiio.FLOAT));names=list(inp.spec().channelnames)
    finally:inp.close()
    if not spec['transparent']:return None
    if 'A' not in names:raise Failure('VALIDATION_FAILED','Transparent preview has no alpha channel')
    y,x=np.nonzero(data[:,:,names.index('A')]>.05)
    if not len(x):raise Failure('VALIDATION_FAILED','Preview is fully transparent')
    rect=[int(x.min()),int(y.min()),int(x.max()),int(y.max())]
    if rect[0]<1 or rect[1]<1 or rect[2]>=spec['width']-1 or rect[3]>=spec['height']-1:raise Failure('VALIDATION_FAILED','Preview silhouette touches image border')
    return rect
def embed(params,spec,output,job):
    folder=job/'embedded';folder.mkdir();result=assets.prepare({'file':params['file'],'manifest':{'operation':'edit','targets':[{'selector':spec['target'],'preview':{'file':output['file'],'expected_sha256':output['sha256']}}]}},folder)
    before=read_json(folder/'before.snapshot.json');after=read_json(folder/'after.snapshot.json')
    def indexed(snapshot):return {key({k:b[k] for k in ('type','name','library')}):copy.deepcopy(b) for b in snapshot['datablocks']}
    old,new=indexed(before),indexed(after)
    if set(old)!=set(new):raise Failure('VALIDATION_FAILED','Preview embedding changed the retained ID set')
    selected=key(spec['target'])
    for ident,a in old.items():
        b=new[ident]
        if ident==selected:
            if a['asset'] and a['asset']!=b['asset']:raise Failure('VALIDATION_FAILED','Preview changed existing asset metadata')
            for row in (a,b):
                row.pop('asset',None);row.pop('asset_id',None);row['custom_properties'].pop('asset_id',None)
        a['fake_user']=b['fake_user']
        if a!=b:raise Failure('VALIDATION_FAILED','Preview embedding changed covered datablock content: '+ident)
    atomic_json(job/'preview-preservation.json',{'ok':True,'ids':len(old),'target':spec['target'],'coverage':before['coverage'],'permitted':'target asset marking/UUID/custom preview, orphan retention; existing metadata retained'})
    return {'file':result['candidate'],'expected_sha256':result['candidate_sha256']},result['previews'][selected]
def prepare(params,job):
    spec=normalize(params['manifest']);start=time.monotonic()
    if params.get('reuse_receipt'):
        result=reuse(params,job);atomic_json(job/'preview-report.json',result);return result
    bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    if tuple(bpy.data.version[:2])!=tuple(bpy.app.version[:2]):raise Failure('UNSUPPORTED','Preview requires matching Blender major/minor')
    with ExitStack() as stack:
        check_spec=render_spec(spec,spec['scene'],spec['view_layer'],'unused')
        archive_preview = spec['fixture'] == 'NONE' and not spec['embed']
        hashes=rendering.preflight(params,job,check_spec,stack,allow_embedded_asset_archives=archive_preview)
        scene=scenes.find(bpy.data.scenes,spec['scene']);layer=scenes.find(scene.view_layers,spec['view_layer']);bpy.context.window.scene=scene;bpy.context.window.view_layer=layer;scene.frame_set(spec['frame']);bpy.context.view_layer.update();drivers.evaluated(params.get('driver_profile'))
        target=resolve_target(spec);selected=targets(spec,target,scene,layer)
        if bpy.data.libraries:
            layer.update()
            selected = localize_archived_instances(selected)
        frozen,world_points=freeze_meshes(selected,spec);drivers.evaluated(params.get('driver_profile'))
        scene,camera,points,center=studio(frozen,world_points,spec);outputs=[];framing=[]
        for view in spec['views']:
            atomic_json(job/'preview-progress.json',{'state':'rendering','view':view,'completed':len(outputs),'total':len(spec['views'])})
            frame=preview_framing.fit(scene,camera,points,view,spec['projection'],spec['margin']);frame['source_center']=list(center);folder=job/view.lower();folder.mkdir();run=rendering.run({'file':params['file'],'manifest':render_spec(spec,scene.name,scene.view_layers[0].name,camera.name)},folder,prepared_hashes=hashes)
            out=run['outputs'][0];frame['alpha_bounds']=alpha_bounds(out['file'],spec);outputs.append({'view':view,**{k:out[k] for k in ('file','sha256','bytes')}});framing.append(frame)
        candidate=embedded=None
        if spec['embed']:candidate,embedded=embed(params,spec,next(o for o in outputs if o['view']==spec['embed_view']),job)
        r={'preview_receipt_version':'1.0','recipe':recipe(params),'recipe_sha256':fingerprint(recipe(params)),'implementation_sha256':implementation(),'blender_build':bpy.app.build_hash.decode(),'outputs':outputs,'candidate':candidate,'embedded_preview':embedded,'framing':framing,'source_saved':False};atomic_json(job/'preview-receipt.json',r)
        result={'preview_report_version':'1.0','mode':'rendered','outputs':outputs,'candidate':candidate['file'] if candidate else None,'candidate_sha256':candidate['expected_sha256'] if candidate else None,'receipt':str(job/'preview-receipt.json'),'receipt_sha256':digest(job/'preview-receipt.json'),'embedded_preview':embedded,'framing':framing,'seconds':time.monotonic()-start,'source_saved':False};atomic_json(job/'preview-report.json',result);atomic_json(job/'preview-progress.json',{'state':'verified','completed':len(outputs)});return result
