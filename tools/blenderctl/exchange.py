"""Isolated import/export adapters with fresh-import geometric verification."""
import json,math,struct,time
from pathlib import Path, PureWindowsPath
from contextlib import ExitStack
import bpy,bmesh
from mathutils import Matrix,Vector,kdtree
import scenes,modeling,nodes
from protocol import Failure,atomic_json,digest
from filesystem import FileGuard
from exchange_contract import FORMATS,SUFFIX,normalize
OPS={'GLB':('import_scene.gltf','export_scene.gltf'),'OBJ':('wm.obj_import','wm.obj_export'),'STL':('wm.stl_import','wm.stl_export'),'PLY':('wm.ply_import','wm.ply_export'),'FBX':('import_scene.fbx','export_scene.fbx'),'USD':('wm.usd_import','wm.usd_export'),'ABC':('wm.alembic_import','wm.alembic_export')}
def operator(name):
    try:
        op=bpy.ops
        for key in name.split('.'):op=getattr(op,key)
        op.get_rna_type();return op
    except Exception as ex:raise Failure('UNSUPPORTED','Required bundled operator is unavailable: '+name) from ex
def formats(params=None,job=None):
    rows=[]
    for kind,names in OPS.items():
        found={}
        for name in names:
            try:found[name]=bool(operator(name).get_rna_type())
            except Exception:found[name]=False
        rows.append({'format':kind,'operators':found,'available':all(found.values()),'status':'registered_not_roundtrip_verified','adapter_modes':['STATIC',*(['ANIMATION'] if kind in ('GLB','FBX') else [])]})
        if kind=='USD':rows[-1].update(adapter_modes=['STATIC','ANIMATION'],animation_adapter_required='EVALUATED_MESH_CACHE_V1')
        if kind in ('FBX','GLB'):rows[-1].update(native_frame_adapter=kind+'_NATIVE_CLOCK_V1',native_frame_conversion='exchange.convert from EVALUATED_MESH_CACHE_V1 USD')
    modules=[]
    for name in ('io_scene_gltf2','io_scene_fbx'):
        import sys
        m=sys.modules.get(name);modules.append({'module':name,'loaded':m is not None,'version':list(getattr(m,'bl_info',{}).get('version',[])) if m else [],'file':getattr(m,'__file__',None)})
    return {'blender':bpy.app.version_string,'build':bpy.app.build_hash.decode(),'formats':rows,'bundled_adapters':modules,'extensions_policy':'only factory/bundled adapters; never auto-install or execute discovered third-party add-ons'}
def safe_source(profile=None,source=None,job=None):
    from rigging import animation_safe
    animation_safe(profile,source,job)
    if bpy.data.libraries:raise Failure('UNSUPPORTED','Exchange requires local data')
    for s in bpy.data.scenes:
        if s.rigidbody_world:raise Failure('UNSUPPORTED','Freeze simulation geometry through a dedicated adapter first')
    for o in bpy.data.objects:
        if o.particle_systems:raise Failure('UNSUPPORTED','Particles need an explicit conversion adapter')
        if any(m.type in ('CLOTH','SOFT_BODY','FLUID','DYNAMIC_PAINT','MESH_CACHE','MESH_SEQUENCE_CACHE') for m in o.modifiers):raise Failure('UNSUPPORTED','Unmanaged cache modifier in export source')
    for t in bpy.data.node_groups:
        if any(n.bl_idname in ('GeometryNodeSimulationInput','GeometryNodeSimulationOutput','GeometryNodeBake') for n in t.nodes):raise Failure('UNSUPPORTED','Node simulation needs a frozen geometry adapter')
def snapshot(frame,driver_profile=None,objects=None):
    selected_names=set(objects) if objects is not None else None
    s=bpy.context.scene;base=math.floor(frame);s.frame_set(base,subframe=frame-base);deps=bpy.context.evaluated_depsgraph_get();verts=[];triangles=0;area=0;uv_layers=0;objects=[]
    if driver_profile:
        from drivers import evaluated
        evaluated(driver_profile)
    for o in s.objects:
        if o.type!='MESH' or selected_names is not None and o.name not in selected_names:continue
        ev=o.evaluated_get(deps);m=ev.to_mesh(preserve_all_data_layers=True,depsgraph=deps)
        try:
            p=[ev.matrix_world@v.co for v in m.vertices];m.calc_loop_triangles();triangles+=len(m.loop_triangles);uv_layers+=len(m.uv_layers)
            for t in m.loop_triangles:
                a,b,c=(p[i] for i in t.vertices);area+=(b-a).cross(c-a).length/2
            verts.extend([list(v) for v in p]);objects.append({'name':o.name,'vertices':len(p),'triangles':len(m.loop_triangles),'uv_layers':len(m.uv_layers),'materials':[slot.material.name if slot.material else None for slot in ev.material_slots]})
        finally:ev.to_mesh_clear()
    if not verts or triangles<1:raise Failure('VALIDATION_FAILED','No nonempty mesh surface')
    if len(verts)>500000:raise Failure('UNSUPPORTED','Evaluated mesh exceeds 500000 vertices')
    if not all(math.isfinite(v) for p in verts for v in p):raise Failure('VALIDATION_FAILED','Nonfinite geometry')
    return {'frame':frame,'vertices':verts,'triangles':triangles,'area':area,'bounds':[[min(p[i] for p in verts) for i in range(3)],[max(p[i] for p in verts) for i in range(3)]],'objects':objects,'armatures':[{'name':o.name,'bones':len(o.data.bones)} for o in s.objects if o.type=='ARMATURE']}
def compare_geometry(a,b):
    tolerance=1e-4+max(abs(x) for p in a['bounds'] for x in p)*1e-5
    def distance(x,y):
        tree=kdtree.KDTree(len(y))
        for i,p in enumerate(y):tree.insert(p,i)
        tree.balance();return max(tree.find(p)[2] for p in x)
    delta=max(distance(a['vertices'],b['vertices']),distance(b['vertices'],a['vertices']))
    area_delta=abs(a['area']-b['area']);ok=delta<=tolerance and a['triangles']==b['triangles'] and area_delta<=max(1e-5,a['area']*1e-4)
    return {'ok':ok,'bidirectional_vertex_distance':delta,'position_tolerance':tolerance,'triangle_counts':[a['triangles'],b['triangles']],'area_delta':area_delta,'scope':'evaluated surface vertices, triangle count and area; not a proof of arbitrary topology or shader equivalence'}
def _static_mesh(obj, depsgraph, triangulate):
    evaluated = obj.evaluated_get(depsgraph)
    mesh = bpy.data.meshes.new_from_object(evaluated, preserve_all_data_layers=True, depsgraph=depsgraph)
    # Effective object-linked slots must become permanent Mesh slots.
    if len(mesh.materials) != len(evaluated.material_slots):
        raise Failure('UNSUPPORTED', 'Evaluated material slot layout cannot be preserved: ' + obj.name)
    for index, slot in enumerate(evaluated.material_slots):
        mesh.materials[index] = slot.material.original if slot.material else None
    if triangulate:
        _triangulate(mesh)
    mesh.update()
    return mesh


def _triangulate(mesh):
    edit = bmesh.new()
    try:
        edit.from_mesh(mesh)
        bmesh.ops.triangulate(edit, faces=list(edit.faces))
        edit.to_mesh(mesh)
    finally:
        edit.free()


def _ancestry_depth(obj):
    depth = 0
    while obj.parent:
        obj = obj.parent
        depth += 1
    return depth


def preserve_static(objects, scale=1, triangulate=False):
    matrix = Matrix.Scale(scale, 4)
    selected = set(objects)
    if any(obj.type not in ('MESH', 'EMPTY') or obj.parent and obj.parent not in selected for obj in objects):
        raise Failure('INVALID_REQUEST', 'Static hierarchy requires mesh/empty objects and all their parents')
    depsgraph = bpy.context.evaluated_depsgraph_get()
    worlds = {obj: matrix @ obj.matrix_world for obj in objects}
    meshes = {obj: _static_mesh(obj, depsgraph, triangulate) for obj in objects if obj.type == 'MESH'}
    for obj in list(bpy.data.objects):
        if obj not in selected:
            bpy.data.objects.remove(obj, do_unlink=True)
    for obj in objects:
        obj.animation_data_clear()
        obj.constraints.clear()
        if obj.type == 'MESH':
            obj.modifiers.clear()
            obj.data = meshes[obj]
            for slot in obj.material_slots:
                slot.link = 'DATA'
        obj.select_set(True)
    for obj in sorted(objects, key=_ancestry_depth):
        obj.matrix_world = worlds[obj]
        bpy.context.view_layer.update()
    bpy.context.scene.unit_settings.system = 'METRIC'
    bpy.context.scene.unit_settings.scale_length = 1


def flatten(objects,scale=1,triangulate=True):
    deps=bpy.context.evaluated_depsgraph_get();copies=[]
    for o in objects:
        if o.type!='MESH':continue
        m=_static_mesh(o,deps,False)
        transform=Matrix.Scale(scale,4)@o.matrix_world;m.transform(transform)
        if transform.determinant()<0:m.flip_normals()
        if triangulate:_triangulate(m)
        m.update();copies.append((o.name,m))
    for o in list(bpy.data.objects):bpy.data.objects.remove(o,do_unlink=True)
    for name,m in copies:
        o=bpy.data.objects.new(name,m);bpy.context.scene.collection.objects.link(o);o.select_set(True)
    bpy.context.scene.unit_settings.system='METRIC';bpy.context.scene.unit_settings.scale_length=1;bpy.context.view_layer.update()
def _relative_texture(root, value):
    relative = PureWindowsPath(value)
    if relative.is_absolute() or relative.drive or '..' in relative.parts or ':' in value:
        raise Failure('UNSUPPORTED','Exchange texture path must be inside the exchange directory')
    resolved = root.joinpath(*relative.parts).resolve()
    if not resolved.is_relative_to(root):
        raise Failure('UNSUPPORTED','Resolved texture path leaves the exchange directory')
    return resolved


def _usd_textures(path, packed_profile):
    from pxr import Sdf
    layer=Sdf.Layer.FindOrOpen(str(path))
    if not layer:raise Failure('INVALID_REQUEST','Cannot decode USD layer')
    if layer.GetCompositionAssetDependencies() or layer.GetExternalReferences():
        raise Failure('UNSUPPORTED','USD external references, layers and payloads are not accepted')
    if not packed_profile:
        if '@' in layer.ExportToString():
            raise Failure('UNSUPPORTED','USD external assets are not accepted in geometry-only import')
        return []

    assets = set(layer.GetExternalAssetDependencies())

    def collect(spec_path):
        attr = layer.GetAttributeAtPath(spec_path)
        if not attr or attr.typeName not in (Sdf.ValueTypeNames.Asset, Sdf.ValueTypeNames.AssetArray):
            return
        values = [attr.default, *(layer.QueryTimeSample(spec_path, t) for t in layer.ListTimeSamplesForPath(spec_path))]
        for value in values:
            if isinstance(value, Sdf.AssetPath) and value.path:
                assets.add(value.path)
            elif value is not None:
                assets.update(member.path for member in value if member.path)

    layer.Traverse(Sdf.Path.absoluteRootPath, collect)
    textures = [_relative_texture(path.parent.resolve(), name) for name in sorted(assets)]
    if any(texture.suffix.lower() not in ('.png','.jpg','.jpeg','.tif','.tiff','.exr') for texture in textures):
        raise Failure('UNSUPPORTED','USD PBR permits only raster texture resources')
    return textures


def _embedded_fbx_textures(tree, base):
    embedded, used = set(), set()
    for node in tree.elems:
        if node.id != b'Objects':
            continue
        for entry in node.elems:
            if entry.id not in (b'Video', b'Texture'):
                continue
            children = {child.id: child.props[0] for child in entry.elems if child.props}
            relative = children.get(b'RelativeFilename')
            if not isinstance(relative, bytes) or not relative:
                raise Failure('UNSUPPORTED','Embedded FBX texture needs a local relative path')
            name = _relative_texture(base, relative.decode('utf-8')).as_posix().casefold()
            used.add(name)
            if entry.id == b'Video' and children.get(b'Content'):
                embedded.add(name)
    if used - embedded:
        raise Failure('UNSUPPORTED','FBX texture lacks embedded bytes; external image search is disabled')


def file_preflight(path,kind,*,studio_static=False,pbr=False):
    if path.stat().st_size>256*1024*1024:raise Failure('UNSUPPORTED','Exchange input exceeds 256 MiB')
    if kind=='GLB':
        with path.open('rb') as f:
            magic,version,total=struct.unpack('<4sII',f.read(12));length,typ=struct.unpack('<I4s',f.read(8))
            if magic!=b'glTF' or version!=2 or total!=path.stat().st_size or typ!=b'JSON' or length>total-20:raise Failure('INVALID_REQUEST','Invalid GLB container')
            doc=json.loads(f.read(length))
        for item in [*doc.get('buffers',[]),*doc.get('images',[])]:
            if 'uri' in item and not item['uri'].startswith('data:'):raise Failure('UNSUPPORTED','Only embedded GLB resources are accepted')
        extensions = {'KHR_texture_transform', 'KHR_materials_sheen', 'KHR_materials_anisotropy'} if pbr else set()
        if set(doc.get('extensionsRequired', ())) - extensions:
            raise Failure('UNSUPPORTED','Required glTF extensions need a dedicated adapter')
    elif kind=='OBJ':
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if '\\' in line or line.split() and line.split()[0].lower() in ('mtllib','call','csh'):raise Failure('UNSUPPORTED','OBJ external materials/scripts/continuations are outside geometry-only import')
    elif kind=='FBX':
        from io_scene_fbx import parse_fbx
        tree,version=parse_fbx.parse(str(path))
        def walk(e):
            if not pbr and e.id in (b'Video',b'Texture'):raise Failure('UNSUPPORTED','FBX texture resources need embedded PBR import')
            if studio_static and e.id in (b'Deformer',b'AnimationCurve'):
                raise Failure('UNSUPPORTED', 'Studio static FBX cannot contain deformers or animation curves')
            for c in e.elems:walk(c)
        walk(tree)
        if pbr:
            _embedded_fbx_textures(tree, path.parent.resolve())
    elif kind=='USD':
        return _usd_textures(path, pbr)
    return []


def import_file(path,kind,mode='STATIC',*,studio_static=False,pbr=False,resources=()):
    textures = file_preflight(path,kind,studio_static=studio_static,pbr=pbr)
    op=operator(OPS[kind][0]);kw={'filepath':str(path)}
    if kind=='GLB':kw.update(import_pack_images=True,import_shading='NORMALS',bone_heuristic='BLENDER',disable_bone_shape=True,import_scene_as_collection=False)
    elif kind=='OBJ':kw.update(forward_axis='NEGATIVE_Z',up_axis='Y',validate_meshes=True)
    elif kind=='STL':kw.update(forward_axis='Y',up_axis='Z',use_scene_unit=False)
    elif kind=='PLY':kw.update(forward_axis='Y',up_axis='Z',merge_verts=False)
    elif kind=='FBX':
        kw.update(use_image_search=False,use_anim=mode=='ANIMATION',anim_offset=0,use_custom_props=False,automatic_bone_orientation=False)
        if studio_static:
            kw['use_custom_normals'] = True
    elif kind=='USD':kw.update(import_textures_mode='IMPORT_PACK' if pbr else 'IMPORT_NONE',import_materials=pbr,import_volumes=False,import_cameras=False,import_lights=False,import_skeletons=False,set_frame_range=False,apply_unit_conversion_scale=True)
    else:kw.update(as_background_job=False,set_frame_range=False,always_add_cache_reader=False)
    declared = {Path(row['file']).resolve(): row['expected_sha256'] for row in resources}
    with ExitStack() as guards:
        for texture in textures:
            if texture not in declared:
                raise Failure('INVALID_REQUEST', f'USD import requires a declared texture SHA-256: {texture.name}')
            guard = guards.enter_context(FileGuard(texture))
            if guard.sha256() != declared[texture]:
                raise Failure('CONFLICT', f'USD texture changed: {texture.name}')
        if kind=='FBX':
            from fbx_compat import stable_armature_traversal
            with stable_armature_traversal():modeling.finished(op(**kw))
        else:modeling.finished(op(**kw))
        if kind == 'USD' and pbr:
            for image in bpy.data.images:
                if image.source != 'FILE' or image.packed_file:
                    continue
                source = Path(bpy.path.abspath(image.filepath)).resolve()
                if source not in textures and image.filepath.startswith('//'):
                    source = _relative_texture(path.parent.resolve(), image.filepath[2:])
                if source not in textures:
                    raise Failure('VALIDATION_FAILED', f'USD loaded an unbound image: {image.name}')
                image.filepath = str(source)
                image.pack()
def export_file(path,spec):
    kind=spec['format'];anim=spec['mode']=='ANIMATION';kw={'filepath':str(path)}
    pbr=spec['materials'] in ('GLTF_PBR','PBR')
    if kind=='GLB':kw.update(export_format='GLB',use_selection=True,export_animations=anim,export_animation_mode='SCENE',export_frame_range=True,export_force_sampling=True,export_frame_step=1,export_optimize_animation_size=False,export_anim_slide_to_zero=False,export_skins=anim,export_apply=False,export_morph=False,export_materials='EXPORT' if pbr else 'NONE',export_yup=True,export_extras=False,export_vertex_color='NONE',export_all_vertex_colors=False,export_lights=False,export_cameras=False,export_current_frame=not anim)
    elif kind=='OBJ':kw.update(export_selected_objects=True,export_materials=False,forward_axis='NEGATIVE_Z',up_axis='Y',export_triangulated_mesh=spec.get('triangulate',False),global_scale=1)
    elif kind=='STL':kw.update(export_selected_objects=True,forward_axis='Y',up_axis='Z',use_scene_unit=False,global_scale=1)
    elif kind=='PLY':kw.update(export_selected_objects=True,forward_axis='Y',up_axis='Z',export_uv=True,export_normals=True,export_triangulated_mesh=spec.get('triangulate',False),global_scale=1)
    elif kind=='FBX':kw.update(use_selection=True,object_types={'MESH','ARMATURE','EMPTY'},bake_anim=anim,bake_anim_use_nla_strips=False,bake_anim_use_all_actions=False,bake_anim_simplify_factor=0,bake_anim_step=1,add_leaf_bones=False,use_mesh_modifiers=not anim,path_mode='STRIP',embed_textures=False,use_custom_props=False,apply_unit_scale=True,global_scale=1,axis_forward='-Z',axis_up='Y')
    elif kind=='USD':kw.update(selected_objects_only=True,export_animation=False,export_materials=pbr,export_armatures=False,export_shapekeys=False,export_custom_properties=False,convert_world_material=False,generate_preview_surface=pbr,export_textures_mode='NEW' if pbr else 'KEEP',export_subdivision='IGNORE',triangulate_meshes=spec.get('triangulate',False),convert_scene_units='METERS')
    else:kw.update(selected=True,start=spec['frame_start'],end=spec['frame_start'],as_background_job=False,export_hair=False,export_particles=False,export_custom_properties=False,triangulate=spec.get('triangulate',False),global_scale=1,init_scene_frame_range=False)
    if kind=='GLB' and (spec.get('rig_adapter') or spec.get('geometry_adapter')):kw.update(export_morph=True,export_all_influences=True,export_anim_scene_split_object=False)
    # Checked cages include every source vertex, including loose points/edges.
    # glTF omits these by default even when triangle/area checks still agree.
    if kind=='GLB' and (spec.get('geometry_adapter') or spec.get('rig_adapter')):kw.update(use_mesh_edges=True,use_mesh_vertices=True)
    if kind=='FBX' and pbr:kw.update(path_mode='COPY',embed_textures=spec.get('embed_textures',True))
    modeling.finished(operator(OPS[kind][1])(**kw))
    if not path.is_file() or not path.stat().st_size:raise Failure('VALIDATION_FAILED','Exporter produced no file')
def save_candidate(job):
    p=job/'exchange-candidate.blend';bpy.context.preferences.filepaths.save_version=0;modeling.finished(bpy.ops.wm.save_as_mainfile(filepath=str(p),relative_remap=False,check_existing=False));return p


def pbr_profile(objects):
    from exchange_pbr import describe, profile
    return describe(profile(objects))


def _static_prepare(spec, objects, scale):
    triangulate = spec.get('triangulate', False)
    if spec.get('preserve_hierarchy', True):
        preserve_static(objects, scale, triangulate)
    else:
        flatten(objects, scale, triangulate)


def _format_output(path):
    return {'file': str(path), 'sha256': digest(path), 'bytes': path.stat().st_size}


def _portable_candidate_layout():
    scene = bpy.context.scene
    objects = list(scene.objects)
    root = bpy.data.collections.new('ExchangeAsset')
    scene.collection.children.link(root)
    for obj in objects:
        root.objects.link(obj)
        if obj.type == 'MESH':
            obj.data.name = obj.name + '_Mesh'
        for previous in list(obj.users_collection):
            if previous != root:
                previous.objects.unlink(obj)
    for previous in list(scene.collection.children):
        if previous != root:
            scene.collection.children.unlink(previous)
    for image in bpy.data.images:
        if image.packed_file:
            image.filepath = '//' + PureWindowsPath(image.filepath).name


def _material_notes(spec):
    notes = []
    if spec['mode'] == 'STATIC':
        notes.append('Static exchange freezes geometry at one frame')
        if not spec.get('preserve_hierarchy', True):
            notes.append('Static export flattens object hierarchy')
    if spec['format'] in ('OBJ','PLY','STL') and spec.get('preserve_hierarchy', True):
        notes.append('The target format does not store object parenting')
    if spec['format'] == 'STL':
        notes.append('STL stores triangle facets')
    if spec['format'] == 'GLB':
        notes.append('glTF mesh primitives store triangles')
    if spec['format'] == 'FBX' and spec.get('materials') == 'PBR':
        notes.append('Native FBX supports mapped scalar textures and normals without glTF occlusion, transmission or coat')
    if spec['format'] == 'USD' and spec.get('materials') == 'PBR':
        notes.append('UsdPreviewSurface uses a separate relative texture set and a limited Principled lobe mapping')
    if spec['mode'] == 'ANIMATION':
        notes.append('Animation geometry verification samples start, middle and end')
    return notes
def analyze(params,job):
    from mesh_clip_analysis import run
    return run(params,normalize(params['manifest'],'analyze'),job)
def export(params,job):
    spec=normalize(params['manifest'],'export');started=time.monotonic();bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    if tuple(bpy.data.version[:2])!=tuple(bpy.app.version[:2]):raise Failure('UNSUPPORTED','Exchange requires matching Blender major/minor')
    if spec.get('rig_adapter')=='BBONE_SHARED_CONTINUOUS_V1':
        import rig_exchange_bridge
        return rig_exchange_bridge.export_shared(params,spec,job,started)
    if spec.get('geometry_adapter')=='EVALUATED_MESH_CACHE_V1':
        from native_mesh_cache import export as export_cache
        return export_cache(params,spec,job,started)
    if spec.get('geometry_adapter'):
        from mesh_clip import export_clip
        return export_clip(params,spec,job,started)
    if spec.get('rig_adapter'):
        from rig_exchange import export_cage
        return export_cage(params,spec,job,started)
    safe_source(params.get('driver_profile'),params['file'],job);s=scenes.find(bpy.data.scenes,spec['scene']);layer=scenes.find(s.view_layers,spec['view_layer']);bpy.context.window.scene=s;bpy.context.window.view_layer=layer
    selected=[scenes.find(bpy.data.objects,n) for n in spec['objects']]
    if any(o.name not in layer.objects or o.type not in ('MESH','ARMATURE','EMPTY') for o in selected):raise Failure('UNSUPPORTED','Selection must be linked mesh/armature/empty objects')
    if not any(o.type=='MESH' for o in selected):raise Failure('INVALID_REQUEST','Export selection has no mesh')
    with ExitStack() as stack:
        hashes=nodes.resource_guards(params,stack);s.frame_set(spec['frame_start']);nodes.sequence_bindings(spec['frame_start'])
        profile=params.get('driver_profile')
        if profile:
            from drivers import evaluated
            for frame in range(spec['frame_start'],spec['frame_end']+1):
                s.frame_set(frame);bpy.context.view_layer.update();evaluated(profile)
            s.frame_set(spec['frame_start']);bpy.context.view_layer.update()
        materials=[]
        material_conversion=[]
        portable_pbr = spec['materials'] in ('GLTF_PBR', 'PBR')
        if spec['mode']=='STATIC':_static_prepare(spec, selected, spec['meters_per_unit'])
        else:
            if s.unit_settings.scale_length!=1:raise Failure('UNSUPPORTED','Animation source must use meter units at scale one')
            for o in selected:
                if o.parent and o.parent not in selected:raise Failure('INVALID_REQUEST','Animated selection must include all parents')
                if o.type=='MESH' and (o.data.shape_keys or any(m.type!='ARMATURE' for m in o.modifiers)):raise Failure('UNSUPPORTED','Animated meshes allow only armature modifiers; apply other modifiers in a working copy')
                if o.type=='MESH' and any(m.object not in selected for m in o.modifiers if m.type=='ARMATURE'):raise Failure('INVALID_REQUEST','Include deformation armature')
            for o in list(bpy.data.objects):
                if o not in selected:bpy.data.objects.remove(o,do_unlink=True)
            for o in selected:o.select_set(True)
        if portable_pbr:
            from exchange_pbr import adapt, describe, profile as native_materials
            material_rows = native_materials(bpy.context.scene.objects)
            materials = describe(material_rows)
            material_conversion = adapt(material_rows, spec['format'], job)
        if spec['materials']=='NONE':
            for o in bpy.context.scene.objects:
                if o.type=='MESH':o.data.materials.clear()
        if spec['format']=='USD' and spec['mode']=='STATIC' and spec.get('preserve_hierarchy', True):
            for obj in bpy.context.scene.objects:
                if obj.type == 'MESH':
                    obj.data.name = obj.name
        s.frame_start=spec['frame_start'];s.frame_end=spec['frame_end'];fps=s.render.fps;fps_base=s.render.fps_base
        if fps_base!=1:raise Failure('UNSUPPORTED','Exchange requires integer fps')
        frames=sorted(set([spec['frame_start'],(spec['frame_start']+spec['frame_end'])//2,spec['frame_end']]))
        expected=[snapshot(f,profile if spec['mode']=='ANIMATION' else None) for f in frames];atomic_json(job/'exchange-before.json',expected);s.frame_set(spec['frame_start'])
        if profile and spec['mode']=='ANIMATION':
            for frame in range(spec['frame_start'],spec['frame_end']+1):
                s.frame_set(frame);bpy.context.view_layer.update();evaluated(profile)
            s.frame_set(spec['frame_start'])
        path=job/('export'+SUFFIX[spec['format']]);atomic_json(job/'exchange-progress.json',{'state':'exporting','format':spec['format']});export_file(path,spec)
        textures = [_format_output(texture) for texture in _usd_textures(path, True)] if portable_pbr and spec['format'] == 'USD' else []
        texture_descriptors = [{'file': row['file'], 'expected_sha256': row['sha256']} for row in textures]
        bpy.ops.wm.read_factory_settings(use_empty=True);bpy.context.scene.render.fps=fps;bpy.context.scene.render.fps_base=1
        import_file(path,spec['format'],spec['mode'],pbr=portable_pbr,resources=texture_descriptors)
        observed=[snapshot(f) for f in frames];checks=[compare_geometry(a,b) for a,b in zip(expected,observed)]
        atomic_json(job/'exchange-after.json',observed)
        if not all(c['ok'] for c in checks):atomic_json(job/'exchange-checks.json',checks);raise Failure('VALIDATION_FAILED','Fresh import geometry differs; inspect exchange-checks.json')
        if spec['mode']=='STATIC':_static_prepare(spec, list(bpy.context.scene.objects), 1)
        if portable_pbr and spec['mode']=='STATIC':_portable_candidate_layout()
        candidate=save_candidate(job);before_save=snapshot(frames[0]);bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False)
        if not compare_geometry(before_save,snapshot(frames[0]))['ok']:raise Failure('VALIDATION_FAILED','Exchange candidate reopen differs')
        imported_materials = describe(native_materials(bpy.context.scene.objects, strict=False)) if portable_pbr else []
        channel_checks = []
        if portable_pbr:
            from exchange_pbr import compare_textures
            channel_checks = compare_textures(materials, imported_materials, spec['format'])
        report={'exchange_report_version':'1.0','operation':'export','format':spec['format'],'mode':spec['mode'],
                'settings':spec,'checks':checks,'outputs':[_format_output(path), *textures],
                'candidate':str(candidate),'candidate_sha256':digest(candidate),'resource_hashes':hashes,
                'seconds':time.monotonic()-started,'source_saved':False,'reopen':'pass','material_profile':materials,
                'imported_material_profile':imported_materials, 'material_conversion':material_conversion,
                'material_texture_checks': channel_checks,
                'losses':_material_notes(spec)}
        atomic_json(job/'exchange-report.json',report);return report
def import_(params,job):
    if params['manifest'].get('rig_adapter')=='SHARED_RIG_NATIVE_TIME_V1':
        import rig_exchange_bridge
        return rig_exchange_bridge.native_import(params,normalize(params['manifest'],'import'),job)
    if params['manifest'].get('geometry_adapter')=='GLB_NATIVE_CLOCK_V1':
        from glb_clock import import_ as import_clock
        return import_clock(params,normalize(params['manifest'],'import'),job)
    if params['manifest'].get('geometry_adapter')=='FBX_NATIVE_CLOCK_V1':
        from fbx_clock import import_ as import_clock
        return import_clock(params,normalize(params['manifest'],'import'),job)
    if params['manifest'].get('geometry_adapter')=='EVALUATED_MESH_CACHE_V1':
        from native_mesh_cache import import_ as import_cache
        return import_cache(params,normalize(params['manifest'],'import'),job)
    spec=normalize(params['manifest'],'import');bpy.ops.wm.read_factory_settings(use_empty=True);s=bpy.context.scene;s.render.fps=spec['fps'];s.render.fps_base=1
    pbr = spec.get('materials') == 'PBR'
    import_file(Path(params['file']),spec['format'],spec['mode'],pbr=pbr,resources=params.get('resources',()))
    s=bpy.context.scene;s.frame_set(spec['frame'])
    if spec['mode']=='STATIC':_static_prepare(spec, list(s.objects), spec['scale_to_meters'])
    if pbr:_portable_candidate_layout()
    scenes.named(s,spec['scene']);before=snapshot(spec['frame']);atomic_json(job/'exchange-before.json',[before]);candidate=save_candidate(job);bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False);after=snapshot(spec['frame']);check=compare_geometry(before,after)
    if not check['ok']:raise Failure('VALIDATION_FAILED','Imported candidate reopen differs')
    from exchange_pbr import describe, profile
    report={'exchange_report_version':'1.0','operation':'import','format':spec['format'],'mode':spec['mode'],
            'settings':spec,'checks':[check],'outputs':[],'candidate':str(candidate),
            'candidate_sha256':digest(candidate),'source_saved':False,'reopen':'pass',
            'material_profile':describe(profile(bpy.context.scene.objects, strict=False)) if pbr else [],
            'losses':_material_notes(spec)}
    atomic_json(job/'exchange-report.json',report);return report
def convert(params,job):
    spec=normalize(params['manifest'],'convert');results=[]
    for i,item in enumerate(spec['items']):
        root=job/f'item-{i:02d}';root.mkdir();inp=root/'import';inp.mkdir();out=root/'export';out.mkdir();atomic_json(job/'exchange-progress.json',{'state':'converting','index':i,'total':len(spec['items'])})
        if item.get('geometry_adapter')=='FBX_NATIVE_CLOCK_V1':
            from fbx_clock import convert as convert_clock
            results.append(convert_clock(item,out));continue
        if item.get('geometry_adapter')=='GLB_NATIVE_CLOCK_V1':
            from glb_clock import convert as convert_clock
            results.append(convert_clock(item,out));continue
        loaded=import_({'file':item['input']['file'],'manifest':item['import']},inp);s=bpy.context.scene
        manifest={'format':item['output_format'],'scene':s.name,'view_layer':s.view_layers[0].name,'objects':[o.name for o in s.objects if o.type in ('MESH','EMPTY')],'mode':'STATIC','frame_start':item['import']['frame'],'frame_end':item['import']['frame'],'meters_per_unit':1,'materials':'NONE'}
        results.append(export({'file':loaded['candidate'],'manifest':manifest},out))
    report={'exchange_batch_version':'1.0','items':results,'policy':'sequential, fail-fast; failed/partial jobs are not accepted outputs'};atomic_json(job/'exchange-batch.json',report);return report
