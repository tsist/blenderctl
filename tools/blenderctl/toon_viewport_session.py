"""Hand off line output from one explicitly attached Blender View3D."""

import argparse
from contextlib import ExitStack
import importlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

sys.dont_write_bytecode = True
from filesystem import FileGuard, PathLocks
from geometry_session import admission_lock, read_result
from material_draft_handoff import Parser, canonical, fingerprint, guard_file, require
from protocol import CODES, DEFAULT_BLENDER, Failure, atomic_json, digest, envelope, read_json
from resource_leases import process_identity
from toon_panel_handoff import PRODUCTS, native_product
import toon_viewport_brush_session as brush_session
import toon_viewport_shadow_session as shadow_session


COMMAND = 'toon-workflow.viewport-session'
SCHEMA = 'toon-viewport-session/1'
OWNER = None
KINDS = {'VIEW', 'CAMERA', 'ELEMENTS', 'SEQUENCE'}
PARAMS = {
    'status': set(),
    'submit': {'view_id','expected_context','kind','output'},
    'job': {'view_id','kind','job_id'},
    'cancel': {'view_id','kind','job_id'},
    'local-visibility': {'view_id','expected_context','enabled'},
    **brush_session.PARAMS,
    **shadow_session.PARAMS,
}


def uuid_value(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def pointer(rna):
    return f'{rna.as_pointer():x}'


def provider(bpy, edition):
    op = bpy.ops.toon_viewport.export_png
    try:
        operator = bpy.types.Operator.bl_rna_get_subclass_py(op.get_rna_type().identifier)
    except KeyError as exc:
        raise Failure('UNSUPPORTED', 'Enable the matching Toon Workflow add-on') from exc
    require(operator is not None, 'UNSUPPORTED', 'Enable the matching Toon Workflow add-on')
    native = importlib.import_module(operator.__module__)
    folder = Path(native.__file__).resolve().parent
    require(folder.name == 'toon_viewport_preview' and folder.parent.joinpath('bundle.json').is_file(),
            'UNSUPPORTED', 'Expected the bundled Pencil provider')
    with ExitStack() as guards:
        product = native_product({'edition': edition}, guards)
        frozen = Path(product['directory'])
        manifest = read_json(frozen/'bundle.json')
        require(digest(folder.parent/'bundle.json') == product['manifest_sha256']
                and tuple(bpy.app.version) == tuple(product['host']),
                'UNSUPPORTED', 'Enabled bundle or Blender build does not match the requested Toon edition')
        files = {'addon.py', 'render_output.py', 'sequence_output.py', 'render_worker.py', 'viewport_scope.py',
                 'brush_ui.py','brush_effects.py','curve_editor.py'}
        require(all(digest(folder/name) == manifest['files']['toon_viewport_preview/'+name] for name in files),
                'UNSUPPORTED', 'Enabled Pencil queue implementation differs from the frozen product')
        return native, product


def attach(directory, edition):
    """Called deliberately in the target Blender window, never from a background worker."""
    import bpy

    global OWNER
    require(not bpy.app.background and bpy.context.window is not None and bpy.data.filepath,
            'UNSUPPORTED', 'Attach inside the chosen GUI window with a named source .blend')
    require(OWNER is None and not any(getattr(fn, '_toon_viewport_session', False)
                                      for fn in bpy.app.handlers.load_pre),
            'CONFLICT', 'A Toon output receiver already owns this process')
    native, product = provider(bpy, edition)
    root = Path(directory)
    require(root.is_absolute() and canonical(root.parent) == root.parent and not os.path.lexists(root),
            'CONFLICT', 'Choose a new absolute reception directory')
    root.mkdir()
    (root/'requests').mkdir()
    window = bpy.context.window
    info = dict(schema=SCHEMA, directory=str(root), session_id=str(uuid.uuid4()),
                process=process_identity(os.getpid()), blender=str(canonical(bpy.app.binary_path)),
                window_id=pointer(window), file=bpy.data.filepath, edition=edition, build=bpy.app.build_hash.decode(),
                version=list(bpy.app.version), manifest_sha256=product['manifest_sha256'])
    atomic_json(root/'session.json', info)
    OWNER = dict(root=root, info=info, window=window, screen=window.screen, scene=window.scene, layer=window.view_layer,
                 native=native, replies={}, jobs={})
    bpy.app.handlers.load_pre.append(close_on_load)
    bpy.app.timers.register(pump, first_interval=.1)
    return info


def views(bpy, owner):
    window = owner['window']
    require(any(item == window for item in bpy.context.window_manager.windows)
            and window.screen == owner['screen'] and window.scene == owner['scene']
            and window.view_layer == owner['layer'] and bpy.data.filepath == owner['info']['file'],
            'CONFLICT', 'The attached window, workspace, scene, view layer or file changed')
    return {pointer(area): (area, region) for area in window.screen.areas if area.type == 'VIEW_3D'
            and not area.spaces.active.region_quadviews
            for region in area.regions if region.type == 'WINDOW'}


def view_key(owner, area, region):
    return tuple(item.as_pointer() for item in (owner['window'], area, region, owner['scene'], owner['layer']))


def visible_jobs(owner, kind, area, region):
    native = owner['native']
    key = view_key(owner, area, region)
    if kind in ('VIEW', 'CAMERA'):
        return [row for row in native.output_jobs() if row.get('mode') == kind and tuple(row.get('key',())) == key]
    if kind == 'ELEMENTS':
        jobs = [row for row in native.render_output.jobs() if 'element_config' in row]
    else:
        jobs = native.sequence_output.jobs()
    return [row for row in jobs if tuple(row['scene_key']) == key[-2:]]


def job_view(kind, job):
    keys = ('id','status','stage','message','mode','path','frames','published_frames','receipt_path','result',
            'cancel_requested','scene','view_layer','snapshot_sha256','submission_frame','output_extent')
    result = {key:job[key] for key in keys if key in job}
    result['kind'] = kind
    if job['status'] == 'COMPLETE' and 'result' in job:
        native = job['result']
        with ExitStack() as guards:
            file = guard_file(native['path'], guards, native['sha256'])
            result['artifact'] = {'file':str(file),'sha256':native['sha256']}
    if job['status'] != 'QUEUED' and job.get('published_frames'):
        with ExitStack() as guards:
            for row in job['published_frames']:
                guard_file(row['path'], guards, row['sha256'])
    return result


def observe(bpy, owner, area, region):
    scene = owner['scene']
    space = area.spaces.active
    settings = scene.toon_viewport_preview
    camera = scene.camera
    core = dict(file=bpy.data.filepath, scene=scene.name, view_layer=owner['layer'].name,
                window_id=owner['info']['window_id'], view_id=pointer(area), region_id=pointer(region),
                dirty=bool(bpy.data.is_dirty), frame=scene.frame_current, subframe=scene.frame_subframe,
                enabled=settings.enabled, style=settings.style_mode, display_mode=settings.display_mode,
                local_enabled=owner['native'].viewport_scope.enabled(owner['screen'], area, space),
                view_type=space.region_3d.view_perspective, view_extent=[region.width,region.height],
                projection=[list(row) for row in space.region_3d.perspective_matrix],
                camera=camera.name if camera else None, camera_matrix=[list(row) for row in camera.matrix_world] if camera else None,
                render_extent=[scene.render.resolution_x,scene.render.resolution_y,scene.render.resolution_percentage])
    return {**core, 'expected_context':fingerprint(core)}


def status(bpy, owner):
    rows = {}
    for key,(area,region) in views(bpy, owner).items():
        rows[key] = observe(bpy, owner, area, region)
        rows[key]['jobs'] = [dict(id=row['id'],status=row['status'],kind=kind)
                            for kind in sorted(KINDS) for row in visible_jobs(owner, kind, area, region)]
    return {**owner['info'], 'views':rows}


def path_plan(owner, kind, output):
    path = Path(output)
    suffixes = {'.exr'} if kind == 'ELEMENTS' else {'.png','.exr'} if kind == 'SEQUENCE' else {'.png'}
    require(path.is_absolute() and path.suffix.lower() in suffixes and canonical(path.parent) == path.parent,
            'INVALID_REQUEST', 'Choose a new absolute PNG/EXR of the correct kind in an existing folder')
    native = owner['native']
    frames = None
    if kind == 'SEQUENCE':
        settings = owner['scene'].toon_viewport_preview
        frames = native._sequence_frames(owner['scene'], settings)
        require(0 < len(frames) <= 96, 'INVALID_REQUEST', 'Use a sequence range of 1..96 frames')
        suffix = '.exr' if settings.sequence_elements else path.suffix.lower()
        names = [path.parent/native.sequence_output.protocol.frame_name(path.stem,frame,suffix) for frame in frames]
    else:
        names = [path]
    return path, names, frames


def submit(bpy, owner, params, area, region):
    native = owner['native']
    kind = params['kind']
    observed = observe(bpy, owner, area, region)
    require(observed['expected_context'] == params['expected_context'],
            'CONFLICT', 'Attached viewport changed after inspection')
    path, outputs, frames = path_plan(owner, kind, params['output'])
    with ExitStack() as guards:
        for parent in reversed(path.parents):
            if parent != Path(parent.anchor):
                guards.enter_context(FileGuard(parent, directory=True))
        guards.enter_context(PathLocks(outputs))
        require(all(not os.path.lexists(item) for item in outputs), 'CONFLICT', 'The planned output already exists')
        functions = {'VIEW':native.request_output, 'CAMERA':native.request_camera_output,
                     'ELEMENTS':native.request_render_elements, 'SEQUENCE':native.request_sequence_output}
        with bpy.context.temp_override(window=owner['window'],area=area,region=region,
                                       scene=owner['scene'],view_layer=owner['layer']):
            response = functions[kind](path, bpy.context, **({'frames':frames} if frames is not None else {}))
        require(response.get('id'), 'WORKER_FAILED', 'Native Pencil did not admit the requested output')
        owner['jobs'][response['id']] = guards.pop_all()
    return dict(job_id=response['id'], kind=kind, outputs=list(map(str,outputs)),
                context=observed, status=response['status'])


def execute(bpy, owner, operation, params):
    if operation == 'status':
        return status(bpy, owner)
    match = views(bpy, owner).get(params['view_id'])
    require(match is not None, 'CONFLICT', 'Choose a View3D ID from this attached window')
    area, region = match
    if operation in shadow_session.PARAMS:
        with bpy.context.temp_override(window=owner['window'],area=area,region=region,
                                       scene=owner['scene'],view_layer=owner['layer']):
            return shadow_session.execute(bpy, owner, operation, params)
    if operation in brush_session.PARAMS:
        viewport = observe(bpy,owner,area,region)
        with bpy.context.temp_override(window=owner['window'],area=area,region=region,
                                       scene=owner['scene'],view_layer=owner['layer']):
            return brush_session.apply(bpy,owner,viewport,operation,params)
    if operation == 'local-visibility':
        before = observe(bpy, owner, area, region)
        require(params['expected_context'] == before['expected_context'], 'CONFLICT', 'Attached viewport changed after inspection')
        if before['local_enabled'] != params['enabled']:
            try:
                owner['native'].viewport_scope.set_enabled(owner['screen'], area, area.spaces.active, params['enabled'])
            except ValueError as exc:
                raise Failure('CONFLICT', str(exc)) from exc
        after = observe(bpy, owner, area, region)
        require(after['local_enabled'] is params['enabled'], 'VALIDATION_FAILED', 'Selected viewport did not read back its visibility')
        return after
    if operation == 'submit':
        return submit(bpy, owner, params, area, region)
    native = owner['native']
    kind, job_id = params['kind'], params['job_id']
    found = next((row for row in visible_jobs(owner, kind, area, region) if row['id'] == job_id), None)
    require(found is not None, 'NOT_FOUND', 'No matching job in the specified view or saved scene/view layer')
    if operation == 'job':
        return job_view(kind, found)
    require(found['status'] == 'QUEUED' and not found.get('cancel_requested'), 'CONFLICT',
            'Only an identified queued job can be cancelled')
    with bpy.context.temp_override(window=owner['window'],area=area,region=region,
                                   scene=owner['scene'],view_layer=owner['layer']):
        if kind in ('VIEW','CAMERA'):
            accepted = native.cancel_output(job_id, owner['scene'])
        elif kind == 'ELEMENTS':
            accepted = bpy.ops.toon_viewport.cancel_output(job_id=job_id) == {'FINISHED'}
        else:
            accepted = bpy.ops.toon_viewport.cancel_sequence(job_id=job_id) == {'FINISHED'}
    require(accepted, 'CONFLICT', 'Native Pencil refused the identified cancellation')
    return dict(job_view(kind, next(row for row in visible_jobs(owner,kind,area,region) if row['id']==job_id)),
                cancel_accepted=True)


def close_on_load(*_):
    import bpy

    global OWNER
    owner, OWNER = OWNER, None
    if owner is None:
        return
    try:
        for guards in owner['jobs'].values():
            guards.close()
        closed = owner['root']/'closed.json'
        with admission_lock(closed):
            for path in (owner['root']/'requests').glob('*/request.json'):
                ident = path.parent.name
                result = path.with_name('result.json')
                if uuid_value(ident) and path.resolve() == path and not result.exists():
                    atomic_json(result, owner['replies'].get(ident) or envelope(COMMAND, ident,
                        error={'code':'CONFLICT','message':'The attached Blender document changed'}))
            atomic_json(closed, {'session_id':owner['info']['session_id']})
    finally:
        bpy.app.handlers.load_pre.remove(close_on_load)


close_on_load._toon_viewport_session = True


def pump():
    import bpy

    owner = OWNER
    if owner is None:
        return None
    native = owner['native']
    rows = native.output_jobs() + native.sequence_output.jobs()
    pending = {row['id'] for row in rows if row['status']=='QUEUED'}
    for ident in list(owner['jobs']):
        if ident not in pending:
            owner['jobs'].pop(ident).close()
    for path in sorted((owner['root']/'requests').glob('*/request.json'), key=lambda row:row.stat().st_mtime_ns):
        ident = path.parent.name
        target = path.with_name('result.json')
        if target.exists() or not uuid_value(ident) or path.resolve() != path:
            continue
        if ident not in owner['replies']:
            try:
                request = read_json(path)
                require(request['session_id'] == owner['info']['session_id'] and request['request_id'] == ident,
                        'CONFLICT', 'Request identity differs from this attachment')
                operation = request['operation']
                data = execute(bpy, owner, operation, normalize(operation, request['params']))
                reply = envelope(COMMAND, ident, data=data)
            except Exception as exc:
                reply = envelope(COMMAND, ident, error={'code':getattr(exc,'code','WORKER_FAILED'),'message':str(exc)})
            owner['replies'][ident] = reply
        try:
            atomic_json(target, owner['replies'][ident])
        except OSError:
            return .1
        del owner['replies'][ident]
        break
    return .1


def normalize(operation, params):
    require(isinstance(params, dict) and set(params) == PARAMS[operation], 'INVALID_REQUEST', 'Expected exact operation parameters')
    formats = {'view_id':'[0-9a-f]+', 'expected_context':'[0-9a-f]{64}', 'job_id':'[0-9a-f]{32}'}
    for key, pattern in formats.items():
        if key in params:
            require(isinstance(params[key], str) and re.fullmatch(pattern,params[key]), 'INVALID_REQUEST', 'Invalid '+key)
    require('kind' not in params or isinstance(params['kind'],str) and params['kind'] in KINDS,
            'INVALID_REQUEST','Use one of VIEW, CAMERA, ELEMENTS and SEQUENCE')
    require('output' not in params or isinstance(params['output'],str) and Path(params['output']).is_absolute(),
            'INVALID_REQUEST','Expected an absolute output path')
    for key in {'actual','collection','curve_identity','group','slot'} & params.keys():
        require(isinstance(params[key],str) and 0 < len(params[key]) <= 255,'INVALID_REQUEST','Expected exact '+key)
    require('editor_id' not in params or uuid_value(params['editor_id']),
            'INVALID_REQUEST','Expected brush dialog UUID')
    require('index' not in params or type(params['index']) is int and -1 <= params['index'] <= 4095,
            'INVALID_REQUEST','Use index -1 to add a point or a known interior index to remove')
    require('enabled' not in params or type(params['enabled']) is bool, 'INVALID_REQUEST', 'Expected explicit visibility boolean')
    if operation in shadow_session.PARAMS:
        shadow_session.validate(operation, params)
    return params


def request_result(args):
    operation = args.operation
    recover = operation == 'result'
    ident = args.request_id or str(uuid.uuid4())
    require(uuid_value(args.session_id) and uuid_value(ident) and (args.request_id or operation == 'status'),
            'INVALID_REQUEST', 'Use the attachment UUID and a stable request UUID')
    require(0 <= args.wait_seconds <= 55 and (args.params is not None) == (operation not in ('status','result')),
            'INVALID_REQUEST', 'Use --params for operations and a wait of 0..55 seconds')
    root = canonical(args.session_dir)
    queue = root/'requests'
    request_dir = queue/ident
    with ExitStack() as held:
        params = normalize(operation,read_json(guard_file(args.params.absolute(),held))) if args.params else {}
        for parent in reversed((queue,*queue.parents)):
            if parent != Path(parent.anchor):
                held.enter_context(FileGuard(parent,directory=True))
        info = read_json(guard_file(root/'session.json',held))
        require(info['schema'] == SCHEMA and info['session_id'] == args.session_id and Path(info['directory']) == root
                and canonical(info['blender']) == canonical(args.blender), 'CONFLICT', 'Attachment or Blender host differs')
        closed = root/'closed.json'
        if not recover:
            body = dict(session_id=args.session_id, request_id=ident, operation=operation, params=params)
            with admission_lock(closed), PathLocks([request_dir]):
                if request_dir.exists():
                    require(canonical(request_dir) == request_dir and read_json(request_dir/'request.json') == body,
                            'CONFLICT','Request UUID already belongs to another input')
                else:
                    require(not closed.exists() and process_identity(info['process']['pid']) == info['process'],
                            'CONFLICT','The attachment is closed')
                    request_dir.mkdir()
                    atomic_json(request_dir/'request.json', body)
        require(request_dir.is_dir() and canonical(request_dir) == request_dir,'NOT_FOUND','Unknown request UUID')
        result = request_dir/'result.json'
        deadline = time.monotonic()+args.wait_seconds
        while True:
            reply, waiting = read_result(result)
            if reply is not None:
                return reply
            if waiting is None and (closed.exists() or process_identity(info['process']['pid']) != info['process']):
                reply, waiting = read_result(result)
                if reply is not None:
                    return reply
                if waiting is None:
                    raise Failure('CONFLICT','The attachment ended without a receipt')
            if time.monotonic() >= deadline:
                return envelope(COMMAND, ident, data={'status':'pending','request_id':ident,'receipt':str(result)})
            time.sleep(.08)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--blender',type=Path,default=DEFAULT_BLENDER)
    for flag in ('--jobs-dir','--transactions-dir','--timeout'):
        parser.add_argument(flag,help=argparse.SUPPRESS)
    for flag in ('--human','--compact','--full'):
        parser.add_argument(flag,action='store_true',help=argparse.SUPPRESS)
    command = parser.add_subparsers(required=True,parser_class=Parser).add_parser('viewport-session',description=__doc__,
        epilog='Attach in the target Blender. Status supplies view_id and expected_context. '
               'Submit takes {view_id,expected_context,kind,output}; job/cancel take {view_id,kind,job_id}. '
               'local-visibility takes {view_id,expected_context,enabled} from status. '
               'brush-status reads the current author/draft and editor identities. '
               'brush-open-author and brush-open-dialog take actual and the brush status context. '
               'brush-commit, brush-point, brush-configure, curve-discard and curve-restore require its expected_context. '
               'brush-configure stages image/scene object IDs, seam fields or point coordinates in the active dialog. '
               'curve-select loads an original group/slot. curve-configure applies exact switches and CurveMapping points. '
               'Toon 0.52.1 on Blender 5.3.0: shadow-status takes {view_id}. '
               'select-shadow-control takes {view_id,expected_context,control,object_id} from shadow-status. '
               'Use the original request UUID with result to recover its immutable receipt.')
    command.add_argument('--session-dir',type=Path,required=True)
    command.add_argument('--session-id',required=True)
    command.add_argument('--operation',choices=(*PARAMS,'result'),required=True)
    command.add_argument('--request-id')
    command.add_argument('--params',type=Path)
    command.add_argument('--wait-seconds',type=float,default=12.)
    try:
        result = request_result(parser.parse_args())
    except (Failure, OSError, ValueError, KeyError, TypeError) as exc:
        result = envelope(COMMAND,error={'code':getattr(exc,'code','INVALID_REQUEST'),'message':str(exc)})
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))
    return 0 if result['ok'] else CODES.get(result['error']['code'],8)


if __name__ == '__main__':
    raise SystemExit(main())
