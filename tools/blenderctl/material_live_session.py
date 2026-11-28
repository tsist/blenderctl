"""Submit a Material Workflow snapshot from an explicitly attached Blender session."""
import argparse
from contextlib import ExitStack
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True
CORE = Path(__file__).resolve().parent
sys.path.insert(0, str(CORE))
from filesystem import FileGuard, PathLocks
from material_draft_handoff import Parser, canonical, edition_directory, fingerprint, require, supports_edition
from protocol import CODES, DEFAULT_BLENDER, Failure, atomic_json, digest, envelope, read_json

COMMAND = 'material.handoff-live'
DOMAIN = 'material-panel-session/1'
_owner = None


def product(bpy):
    import addon_utils

    cls = bpy.types.Operator.bl_rna_get_subclass_py('MATERIAL_WORKFLOW_OT_handoff')
    require(cls is not None, 'UNSUPPORTED', 'Enable the matching Material Workflow add-on in this session')
    module = importlib.import_module(cls.__module__)
    folder = Path(module.__file__).resolve().parent
    addon = importlib.import_module(module.__package__)
    edition = '.'.join(map(str, addon_utils.module_bl_info(addon)['version']))
    require(supports_edition(edition, bpy.app.version),
            'UNSUPPORTED', f'Material Workflow {edition} does not support this live Blender host')
    source = edition_directory(edition)
    required = ('__init__.py', 'ui.py', 'ui_batch.py', 'ui_handoff.py', 'handoff.py',
                'job_bridge.py', 'contract.py', 'batch_contract.py')
    require(all(digest(folder / name) == digest(source / name) for name in required),
            'CONFLICT', 'Enabled material panel differs from the selected host edition')
    return edition, module


def attach(directory, handoff_root=None):
    """Call once from the Blender instance whose live material draft the CLI may submit."""
    import bpy

    global _owner
    require(_owner is None, 'CONFLICT', 'A material handoff queue is already attached')
    root = Path(directory)
    require(root.is_absolute() and canonical(root.parent) == root.parent and not os.path.lexists(root),
            'CONFLICT', 'Choose an unused absolute session directory in an existing parent')
    edition, module = product(bpy)
    destination = canonical(handoff_root) if handoff_root is not None else root
    require(handoff_root is None or destination.is_dir(), 'INVALID_REQUEST', 'Expected an existing handoff root')
    root.mkdir()
    (root / 'requests').mkdir()
    info = dict(schema=DOMAIN, session_id=str(uuid.uuid4()), pid=os.getpid(), blender=bpy.app.binary_path,
                edition=edition, build=bpy.app.build_hash.decode(), directory=str(root), source=bpy.data.filepath,
                handoff_root=str(destination))
    atomic_json(root / 'session.json', info)
    _owner = (root, info, module)
    bpy.app.handlers.load_pre.append(_on_load)
    if not bpy.app.timers.is_registered(_pump):
        bpy.app.timers.register(_pump, first_interval=.15)
    return info


def _on_load(*_):
    detach()


def detach():
    """Detach the queue while keeping the artist's document and Blender instance open."""
    import bpy

    global _owner
    if _owner is None:
        return
    root, info, _ = _owner
    atomic_json(root / 'closed.json', {'session_id': info['session_id']})
    if _on_load in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(_on_load)
    # A timer may detach itself during its own stop request. Returning None removes it.
    _owner = None


def inspect_panel(bpy, module, mode):
    from material_batch_draft import saved_editor

    ctx, scene = bpy.context, bpy.context.scene
    require(ctx.mode == 'OBJECT', 'CONFLICT', 'Live material handoff requires Object Mode')
    if mode == 'SINGLE':
        require(bool(scene.mw_manifest_json), 'NOT_FOUND', 'Live session has no single-material draft')
        draft = module.ui._manifest(scene)
        editor = saved_editor(scene, module.ui)
        details = dict(material_id=draft['material']['id'], target=draft['target'], layer_index=scene.mw_layer_index,
                       draft_sha256=editor['draft_sha256'], panel_sha256=editor['panel_fields_sha256'])
    else:
        require(bool(scene.mw_batch_json), 'NOT_FOUND', 'Live session has no committed batch draft')
        batch = module.ui_batch.get_manifest(scene)
        details = dict(assignments=[row['id'] for row in batch['assignments']], draft_sha256=fingerprint(batch))
    info = dict(mode=mode, file=bpy.data.filepath, scene=scene.name, frame=scene.frame_current,
                view_layer=ctx.view_layer.name, subframe=scene.frame_subframe, draft=details)
    require(scene.frame_subframe == 0, 'UNSUPPORTED', 'The material snapshot supports whole frames')
    return {**info, 'expected_context': fingerprint(info), 'dirty': bpy.data.is_dirty}


def operate(module, directory, request, info):
    import bpy

    action = request['operation']
    if action == 'stop':
        return {'state': 'detached'}
    require(action in ('inspect', 'submit') and request['mode'] in ('SINGLE', 'BATCH'),
            'INVALID_REQUEST', 'Unsupported material handoff operation or mode')
    observed = inspect_panel(bpy, module, request['mode'])
    if action == 'inspect':
        return observed
    require(observed['expected_context'] == request['expected_context'], 'CONFLICT',
            'Current document, scene, frame, layer or material draft differs from inspection')
    # Keep Blender's render/output paths short even when the queue has long request IDs.
    destination = Path(tempfile.mkdtemp(prefix='m-', dir=canonical(info['handoff_root'])))
    atomic_json(directory/'destination.json', {'directory': str(destination)})
    scene = bpy.context.scene
    previous = scene.mw_cli_root, scene.mw_handoff_root, scene.mw_handoff_mode
    try:
        scene.mw_cli_root = str(CORE.parents[1])
        scene.mw_handoff_root = str(destination)
        scene.mw_handoff_mode = request['mode']
        status = bpy.ops.material_workflow.handoff()
        require(status == {'FINISHED'}, 'WORKER_FAILED', scene.mw_last_error or 'Panel handoff failed')
        path = Path(scene.mw_handoff_receipt)
        require(path.parent.parent == destination and path.name == 'handoff.json',
                'CONFLICT', 'Panel returned a receipt outside this request')
        receipt = module.handoff.load_receipt(path)
        require(receipt['state'] == 'submitted' and receipt['mode'] == request['mode'] and receipt.get('job_ref'),
                'VALIDATION_FAILED', 'Panel did not retain an acknowledged job')
        return dict(state='submitted', panel=observed, receipt={'file': str(path), 'sha256': digest(path)},
                    snapshot=receipt['snapshot'], job_ref=receipt['job_ref'])
    finally:
        scene.mw_cli_root, scene.mw_handoff_root, scene.mw_handoff_mode = previous


def receipts(directory, info):
    pin = directory / 'destination.json'
    if not pin.is_file():
        return []
    destination = Path(read_json(pin)['directory'])
    if destination.parent != Path(info['handoff_root']) or destination.resolve() != destination:
        return []
    return [str(p) for p in destination.glob('handoff-*/handoff.json')]


def _pump():
    if _owner is None:
        return None
    root, info, module = _owner
    queue = root / 'requests'
    for path in sorted(queue.glob('*/request.json'), key=lambda p:p.stat().st_mtime_ns):
        directory = path.parent
        if not valid_id(directory.name) or directory.resolve() != directory or path.resolve() != path:
            continue
        marker, done = directory / 'started.json', directory / 'result.json'
        if done.exists() or marker.exists():
            continue
        closing = False
        try:
            require(canonical(directory) == directory and canonical(path) == path,
                    'CONFLICT', 'Redirected queue entry')
            request = read_json(path)
            require(request['session_id'] == info['session_id'] and request['request_id'] == directory.name
                    and valid_id(directory.name), 'CONFLICT', 'Queue entry identity mismatch')
            atomic_json(marker, {'session_id': info['session_id'], 'request_id': directory.name})
            value = operate(module, directory, request, info)
            response = envelope(COMMAND, directory.name, data=value)
            closing = request['operation'] == 'stop'
        except Exception as exc:
            response = envelope(COMMAND, directory.name, error={'code': getattr(exc, 'code', 'WORKER_FAILED'),
                                'message': str(exc)}, artifacts={'receipts': receipts(directory, info)})
        atomic_json(done, response)
        if closing:
            detach()
            return None
        break
    return .2


def valid_id(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def run(args):
    root = canonical(args.session_dir)
    require(valid_id(args.session_id), 'INVALID_REQUEST', 'Expected a session UUID')
    require(0 <= args.wait_seconds <= 55, 'INVALID_REQUEST', 'wait-seconds must be in 0..55')
    writing = args.operation == 'submit'
    lookup = args.operation == 'result'
    require(not (writing or lookup) or valid_id(args.request_id), 'INVALID_REQUEST', 'Supply a request UUID')
    ident = args.request_id or str(uuid.uuid4())
    require(valid_id(ident), 'INVALID_REQUEST', 'Expected a request UUID')
    require((args.mode is not None) == (args.operation in ('inspect','submit')),
            'INVALID_REQUEST', 'inspect and submit require --mode')
    require(bool(args.expected_context) == writing, 'INVALID_REQUEST', 'Only submit takes --expected-context')
    if writing:
        import re
        require(re.fullmatch('[0-9a-f]{64}', args.expected_context), 'INVALID_REQUEST', 'Invalid expected context hash')
    queue = root / 'requests'
    directory = queue / ident
    with ExitStack() as guards:
        for parent in reversed((queue, *queue.parents)):
            if parent != Path(parent.anchor):
                guards.enter_context(FileGuard(parent, directory=True))
        guards.enter_context(FileGuard(root / 'session.json'))
        info = read_json(root / 'session.json')
        require(info['schema'] == DOMAIN and info['session_id'] == args.session_id and Path(info['directory']) == root
                and canonical(info['blender']) == canonical(args.blender), 'CONFLICT', 'Published session identity or host differs')
        with PathLocks([directory]):
            if not lookup:
                request = dict(operation=args.operation, session_id=args.session_id, request_id=ident, mode=args.mode,
                               expected_context=args.expected_context)
                if directory.exists():
                    require(canonical(directory) == directory and read_json(directory/'request.json') == request,
                            'CONFLICT', 'This request UUID already belongs to a different operation')
                else:
                    require(not (root/'closed.json').exists(), 'CONFLICT', 'Session has detached')
                    directory.mkdir()
                    atomic_json(directory/'request.json', request)
            require(directory.is_dir() and canonical(directory) == directory, 'NOT_FOUND', 'Unknown request UUID')
        deadline = time.monotonic() + args.wait_seconds
        while True:
            result = directory / 'result.json'
            if result.is_file():
                return read_json(result)
            if (root/'closed.json').is_file():
                raise Failure('CONFLICT', 'Session detached before completing this request')
            if time.monotonic() >= deadline:
                return envelope(COMMAND, ident, data=dict(state='pending',request_id=ident,session_id=args.session_id,
                                result=str(result)), artifacts={'request_directory':str(directory),
                                                              'receipts':receipts(directory, info)})
            time.sleep(min(.1, max(0, deadline-time.monotonic())))


def main():
    parser = Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--blender', type=Path, default=DEFAULT_BLENDER)
    for flag in ('--jobs-dir', '--transactions-dir', '--timeout'):
        parser.add_argument(flag, help=argparse.SUPPRESS)
    display = parser.add_mutually_exclusive_group()
    for flag in ('--compact', '--human', '--full'):
        display.add_argument(flag, action='store_true')
    sub = parser.add_subparsers(required=True, parser_class=Parser).add_parser('handoff-live',
        description='Inspect or submit a live Material Workflow SINGLE or BATCH draft, or fetch a prior result.',
        epilog='Attach this module once in the target Blender with attach(NEW_ABSOLUTE_DIRECTORY). '
               'Reuse the same request UUID after a wait timeout. Stop detaches the queue and leaves Blender open.')
    sub.add_argument('--session-dir',type=Path,required=True)
    sub.add_argument('--session-id',required=True)
    sub.add_argument('--operation',choices=('inspect','submit','result','stop'),required=True)
    sub.add_argument('--mode',choices=('SINGLE','BATCH'))
    sub.add_argument('--request-id')
    sub.add_argument('--expected-context')
    sub.add_argument('--wait-seconds',type=float,default=10)
    try:
        args=parser.parse_args()
        result=run(args)
    except (Failure,OSError,ValueError,KeyError,TypeError) as exc:
        result=envelope(COMMAND,error={'code':getattr(exc,'code','INVALID_REQUEST'),'message':str(exc)})
    sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))
    return 0 if result['ok'] else CODES.get(result['error']['code'],8)


if __name__ == '__main__':
    raise SystemExit(main())
