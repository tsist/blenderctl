"""Bounded file-queue transport for an owned Blender session; no bpy dependency."""
from contextlib import ExitStack, contextmanager, nullcontext
import json
import os
from pathlib import Path
import time
import uuid

from filesystem import PathLocks
from protocol import Failure, atomic_json, read_json


@contextmanager
def admission_lock(closing):
    deadline = time.monotonic() + 2
    with ExitStack() as held:
        while True:
            try:
                held.enter_context(PathLocks([closing]))
                break
            except Failure as exc:
                if exc.code != 'CONFLICT' or time.monotonic() >= deadline:
                    raise
                time.sleep(.02)
        yield


def admit(root, info, params, request_id):
    """Seal admission before an owned host drains the work it already accepted."""
    action = params.get('action')
    owned = (root / 'host-launch.json').is_file()
    live_owner = root / 'live-owner.json'
    live = live_owner.is_file()
    closing = root / ('live-closing.json' if live else 'host-closing.json')
    with (admission_lock(closing) if owned or live else nullcontext()):
        if live:
            owner = read_json(live_owner)
            if owned or owner.get('schema') != 'geometry-live-queue/1' or owner.get('session_id') != info['id'] \
                    or owner.get('pid') != info['pid'] or Path(owner.get('session_dir', '')) != root:
                raise ValueError('Registered live geometry owner differs from the native session')
        if action == 'stop':
            if live:
                raise ValueError('Use geometry session-live stop with the owning attachment UUID')
            if not owned or not (root / 'host-ready.json').is_file():
                raise ValueError('Remote stop requires a CLI-owned geometry session-host')
            if params.get('session_id') != info['id']:
                raise ValueError('Remote stop requires the explicit current session_id')
            if set(params) - {'session_dir', 'session_id', 'action', 'wait_seconds'}:
                raise ValueError('Stop does not accept candidate or capture inputs')
            if closing.exists():
                return read_json(closing)['request_id']
        if (root / 'stopped.json').exists() or (owned and (root / 'host-ended.json').exists()) \
                or (live and (root / 'live-ended.json').exists()):
            raise ValueError('Session queue has stopped')
        if (owned or live) and closing.exists():
            raise ValueError('Session queue is closing')
        if action == 'stop':
            atomic_json(closing, {'request_id': request_id, 'session_id': info['id']})
        else:
            if action not in ('capture', 'status', 'apply'):
                raise ValueError('Unknown session action')
            payload = {k: v for k, v in params.items() if k not in ('session_dir', 'wait_seconds', 'request_id')}
            payload.update(request_id=request_id, session_id=info['id'])
            target = root / 'requests' / (request_id + '.json')
            temporary = target.with_suffix('.pending')
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
            os.replace(temporary, target)
    return request_id


def read_result(path):
    """Read one published result with share-delete semantics on Windows."""
    try:
        result = read_json(path)
    except Failure as exc:
        if exc.code == 'NOT_FOUND':
            return None, None
        raise
    except OSError as exc:
        if os.name == 'nt' and getattr(exc, 'winerror', None) in (5, 32, 33):
            return None, exc
        raise
    if not isinstance(result, dict):
        raise ValueError('Persisted session result must be a JSON object')
    return result, None


def dispatch(params):
    if not isinstance(params, dict) or set(params) - {'session_dir', 'action', 'manifest', 'resources', 'baseline', 'candidate', 'report', 'request_id', 'wait_seconds', 'session_id'}:
        raise ValueError('Unknown session request fields')
    root = Path(params.get('session_dir', ''))
    if not root.is_absolute(): raise ValueError('Absolute session_dir required')
    info = read_json(root / 'session.json')
    if params.get('session_id', info['id']) != info['id']: raise ValueError('Session identity changed')
    wait = params.get('wait_seconds', 0)
    if type(wait) not in (int, float) or not 0 <= wait <= 55: raise ValueError('wait_seconds must be 0..55')
    existing = params.get('request_id')
    request_id = str(uuid.UUID(existing)) if existing else str(uuid.uuid4())
    if not existing:
        request_id = admit(root, info, params, request_id)
    result_path = root / 'results' / (request_id + '.json')
    deadline = time.monotonic() + wait
    while True:
        result, read_error = read_result(result_path)
        if result is not None:
            return result
        finished = (root / 'host-ended.json').is_file() or (root / 'live-ended.json').is_file()
        if read_error is None and finished:
            # The result may have been published just before host-ended.
            result, read_error = read_result(result_path)
            if result is not None:
                return result
            if read_error is None:
                return {'ok': False, 'status': 'ended', 'session_id': info['id'], 'request_id': request_id,
                        'error': 'The owning process ended without a result for this request'}
        if time.monotonic() >= deadline:
            pending = {'ok': True, 'status': 'pending', 'request_id': request_id, 'session_id': info['id'],
                       'result': str(result_path)}
            if read_error is not None:
                pending['read_pending'] = str(read_error)
            return pending
        time.sleep(min(.1, max(0, deadline - time.monotonic())))
