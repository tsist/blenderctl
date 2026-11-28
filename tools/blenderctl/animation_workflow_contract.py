"""Host-safe animation.inspect/animation.run request contract.

The only domain grammar is the fixed, host-safe
``animation_curves_workflow_addon.clip_contract.normalize_request`` function.
This module adds the public CLI envelope, strict source/resource descriptor
checks and the immutable implementation identity. It never imports ``bpy`` and
never imports a path supplied by a request.
"""
import copy
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from protocol import CODES, Failure
from animation_identity import implementation_sha256, runtime_documents


PARAMS_REQUIRED = ('file', 'expected_sha256', 'manifest', 'resources')
PARAMS_OPTIONAL = ('implementation_sha256',)
RESOURCE_LIMIT = 256
ACTIONS = ('capture', 'retime')
MANIFEST_FIELDS = {'schema', 'action', 'context', 'event', 'participants', 'locks'}
PARTICIPANT_FIELDS = {'rigs', 'roots', 'lights', 'materials'}
PARTICIPANT_LIMITS = {'rigs': 64, 'roots': 64, 'lights': 128, 'materials': 256,
                      'objects': 8}
LOCK_LIMIT = 64
FRAME_LIMIT = 1000000


def _fail(message, code='INVALID_REQUEST'):
    raise Failure(code, message)


def _sha(value, label):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        _fail(label + ' must be lowercase SHA-256')
    return value


def _absolute(value, label):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        _fail(label + ' must be a non-empty path')
    path = Path(value)
    if not path.is_absolute():
        _fail(label + ' must be an absolute path')
    return str(path.resolve())


def _descriptor(value, label, *, blend=False):
    if not isinstance(value, dict) or set(value) != {'file', 'expected_sha256'}:
        _fail(label + ' requires exactly file and expected_sha256')
    result = {'file': _absolute(value['file'], label + '.file'),
              'expected_sha256': _sha(value['expected_sha256'],
                                      label + '.expected_sha256')}
    if blend and Path(result['file']).suffix.lower() != '.blend':
        _fail(label + ' must be a .blend file')
    return result


def _finite(value, path='/'):
    if isinstance(value, float) and not math.isfinite(value):
        _fail('Non-finite number at ' + path)
    if isinstance(value, dict):
        for key, child in value.items():
            _finite(child, path + str(key) + '/')
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _finite(child, path + str(index) + '/')


def _name(value, label):
    if not isinstance(value, str) or not value.strip() or '\x00' in value or len(value) > 256:
        _fail(label + ' must be a non-empty string')
    return value


def _frame(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(label + ' must be a finite frame number')
    try:
        finite = math.isfinite(float(value))
    except (OverflowError, ValueError):
        finite = False
    if not finite:
        _fail(label + ' must be a finite frame number')
    if not -FRAME_LIMIT <= value <= FRAME_LIMIT:
        _fail(label + ' is outside the supported frame range')
    return value


def _strict_manifest(value):
    """Reject unknown/missing/non-finite fields before the add-on sees them."""
    if not isinstance(value, dict):
        _fail('manifest must be an object')
    unknown = set(value) - MANIFEST_FIELDS
    if unknown:
        _fail('Unknown manifest fields: ' + ', '.join(sorted(unknown)))
    missing = MANIFEST_FIELDS - set(value)
    if missing:
        _fail('Missing manifest fields: ' + ', '.join(sorted(missing)))
    _finite(value)
    if value['schema'] != 'animation-clip-event-v1':
        _fail('manifest.schema must be animation-clip-event-v1')
    action = value['action']
    if action not in ACTIONS:
        _fail('manifest.action must be capture or retime')
    context = value['context']
    if not isinstance(context, dict) or set(context) != {'scene', 'view_layer'}:
        _fail('manifest.context requires exactly scene and view_layer')
    _name(context['scene'], 'context.scene')
    _name(context['view_layer'], 'context.view_layer')
    event = value['event']
    expected = {'id', 'start', 'contact', 'end'}
    if action == 'retime':
        expected.add('target_contact')
    if not isinstance(event, dict) or set(event) != expected:
        _fail('manifest.event fields are invalid for ' + action)
    _name(event['id'], 'event.id')
    start = _frame(event['start'], 'event.start')
    contact = _frame(event['contact'], 'event.contact')
    end = _frame(event['end'], 'event.end')
    if not start < contact < end:
        _fail('Event window must satisfy start < contact < end')
    if action == 'retime':
        target = _frame(event['target_contact'], 'event.target_contact')
        if not start < target < end:
            _fail('target_contact must lie strictly inside the event window')
    participants = value['participants']
    if (not isinstance(participants, dict)
            or set(participants) not in (PARTICIPANT_FIELDS,
                                        PARTICIPANT_FIELDS | {'objects'})):
        if not isinstance(participants, dict) or 'objects' not in participants:
            _fail('manifest.participants requires exactly rigs, roots, lights and materials')
        _fail('manifest.participants requires rigs, roots, lights and materials; '
              'pure object mode additionally requires objects')
    if 'objects' in participants:
        if not isinstance(participants['objects'], list) or not participants['objects']:
            _fail('participants.objects must be a nonempty explicit list')
        if any(participants[field] for field in PARTICIPANT_FIELDS):
            _fail('Object clip mode requires empty rigs, roots, lights and materials')
    for key, limit in PARTICIPANT_LIMITS.items():
        if key not in participants:
            continue
        rows = participants[key]
        if not isinstance(rows, list) or len(rows) > limit:
            _fail('participants.' + key + ' must be an array of at most ' + str(limit))
        seen = set()
        for index, item in enumerate(rows):
            _name(item, 'participants.' + key + '[' + str(index) + ']')
            if item in seen:
                _fail('Duplicate participant in participants.' + key + ': ' + item)
            seen.add(item)
    locks = value['locks']
    if not isinstance(locks, list) or len(locks) > LOCK_LIMIT:
        _fail('locks must be an array of at most ' + str(LOCK_LIMIT) + ' frames')
    seen = set()
    for index, frame in enumerate(locks):
        frame = _frame(frame, 'locks[' + str(index) + ']')
        if frame in seen:
            _fail('Duplicate lock frame: ' + str(frame))
        seen.add(frame)
    return value


def _fixed_contract():
    addon = Path(__file__).resolve().parent.parent / 'animation_curves_workflow_addon'
    if not addon.is_dir() or not (addon / 'clip_contract.py').is_file():
        _fail('Fixed animation add-on clip_contract.py is missing: ' + str(addon),
              'NOT_FOUND')
    try:
        from animation_curves_workflow_addon import clip_contract
    except Exception as exc:
        _fail('Fixed animation add-on clip_contract import failed: ' + str(exc),
              'NOT_FOUND')
    return clip_contract


def _normalize_addon_request(value):
    contract = _fixed_contract()
    try:
        normalized = contract.normalize_request(copy.deepcopy(value))
    except Failure:
        raise
    except Exception as exc:
        code = getattr(exc, 'code', None)
        _fail(str(exc), code if code in CODES else 'INVALID_REQUEST')
    return _strict_manifest(normalized)


def _identity(value):
    if not isinstance(value, dict):
        _fail('params must be an object')
    result = copy.deepcopy(value)
    supplied = result.pop('implementation_sha256', None)
    current = implementation_sha256()
    if supplied is not None:
        _sha(supplied, 'implementation_sha256')
        if supplied != current:
            _fail('Animation implementation changed since request normalization',
                  'CONFLICT')
    return result, current


def normalize_params(value):
    """Normalize one animation.inspect or animation.run params object."""
    result, current = _identity(value)
    unknown = set(result) - set(PARAMS_REQUIRED)
    if unknown:
        _fail('Unknown animation params fields: ' + ', '.join(sorted(unknown)))
    missing = set(PARAMS_REQUIRED) - set(result)
    if missing:
        _fail('Animation params require file, expected_sha256, manifest and resources; missing: '
              + ', '.join(sorted(missing)))
    source = _descriptor({'file': result['file'],
                          'expected_sha256': result['expected_sha256']},
                         'source', blend=True)
    if not Path(source['file']).is_file():
        _fail('Input file not found: ' + source['file'], 'NOT_FOUND')
    resources = result['resources']
    if not isinstance(resources, list) or len(resources) > RESOURCE_LIMIT:
        _fail('resources must be an array of 0..' + str(RESOURCE_LIMIT)
              + ' explicit descriptors')
    normalized_resources = []
    seen = set()
    for index, row in enumerate(resources):
        item = _descriptor(row, 'resources[' + str(index) + ']')
        if Path(item['file']).suffix.lower() in ('.py', '.pyw', '.pyd'):
            _fail('Python/plugin files are not animation resource inputs')
        if not Path(item['file']).is_file():
            _fail('Animation resource not found: ' + item['file'], 'NOT_FOUND')
        key = os.path.normcase(item['file'])
        if key in seen:
            _fail('Duplicate animation resource path: ' + item['file'])
        seen.add(key)
        normalized_resources.append(item)
    manifest = _normalize_addon_request(result['manifest'])
    return {**source, 'resources': normalized_resources, 'manifest': manifest,
            'implementation_sha256': current}


def normalize_inspect_params(value):
    return normalize_params(value)


def signature(params):
    body = {key: params.get(key) for key in
            ('file', 'expected_sha256', 'resources', 'manifest', 'implementation_sha256')}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(',', ':'),
                   ensure_ascii=False).encode('utf-8')
    ).hexdigest()


def input_documents(params):
    """Source, explicit resources and the complete fixed runtime surface."""
    rows = [{'file': params['file'], 'expected_sha256': params['expected_sha256']}]
    rows.extend(params.get('resources', []))
    rows.extend(runtime_documents())
    merged = {}
    for row in rows:
        key = os.path.normcase(str(Path(row['file']).resolve()))
        if key in merged and merged[key]['expected_sha256'] != row['expected_sha256']:
            _fail('Conflicting animation input hashes: ' + row['file'], 'CONFLICT')
        merged[key] = {'file': row['file'], 'expected_sha256': row['expected_sha256']}
    return list(merged.values())
