"""Use the native Animation Curves recorder attached to an exact VIEW_3D region."""

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
ENTRY = Path(__file__).resolve()
sys.path.insert(0, str(ENTRY.parent))
sys.path.append(str(ENTRY.parents[1]))

from animation_support_handoff.editions import package
from host_compat import is_supported_host
from filesystem import FileGuard, PathLocks
from material_draft_handoff import Parser, canonical, fingerprint, require
from material_live_session import valid_id
from protocol import CODES, DEFAULT_BLENDER, Failure, atomic_json, digest, envelope, read_json

COMMAND = 'animation.record-live'
SCHEMA = 'animation-native-viewport-session/1'
MODES = ('SOLID_WHITE_UNLIT', 'SOLID_OBJECT_UNLIT', 'CURRENT')
OPERATIONS = ('inspect','start','cancel','status','result','detach')
CURRENT_EDITION = '0.5.0'
LEGACY = '0.4.3'
_attached = None


def driver(bpy, edition=None):
    require(not bpy.app.background and os.name == 'nt', 'UNSUPPORTED', 'Native recording requires a Windows Blender window')
    cls = bpy.types.Operator.bl_rna_get_subclass_py('ANIMATION_NATIVE_OT_record_viewport')
    cancel = bpy.types.Operator.bl_rna_get_subclass_py('ANIMATION_NATIVE_OT_cancel_recording')
    require(cls is not None and cancel is not None and cls.__module__ == cancel.__module__, 'UNSUPPORTED',
            'Enable a supported Animation Curves recorder and its cancel operator')
    module = importlib.import_module(cls.__module__)
    product = importlib.import_module(module.__package__)
    installed = '.'.join(map(str, getattr(product, 'bl_info', {}).get('version', ())))
    require(installed == CURRENT_EDITION and (edition is None or installed == edition), 'UNSUPPORTED',
            f'Registered Animation Curves recorder release {installed or "unknown"} does not match the request')
    require(is_supported_host(bpy.app.version), 'UNSUPPORTED',
            f'Animation Curves Workflow {installed} recording is unavailable on this Blender build')
    source = Path(module.__file__).resolve().parent
    reference = package(installed)
    modules = ((product, '__init__.py'), (module, 'native_recording.py'),
               (getattr(module, 'core', None), 'recording_core.py'),
               (getattr(module, 'capture_core', None), 'screen_capture_core.py'))
    for imported, name in modules:
        file = source / name
        require(Path(getattr(imported, '__file__', '')).resolve() == file and file.is_file()
                and digest(file) == digest(reference / name), 'UNSUPPORTED',
                f'The loaded Animation Curves {installed} recorder differs from its release: {name}')
    return module, installed


def attach(directory, output_root, ffmpeg, *, edition=None):
    """Must be called in the exact authorized Blender VIEW_3D/WINDOW context."""
    import bpy

    global _attached
    require(_attached is None, 'CONFLICT', 'A recorder queue is already attached')
    context = bpy.context
    module, installed = driver(bpy, edition)
    require(context.window and context.area and context.area.type == 'VIEW_3D' and context.region
            and context.region.type == 'WINDOW' and not module.active_summary(), 'CONFLICT',
            'Attach in the intended idle VIEW_3D WINDOW region')
    directory, output_root, binary = Path(directory), canonical(output_root), canonical(ffmpeg)
    require(directory.is_absolute() and canonical(directory.parent) == directory.parent and not os.path.lexists(directory)
            and output_root.is_dir() and binary.is_file() and binary.name.lower() == 'ffmpeg.exe',
            'INVALID_REQUEST', 'Supply new queue, existing output directory and the authorized native ffmpeg.exe')
    with binary.open('rb') as stream:
        require(stream.read(2) == b'MZ', 'INVALID_REQUEST', 'The encoder must be a native Windows executable')
    file = bpy.data.filepath
    require(bool(file) and Path(file).is_file(), 'CONFLICT', 'Save the source before attaching its viewport')
    directory.mkdir()
    (directory/'requests').mkdir()
    info = {'schema':SCHEMA, 'session_id':str(uuid.uuid4()), 'directory':str(directory), 'blender':bpy.app.binary_path,
            'edition':installed,'build':bpy.app.build_hash.decode(),'pid':os.getpid(),'output_root':str(output_root),
            'ffmpeg':{'file':str(binary),'sha256':digest(binary)}}
    handle = (context.window.as_pointer(), context.area.as_pointer(), context.region.as_pointer(), context.scene.as_pointer(), file)
    _attached = (directory, info, module, handle, {})
    atomic_json(directory/'session.json', info)
    bpy.app.handlers.load_pre.append(on_load)
    bpy.app.timers.register(pump, first_interval=.1)
    return info


def on_load(*_):
    detach()


def detach():
    import bpy

    global _attached
    if _attached is not None:
        directory, info, _, _, _ = _attached
        atomic_json(directory/'closed.json', {'session_id':info['session_id']})
        if on_load in bpy.app.handlers.load_pre:
            bpy.app.handlers.load_pre.remove(on_load)
        _attached = None


def viewport(bpy, handle):
    window_id, area_id, region_id, scene_id, file = handle
    window = next((item for item in bpy.context.window_manager.windows if item.as_pointer() == window_id), None)
    require(window is not None and window.scene.as_pointer() == scene_id and bpy.data.filepath == file,
            'CONFLICT', 'The attached scene or window changed')
    area = next((item for item in window.screen.areas if item.as_pointer() == area_id and item.type == 'VIEW_3D'), None)
    region = next((item for item in area.regions if item.as_pointer() == region_id and item.type == 'WINDOW'), None) if area else None
    require(region is not None, 'CONFLICT', 'The attached VIEW_3D WINDOW region changed')
    return window, area, region


def job_id(raw):
    return type(raw) is str and re.fullmatch(r'native-recording-\d{8}T\d{6}Z-[0-9a-f]{12}', raw) is not None


def inspect_context(bpy, mode):
    context = bpy.context
    area, region, window = context.area, context.region, context.window
    state = {'file': bpy.data.filepath, 'scene': context.scene.name, 'view_layer': context.view_layer.name,
             'view': {'window':window.as_pointer(), 'area':area.as_pointer(), 'region':region.as_pointer(),
                      'width':region.width,'height':region.height}, 'mode':mode}
    state['expected_context'] = fingerprint(state)
    state.update(frame=context.scene.frame_current, active_playback=window.screen.is_animation_playing)
    return state


def response(path):
    report = read_json(path)
    return {'report':{'file':str(path),'sha256':digest(path)}, 'terminal_status':report.get('status'),
            'technical_complete':report.get('technical_complete'), 'frames':report.get('frames'),
            'video':report.get('video'), 'encoder_reaped':report.get('encoder',{}).get('reaped')}


def operate(bpy, module, request, info, owned):
    action = request['operation']
    active = module.active_summary()
    if action == 'inspect':
        return {'panel':inspect_context(bpy, bpy.context.space_data.shading.type), 'active':active}
    if action == 'start':
        state = inspect_context(bpy, bpy.context.space_data.shading.type)
        require(state['expected_context'] == request['expected_context'] and not active, 'CONFLICT',
                'The authorized viewport changed or a native recording is already active')
        binary = canonical(info['ffmpeg']['file'])
        with FileGuard(binary) as guard:
            require(guard.sha256() == info['ffmpeg']['sha256'], 'CONFLICT', 'Attached FFmpeg identity changed')
            require(bpy.ops.animation_native.record_viewport('INVOKE_DEFAULT', display_mode=request['mode'],
                    capture_source='WINDOW_SCREENSHOT', start_frame=request['start_frame'], end_frame=request['end_frame'],
                    ffmpeg_path=str(binary), output_root=info['output_root']) == {'RUNNING_MODAL'}, 'WORKER_FAILED',
                    'The native viewport recorder did not start')
        active = module.active_summary()
        folder = Path(active['job_dir']) if active else None
        require(folder is not None and folder.parent == Path(info['output_root']) and job_id(folder.name),
                'VALIDATION_FAILED', 'The native recording did not publish a matching task directory')
        owned[folder.name] = folder
        return {'state':'recording','native_job':folder.name,'path':str(folder),'active':active}
    if action == 'detach':
        require(not active or Path(active['job_dir']).name not in owned, 'CONFLICT', 'Wait for the owned recording to finish')
        return {'state':'detached'}
    label = request['native_job']
    require(label in owned, 'NOT_FOUND', 'The requested recording does not belong to this attached viewport')
    folder = owned[label]
    current = active if active and Path(active['job_dir']) == folder else None
    if action == 'cancel':
        require(current is not None, 'CONFLICT', 'The named recording is no longer active')
        require(bpy.ops.animation_native.cancel_recording() == {'FINISHED'}, 'WORKER_FAILED', 'Native cancellation did not start')
        return {'state':'cancel_requested','native_job':label}
    report = folder/'report.json'
    return {'state':'terminal','native_job':label,**response(report)} if report.is_file() and current is None else {
        'state':'recording','native_job':label,'active':current,'report':str(report)}


def validate(request):
    required = {'operation','session_id','request_id','mode','start_frame','end_frame','native_job','expected_context'}
    action = request.get('operation') if type(request) is dict else None
    if type(request) is dict and request.get('edition') == CURRENT_EDITION:
        required.add('edition')
    require(action in OPERATIONS and request.keys() == required and valid_id(request['session_id']) and valid_id(request['request_id']),
            'INVALID_REQUEST','Invalid recorder queue entry')
    controls = (request['mode'],request['start_frame'],request['end_frame'],request['expected_context'])
    if action == 'start':
        mode,start,end,sha = controls
        require(mode in MODES and type(start) is int and type(end) is int and 0 <= start <= end <= 1000000
                and type(sha) is str and re.fullmatch('[0-9a-f]{64}',sha), 'INVALID_REQUEST',
                'Start requires a supported display mode, exact frame range and current context SHA')
    else:
        require(all(item is None for item in controls), 'INVALID_REQUEST', 'Only start takes viewport or frame controls')
    require(job_id(request['native_job']) if action in ('cancel','status') else request['native_job'] is None,
            'INVALID_REQUEST', 'Cancel and status require an owned native-job identity')


def pump():
    import bpy

    if _attached is None:
        return None
    directory, info, module, handle, owned = _attached
    for file in sorted((directory/'requests').glob('*/request.json')):
        ref = file.parent.name
        done, started = file.parent/'result.json', file.parent/'started.json'
        if not valid_id(ref) or file.resolve() != file or done.exists() or started.exists():
            continue
        closing = False
        try:
            request = read_json(file)
            validate(request)
            require(request['session_id'] == info['session_id'] and request['request_id'] == ref
                    and request.get('edition', LEGACY) == info['edition'], 'CONFLICT', 'Queue identity or release differs')
            atomic_json(started, {'request_id':ref})
            registered, _ = driver(bpy, info['edition'])
            require(registered is module, 'CONFLICT', 'The registered recorder changed after attachment')
            window,area,region = viewport(bpy,handle)
            with bpy.context.temp_override(window=window,area=area,region=region):
                data = operate(bpy,module,request,info,owned)
            closing = data['state'] == 'detached' if 'state' in data else False
            result = envelope(COMMAND,ref,data={**data,'request_id':ref})
        except Exception as error:
            result = envelope(COMMAND,ref,error={'code':getattr(error,'code','WORKER_FAILED'),'message':str(error)})
        atomic_json(done,result)
        if closing:
            detach()
            return None
        break
    return .15


def run(args):
    directory = canonical(args.session_dir)
    require(0 <= args.wait_seconds <= 55, 'INVALID_REQUEST', 'Wait must be 0–55 seconds')
    require(args.operation not in {'start','cancel','result'} or valid_id(args.request_id), 'INVALID_REQUEST', 'Supply a stable request UUID')
    ref = args.request_id or str(uuid.uuid4())
    request = {key:getattr(args,key) for key in ('operation','session_id','mode','start_frame','end_frame','native_job','expected_context')}
    request['request_id'] = ref
    if args.edition == CURRENT_EDITION:
        request['edition'] = args.edition
    validate(request)
    queue = directory/'requests'
    item = queue/ref
    with ExitStack() as guards:
        for parent in reversed((queue,*queue.parents)):
            if parent != Path(parent.anchor):
                guards.enter_context(FileGuard(parent,directory=True))
        guards.enter_context(FileGuard(directory/'session.json'))
        info = read_json(directory/'session.json')
        require(info['schema'] == SCHEMA and info['session_id'] == args.session_id and Path(info['directory']) == directory
                and canonical(info['blender']) == canonical(args.blender), 'CONFLICT', 'Attached viewport or host differs')
        require(info.get('edition', LEGACY) == args.edition, 'CONFLICT', 'Attached recorder release differs; select its --edition')
        with PathLocks([item]):
            require(not os.path.lexists(item) or (item.is_dir() and canonical(item) == item),'CONFLICT','Redirected recording queue')
            if args.operation != 'result':
                if item.exists():
                    require(read_json(item/'request.json') == request,'CONFLICT','Request UUID is already in use')
                else:
                    require(not (directory/'closed.json').exists(),'CONFLICT','Attached viewport was closed')
                    item.mkdir()
                    atomic_json(item/'request.json',request)
            require(item.is_dir(),'NOT_FOUND','Unknown request UUID')
            if args.operation == 'result':
                stored = read_json(item/'request.json')
                validate(stored)
                require(stored['session_id'] == info['session_id'] and stored['request_id'] == ref
                        and stored.get('edition', LEGACY) == args.edition, 'CONFLICT', 'Stored recording request identity differs')
        result = item/'result.json'
        deadline = time.monotonic()+args.wait_seconds
        while not result.is_file():
            require(not (directory/'closed.json').exists(),'CONFLICT','Viewport closed before completing the operation')
            if time.monotonic() >= deadline:
                return envelope(COMMAND,ref,data={'state':'pending','request_id':ref})
            time.sleep(min(.1,max(0,deadline-time.monotonic())))
        return read_json(result)


def main():
    cli = Parser(description=__doc__,allow_abbrev=False)
    cli.add_argument('--blender',type=Path,default=DEFAULT_BLENDER)
    for flag in ('--jobs-dir','--transactions-dir','--timeout'):
        cli.add_argument(flag,help=argparse.SUPPRESS)
    style = cli.add_mutually_exclusive_group()
    for flag in ('--human','--compact','--full'):
        style.add_argument(flag,action='store_true')
    fields = cli.add_subparsers(required=True,parser_class=Parser).add_parser('record-live', description=__doc__,
            epilog='Attach in the exact idle VIEW_3D WINDOW region using attach(queue, output_root, ffmpeg). '
                   'Select the release recorded by attach; the current edition is selected by default. '
                   'Inspect gives the viewport SHA. Start gives the named native 1080p task. Cancel and status use that task identity.')
    fields.add_argument('--edition', choices=(CURRENT_EDITION,), default=CURRENT_EDITION, help='Attached Animation Curves release (default: 0.5.0)')
    fields.add_argument('--session-dir',type=Path,required=True)
    fields.add_argument('--session-id',required=True)
    fields.add_argument('--operation',choices=OPERATIONS,required=True)
    fields.add_argument('--request-id')
    fields.add_argument('--native-job')
    fields.add_argument('--mode',choices=MODES)
    fields.add_argument('--start-frame',type=int)
    fields.add_argument('--end-frame',type=int)
    fields.add_argument('--expected-context')
    fields.add_argument('--wait-seconds',type=float,default=10)
    try:
        result = run(cli.parse_args())
    except (Failure,OSError,ValueError,TypeError,KeyError) as error:
        result = envelope(COMMAND,error={'code':getattr(error,'code','INVALID_REQUEST'),'message':str(error)})
    sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))
    return 0 if result['ok'] else CODES.get(result['error']['code'],8)


if __name__ == '__main__':
    raise SystemExit(main())
