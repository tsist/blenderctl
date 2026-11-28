"""Stage and load the two source files used by the native Pencil viewport."""

from contextlib import ExitStack
import os
from pathlib import Path

from material_draft_handoff import fingerprint, guard_file, read_document, require
from protocol import Failure, digest
import toon_viewport_lines_contract as contract


READ = 'inspect-style-pair'
STAGE = 'stage-style-pair'
LOAD = 'load-style-pair'
OPERATIONS = (READ, STAGE, LOAD)
INPUTS = {'document':'document_path','recipe':'recipe_path'}


def normalize_request(document, editions):
    operation = document['operation']
    payload = {key:value for key,value in document.items() if key not in INPUTS}
    require(not {'scope','patch','frames','elements'} & payload.keys(),'INVALID_REQUEST',
            'The raw style pair does not use group or render options')
    result = contract.normalize_request({**payload,'operation':'inspect','scope':{'groups':['style-pair']}},editions)
    result.pop('scope')
    result['operation'] = operation
    ref_keys = document.keys() & INPUTS.keys()
    if operation==READ:
        require(not ref_keys,'INVALID_REQUEST','Inspect does not set or load the raw inputs')
        return result
    require('expected_author_sha256' in result and ref_keys
            and (operation==STAGE or ref_keys==INPUTS.keys()),'INVALID_REQUEST',
            'Supply the expected author SHA and the staged path(s), or both source files to load')
    for key in ref_keys:
        row = document[key]
        require(isinstance(row,dict) and row.keys()=={'file','sha256'} and isinstance(row['file'],str)
                and isinstance(row['sha256'],str) and contract.SHA256.fullmatch(row['sha256']),
                'INVALID_REQUEST','Expected JSON file and SHA-256 for '+key)
        result[key] = row
    return result


def guard_inputs(request, guards):
    for key in request.keys() & INPUTS.keys():
        pair = request[key]
        path = guard_file(pair['file'],guards,pair['sha256'])
        require(path.suffix.lower()=='.json' and path.stat().st_size<=16*1024*1024,
                'INVALID_REQUEST','Pencil source inputs must be bounded JSON files')
        pair['file']=str(path)
    require(not INPUTS.keys() <= request.keys() or request['document']['file']!=request['recipe']['file'],
            'INVALID_REQUEST','Style document and binding recipe need distinct files')


def protect(bpy, context, bindings, dependencies):
    from inspection import custom_value
    from toon_workflow.toon_workflow_addon import joint_workflow

    scene = context.scene
    other = {}
    for row in bpy.data.scenes:
        if row==scene:
            continue
        text = row.toon_viewport_preview.preset_text
        other[row.name_full] = [custom_value(row.get('toon_viewport_preview')),text.as_string() if text else None]
    return fingerprint([joint_workflow.non_author_signature(scene),bindings(),dependencies(),other,
                        scene.camera.name_full if scene.camera else None,
                        scene.frame_current,scene.frame_subframe,scene.frame_start,scene.frame_end,
                        (scene.render.resolution_x,scene.render.resolution_y,scene.render.resolution_percentage),
                        context.active_object.name_full if context.active_object else None,
                        sorted(obj.name_full for obj in context.selected_objects)])


def verify_bindings(scene, document, plan, preview):
    selectors, _ = preview.selector_bindings.records(document)
    local_meshes = [obj for obj in scene.objects if obj.type=='MESH']
    collection_root = scene.collection
    collections = collection_root.children_recursive
    materials = {slot.material for obj in local_meshes for slot in obj.material_slots if slot.material}
    targets = {}
    for kind, blocks in [('objects',local_meshes),('collections',collections),('materials',materials)]:
        refs = [preview.selector_capture.reference(block) for block in blocks]
        targets[kind] = ({preview.selector_bindings.key(row) for row in refs},{row['name'] for row in refs})
    for group in plan['groups']:
        row = selectors.get(group['id'])
        if row and row['enabled']:
            for kind in ('objects','collections','materials'):
                bad = [ref['name'] for ref in row[kind] if preview.selector_bindings.key(ref) not in targets[kind][0]]
                require(not bad,'NOT_FOUND',f"Pencil selector {kind} not present in this scene: {', '.join(bad)}")
        else:
            absent = (set(group['object_ids'])-targets['objects'][1]) | (set(group['material_names'])-targets['materials'][1])
            require(not absent,'NOT_FOUND','Pencil bindings not present in this scene: '+', '.join(sorted(absent)))


def work(bpy, context, request, author, *, bindings, dependencies, save_candidate, author_sha):
    from toon_workflow import toon_viewport_preview as preview

    scene = context.scene
    settings = scene.toon_viewport_preview
    source_sha = request['expected_sha256']
    operation = request['operation']
    protected = protect(bpy,context,bindings,dependencies)

    def read_state():
        return {'document_path':settings.document_path,'recipe_path':settings.recipe_path,'style_mode':settings.style_mode,
                'groups':[{'id':group.name,'label':group.label} for group in settings.groups]}

    before = read_state()
    response = {'operation':operation,'style_pair':before,'author_sha256':author,'state_sha256':fingerprint(before),
                'scene_sha256':protected,'product':request['product'],'context':request['context'],
                'source':{'file':request['file'],'sha256':source_sha,'unchanged':True},
                'runtime':{'blender':bpy.app.version_string,'build':bpy.app.build_hash.decode(),'pid':os.getpid()}}
    if operation==READ:
        require(request.get('verify_state_sha256',response['state_sha256'])==response['state_sha256']
                and request.get('verify_scene_sha256',protected)==protected,'VALIDATION_FAILED',
                'Cold reopen changed the author paths, groups or protected scene')
        return response

    require(scene.library is None and scene.override_library is None and scene.is_editable,'UNSUPPORTED',
            'The raw style pair requires a local editable scene')
    with ExitStack() as guards:
        guard_inputs(request,guards)
        if operation==LOAD:
            curves = preview.curve_editor
            tree = settings.curve_tree
            require(tree is None or tree.get('toon_viewport_curve_role')!=curves.ROLE
                    or curves.signature(tree,settings)==tree.get('last_signature'),'CONFLICT',
                    'Resolve pending curves before loading a new pair')
            document = read_document(Path(request['document']['file']))
            recipe = read_document(Path(request['recipe']['file']))
            try:
                compiled = preview.addon.compile_preset(document,recipe)
            except (TypeError,ValueError,KeyError) as exc:
                raise Failure('INVALID_REQUEST','Invalid Pencil document/recipe pair: '+str(exc)) from exc
            verify_bindings(scene,document,compiled,preview)
        for key in request.keys() & INPUTS.keys():
            setattr(settings,INPUTS[key],request[key]['file'])
        if operation==LOAD:
            try:
                outcome = bpy.ops.toon_viewport.load_style()
            except RuntimeError as exc:
                raise Failure('CONFLICT',str(exc)) from exc
            require(outcome=={'FINISHED'},'CONFLICT',settings.curve_error or 'Native Pencil did not load the pair')
            saved = preview.file_authoring.read_scene(scene)
            require(saved['document']==document and saved['recipe']==recipe and settings.style_mode=='PRESET',
                    'VALIDATION_FAILED','Loaded native author differs from the guarded raw pair')
        observed = read_state()
        for key, prop in INPUTS.items():
            expected = request[key]['file'] if key in request else before[prop]
            require(observed[prop]==expected,'VALIDATION_FAILED','The native staged path did not persist')
        after_author = author_sha(scene)
        require(operation!=STAGE or after_author==author,'VALIDATION_FAILED','Staging paths unexpectedly changed author content')
        require(protect(bpy,context,bindings,dependencies)==protected,'VALIDATION_FAILED',
                'The raw style operation changed protected scene data')
        retained = save_candidate(bpy,request)
        return {**response,'style_pair':observed,'state_sha256':fingerprint(observed),
                'author_before_sha256':author,'author_after_sha256':after_author,
                'candidate_sha256':digest(request['stage']),'retained_orphans':retained,
                'protected':{'status':'passed','scene_sha256':protected}}
