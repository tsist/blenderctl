"""Explicit static material-only node snapshot and non-target preservation.

The native hashes are a new content format, not legacy JSON hash equivalents.
No geometry quality, UV islands, BMesh or evaluated mesh is requested here.
"""
from array import array
from contextlib import ExitStack
import copy
import hashlib
import math
import sys

from protocol import Failure, atomic_json, digest

MODE = 'material_only_v1'
NODE_OPS = {'node.add','node.remove','node.input','node.output','node.properties','node.link','node.unlink','node.ramp'}
ATTRIBUTE_TYPES = {'FLOAT','INT','BOOLEAN','INT8','INT16_2D','INT32_2D','FLOAT2','FLOAT_VECTOR','FLOAT4','QUATERNION','FLOAT_COLOR','BYTE_COLOR'}


def block_ref(key, block):
    if block is None:return None
    for index, item in enumerate(key.key_blocks):
        if item.as_pointer()==block.as_pointer():return {'index':index,'name':item.name}
    raise Failure('UNSUPPORTED','Shape key reference is outside its owning Key')


def block_metadata(key, block, index):
    import inspection
    return {'index':index,'name':block.name,'frame':inspection.value(block.frame),
        'settings':inspection.properties(block),'relative_key':block_ref(key,block.relative_key)}


def validate_manifest(manifest, shader_types):
    if 'initial_scene' in manifest or 'deformation_guard' in manifest:
        raise Failure('UNSUPPORTED', MODE+' requires an existing file without initial_scene/deformation_guard')
    created=set()
    for op in manifest['operations']:
        kind=op['op']
        if kind in ('material.create','material.copy'):
            if op['name'] in created:raise Failure('CONFLICT','Duplicate new material name')
            created.add(op['name'])
        elif kind=='material.assign':
            if op['mode']!='replace' or op['data_scope']!='reject_shared':
                raise Failure('UNSUPPORTED',MODE+' requires replace DATA slots with reject_shared')
        elif kind in NODE_OPS:
            if op['tree']['kind']!='MATERIAL' or op['tree']['name'] not in created:
                raise Failure('UNSUPPORTED','Node edits require a material created/copied earlier in this request')
            if kind=='node.add' and op['type'] not in shader_types:
                raise Failure('UNSUPPORTED','Material snapshot only permits ShaderNodeTree node types')
            if kind in ('node.input','node.output') and isinstance(op['value'],dict):
                raise Failure('UNSUPPORTED','ID socket references require a separate resource contract')
        else:raise Failure('UNSUPPORTED','Operation outside static material snapshot scope: '+kind)


def mesh_content(mesh):
    import inspection
    hashes={}; non_finite=0
    def field(collection, prop, width, kind):
        nonlocal non_finite
        data=array(kind,[0])*(len(collection)*width)
        collection.foreach_get(prop,data)
        if kind=='f':non_finite+=sum(not math.isfinite(v) for v in data)
        return {'field':prop,'width':width,'scalar':kind,'itemsize':data.itemsize,'count':len(collection),
                'sha256':hashlib.sha256(data.tobytes()).hexdigest()}
    hashes['positions']=field(mesh.vertices,'co',3,'f')
    hashes['edges']=field(mesh.edges,'vertices',2,'i')
    hashes['loop_vertices']=field(mesh.loops,'vertex_index',1,'i')
    for prop in ('loop_start','loop_total','material_index'):hashes[prop]=field(mesh.polygons,prop,1,'i')
    hashes['smooth']=field(mesh.polygons,'use_smooth',1,'b')
    uv=[]
    for layer in mesh.uv_layers:
        row={'name':layer.name,'settings':inspection.properties(layer),'uv':field(layer.data,'uv',2,'f')}
        for prop in ('pin_uv','select','select_edge'):
            if len(layer.data) and layer.data[0].bl_rna.properties.get(prop):row[prop]=field(layer.data,prop,1,'b')
        uv.append(row)
    shapes=[]
    if mesh.shape_keys:
        for index,block in enumerate(mesh.shape_keys.key_blocks):
            shapes.append({**block_metadata(mesh.shape_keys,block,index),'coordinates':field(block.data,'co',3,'f')})
    # Inspect real vertex.groups even if no Object exposes matching group names.
    offsets=array('I',[0]); groups=array('I'); weights=array('f')
    for vertex in mesh.vertices:
        for group in vertex.groups:
            groups.append(group.group);weights.append(group.weight)
            non_finite+=not math.isfinite(group.weight)
        offsets.append(len(groups))
    hashes['deform_weights']={'encoding':'vertex_offsets_group_indices_float32_v1','vertices':len(mesh.vertices),'entries':len(groups),
        'offsets_sha256':hashlib.sha256(offsets.tobytes()).hexdigest(),'groups_sha256':hashlib.sha256(groups.tobytes()).hexdigest(),
        'weights_sha256':hashlib.sha256(weights.tobytes()).hexdigest()}
    attributes=[]
    for attribute in mesh.attributes:
        data=attribute.data; count=len(data)
        row={'name':attribute.name,'domain':attribute.domain,'data_type':attribute.data_type,'count':count}
        props=[] if not count else [p for p in data[0].bl_rna.properties if not p.is_readonly and p.identifier not in {'rna_type','pixels','tag'}]
        numeric=attribute.data_type in ATTRIBUTE_TYPES and (not count or props and all(p.type in ('FLOAT','INT','BOOLEAN') and tuple(p.array_dimensions)==(p.array_length,0,0) for p in props))
        if numeric:
            # One bulk read per actual numeric RNA field; metadata is hashed too.
            saved_non_finite=non_finite
            try:
                fields=[{**field(data,p.identifier,p.array_length or 1,{'FLOAT':'f','INT':'i','BOOLEAN':'b'}[p.type]),'rna_type':p.type,'array_length':p.array_length} for p in props]
                row.update(encoding='native_numeric_fields_v1',entry_type=data[0].bl_rna.identifier if count else None,fields=fields)
            except (AttributeError,TypeError,ValueError,RuntimeError):
                non_finite=saved_non_finite;numeric=False
        if not numeric:
            payloads=list(inspection._attribute_payloads(attribute))
            non_finite+=sum(not math.isfinite(v) for payload in payloads for v in inspection._numbers(payload))
            row.update(encoding='legacy_full_payload_v1',payloads=payloads)
        row['sha256']=hashlib.sha256(inspection.key(row).encode('utf-8')).hexdigest();attributes.append(row)
    return {'mesh_content_version':'material_native_v1','byteorder':sys.byteorder,
        'counts':{'vertices':len(mesh.vertices),'edges':len(mesh.edges),'loops':len(mesh.loops),'polygons':len(mesh.polygons)},
        'hashes':hashes,'materials':[inspection.identity(m) if m else None for m in mesh.materials],
        'uv_layers':uv,'shape_keys_id':inspection.identity(mesh.shape_keys) if mesh.shape_keys else None,
        'shape_keys':shapes,'attributes':sorted(attributes,key=inspection.key),'non_finite_values':int(non_finite),
        'settings':inspection.properties(mesh,exclude=('name','name_full','use_fake_user')),
        'has_custom_normals':mesh.has_custom_normals,
        'coverage':'base numeric geometry/topology/UV/shape/deform weights/all attributes and normal-related attributes; geometry quality/evaluated meshes not run'}


def content_record(item):
    """Keep normal records and explicitly cover static shape/group RNA relations."""
    import bpy,inspection
    row=inspection.record(item,mesh_reader=mesh_content)
    if isinstance(item,bpy.types.Key):
        row['shape_key_content']={'use_relative':item.use_relative,'eval_time':inspection.value(item.eval_time),
            'user':inspection.identity(item.user) if item.user else None,'reference_key':block_ref(item,item.reference_key),
            'key_blocks':[block_metadata(item,block,index) for index,block in enumerate(item.key_blocks)]}
    if isinstance(item,bpy.types.Object):
        row['object']['vertex_group_settings']={'active_index':item.vertex_groups.active_index,
            'groups':[{'index':group.index,'settings':inspection.properties(group)} for group in item.vertex_groups]}
        if item.type=='MESH':
            row['object']['shape_key_settings']={name:inspection.value(getattr(item,name)) for name in
                ('active_shape_key_index','show_only_shape_key','use_shape_key_edit_mode')}
    return row


def model_state():
    import scenes
    return {'scene_content_version':'material_only_v1','snapshot_kind':'scene_content',
        'scene':scenes.state(record_fn=content_record),
        'coverage':{'scene_content':'existing scenes.state/inspection.record coverage plus native mesh content',
                    'model_quality':'not_run','evaluated_geometry':'not_run','uv_islands':'not_run',
                    'limits':'RNA configuration depth and unsaved image pixels retain existing inspection boundaries'}}


def surface_only(material):
    if not material.node_tree:return
    for link in material.node_tree.links:
        if link.to_node.bl_idname=='ShaderNodeOutputMaterial' and link.to_socket.identifier in ('Volume','Displacement'):
            raise Failure('UNSUPPORTED','Material snapshot supports surface output only')


def assignment_target(op):
    """A DATA-slot change protects static shapes/weights instead of editing geometry."""
    import bpy,modeling,scenes
    if op['mode']!='replace' or op['data_scope']!='reject_shared':
        raise Failure('UNSUPPORTED',MODE+' requires replace DATA slots with reject_shared')
    item=scenes.find(bpy.data.objects,op['object'],True);modeling.local_static(item)
    if item.type!='MESH':raise Failure('INVALID_REQUEST','Material snapshot assignment requires Mesh')
    mesh=item.data;modeling.local_static(mesh)
    if mesh.shape_keys:modeling.local_static(mesh.shape_keys)
    consumers=[owner for owner in bpy.data.objects if owner.data==mesh]
    if mesh.users!=1 or len(consumers)!=1 or consumers[0]!=item:
        raise Failure('CONFLICT','Material snapshot requires an exclusive Mesh DATA owner')
    modeling.bounded(mesh)
    if op['slot']>=len(item.material_slots):raise Failure('NOT_FOUND','Material slot missing')
    if item.material_slots[op['slot']].link!='DATA':raise Failure('UNSUPPORTED','Material snapshot requires DATA material slots')
    return item


def assign_material(op):
    import bpy,scenes
    item=assignment_target(op)
    item.data.materials[op['slot']]=scenes.find(bpy.data.materials,op['material'])
    item.data.update()


def preflight(manifest):
    import bpy,inspection,nodes,scenes
    for item in inspection.all_ids():
        for owner in (item,getattr(item,'node_tree',None)):
            animation=getattr(owner,'animation_data',None)
            if animation and animation.drivers:raise Failure('UNSUPPORTED','Material snapshot refuses source drivers')
    if any(m.type=='NODES' for o in bpy.data.objects for m in o.modifiers):
        raise Failure('UNSUPPORTED','Material snapshot refuses Geometry Nodes modifiers; no caches are detached')
    new_names=set(); assignments=[]
    for op in manifest['operations']:
        kind=op['op']
        if kind in ('material.create','material.copy'):
            scenes.fresh(bpy.data.materials,op['name']);new_names.add(op['name'])
            if kind=='material.copy' and op['material'] not in new_names:
                source=scenes.find(bpy.data.materials,op['material']);nodes.mutable(source)
                if source.node_tree:nodes.mutable(source.node_tree)
                if source.asset_data or source.get('asset_id') or any(k.startswith('mw_') for k in source.keys()):
                    raise Failure('UNSUPPORTED','Cannot copy material asset/managed identity')
                surface_only(source)
        elif kind=='material.assign':
            item=assignment_target(op)
            if op['material'] not in new_names:scenes.find(bpy.data.materials,op['material'])
            assignments.append({'object':item.name,'mesh':inspection.identity(item.data),'slot':op['slot'],'material':op['material']})
    return sorted(new_names),assignments


def exact_mismatch(a,b,path='$'):
    """Non-target preservation never borrows the scene reopen float tolerance."""
    if type(a) is not type(b):return path+'/type'
    if isinstance(a,dict):
        if a.keys()!=b.keys():return path+'/keys'
        for key in a:
            mismatch=exact_mismatch(a[key],b[key],path+'/'+key)
            if mismatch:return mismatch
    elif isinstance(a,list):
        if len(a)!=len(b):return path+'/length'
        for index,(x,y) in enumerate(zip(a,b)):
            mismatch=exact_mismatch(x,y,f'{path}/{index}')
            if mismatch:return mismatch
    elif a!=b:return path
    return None


def verify_preservation(before,after,new_names,assignments,retained):
    import inspection
    expected=copy.deepcopy(before);observed=copy.deepcopy(after)
    base=expected['model']['scene'];current=observed['model']['scene']
    ids={inspection.key({k:r[k] for k in ('type','name','library')}):r for r in base['datablocks']}
    current_ids={inspection.key({k:r[k] for k in ('type','name','library')}):r for r in current['datablocks']}
    allowed={inspection.key({'type':'Material','name':name,'library':None}) for name in new_names}
    if set(current_ids)-set(ids)!=allowed or set(ids)-set(current_ids):
        raise Failure('VALIDATION_FAILED','Material preservation ID集合发生非许可变化')
    current['datablocks']=[r for r in current['datablocks'] if inspection.key({k:r[k] for k in ('type','name','library')}) not in allowed]
    for retained_id in retained:
        k=inspection.key(retained_id)
        if k in ids and ids[k]['fake_user'] is False:
            ids[k]['fake_user']=True
    for assignment in assignments:
        material={'type':'Material','name':assignment['material'],'library':None};slot=assignment['slot']
        ids[inspection.key(assignment['mesh'])]['mesh']['materials'][slot]=material
        binding=next(r for r in expected['material_bindings'] if r['object']==assignment['object'])
        binding['slots'][slot]={'material':assignment['material'],'link':'DATA'}
    mismatch=exact_mismatch(expected,observed)
    if mismatch:raise Failure('VALIDATION_FAILED','Material preservation mismatch: '+mismatch)


def prepare(params,job,manifest):
    import bpy,inspection,nodes,node_zones,scenes
    if not params.get('file'):raise Failure('INVALID_REQUEST',MODE+' requires an existing input file')
    source_sha=digest(params['file'])
    if params.get('expected_sha256') and source_sha!=params['expected_sha256']:raise Failure('CONFLICT','Source SHA changed')
    bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False)
    if tuple(bpy.data.version[:2])!=tuple(bpy.app.version[:2]):raise Failure('UNSUPPORTED','Node edits require matching Blender major/minor')
    if bpy.context.mode!='OBJECT':raise Failure('UNSUPPORTED','Object mode required')
    scenes.activate(manifest['context'])
    new_names,assignments=preflight(manifest)
    with ExitStack() as guards:
        resources=nodes.resource_guards(params,guards);node_zones.validate_all()
        before=nodes.state(MODE);atomic_json(job/'node-before.json',before);actions=[]
        for index,op in enumerate(manifest['operations']):
            scenes.prepare_context(manifest['context'])
            try:
                if op['op']=='material.assign':assign_material(op)
                else:nodes.execute(op,manifest['context'])
                for name in new_names:
                    material=bpy.data.materials.get(name)
                    if material:surface_only(material)
            except Failure as exc:raise Failure(exc.code,f'Operation {index} ({op["op"]}): {exc}') from exc
            actions.append({'index':index,'op':op['op'],'status':'applied_in_candidate_memory'})
            atomic_json(job/'node-actions.json',actions)
        scenes.activate(manifest['context']);node_zones.validate_all()
        sequence=nodes.sequence_bindings(manifest['context']['frame']);retained=[]
        for item in inspection.all_ids():
            if not item.library and not item.is_embedded_data and item.users==0 and not item.use_fake_user:
                item.use_fake_user=True;retained.append(inspection.identity(item))
        expected=nodes.state(MODE);verify_preservation(before,expected,new_names,assignments,retained)
        atomic_json(job/'node-expected.json',expected)
        if digest(params['file'])!=source_sha:raise Failure('CONFLICT','Source changed before candidate save')
        candidate=job/'node-candidate.blend'
        if candidate.exists():raise Failure('CONFLICT','Candidate already exists')
        bpy.context.preferences.filepaths.save_version=0
        bpy.ops.wm.save_as_mainfile(filepath=str(candidate),copy=True,relative_remap=True,check_existing=False)
        bpy.ops.wm.open_mainfile(filepath=str(candidate),load_ui=False,use_scripts=False);scenes.activate(manifest['context'])
        observed=nodes.state(MODE);atomic_json(job/'node-report.json',observed)
        verify_preservation(before,observed,new_names,assignments,retained)
        mismatch=scenes.compare(expected,observed)
        if mismatch:raise Failure('VALIDATION_FAILED','Node candidate reopen mismatch: '+mismatch)
        if digest(params['file'])!=source_sha:raise Failure('CONFLICT','Source changed during candidate verification')
        protection={'material_preservation_version':'1.0','snapshot_mode':MODE,'ok':True,'source_sha256':source_sha,
            'original_to_memory':'pass','original_to_reopen':'pass','expected_to_observed':'pass',
            'allowed_new_materials':new_names,'allowed_data_slots':assignments,
            'retained_orphan_fake_user_false_to_true':retained,'protected':'all original content records and relations; Mesh contents exact native hashes, original materials unchanged',
            'not_run':['model_quality','BMesh','UV islands','evaluated mesh contents','render/appearance'],
            'limits':'existing scene/inspection RNA coverage; bounded nested configuration; unsaved Image pixels not fully covered'}
        atomic_json(job/'material-preservation.json',protection)
        atomic_json(job/'node-change.json',{'snapshot_mode':MODE,'operations':manifest['operations'],'context':manifest['context'],
            'retained_orphans':retained,'source_resources':resources,'sequence_bindings':sequence,'reopen':'pass'})
        return {'snapshot_mode':MODE,'candidate':str(candidate),'candidate_sha256':digest(candidate),'operations':len(actions),
            'report':str(job/'node-report.json'),'material_preservation':str(job/'material-preservation.json'),
            'context':manifest['context'],'reopen':'pass','publication':'working_candidate_only','next_step':'project plan-copy/plan-files; transaction apply; material preview'}
