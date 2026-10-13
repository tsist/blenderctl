"""Bounded file-queue transport for an owned Blender session; no bpy dependency."""
import hashlib
import json
import os
from pathlib import Path
import time
import uuid


def dispatch(params):
    if not isinstance(params, dict) or set(params) - {'session_dir', 'action', 'manifest', 'resources', 'baseline', 'candidate', 'report', 'request_id', 'wait_seconds', 'session_id'}:
        raise ValueError('Unknown session request fields')
    root = Path(params.get('session_dir', ''))
    if not root.is_absolute(): raise ValueError('Absolute session_dir required')
    info = json.loads((root / 'session.json').read_text(encoding='utf-8'))
    if params.get('session_id', info['id']) != info['id']: raise ValueError('Session identity changed')
    wait = params.get('wait_seconds', 0)
    if type(wait) not in (int, float) or not 0 <= wait <= 55: raise ValueError('wait_seconds must be 0..55')
    existing = params.get('request_id')
    request_id = str(uuid.UUID(existing)) if existing else str(uuid.uuid4())
    result_path = root / 'results' / (request_id + '.json')
    if not existing:
        if (root / 'stopped.json').exists(): raise ValueError('Session queue has stopped')
        action = params.get('action')
        if action not in ('capture', 'status', 'apply'): raise ValueError('Unknown session action')
        payload = {k: v for k, v in params.items() if k not in ('session_dir', 'wait_seconds', 'request_id')}
        payload.update(request_id=request_id, session_id=info['id'])
        target = root / 'requests' / (request_id + '.json')
        temporary = target.with_suffix('.pending')
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
        os.replace(temporary, target)
    deadline = time.monotonic() + wait
    while True:
        if result_path.exists(): return json.loads(result_path.read_text(encoding='utf-8'))
        if time.monotonic() >= deadline:
            return {'ok': True, 'status': 'pending', 'request_id': request_id, 'session_id': info['id'], 'result': str(result_path)}
        time.sleep(min(.1, max(0, deadline - time.monotonic())))
