"""Host-safe contract for the public ``animation.clip-apply`` adapter.

The only native work unit is the fixed add-on entry
``animation_curves_workflow_addon.clip_asset.apply_clip``.  This module owns
the public CLI envelope, the frozen request grammar and the immutable input
set.  It never imports ``bpy`` and never imports a path supplied by a request.

The source span comes from ``clip.timing`` and the target span from
``request.start/end``.  No work duration is hard-coded: both spans must be
finite positive ordered numbers, and the destination Scene fps/fps_base/unit
must match the clip before the native core sees the request.  The native core
enforces whole-clip, no-trim Action spans.  The apply step preserves Scene
settings and never sets the Scene frame range.
"""
import copy
import hashlib
import json
import math
import os
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from protocol import Failure
from animation_identity import implementation_sha256, runtime_documents


COMMAND = 'animation.clip-apply'
APPLY_SCHEMA = 'animation-native-clip-apply-v1'
CLIP_SCHEMA = 'animation-native-clip-v1'
PARAMS_REQUIRED = ('file', 'expected_sha256', 'manifest', 'resources')
RESOURCE_LIMIT = 256
ACTOR_ROLES = ('A', 'B')
LIGHT_ROLES = ('block', 'counter')
PATH_LIMIT = 4096
NAME_LIMIT = 512
INSTANCE_LIMIT = 200


def _fail(message, code='INVALID_REQUEST'):
    raise Failure(code, message)


def _sha(value, label):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        _fail(label + ' must be lowercase SHA-256')
    return value


def _uuid(value, label):
    if not isinstance(value, str):
        _fail(label + ' must be a canonical UUID string')
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as exc:
        _fail(label + ' must be a canonical UUID string: ' + str(exc))
    if canonical != value:
        _fail(label + ' must be a canonical lowercase UUID string')
    return value


def _hex32(value, label):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{32}', value) is None:
        _fail(label + ' must be 32 lowercase hexadecimal characters')
    return value


def _name(value, label, maximum=NAME_LIMIT):
    if (not isinstance(value, str) or not value.strip() or '\x00' in value
            or len(value) > maximum):
        _fail(label + ' must be a non-empty string of at most '
              + str(maximum) + ' characters')
    return value


def _provenance_path(value, label):
    # Provenance is informational: do not require the historical file to exist.
    if (not isinstance(value, str) or not value.strip() or '\x00' in value
            or len(value) > PATH_LIMIT):
        _fail(label + ' must be a non-empty path string')
    return value


def _absolute(value, label):
    if (not isinstance(value, str) or not value.strip() or '\x00' in value
            or len(value) > PATH_LIMIT):
        _fail(label + ' must be a non-empty path')
    # pathlib treats //name as a UNC absolute path on Windows, while Blender
    # interprets it as relative to the opened file.  The public contract only
    # accepts real absolute filesystem paths.
    if value.startswith('//'):
        _fail(label + ' must be an absolute filesystem path')
    try:
        path = Path(value)
        if not path.is_absolute():
            _fail(label + ' must be an absolute path')
        return str(path.resolve())
    except Failure:
        raise
    except (OSError, ValueError) as exc:
        _fail(label + ' is not a valid path: ' + str(exc))


def _number(value, label, *, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(label + ' must be a finite JSON number')
    try:
        finite = math.isfinite(float(value))
    except (OverflowError, ValueError):
        finite = False
    if not finite:
        _fail(label + ' must be a finite JSON number')
    if minimum is not None and value < minimum:
        _fail(label + ' must be >= ' + str(minimum))
    if maximum is not None and value > maximum:
        _fail(label + ' must be <= ' + str(maximum))
    return value


def _integer(value, label, *, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(label + ' must be an integer')
    if minimum is not None and value < minimum:
        _fail(label + ' must be >= ' + str(minimum))
    if maximum is not None and value > maximum:
        _fail(label + ' must be <= ' + str(maximum))
    return value


def _fields(value, expected, label):
    if not isinstance(value, dict):
        _fail(label + ' must be an object')
    unknown = set(value) - set(expected)
    if unknown:
        _fail('Unknown ' + label + ' fields: ' + ', '.join(sorted(unknown)))
    missing = set(expected) - set(value)
    if missing:
        _fail('Missing ' + label + ' fields: ' + ', '.join(sorted(missing)))
    return value


def _finite(value, path='/'):
    if isinstance(value, float) and not math.isfinite(value):
        _fail('Non-finite number at ' + path)
    if isinstance(value, dict):
        for key, child in value.items():
            _finite(child, path + str(key) + '/')
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _finite(child, path + str(index) + '/')


def _descriptor(value, label, *, blend=False):
    _fields(value, ('file', 'expected_sha256'), label)
    result = {'file': _absolute(value['file'], label + '.file'),
              'expected_sha256': _sha(value['expected_sha256'],
                                      label + '.expected_sha256')}
    if blend and Path(result['file']).suffix.lower() != '.blend':
        _fail(label + ' must be a .blend file')
    return result


def _library(value):
    _fields(value, ('file', 'expected_sha256'), 'clip.library')
    result = _descriptor(value, 'clip.library', blend=True)
    if not Path(result['file']).is_file():
        _fail('Frozen clip library not found: ' + result['file'], 'NOT_FOUND')
    return result


def _source(value):
    _fields(value, ('file', 'sha256'), 'clip.source')
    path = _provenance_path(value['file'], 'clip.source.file')
    return {'file': path, 'sha256': _sha(value['sha256'], 'clip.source.sha256')}


def _timing(value):
    _fields(value, ('start', 'end', 'fps', 'fps_base'), 'clip.timing')
    start = _number(value['start'], 'clip.timing.start')
    end = _number(value['end'], 'clip.timing.end')
    if start <= 0 or end <= 0 or end <= start:
        _fail('clip.timing requires finite positive start < end')
    fps = _number(value['fps'], 'clip.timing.fps')
    fps_base = _number(value['fps_base'], 'clip.timing.fps_base')
    if fps <= 0 or fps_base <= 0:
        _fail('clip.timing fps/fps_base must be finite positive numbers')
    return {'start': start, 'end': end, 'fps': fps, 'fps_base': fps_base}


def _actors(value):
    if not isinstance(value, list) or len(value) != len(ACTOR_ROLES):
        _fail('clip.actors must contain exactly two actor roles A and B')
    roles = {}
    for index, row in enumerate(value):
        label = 'clip.actors[' + str(index) + ']'
        _fields(row, ('role', 'collection', 'asset_id', 'template_uuid'), label)
        role = row['role']
        if role not in ACTOR_ROLES:
            _fail('Unknown ' + label + '.role: ' + str(role))
        if role in roles:
            _fail('Duplicate actor role: ' + str(role))
        roles[role] = {
            'role': role,
            'collection': _name(row['collection'], label + '.collection'),
            'asset_id': _uuid(row['asset_id'], label + '.asset_id'),
            'template_uuid': _hex32(row['template_uuid'],
                                    label + '.template_uuid'),
        }
    if set(roles) != set(ACTOR_ROLES):
        _fail('clip.actors must contain exactly roles A and B')
    return [roles[role] for role in ACTOR_ROLES]


def _lights(value):
    if not isinstance(value, list) or len(value) != len(LIGHT_ROLES):
        _fail('clip.lights must contain exactly two light roles block and counter')
    roles = {}
    for index, row in enumerate(value):
        label = 'clip.lights[' + str(index) + ']'
        _fields(row, ('role', 'object', 'asset_id'), label)
        role = row['role']
        if role not in LIGHT_ROLES:
            _fail('Unknown ' + label + '.role: ' + str(role))
        if role in roles:
            _fail('Duplicate light role: ' + str(role))
        roles[role] = {
            'role': role,
            'object': _name(row['object'], label + '.object'),
            'asset_id': _uuid(row['asset_id'], label + '.asset_id'),
        }
    if set(roles) != set(LIGHT_ROLES):
        _fail('clip.lights must contain exactly roles block and counter')
    return [roles[role] for role in LIGHT_ROLES]


def _clip(value):
    expected = ('schema', 'clip_id', 'title', 'library', 'source', 'timing',
                'meters_per_blender_unit', 'actors', 'lights')
    _fields(value, expected, 'clip')
    if value['schema'] != CLIP_SCHEMA:
        _fail('clip.schema must be ' + CLIP_SCHEMA)
    meters = _number(value['meters_per_blender_unit'],
                     'clip.meters_per_blender_unit')
    if meters <= 0:
        _fail('clip.meters_per_blender_unit must be finite positive')
    return {
        'schema': CLIP_SCHEMA,
        'clip_id': _uuid(value['clip_id'], 'clip.clip_id'),
        'title': _name(value['title'], 'clip.title'),
        'library': _library(value['library']),
        'source': _source(value['source']),
        'timing': _timing(value['timing']),
        'meters_per_blender_unit': meters,
        'actors': _actors(value['actors']),
        'lights': _lights(value['lights']),
    }


def _manifest(value):
    expected = ('schema', 'clip', 'instance_name', 'start', 'end', 'scale',
                'translation', 'yaw_quarter_turns')
    if isinstance(value, dict) and 'source_window' in value:
        expected += ('source_window',)
    _fields(value, expected, 'manifest')
    _finite(value)
    if value['schema'] != APPLY_SCHEMA:
        _fail('manifest.schema must be ' + APPLY_SCHEMA)
    start = _number(value['start'], 'manifest.start')
    end = _number(value['end'], 'manifest.end')
    if start <= 0 or end <= 0 or end <= start:
        _fail('manifest requires finite positive start < end')
    scale = _number(value['scale'], 'manifest.scale', minimum=0)
    if scale <= 0:
        _fail('manifest.scale must be greater than zero')
    translation = value['translation']
    if not isinstance(translation, list) or len(translation) != 3:
        _fail('manifest.translation must be a three-number array')
    translation = [_number(item, 'manifest.translation[' + str(index) + ']')
                   for index, item in enumerate(translation)]
    yaw = _integer(value['yaw_quarter_turns'], 'manifest.yaw_quarter_turns',
                   minimum=0, maximum=3)
    result = {
        'schema': APPLY_SCHEMA,
        'clip': _clip(value['clip']),
        'instance_name': _name(value['instance_name'], 'manifest.instance_name',
                               maximum=INSTANCE_LIMIT),
        'start': start,
        'end': end,
        'scale': scale,
        'translation': translation,
        'yaw_quarter_turns': yaw,
    }
    if 'source_window' in value:
        window = value['source_window']
        _fields(window, ('start', 'end'), 'manifest.source_window')
        first = _number(window['start'], 'manifest.source_window.start')
        last = _number(window['end'], 'manifest.source_window.end')
        timing = result['clip']['timing']
        if not timing['start'] <= first < last <= timing['end']:
            _fail('manifest.source_window must lie within frozen clip.timing')
        result['source_window'] = {'start': first, 'end': last}
    return result


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
    """Normalize one ``animation.clip-apply`` params object."""
    result, current = _identity(value)
    unknown = set(result) - set(PARAMS_REQUIRED)
    if unknown:
        _fail('Unknown animation.clip-apply params fields: '
              + ', '.join(sorted(unknown)))
    missing = set(PARAMS_REQUIRED) - set(result)
    if missing:
        _fail('Animation clip-apply params require file, expected_sha256, '
              'manifest and resources; missing: ' + ', '.join(sorted(missing)))
    source = _descriptor({'file': result['file'],
                          'expected_sha256': result['expected_sha256']},
                         'source', blend=True)
    if not Path(source['file']).is_file():
        _fail('Input file not found: ' + source['file'], 'NOT_FOUND')
    manifest = _manifest(result['manifest'])
    library = manifest['clip']['library']
    if os.path.normcase(source['file']) == os.path.normcase(library['file']):
        _fail('Frozen clip library must differ from the destination source')
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
    return {**source, 'resources': normalized_resources, 'manifest': manifest,
            'implementation_sha256': current}


def signature(params):
    body = {key: params.get(key) for key in
            ('file', 'expected_sha256', 'resources', 'manifest',
             'implementation_sha256')}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(',', ':'),
                   ensure_ascii=False, allow_nan=False).encode('utf-8')
    ).hexdigest()


def input_documents(params):
    """Destination, frozen clip library, explicit resources and runtime files.

    ``clip.source`` is provenance only.  It is intentionally absent here: the
    historical source need not exist, while the frozen library does.
    """
    rows = [{'file': params['file'],
             'expected_sha256': params['expected_sha256']},
            dict(params['manifest']['clip']['library'])]
    rows.extend(params.get('resources', []))
    rows.extend(runtime_documents())
    merged = {}
    for row in rows:
        key = os.path.normcase(str(Path(row['file']).resolve()))
        if key in merged and merged[key]['expected_sha256'] != row['expected_sha256']:
            _fail('Conflicting animation clip-apply input hashes: ' + row['file'],
                  'CONFLICT')
        merged[key] = {'file': row['file'],
                       'expected_sha256': row['expected_sha256']}
    return list(merged.values())
