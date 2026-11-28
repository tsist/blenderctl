"""Inspect, edit or export the saved Toon Vector Output settings."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

sys.dont_write_bytecode = True
ENTRY = Path(__file__).resolve()
sys.path.insert(0, str(ENTRY.parent))
from filesystem import FileGuard, PathLocks
from material_draft_handoff import Parser, canonical, fingerprint, guard_file, open_saved_context, read_document, require
from material_projection_handoff import error_payload, run_worker
from protocol import CODES, DEFAULT_BLENDER, DEFAULT_JOBS, Failure, atomic_json, digest, envelope, read_json, timeout_value
from toon_panel_handoff import PRODUCTS, native_product
from toon_viewport_lines import _author_sha256, _bindings, _dependencies, _load_preview, _save_element_or_line_candidate

COMMAND = 'toon-workflow.vector-output'
OPTIONS = {'on', 'label', 'sub_path', 'file_type', 'grouping', 'visible_lines_on', 'hidden_lines_on', 'line_set_ids'}
ACTION_FIELDS = {'ADD':set(), 'SELECT':{'index'}, 'UPDATE':{'index', 'values'},
                 'REMOVE':{'index'}, 'UP':{'index'}, 'DOWN':{'index'}}
DIGEST = re.compile(r'[0-9a-f]{64}\Z')


def parser():
    cli = Parser(description=__doc__, allow_abbrev=False)
    cli.add_argument('--blender',type=Path,default=DEFAULT_BLENDER)
    cli.add_argument('--jobs-dir',type=Path,default=DEFAULT_JOBS)
    cli.add_argument('--transactions-dir',help=argparse.SUPPRESS)
    cli.add_argument('--timeout',type=float,default=180)
    display=cli.add_mutually_exclusive_group()
    for flag in ('--human','--compact','--full'):
        display.add_argument(flag,action='store_true')
    sub=cli.add_subparsers(required=True,parser_class=Parser).add_parser('vector-output',description=__doc__,
        epilog=('JSON operation inspect/edit/export; edition, file, expected_sha256, context {scene,view_layer,frame}, '
                'scope {kind VIEW_LAYER or SCENE_LEGACY; IMAGE_NODE also takes tree and node names}. '
                'Edit/export require expected_vector_sha256 from inspect. Edit uses actions '
                '[{action ADD/SELECT/UPDATE/REMOVE/UP/DOWN, index as needed, values for ADD/UPDATE}], '
                'optional base_path, and a new .blend --output. SELECT saves the active row by index. '
                'Export uses the recorded filters and a new --output directory. EPS and AIEPS only.'))
    sub.add_argument('--params',type=Path,required=True)
    sub.add_argument('--output',type=Path)
    return cli


def name(value):
    return isinstance(value,str) and 0 < len(value) <= 255 and all(ord(c)>=32 for c in value)


def request_for(args, guards):
    request=read_document(guard_file(args.params.absolute(),guards))
    fields={'operation','edition','file','expected_sha256','context','scope'}
    require(isinstance(request,dict) and fields <= request.keys(),'INVALID_REQUEST','Supply the saved source and vector scope')
    operation=request['operation']
    require(isinstance(operation,str) and operation in ('inspect','edit','export'),'INVALID_REQUEST','Unknown vector operation')
    extra={'expected_vector_sha256'} if operation!='inspect' else set()
    allowed=fields | extra | ({'actions','base_path'} if operation=='edit' else set())
    require(fields | extra <= request.keys() <= allowed,'INVALID_REQUEST','Missing or unknown vector request fields')
    if extra:
        expected=request['expected_vector_sha256']
        require(isinstance(expected,str) and DIGEST.fullmatch(expected),'INVALID_REQUEST','Expected inspected vector SHA-256')
    require(isinstance(request['edition'],str) and request['edition'] in PRODUCTS,'UNSUPPORTED','Unknown Toon release')
    context=request['context']
    require(isinstance(context,dict) and context.keys()=={'scene','view_layer','frame'} and name(context['scene'])
            and name(context['view_layer']) and type(context['frame']) is int,'INVALID_REQUEST','Expected saved scene, view layer and integer frame')
    scope=request['scope']
    require(isinstance(scope,dict) and scope.get('kind') in ('VIEW_LAYER','SCENE_LEGACY','IMAGE_NODE'),
            'INVALID_REQUEST','Expected a ViewLayer, legacy Scene or named local Image node scope')
    if scope['kind']=='IMAGE_NODE':
        require(scope.keys()=={'kind','tree','node'} and name(scope['tree']) and name(scope['node']),
                'INVALID_REQUEST','Image node scope requires its exact compositor tree and node names')
    else:
        require(scope.keys()=={'kind'},'INVALID_REQUEST','Only Image node scope accepts tree and node identities')
    require(isinstance(request['file'],str) and Path(request['file']).suffix.lower()=='.blend',
            'INVALID_REQUEST','Expected a hash-pinned .blend work file')
    request['file']=str(guard_file(request['file'],guards,request['expected_sha256']))
    if operation=='edit':
        actions=request.get('actions',[])
        require(isinstance(actions,list) and len(actions)<=64 and (actions or 'base_path' in request),
                'INVALID_REQUEST','Provide up to 64 list edits or an existing base path')
        for row in actions:
            require(isinstance(row,dict) and isinstance(row.get('action'),str) and row['action'] in ACTION_FIELDS,
                    'INVALID_REQUEST','Invalid vector list action')
            required={'action'} | ACTION_FIELDS[row['action']]
            allowed_row=required | ({'values'} if row['action']=='ADD' else set())
            require(required <= row.keys() <= allowed_row,'INVALID_REQUEST','Unexpected fields for vector '+row['action'])
            if 'index' in row:
                require(type(row['index']) is int and row['index']>=0,'INVALID_REQUEST','List indices are zero based')
            if 'values' in row:
                require(isinstance(row['values'],dict) and row['values'],'INVALID_REQUEST','Expected a sparse vector settings patch')
        if 'base_path' in request:
            base=canonical(request['base_path'])
            require(base.is_dir(),'INVALID_REQUEST','Base path must be an existing directory')
            guards.enter_context(FileGuard(base,directory=True))
            request['base_path']=str(base)
    required_output=operation!='inspect'
    require(required_output==(args.output is not None) and (not required_output or args.output.is_absolute()),
            'INVALID_REQUEST','Edits and exports require a new absolute --output. Inspect needs none.')
    if operation=='edit':
        require(args.output.suffix.lower()=='.blend','INVALID_REQUEST','List edit requires a new .blend work copy')
    request['product']=native_product(request,guards)
    bundle=Path(request['product']['directory'])
    files=read_json(bundle/'bundle.json')['files']
    backend={path:sha for path,sha in files.items() if path.startswith('toon_viewport_preview/') and path.endswith('.py')}
    require({'toon_viewport_preview/vector_ui.py','toon_viewport_preview/vector_output.py','toon_viewport_preview/vector_scope.py'}
            <= backend.keys(),'CONFLICT','Missing frozen vector modules')
    for rel,sha in backend.items():
        guard=guards.enter_context(FileGuard(bundle/rel))
        require(guard.sha256()==sha,'CONFLICT','Frozen viewport module changed: '+rel)
    request['product']['vector_backend_files_verified']=len(backend)
    return request


def target(context, scope, native, *, mutating=False):
    kind=scope['kind']
    scene=context.scene
    layer=context.view_layer if kind!='SCENE_LEGACY' else None
    node_pointer=''
    if kind=='IMAGE_NODE':
        if hasattr(native,'local_tree'):
            local=(native.local_tree(scene),)
        else:
            from toon_workflow.toon_viewport_preview.compositor_scope import editable_trees
            local=editable_trees(scene)
        trees=[tree for tree in local if tree.name==scope['tree']]
        require(len(trees)==1,'NOT_FOUND','Missing editable local compositor tree')
        node=trees[0].nodes.get(scope['node'])
        require(node is not None and node.bl_idname=='CompositorNodeImage','NOT_FOUND','Missing named local compositor Image node')
        node_pointer=native.node_token(scene,node) if mutating else native.node_address(scene,node)
    state,layer,info=native.resolve(scene,view_layer=layer,node_ptr=node_pointer)
    return state, layer, node_pointer, {**info, **scope}


def saved(state, vector):
    return dict(base_path=state.vector_output_base_path,selected_index=state.vector_output_selected_index,
                outputs=vector.outputs_from_state(state))


def protected(bpy, selected_scene, selected_state, vector, scope):
    from inspection import key, record
    from toon_workflow.toon_workflow_addon import joint_workflow
    witnesses=[]
    for scene in sorted(bpy.data.scenes,key=lambda s:s.name):
        if scene.library:
            continue
        other=[(info,saved(state,vector)) for state,info in scope.states(scene)
               if scene!=selected_scene or state.as_pointer()!=selected_state.as_pointer()]
        witnesses.append(dict(scene=scene.name, vector_states=sorted(other,key=key),
            author=_author_sha256(scene),geometry=joint_workflow.non_author_signature(scene),
            camera=scene.camera.name if scene.camera else None, frame=scene.frame_current,
            view_layers=[layer.name for layer in scene.view_layers]))
    return fingerprint([witnesses,_bindings(bpy),_dependencies(bpy),sorted((record(a) for a in bpy.data.actions),key=key)])


def apply_values(item, incoming, vector):
    categories={category+'_on' for category in vector.render_elements.CATEGORIES}
    require(incoming.keys() <= OPTIONS | categories,'INVALID_REQUEST','Unknown native vector settings')
    if 'file_type' in incoming:
        require(incoming['file_type'] in ('EPS','AIEPS'),'UNSUPPORTED','Frozen Toon vector output supports EPS and AIEPS')
    validated=vector.normalize_outputs([incoming])[0]
    # The native file-type callback may normalize the old subpath.
    for key in ('file_type', *sorted(incoming.keys()-{'file_type','sub_path'}), 'sub_path'):
        if key in incoming:
            setattr(item,key,validated[key])
    return {key:validated[key] for key in incoming}


def edit_list(bpy, context, state, node_address, request, vector):
    legacy=request['scope']['kind']=='SCENE_LEGACY'
    receipts=[]
    for row in request.get('actions',[]):
        action=row['action']
        before=vector.outputs_from_state(state)
        old_len=len(before)
        index=row.get('index',old_len)
        requested={}
        if action=='ADD':
            require(old_len<vector.MAX_OUTPUTS,'CONFLICT','Vector output list is full')
            if legacy:
                state.vector_outputs.add().label='Output '+str(old_len+1)
                state.vector_output_selected_index=index
            else:
                require(bpy.ops.toon_vector.new_vector_output(node_ptr=node_address)=={'FINISHED'},'WORKER_FAILED','Native Add failed')
            requested=apply_values(state.vector_outputs[index],row.get('values',{}),vector)
            changed_index=index
        else:
            require(index<old_len,'NOT_FOUND','Vector row index does not exist in the inspected saved copy')
            if action=='UPDATE':
                requested=apply_values(state.vector_outputs[index],row['values'],vector)
                changed_index=index
            elif action=='SELECT':
                state.vector_output_selected_index=index
                changed_index=index
            else:
                state.vector_output_selected_index=index
                if action=='REMOVE':
                    if legacy:
                        state.vector_outputs.remove(index)
                        state.vector_output_selected_index=min(index,old_len-2)
                    else:
                        require(bpy.ops.toon_vector.remove_vector_output(node_ptr=node_address)=={'FINISHED'},'WORKER_FAILED','Native Remove failed')
                    changed_index=None
                else:
                    neighbor=index+(-1 if action=='UP' else 1)
                    require(0<=neighbor<old_len,'CONFLICT','Vector row is already at the requested list boundary')
                    if legacy:
                        state.vector_outputs.move(max(index,neighbor),min(index,neighbor))
                        state.vector_output_selected_index=neighbor
                    else:
                        require(bpy.ops.toon_vector.vector_outputs_list_move_item(node_ptr=node_address,button_type=action)
                                =={'FINISHED'},'WORKER_FAILED','Native Move failed')
                    changed_index=neighbor
        after=vector.outputs_from_state(state)
        before_plain=[{k:v for k,v in output.items() if k!='index'} for output in before]
        after_plain=[{k:v for k,v in output.items() if k!='index'} for output in after]
        if action=='ADD':
            preserved=after_plain[:-1]==before_plain and len(after_plain)==old_len+1
        elif action=='REMOVE':
            preserved=after_plain==before_plain[:index]+before_plain[index+1:]
        elif action=='UPDATE':
            preserved=after_plain[:index]+after_plain[index+1:]==before_plain[:index]+before_plain[index+1:]
        elif action=='SELECT':
            preserved=after==before and state.vector_output_selected_index==index
        else:
            before_plain[index],before_plain[changed_index]=before_plain[changed_index],before_plain[index]
            preserved=after_plain==before_plain
        require(preserved,'VALIDATION_FAILED','List action changed an untargeted vector row')
        if requested:
            require(all(after[changed_index][key]==value for key,value in requested.items()),'VALIDATION_FAILED',
                    'Native vector values did not read back as requested')
        receipts.append(dict(action=action,input_index=None if action=='ADD' else index,result_index=changed_index,
                             result=None if changed_index is None else after[changed_index]))
    if 'base_path' in request:
        state.vector_output_base_path=request['base_path']
    return receipts


def outputs_media(report, stage):
    files=[]
    require(report['ok'] and report['complete'] and report['files'],'UNSUPPORTED',report.get('error','Vector output was blocked'))
    for row in report['files']:
        path=Path(row['path'])
        require(path.resolve()==path and path.is_relative_to(stage) and path.suffix.lower()=='.eps',
                'VALIDATION_FAILED','Native writer returned an EPS outside the declared output directory')
        body=path.read_bytes()
        require(body.startswith(b'%!PS-Adobe-3.0 EPSF-3.0\n') and body.endswith(b'%%EOF\n')
                and len(body)==row['bytes'] and digest(path)==row['sha256'],'VALIDATION_FAILED','EPS payload unreadable or differs from native report')
        require(b'%%ToonVectorFileType: '+row['file_type'].encode('ascii')+b'\n' in body
                and b'%%ToonVectorFrame: '+str(report['frame']).encode('ascii')+b'\n' in body,
                'VALIDATION_FAILED','EPS media header and native report disagree')
        is_ai=row['file_type']=='AIEPS'
        require((b'%%AI8_CreatorVersion: 8.0\n' in body)==is_ai,'VALIDATION_FAILED','EPS/AIEPS markers disagree')
        polygon_count=body.count(b'\nclosepath\n')
        require(polygon_count==row['visible_polygons']+row['hidden_polygons'],'VALIDATION_FAILED','EPS polygons differ from filter output count')
        files.append(dict(file=str(path),relative=path.relative_to(stage).as_posix(),sha256=row['sha256'],
                          bytes=row['bytes'],file_type=row['file_type'],polygons=polygon_count))
    require(set(p for p in stage.rglob('*') if p.is_file())=={Path(row['file']) for row in files},
            'VALIDATION_FAILED','Vector stage contains undeclared output files')
    return files


def work(request):
    import bpy
    require(bpy.app.background and not bpy.app.online_access,'UNSUPPORTED','Offline Blender worker required')
    _load_preview(bpy,request['product'],request['edition'])
    from toon_workflow.toon_viewport_preview import vector_output as vector, vector_scope as scope
    context=open_saved_context(bpy,request)
    kind=request['operation']
    state, layer, node, identity=target(context,request['scope'],scope)
    before=saved(state,vector)
    before_sha=fingerprint(before)
    if kind!='inspect':
        require(before_sha==request['expected_vector_sha256'],'CONFLICT','Vector settings changed since inspection')
    frozen=protected(bpy,context.scene,state,vector,scope)
    data=dict(operation=kind,scope=identity,context=request['context'],product=request['product'],
              source=dict(file=request['file'],sha256=request['expected_sha256']),
              runtime=dict(blender=bpy.app.version_string,build=bpy.app.build_hash.decode(),pid=os.getpid()))
    if kind=='edit':
        selection_only = ('base_path' not in request
                          and all(row['action']=='SELECT' for row in request.get('actions', [])))
        if not selection_only:
            state,layer,node,identity=target(context,request['scope'],scope,mutating=True)
            data['scope']=identity
        data['actions']=edit_list(bpy,context,state,node,request,vector)
        after=saved(state,vector)
        require(selection_only or after!=before,'CONFLICT','Vector list already matches the requested values')
        require(protected(bpy,context.scene,state,vector,scope)==frozen,'VALIDATION_FAILED','Edit changed unrelated author, geometry or vector scopes')
        data['retained_orphans']=_save_element_or_line_candidate(bpy,request)
        data['stage_sha256']=digest(request['stage'])
    elif kind=='export':
        stage=Path(request['stage'])
        report=vector.request_vector_output(stage,context,view_layer=layer,node_ptr=node)
        data.update(media=outputs_media(report,stage),native_report=report)
        require(saved(state,vector)==before and protected(bpy,context.scene,state,vector,scope)==frozen,
                'VALIDATION_FAILED','Vector export changed saved controls or unrelated author data')
    data['list']=saved(state,vector)
    data['vector_sha256']=fingerprint(data['list'])
    data['protected_sha256']=protected(bpy,context.scene,state,vector,scope)
    data['calculated_paths']=[item.output_path for item in state.vector_outputs]
    require(digest(request['file'])==request['expected_sha256'],'CONFLICT','Source .blend changed during vector operation')
    return data


def worker(phase, path):
    try:
        data=work(read_json(path))
        result={'ok':True,'data':data}
    except Exception as exc:
        result={'ok':False,'error':error_payload(exc,'VALIDATION_FAILED')}
    atomic_json(path.parent/(phase+'-result.json'),result)


def run(args):
    job=args.jobs_dir.resolve()/uuid.uuid4().hex
    job.mkdir(parents=True)
    deadline=time.monotonic()+timeout_value(args.timeout)
    error=data=None
    artifacts={'job_directory':str(job),'result':str(job/'result.json')}

    def check():
        require(not (job/'cancel.request').exists(),'CANCELLED','Vector output cancelled before publication')
        require(time.monotonic()<deadline,'TIMEOUT','Vector output exceeded its timeout')

    def status(state,phase):
        atomic_json(job/'status.json',dict(job_id=job.name,state=state,phase=phase,pid=os.getpid(),heartbeat_at=time.time()))

    def execute(request,phase):
        status('running',phase)
        atomic_json(job/(phase+'-request.json'),request)
        atomic_json(job/'request.json',request)
        return run_worker(blender,job,phase,deadline,check,status,entry=ENTRY,private_temp=True)

    status('running','inputs')
    try:
        with ExitStack() as guards:
            request=request_for(args,guards)
            blender=canonical(args.blender)
            operation=request['operation']
            if operation=='inspect':
                data=execute(request,operation)
            else:
                parent=canonical(args.output.parent)
                for folder in reversed((parent,*parent.parents)):
                    if folder!=Path(folder.anchor):
                        guards.enter_context(FileGuard(folder,directory=True))
                output=parent/args.output.name
                stage=parent/('.toon-vector-'+job.name+('.blend' if operation=='edit' else ''))
                guards.enter_context(PathLocks([output,stage]))
                require(not os.path.lexists(output),'CONFLICT','Output already exists')
                if operation=='edit':
                    with stage.open('xb'):
                        pass
                    guards.callback(stage.unlink,missing_ok=True)
                else:
                    stage.mkdir()
                    artifacts['unpublished_stage']=str(stage)
                data=execute({**request,'stage':str(stage)},operation)
                check()
                if operation=='edit':
                    stage_sha=data['stage_sha256']
                    with FileGuard(stage) as hold:
                        require(hold.sha256(check)==stage_sha,'CONFLICT','Saved vector candidate SHA differs')
                        cold=execute({**request,'operation':'inspect','file':str(stage),'expected_sha256':stage_sha},'reopen')
                    require(cold['runtime']['pid']!=data['runtime']['pid'] and cold['vector_sha256']==data['vector_sha256']
                            and cold['protected_sha256']==data['protected_sha256'],'VALIDATION_FAILED','Cold reopen found different vector or protected settings')
                    with FileGuard(stage,renameable=True) as hold:
                        require(hold.sha256(check)==stage_sha,'CONFLICT','Verified vector candidate changed')
                        hold.rename_new(output)
                    data.update(candidate=dict(file=str(output),sha256=stage_sha),independent_reopen=cold['runtime'])
                else:
                    outputs_media(data['native_report'],stage)
                    require(not os.path.lexists(output),'CONFLICT','Output was created during vector export')
                    stage.rename(output)
                    artifacts.pop('unpublished_stage')
                    native=data['native_report']
                    native['base_path']=str(output)
                    for row, media in zip(native['files'],data['media'],strict=True):
                        file=str(output/media['relative'])
                        row['path']=media['file']=file
                    data['output_directory']=str(output)
                    atomic_json(job/'vector-report.json',native)
                    artifacts['vector_report']=str(job/'vector-report.json')
    except Exception as exc:
        data=None
        error=error_payload(exc,'VALIDATION_FAILED')
    result=envelope(COMMAND,job.name,data,error,artifacts)
    atomic_json(job/'result.json',result)
    status('completed' if result['ok'] else 'failed','published' if result['ok'] else 'failed')
    return result


def main():
    try:
        result=run(parser().parse_args())
    except Failure as exc:
        result=envelope(COMMAND,error=error_payload(exc,'INVALID_REQUEST'))
    sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))
    return 0 if result['ok'] else CODES.get(result['error']['code'],8)


if __name__=='__main__':
    if '--background' in sys.argv and '--' in sys.argv:
        phase,path=sys.argv[sys.argv.index('--')+1:]
        worker(phase,Path(path))
    else:
        raise SystemExit(main())
