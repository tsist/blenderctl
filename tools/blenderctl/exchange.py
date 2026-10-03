# SPDX-License-Identifier: GPL-3.0-or-later
"""Isolated import/export adapters with fresh-import geometric verification."""
import json,math,struct,time
from pathlib import Path
from contextlib import ExitStack
import bpy,bmesh
from mathutils import Matrix,Vector,kdtree
import scenes,modeling,nodes
from protocol import Failure,atomic_json,digest
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
            verts.extend([list(v) for v in p]);objects.append({'name':o.name,'vertices':len(p),'triangles':len(m.loop_triangles),'uv_layers':len(m.uv_layers),'materials':[m.name if m else None for m in o.data.materials]})
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
def flatten(objects,scale=1):
    deps=bpy.context.evaluated_depsgraph_get();copies=[]
    for o in objects:
        if o.type!='MESH':continue
        m=bpy.data.meshes.new_from_object(o.evaluated_get(deps),preserve_all_data_layers=True,depsgraph=deps);transform=Matrix.Scale(scale,4)@o.matrix_world;m.transform(transform)
        if transform.determinant()<0:m.flip_normals()
        bm=bmesh.new();bm.from_mesh(m);bmesh.ops.triangulate(bm,faces=list(bm.faces));bm.to_mesh(m);bm.free();m.update();copies.append((o.name,m))
    for o in list(bpy.data.objects):bpy.data.objects.remove(o,do_unlink=True)
    for name,m in copies:
        o=bpy.data.objects.new(name,m);bpy.context.scene.collection.objects.link(o);o.select_set(True)
    bpy.context.scene.unit_settings.system='METRIC';bpy.context.scene.unit_settings.scale_length=1;bpy.context.view_layer.update()
def file_preflight(path,kind):
    if path.stat().st_size>256*1024*1024:raise Failure('UNSUPPORTED','Exchange input exceeds 256 MiB')
    if kind=='GLB':
        with path.open('rb') as f:
            magic,version,total=struct.unpack('<4sII',f.read(12));length,typ=struct.unpack('<I4s',f.read(8))
            if magic!=b'glTF' or version!=2 or total!=path.stat().st_size or typ!=b'JSON' or length>total-20:raise Failure('INVALID_REQUEST','Invalid GLB container')
            doc=json.loads(f.read(length))
        for item in [*doc.get('buffers',[]),*doc.get('images',[])]:
            if 'uri' in item and not item['uri'].startswith('data:'):raise Failure('UNSUPPORTED','Only embedded GLB resources are accepted')
        if doc.get('extensionsRequired'):raise Failure('UNSUPPORTED','Required glTF extensions need a dedicated adapter')
    elif kind=='OBJ':
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if '\\' in line or line.split() and line.split()[0].lower() in ('mtllib','call','csh'):raise Failure('UNSUPPORTED','OBJ external materials/scripts/continuations are outside geometry-only import')
    elif kind=='FBX':
        from io_scene_fbx import parse_fbx
        tree,version=parse_fbx.parse(str(path))
        def walk(e):
            if e.id in (b'Video',b'Texture'):raise Failure('UNSUPPORTED','FBX texture resources need a separate adapter; use embedded GLB PBR')
            for c in e.elems:walk(c)
        walk(tree)
    elif kind=='USD':
        from pxr import Sdf
        layer=Sdf.Layer.FindOrOpen(str(path))
        if not layer:raise Failure('INVALID_REQUEST','Cannot decode USD layer')
        if layer.GetExternalReferences() or '@' in layer.ExportToString():raise Failure('UNSUPPORTED','USD external references/payloads/assets are not accepted')
def import_file(path,kind,mode='STATIC'):
    file_preflight(path,kind);op=operator(OPS[kind][0]);kw={'filepath':str(path)}
    if kind=='GLB':kw.update(import_pack_images=True,import_shading='NORMALS',bone_heuristic='BLENDER',disable_bone_shape=True,import_scene_as_collection=False)
    elif kind=='OBJ':kw.update(forward_axis='NEGATIVE_Z',up_axis='Y',validate_meshes=True)
    elif kind=='STL':kw.update(forward_axis='Y',up_axis='Z',use_scene_unit=False)
    elif kind=='PLY':kw.update(forward_axis='Y',up_axis='Z',merge_verts=False)
    elif kind=='FBX':kw.update(use_image_search=False,use_anim=mode=='ANIMATION',anim_offset=0,use_custom_props=False,automatic_bone_orientation=False)
    elif kind=='USD':kw.update(import_textures_mode='IMPORT_NONE',import_materials=False,import_volumes=False,import_cameras=False,import_lights=False,import_skeletons=False,set_frame_range=False,apply_unit_conversion_scale=True)
    else:kw.update(as_background_job=False,set_frame_range=False,always_add_cache_reader=False)
    if kind=='FBX':
        from fbx_compat import stable_armature_traversal
        with stable_armature_traversal():modeling.finished(op(**kw))
    else:modeling.finished(op(**kw))
def export_file(path,spec):
    kind=spec['format'];anim=spec['mode']=='ANIMATION';kw={'filepath':str(path)}
    if kind=='GLB':kw.update(export_format='GLB',use_selection=True,export_animations=anim,export_animation_mode='SCENE',export_frame_range=True,export_force_sampling=True,export_frame_step=1,export_optimize_animation_size=False,export_anim_slide_to_zero=False,export_skins=anim,export_apply=False,export_morph=False,export_materials='EXPORT' if spec['materials']=='GLTF_PBR' else 'NONE',export_yup=True,export_extras=False,export_vertex_color='NONE',export_all_vertex_colors=False,export_lights=False,export_cameras=False,export_current_frame=not anim)
    elif kind=='OBJ':kw.update(export_selected_objects=True,export_materials=False,forward_axis='NEGATIVE_Z',up_axis='Y',export_triangulated_mesh=True,global_scale=1)
    elif kind=='STL':kw.update(export_selected_objects=True,forward_axis='Y',up_axis='Z',use_scene_unit=False,global_scale=1)
    elif kind=='PLY':kw.update(export_selected_objects=True,forward_axis='Y',up_axis='Z',export_uv=True,export_normals=True,export_triangulated_mesh=True,global_scale=1)
    elif kind=='FBX':kw.update(use_selection=True,object_types={'MESH','ARMATURE','EMPTY'},bake_anim=anim,bake_anim_use_nla_strips=False,bake_anim_use_all_actions=False,bake_anim_simplify_factor=0,bake_anim_step=1,add_leaf_bones=False,use_mesh_modifiers=not anim,path_mode='STRIP',embed_textures=False,use_custom_props=False,apply_unit_scale=True,global_scale=1,axis_forward='-Z',axis_up='Y')
    elif kind=='USD':kw.update(selected_objects_only=True,export_animation=False,export_materials=False,export_armatures=False,export_shapekeys=False,export_custom_properties=False,convert_world_material=False,export_textures_mode='KEEP',export_subdivision='IGNORE',triangulate_meshes=True,convert_scene_units='METERS')
    else:kw.update(selected=True,start=spec['frame_start'],end=spec['frame_start'],as_background_job=False,export_hair=False,export_particles=False,export_custom_properties=False,triangulate=True,global_scale=1,init_scene_frame_range=False)
    if kind=='GLB' and (spec.get('rig_adapter') or spec.get('geometry_adapter')):kw.update(export_morph=True,export_all_influences=True,export_anim_scene_split_object=False)
    if kind=='GLB' and spec.get('geometry_adapter'):kw.update(use_mesh_edges=True,use_mesh_vertices=True)
    modeling.finished(operator(OPS[kind][1])(**kw))
    if not path.is_file() or not path.stat().st_size:raise Failure('VALIDATION_FAILED','Exporter produced no file')
def save_candidate(job):
    p=job/'exchange-candidate.blend';bpy.context.preferences.filepaths.save_version=0;modeling.finished(bpy.ops.wm.save_as_mainfile(filepath=str(p),relative_remap=False,check_existing=False));return p
def pbr_profile(objects):
    result=[];seen=set()
    for o in objects:
        if o.type!='MESH':continue
        for m in o.data.materials:
            if not m or m.as_pointer() in seen:continue
            seen.add(m.as_pointer())
            if not m.use_nodes or not m.node_tree:raise Failure('UNSUPPORTED','GLTF_PBR requires a Principled material')
            tree=m.node_tree;bs=[n for n in tree.nodes if n.bl_idname=='ShaderNodeBsdfPrincipled'];outs=[n for n in tree.nodes if n.bl_idname=='ShaderNodeOutputMaterial' and n.is_active_output]
            if len(bs)!=1 or len(outs)!=1 or any(n.bl_idname not in ('ShaderNodeBsdfPrincipled','ShaderNodeOutputMaterial','ShaderNodeTexImage') for n in tree.nodes):raise Failure('UNSUPPORTED','PBR subset permits one Principled, output and base-color image nodes')
            if len(outs[0].inputs['Surface'].links)!=1 or outs[0].inputs['Surface'].links[0].from_node!=bs[0]:raise Failure('UNSUPPORTED','Principled must directly drive Surface')
            if outs[0].inputs['Volume'].is_linked or outs[0].inputs['Displacement'].is_linked:raise Failure('UNSUPPORTED','PBR does not export volume or displacement')
            for link in tree.links:
                if link.to_node==bs[0] and (link.to_socket.name!='Base Color' or link.from_node.bl_idname!='ShaderNodeTexImage' or link.from_socket.name!='Color'):raise Failure('UNSUPPORTED','Only base-color image texture links are supported')
            textures=[n for n in tree.nodes if n.bl_idname=='ShaderNodeTexImage']
            for n in textures:
                if not n.image or n.image.source not in ('FILE','GENERATED') or n.inputs['Vector'].is_linked or n.projection!='FLAT' or n.extension not in ('REPEAT','EXTEND') or n.interpolation not in ('Linear','Closest'):raise Failure('UNSUPPORTED','PBR texture requires a raster image, default UV and supported sampler')
            for key in ('Transmission Weight','Coat Weight','Sheen Weight','Subsurface Weight','Anisotropic IOR Level'):
                if bs[0].inputs.get(key) and abs(bs[0].inputs[key].default_value)>1e-6:raise Failure('UNSUPPORTED','Advanced PBR lobe requires a dedicated glTF extension adapter')
            if bs[0].inputs['Alpha'].default_value!=1:raise Failure('UNSUPPORTED','PBR subset is opaque')
            result.append({'name':m.name,'base_color':list(bs[0].inputs['Base Color'].default_value),'metallic':bs[0].inputs['Metallic'].default_value,'roughness':bs[0].inputs['Roughness'].default_value,'textures':[n.image.name for n in textures]})
    return result
def analyze(params,job):
    from mesh_clip_analysis import run
    return run(params,normalize(params['manifest'],'analyze'),job)
def export(params,job):
    spec=normalize(params['manifest'],'export');started=time.monotonic();bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    if tuple(bpy.data.version[:2])!=tuple(bpy.app.version[:2]):raise Failure('UNSUPPORTED','Exchange requires matching Blender major/minor')
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
        materials=pbr_profile(selected) if spec['materials']=='GLTF_PBR' else []
        if spec['mode']=='STATIC':flatten(selected,spec['meters_per_unit'])
        else:
            if s.unit_settings.scale_length!=1:raise Failure('UNSUPPORTED','Animation source must use meter units at scale one')
            for o in selected:
                if o.parent and o.parent not in selected:raise Failure('INVALID_REQUEST','Animated selection must include all parents')
                if o.type=='MESH' and (o.data.shape_keys or any(m.type!='ARMATURE' for m in o.modifiers)):raise Failure('UNSUPPORTED','Animated meshes allow only armature modifiers; apply other modifiers in a working copy')
                if o.type=='MESH' and any(m.object not in selected for m in o.modifiers if m.type=='ARMATURE'):raise Failure('INVALID_REQUEST','Include deformation armature')
            for o in list(bpy.data.objects):
                if o not in selected:bpy.data.objects.remove(o,do_unlink=True)
            for o in selected:o.select_set(True)
        if spec['materials']=='NONE':
            for o in bpy.context.scene.objects:
                if o.type=='MESH':o.data.materials.clear()
        s.frame_start=spec['frame_start'];s.frame_end=spec['frame_end'];fps=s.render.fps;fps_base=s.render.fps_base
        if fps_base!=1:raise Failure('UNSUPPORTED','Exchange requires integer fps')
        frames=sorted(set([spec['frame_start'],(spec['frame_start']+spec['frame_end'])//2,spec['frame_end']]))
        expected=[snapshot(f,profile if spec['mode']=='ANIMATION' else None) for f in frames];atomic_json(job/'exchange-before.json',expected);s.frame_set(spec['frame_start'])
        if profile and spec['mode']=='ANIMATION':
            for frame in range(spec['frame_start'],spec['frame_end']+1):
                s.frame_set(frame);bpy.context.view_layer.update();evaluated(profile)
            s.frame_set(spec['frame_start'])
        path=job/('export'+SUFFIX[spec['format']]);atomic_json(job/'exchange-progress.json',{'state':'exporting','format':spec['format']});export_file(path,spec)
        bpy.ops.wm.read_factory_settings(use_empty=True);bpy.context.scene.render.fps=fps;bpy.context.scene.render.fps_base=1;import_file(path,spec['format'],spec['mode']);observed=[snapshot(f) for f in frames];checks=[compare_geometry(a,b) for a,b in zip(expected,observed)]
        atomic_json(job/'exchange-after.json',observed)
        if not all(c['ok'] for c in checks):atomic_json(job/'exchange-checks.json',checks);raise Failure('VALIDATION_FAILED','Fresh import geometry differs; inspect exchange-checks.json')
        if spec['mode']=='STATIC':flatten(list(bpy.context.scene.objects))
        candidate=save_candidate(job);before_save=snapshot(frames[0]);bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False)
        if not compare_geometry(before_save,snapshot(frames[0]))['ok']:raise Failure('VALIDATION_FAILED','Exchange candidate reopen differs')
        report={'exchange_report_version':'1.0','operation':'export','format':spec['format'],'mode':spec['mode'],'settings':spec,'checks':checks,'outputs':[{'file':str(path),'sha256':digest(path),'bytes':path.stat().st_size}],'candidate':str(candidate),'candidate_sha256':digest(candidate),'resource_hashes':hashes,'seconds':time.monotonic()-started,'source_saved':False,'reopen':'pass','material_profile':materials,'losses':['STATIC bakes evaluated world geometry and triangulates; hierarchy/rig/constraints are not retained','NONE drops all materials; GLTF_PBR accepts the documented opaque Principled/base-color subset; pixel equivalence is independently tested, not automatically proven for every source','STL drops UV/normals/materials/names; PLY/STL can merge objects','Animation verification samples start/mid/end; arbitrary rig constraints/shaders are not universally lossless']};atomic_json(job/'exchange-report.json',report);return report
def import_(params,job):
    if params['manifest'].get('geometry_adapter')=='GLB_NATIVE_CLOCK_V1':
        from glb_clock import import_ as import_clock
        return import_clock(params,normalize(params['manifest'],'import'),job)
    if params['manifest'].get('geometry_adapter')=='FBX_NATIVE_CLOCK_V1':
        from fbx_clock import import_ as import_clock
        return import_clock(params,normalize(params['manifest'],'import'),job)
    if params['manifest'].get('geometry_adapter')=='EVALUATED_MESH_CACHE_V1':
        from native_mesh_cache import import_ as import_cache
        return import_cache(params,normalize(params['manifest'],'import'),job)
    spec=normalize(params['manifest'],'import');bpy.ops.wm.read_factory_settings(use_empty=True);s=bpy.context.scene;s.render.fps=spec['fps'];s.render.fps_base=1;import_file(Path(params['file']),spec['format'],spec['mode']);s=bpy.context.scene;s.frame_set(spec['frame'])
    if spec['mode']=='STATIC':flatten(list(s.objects),spec['scale_to_meters'])
    scenes.named(s,spec['scene']);before=snapshot(spec['frame']);atomic_json(job/'exchange-before.json',[before]);candidate=save_candidate(job);bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False);after=snapshot(spec['frame']);check=compare_geometry(before,after)
    if not check['ok']:raise Failure('VALIDATION_FAILED','Imported candidate reopen differs')
    report={'exchange_report_version':'1.0','operation':'import','format':spec['format'],'mode':spec['mode'],'settings':spec,'checks':[check],'outputs':[],'candidate':str(candidate),'candidate_sha256':digest(candidate),'source_saved':False,'reopen':'pass','losses':['STATIC freezes and triangulates one evaluated frame','Reopen verification is not a source-format semantic equivalence proof','Only embedded GLB and geometry-only OBJ/FBX/USD resources accepted']};atomic_json(job/'exchange-report.json',report);return report
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
        manifest={'format':item['output_format'],'scene':s.name,'view_layer':s.view_layers[0].name,'objects':[o.name for o in s.objects if o.type=='MESH'],'mode':'STATIC','frame_start':item['import']['frame'],'frame_end':item['import']['frame'],'meters_per_unit':1,'materials':'NONE'}
        results.append(export({'file':loaded['candidate'],'manifest':manifest},out))
    report={'exchange_batch_version':'1.0','items':results,'policy':'sequential, fail-fast; failed/partial jobs are not accepted outputs'};atomic_json(job/'exchange-batch.json',report);return report
