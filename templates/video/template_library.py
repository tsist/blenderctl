"""Declarative SDR recipe compiler; never executes Blender or media commands."""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import sys
import uuid

VERSION = '0.1.0'
ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = Path(os.environ.get('BLENDERCTL_ROOT', str(ROOT.parents[1]))).resolve()
RUNTIME_SHA256 = '4a6e44b18e1187baf1569e0f93f3eda4ae2ffa170a3d73b8c7083c0ca8f38bcb'
PINNED_CATALOG_SHA256 = '9f41f0e04de4404d16c8c10d6bef8da3d59f514a82316d5d0c7252629681862f'
MAX_JSON_BYTES = 16 * 1024 * 1024
_CONTRACT = None


class TemplateError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fail(message, code='INVALID_REQUEST'):
    raise TemplateError(code, message)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def canonical_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _unique_pairs(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            fail('Duplicate JSON key: ' + key)
        value[key] = child
    return value


def read_bound_json(path, expected_sha256):
    descriptor = _descriptor({'file': str(path), 'expected_sha256': expected_sha256})
    file = Path(descriptor['file'])
    with file.open('rb') as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        fail('JSON exceeds 16 MiB', 'RESOURCE_LIMIT')
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        fail('JSON SHA mismatch: ' + str(file), 'CONFLICT')
    try:
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_pairs,
                           parse_constant=lambda value: fail('Non-finite JSON number: ' + value))
    except (UnicodeError, json.JSONDecodeError) as exc:
        fail('Invalid UTF-8 JSON: ' + str(exc))
    return value


def _runtime_identity():
    paths = set((RUNTIME_ROOT / 'tools/blenderctl').glob('*.py'))
    package = RUNTIME_ROOT / 'tools/video_editing_workflow_addon'
    paths.update(package.glob('*.py'))
    paths.add(package / 'blender_manifest.toml')
    rows = [{'file': p.relative_to(RUNTIME_ROOT).as_posix(), 'expected_sha256': sha256_file(p)}
            for p in sorted(paths)]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def runtime_contract():
    global _CONTRACT
    if _CONTRACT is not None:
        if _runtime_identity() != RUNTIME_SHA256:
            fail('Pinned video runtime changed', 'CONFLICT')
        return _CONTRACT
    if _runtime_identity() != RUNTIME_SHA256:
        fail('Pinned video runtime changed', 'CONFLICT')
    # Do this before any local runtime import. Fresh absent prefix prevents reads
    # of stale local bytecode; disabled writes keep the frozen runtime intact.
    prefix = ROOT / ('_absent_pycache_' + uuid.uuid4().hex)
    if prefix.exists():
        fail('Bytecode prefix unexpectedly exists', 'CONFLICT')
    sys.dont_write_bytecode = True
    sys.pycache_prefix = str(prefix)
    for name in ('video_workflow_contract', 'video_identity', 'protocol', 'video_editing_workflow_addon'):
        if name in sys.modules:
            fail('Runtime module was imported before source policy: ' + name, 'CONFLICT')
    sys.path.insert(0, str(RUNTIME_ROOT / 'tools/blenderctl'))
    sys.path.insert(1, str(RUNTIME_ROOT / 'tools'))
    module = importlib.import_module('video_workflow_contract')
    for name in ('video_workflow_contract', 'video_identity', 'protocol',
                 'video_editing_workflow_addon', 'video_editing_workflow_addon.contract'):
        origin = Path(sys.modules[name].__file__).resolve()
        if not origin.is_relative_to(RUNTIME_ROOT.resolve()):
            fail('Runtime import origin conflict: ' + name, 'CONFLICT')
    if module.implementation_sha256() != RUNTIME_SHA256 or prefix.exists():
        fail('Runtime source identity/policy changed', 'CONFLICT')
    _CONTRACT = module
    return module


def obj(properties, required=()):
    return {'type': 'object', 'properties': properties,
            'required': list(required), 'additionalProperties': False}


def arr(items, lo=0, hi=None):
    result = {'type': 'array', 'items': items, 'minItems': lo}
    if hi is not None:
        result['maxItems'] = hi
    return result


def integer(lo, hi=None):
    result = {'type': 'integer', 'minimum': lo}
    if hi is not None:
        result['maximum'] = hi
    return result


def number(lo, hi):
    return {'type': 'number', 'minimum': lo, 'maximum': hi}


ID = {'type': 'string', 'pattern': r'^[A-Za-z][A-Za-z0-9_-]{0,63}$'}
TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 100000}
NAME = {'type': 'string', 'minLength': 1, 'maxLength': 1024}
SHA = {'type': 'string', 'pattern': r'^[0-9a-f]{64}$'}
FILE = obj({'file': {'type': 'string', 'minLength': 1, 'maxLength': 32767}, 'expected_sha256': SHA}, ('file', 'expected_sha256'))
COLOR = arr(number(0, 1), 4, 4)
FIT = {'enum': ['FIT', 'CROP', 'ORIGINAL']}
TEXT_STYLE = obj({'font_ratio': number(0.001, 1), 'y_ratio': number(-0.5, 0.5), 'color': COLOR}, ('font_ratio', 'y_ratio', 'color'))
STYLE = obj({'background_color': COLOR, 'show_shot_labels': {'type': 'boolean'},
             'label_prefix': {'type': 'string', 'maxLength': 1024}, 'fit': FIT,
             'transition_frames': {'type': 'integer', 'const': 0},
             'grade': obj({'multiply': number(0, 20), 'saturation': number(0, 20)}, ('multiply', 'saturation')),
             'title': TEXT_STYLE, 'label': TEXT_STYLE, 'subtitle': TEXT_STYLE},
            ('background_color', 'show_shot_labels', 'label_prefix', 'fit', 'transition_frames', 'grade', 'title', 'label', 'subtitle'))
TEMPLATE_SCHEMA = obj({'schema_version': {'const': 'video-template/1.0'}, 'id': ID,
                       'name': NAME, 'description': TEXT, 'layout': {'const': 'SEQUENCE'}, 'style': STYLE},
                      ('schema_version', 'id', 'name', 'description', 'layout', 'style'))
CATALOG_SCHEMA = obj({'schema_version': {'const': 'video-template-catalog/1.0'}, 'library_version': {'const': VERSION},
                      'templates': arr(obj({'id': ID, 'name': NAME, 'path': NAME, 'sha256': SHA}, ('id', 'name', 'path', 'sha256')), 1, 1000)},
                     ('schema_version', 'library_version', 'templates'))


def schemas():
    c = runtime_contract().c
    source = copy.deepcopy(c.SOURCE)
    single_source = copy.deepcopy(c.SINGLE_SOURCE)
    sound_source = copy.deepcopy(c.SOUND_SOURCE)
    sound_source['properties']['speed'] = {'type': 'number', 'const': 1}
    sound_source['properties']['freeze'] = {'type': 'boolean', 'const': False}
    output = copy.deepcopy(c.OUTPUT_SCHEMA)
    output['properties'].pop('hdr')
    output['properties']['color_mode'] = {'enum': ['SDR']}
    output['allOf'] = [output['allOf'][1]]
    variants = []
    for kind in ('IMAGE', 'MOVIE', 'COLOR'):
        fields = {'id': ID, 'kind': {'const': kind}, 'duration': integer(1, 2097148),
                  'candidate_id': ID, 'name': NAME, 'fit': FIT}
        if kind == 'COLOR':
            fields['color'] = COLOR
            required = ('id', 'kind', 'duration', 'color')
        else:
            kind_source = source if kind == 'IMAGE' else single_source
            fields.update(source=kind_source,
                          alternatives=arr(obj({'candidate_id': ID, 'source': kind_source}, ('candidate_id', 'source'))))
            required = ('id', 'kind', 'duration', 'source')
        variants.append(obj(fields, required))
    parameters = obj({'workflow_id': ID, 'scene': NAME, 'fps': arr(integer(1, 1000000), 2, 2),
                      'width': integer(1, 65536), 'height': integer(1, 65536),
                      'frame_start': integer(-1048574, 1048574), 'shots': arr({'oneOf': variants}, 1, 1000),
                      'font': FILE, 'title': TEXT, 'title_duration': integer(1, 2097148),
                      'subtitles': arr(obj({'id': ID, 'offset': integer(0, 2097148), 'duration': integer(1, 2097148), 'text': TEXT},
                                           ('id', 'offset', 'duration', 'text')), 0, 4096),
                      'audio_tracks': arr(obj({'id': ID, 'offset': integer(0, 2097148), 'duration': integer(1, 2097148),
                                              'source': sound_source, 'volume': number(0, 1000),
                                              'fade_in': integer(0, 2097148), 'fade_out': integer(0, 2097148)},
                                             ('id', 'offset', 'duration', 'source')), 0, 4096),
                      'output': output},
                     ('workflow_id', 'scene', 'fps', 'width', 'height', 'frame_start', 'shots'))
    recipe = obj({'context': obj({'file': FILE['properties']['file'], 'expected_sha256': SHA,
                                'resources': arr(FILE, 0, 100000), 'media_probe': FILE},
                               ('file', 'expected_sha256', 'resources')), 'parameters': parameters}, ('context', 'parameters'))
    template_ref = {'oneOf': [obj({'id': ID}, ('id',)), FILE]}
    batch = obj({'schema_version': {'const': 'video-template-batch/1.0'},
                 'members': arr(obj({'id': ID, 'template': template_ref, 'input': FILE}, ('id', 'template', 'input')), 1, 16)},
                ('schema_version', 'members'))
    recipe['description'] = 'Structural first layer only; compiler semantic preflight and final pinned video.run normalization are mandatory.'
    return {'template': copy.deepcopy(TEMPLATE_SCHEMA), 'input': recipe, 'batch': batch, 'catalog': copy.deepcopy(CATALOG_SCHEMA)}


def validate(value, schema, path='$'):
    if 'oneOf' in schema:
        matches = 0
        for branch in schema['oneOf']:
            try:
                validate(value, branch, path)
                matches += 1
            except TemplateError:
                pass
        if matches != 1:
            fail(path + ': expected exactly one schema branch')
        return
    if 'const' in schema:
        expected = schema['const']
        numeric_const = schema.get('type') == 'number' and isinstance(value, (int, float)) and not isinstance(value, bool)
        if (not numeric_const and type(value) is not type(expected)) or value != expected:
            fail(path + ': wrong constant')
    if 'enum' in schema and not any(type(value) is type(item) and value == item for item in schema['enum']):
        fail(path + ': unsupported value')
    kind = schema.get('type')
    if kind == 'object':
        if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
            fail(path + ': expected string-key object')
        unknown = set(value) - set(schema['properties'])
        missing = set(schema.get('required', ())) - set(value)
        if unknown or missing:
            fail(path + ': unknown ' + str(sorted(unknown)) + '; missing ' + str(sorted(missing)))
        for key, item in value.items():
            validate(item, schema['properties'][key], path + '.' + key)
    elif kind == 'array':
        if not isinstance(value, list) or not schema.get('minItems', 0) <= len(value) <= schema.get('maxItems', math.inf):
            fail(path + ': array length/type invalid')
        for index, item in enumerate(value):
            validate(item, schema['items'], path + '[' + str(index) + ']')
        if schema.get('uniqueItems') and len({canonical_bytes(x) for x in value}) != len(value):
            fail(path + ': duplicate array items')
    elif kind == 'string':
        if not isinstance(value, str) or '\x00' in value or not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', math.inf):
            fail(path + ': invalid string')
        try:
            value.encode('utf-8')
        except UnicodeEncodeError:
            fail(path + ': isolated surrogate is not valid UTF-8 text')
        if 'pattern' in schema and re.fullmatch(schema['pattern'], value) is None:
            fail(path + ': string format invalid')
    elif kind in ('integer', 'number'):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            fail(path + ': numeric type invalid')
        if kind == 'integer' and not isinstance(value, int):
            fail(path + ': integer required')
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite or value < schema.get('minimum', -math.inf) or value > schema.get('maximum', math.inf):
            fail(path + ': finite bounded number required')
        if 'exclusiveMinimum' in schema and value <= schema['exclusiveMinimum']:
            fail(path + ': positive number required')
    elif kind == 'boolean' and not isinstance(value, bool):
        fail(path + ': boolean required')


def _descriptor(value):
    validate(value, FILE, 'descriptor')
    path = Path(value['file'])
    if not path.is_absolute():
        fail('Absolute file required: ' + value['file'])
    return {'file': str(path.resolve()), 'expected_sha256': value['expected_sha256']}


def verify_descriptor(value):
    result = _descriptor(value)
    if not Path(result['file']).is_file():
        fail('Bound input is not a file: ' + result['file'], 'NOT_FOUND')
    if sha256_file(result['file']) != result['expected_sha256']:
        fail('Bound input SHA mismatch: ' + result['file'], 'CONFLICT')
    return result


def validate_template(template):
    if isinstance(template, dict) and isinstance(template.get('style'), dict):
        transition = template['style'].get('transition_frames')
        if isinstance(transition, int) and not isinstance(transition, bool) and transition != 0:
            fail('Nonzero transitions await native qualification; use transition_frames=0', 'UNSUPPORTED')
    validate(template, TEMPLATE_SCHEMA, 'template')
    if template['style']['transition_frames'] != 0:
        fail('Nonzero transitions await native qualification; use transition_frames=0', 'UNSUPPORTED')
    if template['style']['background_color'][3] != 1:
        fail('Native COLOR background requires alpha=1; opacity is outside this template schema', 'UNSUPPORTED')
    return copy.deepcopy(template)


def load_catalog(library_dir=ROOT):
    library_dir = Path(library_dir).resolve()
    if library_dir != ROOT:
        fail('Built-in catalog only loads from pinned compiler root; custom recipes use explicit file+SHA', 'UNSUPPORTED')
    if PINNED_CATALOG_SHA256 == 'PENDING':
        fail('Catalog pin not finalized', 'CONFLICT')
    catalog = read_bound_json(library_dir / 'catalog.json', PINNED_CATALOG_SHA256)
    validate(catalog, CATALOG_SCHEMA, 'catalog')
    ids = set()
    for row in catalog['templates']:
        if row['id'] in ids:
            fail('Duplicate catalog id')
        ids.add(row['id'])
        path = (library_dir / row['path']).resolve()
        if Path(row['path']).is_absolute() or not path.is_relative_to(library_dir / 'templates'):
            fail('Catalog template path escaped templates directory')
        template = validate_template(read_bound_json(path, row['sha256']))
        if template['id'] != row['id'] or template['name'] != row['name']:
            fail('Catalog/template identity differs', 'CONFLICT')
    return catalog


def load_template(template_id=None, *, template_file=None, expected_template_sha256=None):
    if template_id is not None:
        if template_file is not None or expected_template_sha256 is not None:
            fail('Choose built-in id or explicit template file+SHA')
        catalog = load_catalog()
        rows = [r for r in catalog['templates'] if r['id'] == template_id]
        if len(rows) != 1:
            fail('Unknown built-in template: ' + str(template_id), 'NOT_FOUND')
        row = rows[0]
        descriptor = {'file': str((ROOT / row['path']).resolve()), 'expected_sha256': row['sha256']}
    else:
        if template_file is None or expected_template_sha256 is None:
            fail('Explicit template requires file and expected SHA')
        descriptor = _descriptor({'file': str(template_file), 'expected_sha256': expected_template_sha256})
    return validate_template(read_bound_json(descriptor['file'], descriptor['expected_sha256'])), descriptor


def _bound_descriptors(value):
    if isinstance(value, dict):
        if 'file' in value and 'expected_sha256' in value:
            yield {k: value[k] for k in ('file', 'expected_sha256')}
        for key, child in value.items():
            if key not in ('file', 'expected_sha256'):
                yield from _bound_descriptors(child)
    elif isinstance(value, list):
        for child in value:
            yield from _bound_descriptors(child)


def _strip_id(category, identity):
    return category + '_' + hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]


def _unique_ids(rows, label):
    values = [r['id'] for r in rows]
    if len(values) != len(set(values)):
        fail('Duplicate ' + label + ' id')


def compile_recipe(template, recipe):
    """Return ordinary normalized video.run params; validation is not execution."""
    template = validate_template(template)
    validate(recipe, schemas()['input'], 'input')
    recipe = copy.deepcopy(recipe)
    context, p, style = recipe['context'], recipe['parameters'], template['style']
    _unique_ids(p['shots'], 'shot')
    _unique_ids(p.get('subtitles', []), 'subtitle')
    _unique_ids(p.get('audio_tracks', []), 'audio')
    if 'title_duration' in p and 'title' not in p:
        fail('title_duration requires title')
    if p.get('output', {}).get('color_mode', 'SDR') != 'SDR' or 'hdr' in p.get('output', {}):
        fail('Template library supports SDR output only', 'UNSUPPORTED')
    if p.get('output', {}).get('format') == 'MP4' and not all(k in p['output'] for k in ('ffmpeg', 'ffprobe')):
        fail('MP4 output requires explicit SHA-bound ffmpeg and ffprobe descriptors')
    start = p['frame_start']
    total = sum(row['duration'] for row in p['shots'])
    end = start + total
    clips = [{'id': 'template_background', 'kind': 'COLOR', 'channel': 1,
              'start': start, 'duration': total, 'color': style['background_color']}]
    cursor = start
    for row in p['shots']:
        if row['kind'] == 'COLOR' and row['color'][3] != 1:
            fail('Native COLOR shot requires alpha=1', 'UNSUPPORTED')
        if row['kind'] == 'IMAGE':
            for source in [row['source']] + [a['source'] for a in row.get('alternatives', [])]:
                parents = {str(Path(_descriptor(r)['file']).parent).casefold() for r in source['files']}
                if len(parents) != 1:
                    fail('IMAGE sequence requires one common directory, including alternatives', 'UNSUPPORTED')
        clip = {k: copy.deepcopy(v) for k, v in row.items() if k not in ('id', 'duration')}
        clip.update(id=_strip_id('shot', row['id']), shot_id=row['id'], channel=2,
                    start=cursor, duration=row['duration'], grade=copy.deepcopy(style['grade']))
        clip.setdefault('fit', style['fit'])
        clips.append(clip)
        if style['show_shot_labels']:
            text = (style['label_prefix'] + ' ' + row['id']).strip()
            clips.append(_text_clip(p, style['label'], _strip_id('label', row['id']), text, cursor, row['duration'], 20))
        cursor += row['duration']
    if 'title' in p:
        duration = p.get('title_duration', p['shots'][0]['duration'])
        if duration > total:
            fail('Title exceeds timeline')
        clips.append(_text_clip(p, style['title'], 'template_title', p['title'], start, duration, 21))
    # Reuse a subtitle channel only after its previous half-open interval ends.
    channels = {channel: start for channel in range(30, 50)}
    for row in sorted(p.get('subtitles', []), key=lambda r: (r['offset'], r['id'])):
        left, right = _interval(row, start, end, 'subtitle')
        available = [channel for channel, free in channels.items() if free <= left]
        if not available:
            fail('More than 20 concurrent subtitles', 'RESOURCE_LIMIT')
        channel = min(available)
        channels[channel] = right
        clips.append(_text_clip(p, style['subtitle'], _strip_id('subtitle', row['id']), row['text'], left, row['duration'], channel))
    audio_channels = {channel: start for channel in range(64, 96)}
    for row in sorted(p.get('audio_tracks', []), key=lambda r: (r['offset'], r['id'])):
        left, right = _interval(row, start, end, 'audio')
        source = row['source']
        if source.get('speed', 1) != 1 or source.get('freeze', False):
            fail('Audio speed/freeze unsupported in template library', 'UNSUPPORTED')
        available = [channel for channel, free in audio_channels.items() if free <= left]
        if not available:
            fail('More than 32 concurrent audio tracks', 'RESOURCE_LIMIT')
        channel = min(available)
        audio_channels[channel] = right
        clip = {k: copy.deepcopy(v) for k, v in row.items() if k not in ('id', 'offset')}
        clip.update(id=_strip_id('audio', row['id']), kind='SOUND', start=left, channel=channel)
        clips.append(clip)
    if len({c['id'] for c in clips}) != len(clips):
        fail('Generated strip id collision', 'CONFLICT')
    validate_channel_intervals(clips)
    resource_map = {}
    for descriptor in context['resources'] + list(_bound_descriptors(p)):
        descriptor = _descriptor(descriptor)
        key = descriptor['file'].casefold()
        old = resource_map.get(key)
        if old and old['expected_sha256'] != descriptor['expected_sha256']:
            fail('Conflicting resource hashes: ' + descriptor['file'], 'CONFLICT')
        resource_map[key] = descriptor
    source = verify_descriptor({k: context[k] for k in ('file', 'expected_sha256')})
    if Path(source['file']).suffix.lower() != '.blend':
        fail('Source must be .blend')
    if 'media_probe' in context:
        descriptor = _descriptor(context['media_probe'])
        key = descriptor['file'].casefold()
        old = resource_map.get(key)
        if old and old['expected_sha256'] != descriptor['expected_sha256']:
            fail('Conflicting media probe hash', 'CONFLICT')
        resource_map[key] = descriptor
    resources = [verify_descriptor(row) for row in resource_map.values()]
    params = {**source, 'resources': resources, 'implementation_sha256': RUNTIME_SHA256,
              'manifest': {'version': '1.0', 'action': 'create', 'workflow_id': p['workflow_id'],
                           'scene': p['scene'], 'timeline': {'fps': p['fps'], 'width': p['width'], 'height': p['height'],
                                                          'frame_start': start, 'frame_end_exclusive': end}, 'clips': clips}}
    if 'media_probe' in context:
        params['media_probe'] = context['media_probe']
    if 'output' in p:
        params['output'] = p['output']
        if params['output'].get('shots') and 'shot_ids' not in params['output']:
            params['output']['shot_ids'] = [r['id'] for r in p['shots']]
    try:
        return runtime_contract().normalize_command('video.run', params)
    except Exception as exc:
        if isinstance(exc, TemplateError):
            raise
        fail(str(exc), getattr(exc, 'code', 'INVALID_REQUEST'))


def _interval(row, start, end, label):
    left = start + row['offset']
    right = left + row['duration']
    if not start <= left < right <= end:
        fail(label + ' interval exceeds timeline: ' + row['id'])
    return left, right


def validate_channel_intervals(clips):
    """Native core rejects any overlapping half-open strips on one channel."""
    channels = {}
    for clip in clips:
        channels.setdefault(clip['channel'], []).append(clip)
    for channel, rows in channels.items():
        previous_end = None
        for row in sorted(rows, key=lambda r: (r['start'], r['id'])):
            if previous_end is not None and row['start'] < previous_end:
                fail('Overlapping half-open strips on channel ' + str(channel), 'CONFLICT')
            previous_end = row['start'] + row['duration']


def _text_clip(p, style, identity, text, start, duration, channel):
    if 'font' not in p:
        fail('Explicit SHA-bound font required for generated text')
    font_size = max(1, round(p['height'] * style['font_ratio']))
    if font_size > 4096:
        fail('Generated font size exceeds native contract', 'RESOURCE_LIMIT')
    return {'id': identity, 'kind': 'TEXT', 'channel': channel, 'start': start, 'duration': duration,
            'text': text, 'font': p['font'], 'font_size': font_size, 'color': style['color'],
            'transform': {'x': 0, 'y': p['height'] * style['y_ratio']}}


def _write_json(path, value):
    serialized = canonical_bytes(value)
    with Path(path).open('xb') as stream:
        stream.write(serialized)


def _filesystem_component(identity):
    """Only filesystem destinations need the extra Windows device-name rule."""
    if re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', identity, flags=re.IGNORECASE):
        fail('Windows reserved device name cannot be a filesystem component: ' + identity)


def _record_failure(directory, exc):
    try:
        _write_json(directory / 'FAILED.json', {'status': 'failed', 'code': getattr(exc, 'code', 'IO_ERROR'), 'message': str(exc)})
    except Exception:
        # A full disk or malformed exception text may also prevent the marker.
        # Keep the original failure; missing final receipt still rejects package.
        pass


def _new_output(output_dir):
    output_dir = Path(output_dir)
    if not output_dir.is_absolute():
        fail('Output directory must be absolute')
    output_dir = output_dir.resolve()
    if output_dir.is_relative_to(RUNTIME_ROOT.resolve()):
        fail('Output cannot modify frozen runtime', 'CONFLICT')
    if output_dir.exists():
        fail('Output directory already exists; no overwrite', 'CONFLICT')
    if not output_dir.parent.is_dir():
        fail('Output parent must already exist', 'NOT_FOUND')
    output_dir.mkdir()
    return output_dir


def _package(output_dir, template, template_descriptor, recipe, input_descriptor, params):
    request = sys.modules['protocol'].validate({'schema_version': '1.0', 'command': 'video.run', 'params': params})
    _write_json(output_dir / 'request.json', request)
    _write_json(output_dir / 'recipe-used.json', {'template': template, 'input': recipe})
    receipt = {'schema_version': 'video-template-build/1.0', 'compiler_version': VERSION,
               'status': 'compiled', 'execution_status': 'not_run',
               'runtime_implementation_sha256': RUNTIME_SHA256,
               'template': template_descriptor, 'input': input_descriptor,
               'template_id': template['id'], 'request_sha256': sha256_file(output_dir / 'request.json'),
               'recipe_used_sha256': sha256_file(output_dir / 'recipe-used.json'),
               'compiler_sources': [{'file': str(ROOT / name), 'expected_sha256': sha256_file(ROOT / name)}
                                    for name in ('template_library.py', 'cli.py')],
               'source_policy': {'dont_write_bytecode': sys.dont_write_bytecode, 'pycache_prefix': sys.pycache_prefix,
                                 'prefix_absent': not Path(sys.pycache_prefix).exists()},
               'verification': {'declared_files_exist_and_sha_checked': True,
                                'ordinary_video_run_contract_normalized': True,
                                'actual_blend_dependency_closure': 'not_run',
                                'new_scene_name_available_in_blend': 'not_run',
                                'media_decode_duration_glyphs_native_execution': 'not_run'},
               'consumer_rule': 'Accept only a complete final build-receipt and no FAILED.json in this package or enclosing batch; execution_status remains not_run.',
               'limitations': ['SDR SEQUENCE only', 'transition_frames=0', 'Public Host checks actual source dependency closure and executes request',
                               'Text is positioned with fixed ratios; arbitrary long text is not automatically fitted']}
    _write_json(output_dir / 'build-receipt.json', receipt)
    return receipt


def compile_to_directory(*, input_file, expected_input_sha256, output_dir,
                         template_id=None, template_file=None, expected_template_sha256=None):
    template, template_descriptor = load_template(template_id, template_file=template_file,
                                                  expected_template_sha256=expected_template_sha256)
    input_descriptor = _descriptor({'file': str(input_file), 'expected_sha256': expected_input_sha256})
    recipe = read_bound_json(input_descriptor['file'], expected_input_sha256)
    params = compile_recipe(template, recipe)
    directory = _new_output(output_dir)
    try:
        return _package(directory, template, template_descriptor, recipe, input_descriptor, params)
    except Exception as exc:
        _record_failure(directory, exc)
        raise


def save_template(*, template_file, expected_template_sha256, library_dir):
    template, source = load_template(template_file=template_file, expected_template_sha256=expected_template_sha256)
    _filesystem_component(template['id'])
    directory = Path(library_dir)
    if not directory.is_absolute() or not directory.is_dir():
        fail('Custom library directory must be an existing absolute directory')
    if directory.resolve().is_relative_to(RUNTIME_ROOT.resolve()) or directory.resolve() == ROOT or directory.resolve().is_relative_to(ROOT / 'templates'):
        fail('Custom save cannot alter frozen runtime or built-in compiler root', 'CONFLICT')
    destination = directory / (template['id'] + '.json')
    if destination.exists():
        fail('Template already exists; no overwrite', 'CONFLICT')
    _write_json(destination, template)
    return {'status': 'saved', 'template_id': template['id'], 'source': source,
            'saved': {'file': str(destination.resolve()), 'expected_sha256': sha256_file(destination)}}


def compile_batch(*, batch_file, expected_batch_sha256, output_dir):
    batch_descriptor = _descriptor({'file': str(batch_file), 'expected_sha256': expected_batch_sha256})
    batch = read_bound_json(batch_descriptor['file'], expected_batch_sha256)
    validate(batch, schemas()['batch'], 'batch')
    _unique_ids(batch['members'], 'batch member')
    if len({row['id'].casefold() for row in batch['members']}) != len(batch['members']):
        fail('Batch member directories collide case-insensitively', 'CONFLICT')
    for row in batch['members']:
        _filesystem_component(row['id'])
    prepared = []
    for row in batch['members']:
        reference = row['template']
        if 'id' in reference:
            template, template_descriptor = load_template(reference['id'])
        else:
            template, template_descriptor = load_template(template_file=reference['file'], expected_template_sha256=reference['expected_sha256'])
        input_descriptor = _descriptor(row['input'])
        recipe = read_bound_json(input_descriptor['file'], input_descriptor['expected_sha256'])
        params = compile_recipe(template, recipe)
        prepared.append((row['id'], template, template_descriptor, recipe, input_descriptor, params))
    directory = _new_output(output_dir)
    receipts = []
    try:
        for identity, template, desc, recipe, input_desc, params in prepared:
            member_dir = directory / identity
            member_dir.mkdir()
            receipt = _package(member_dir, template, desc, recipe, input_desc, params)
            receipts.append({'id': identity, 'request_sha256': receipt['request_sha256'], 'status': 'compiled'})
        receipt = {'schema_version': 'video-template-batch-build/1.0', 'status': 'compiled',
                   'execution_status': 'not_run', 'batch': batch_descriptor, 'members': receipts,
                   'consumer_rule': 'Accept only a complete final batch-receipt with complete member build-receipts and no FAILED.json in batch or members.'}
        _write_json(directory / 'batch-receipt.json', receipt)
        return receipt
    except Exception as exc:
        _record_failure(directory, exc)
        raise
