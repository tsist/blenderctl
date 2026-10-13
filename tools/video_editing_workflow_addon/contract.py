"""Host-safe strict video editing contracts; no bpy or third-party dependency.

JSON Schema validates structure. normalize_* additionally checks identity,
half-open intervals, source sampling, resource closure and full render budgets.
HDR10_PQ is representable but requires separate worker qualification.
"""
from copy import deepcopy
from pathlib import Path
import math
import re

SCHEMA_VERSION = '1.0'
FRAME_MIN, FRAME_MAX = -1048574, 1048574


class WorkflowError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def obj(properties, required=()):
    return {'type': 'object', 'properties': properties,
            'required': list(required), 'additionalProperties': False}


def arr(items, minimum=0, maximum=None):
    result = {'type': 'array', 'items': items, 'minItems': minimum}
    if maximum is not None:
        result['maxItems'] = maximum
    return result


def integer(lo, hi=None):
    result = {'type': 'integer', 'minimum': lo}
    if hi is not None:
        result['maximum'] = hi
    return result


def number(lo=None, hi=None, positive=False):
    result = {'type': 'number'}
    if lo is not None:
        result['minimum'] = lo
    if hi is not None:
        result['maximum'] = hi
    if positive:
        result['exclusiveMinimum'] = 0
    return result


NAME = {'type': 'string', 'minLength': 1, 'maxLength': 1024}
ID = {'type': 'string', 'pattern': r'^[A-Za-z][A-Za-z0-9_-]{0,63}$'}
SHA = {'type': 'string', 'pattern': r'^[0-9a-f]{64}$'}
BOOL = {'type': 'boolean'}
PATH = {'type': 'string', 'minLength': 1, 'maxLength': 32767,
        'description': 'Absolute local path; runtime checks path and resource closure.'}
FILE = obj({'file': PATH, 'expected_sha256': SHA}, ('file', 'expected_sha256'))
FRAME = integer(FRAME_MIN, FRAME_MAX)
DURATION = integer(1, FRAME_MAX - FRAME_MIN)
COLOR = arr(number(0, 1), 4, 4)
TRANSFORM = obj({'x': number(-1e9, 1e9), 'y': number(-1e9, 1e9),
                 'scale_x': number(positive=True), 'scale_y': number(positive=True),
                 'rotation_degrees': number(-360000, 360000)})
CROP = obj({key: integer(0, 2147483647) for key in ('left', 'right', 'top', 'bottom')})
GRADE = obj({'multiply': number(0, 20), 'saturation': number(0, 20)})
SOURCE_FIELDS = {'files': arr(FILE, 1), 'source_in': integer(0, FRAME_MAX - FRAME_MIN),
                 'speed': number(positive=True), 'freeze': BOOL,
                 'stream': integer(0, 2147483647), 'color_space': NAME}
SOURCE = obj(SOURCE_FIELDS, ('files',))
SINGLE_SOURCE = obj({**SOURCE_FIELDS, 'files': arr(FILE, 1, 1)}, ('files',))
SOUND_SOURCE = obj({**SOURCE_FIELDS, 'files': arr(FILE, 1, 1), 'freeze': {'const': False, 'type': 'boolean'}}, ('files',))
TIMELINE = obj({'fps': arr(integer(1, 1000000), 2, 2),
                'width': integer(1, 65536), 'height': integer(1, 65536),
                'frame_start': FRAME, 'frame_end_exclusive': integer(FRAME_MIN + 1, FRAME_MAX + 1)},
               ('fps', 'width', 'height', 'frame_start', 'frame_end_exclusive'))
COMMON = {'id': ID, 'kind': {'enum': ['COLOR', 'IMAGE', 'MOVIE', 'SOUND', 'TEXT']},
          'channel': integer(1, 128), 'start': FRAME, 'duration': DURATION,
          'shot_id': ID, 'name': NAME, 'lock': BOOL, 'mute': BOOL,
          'candidate_id': ID, 'fade_in': integer(0, FRAME_MAX - FRAME_MIN),
          'fade_out': integer(0, FRAME_MAX - FRAME_MIN)}
VISUAL = {'opacity': number(0, 1), 'transform': TRANSFORM, 'crop': CROP, 'grade': GRADE,
          'fit': {'enum': ['FIT', 'CROP', 'ORIGINAL'],
                  'description': 'CROP uses native FILL. Explicit transform scales are native absolute values; media replacement without transform preserves relative fit scale.'}}
KIND_FIELDS = {
    'COLOR': {'color': COLOR, **VISUAL},
    'IMAGE': {'source': SOURCE, **VISUAL},
    'MOVIE': {'source': SINGLE_SOURCE, **VISUAL},
    'SOUND': {'source': SOUND_SOURCE, 'volume': number(0, 1000), 'pan': number(-1, 1)},
    'TEXT': {'text': {'type': 'string', 'minLength': 1, 'maxLength': 100000},
             'font': FILE, 'font_size': integer(1, 4096), 'color': COLOR, **VISUAL},
}
CLIP_VARIANTS = []
for _kind, _fields in KIND_FIELDS.items():
    if _kind in ('IMAGE', 'MOVIE', 'SOUND'):
        _fields['alternatives'] = arr(obj({'candidate_id': ID, 'source': _fields['source']}, ('candidate_id', 'source')))
    _required = ['id', 'kind', 'channel', 'start', 'duration']
    _required += ['source'] if _kind in ('IMAGE', 'MOVIE', 'SOUND') else ['text'] if _kind == 'TEXT' else ['color']
    CLIP_VARIANTS.append(obj({**COMMON, **_fields, 'kind': {'const': _kind}}, _required))
CLIP = {'oneOf': CLIP_VARIANTS}
CLIP_SCHEMA = CLIP
CHANGE_FIELDS = {k: v for k, v in COMMON.items() if k not in ('id', 'kind')}
for _fields in KIND_FIELDS.values():
    CHANGE_FIELDS.update(_fields)
# Unknown target kind is checked against the actual clip by core before mutation.
CHANGE_FIELDS['source'] = SOURCE
CHANGE_FIELDS['alternatives'] = arr(obj({'candidate_id': ID, 'source': SOURCE}, ('candidate_id', 'source')))
CHANGES = {**obj(CHANGE_FIELDS), 'minProperties': 1}
EDIT = {'oneOf': [
    obj({'op': {'const': 'add'}, 'clip': CLIP}, ('op', 'clip')),
    obj({'op': {'const': 'update'}, 'id': ID, 'changes': CHANGES}, ('op', 'id', 'changes')),
    obj({'op': {'const': 'remove'}, 'id': ID}, ('op', 'id')),
    obj({'op': {'const': 'select_candidate'}, 'id': ID, 'candidate_id': ID}, ('op', 'id', 'candidate_id')),
]}
BASE_MANIFEST = {'version': {'const': SCHEMA_VERSION}, 'action': {'enum': ['create', 'patch']},
                 'workflow_id': ID, 'scene': NAME, 'timeline': TIMELINE}
MANIFEST_SCHEMA = {'oneOf': [
    obj({**BASE_MANIFEST, 'action': {'const': 'create'}, 'clips': arr(CLIP, 1)},
        ('version', 'action', 'workflow_id', 'scene', 'timeline', 'clips')),
    obj({**BASE_MANIFEST, 'action': {'const': 'patch'}, 'edits': arr(EDIT, 1)},
        ('version', 'action', 'workflow_id', 'scene', 'edits')),
]}
HDR_POLICY = obj({'ocio': FILE,
    'mapping': {'type': 'string', 'const': 'STANDARD_100'},
    'reference_white_nits': {'type': 'integer', 'const': 100},
    'exposure': {'type': 'integer', 'const': 0},
    'mastering': {'type': 'string', 'const': 'VIRTUAL_BT2020_10000NIT'},
    'alpha': {'type': 'string', 'const': 'REQUIRE_OPAQUE'}},
    ('ocio', 'mapping', 'reference_white_nits', 'exposure', 'mastering', 'alpha'))
PREVIEW = obj({'proxies': arr(obj({'clip_id': ID, 'source_files': arr(FILE, 1), 'files': arr(FILE, 1)},
                                      ('clip_id', 'source_files', 'files')), 1)}, ('proxies',))
OUTPUT_SCHEMA = obj({'format': {'enum': ['PNG', 'MP4']}, 'frames': arr(integer(FRAME_MIN, FRAME_MAX + 1), 2, 2),
    'width': {**integer(1, 65536), 'description': 'Optional exact integer-percent downscale of authored canvas; use timeline+fit edits for another canvas.'},
    'height': integer(1, 65536), 'shots': BOOL, 'preview': PREVIEW,
    'shot_ids': {**arr(ID, 1, 4096), 'uniqueItems': True,
                 'description': 'Explicit active visual shot_id selection; only valid with shots=true. Missing clip shot_id falls back to clip id; TEXT/SOUND are not shots.'},
    'subtitles': BOOL, 'audio': BOOL, 'max_pixel_frames': integer(1),
    'ffmpeg': FILE, 'ffprobe': FILE,
    'hdr': HDR_POLICY,
    'color_mode': {'enum': ['SDR', 'HDR10_PQ'],
                   'description': 'HDR10_PQ requires explicit Standard100, active SHA-bound OCIO, virtual mastering and opaque output; technical qualification is separate from device viewing.'}},
    ('format', 'max_pixel_frames'))
OUTPUT_SCHEMA['allOf'] = [{'if': {'properties': {'color_mode': {'const': 'HDR10_PQ'}}, 'required': ['color_mode']},
    'then': {'properties': {'format': {'const': 'MP4'}}, 'required': ['hdr', 'ffmpeg', 'ffprobe']},
    'else': {'not': {'required': ['hdr']}}},
    {'if': {'required': ['shot_ids']}, 'then': {'required': ['shots'], 'properties': {'shots': {'const': True}}}}]
RUN_PARAMS_SCHEMA = obj({'file': PATH, 'expected_sha256': SHA, 'resources': arr(FILE),
    'manifest': MANIFEST_SCHEMA, 'output': OUTPUT_SCHEMA, 'resume': FILE, 'media_probe': FILE,
    'implementation_sha256': SHA}, ('file', 'expected_sha256', 'resources', 'manifest'))
INSPECT_PARAMS_SCHEMA = obj({'file': PATH, 'expected_sha256': SHA, 'resources': arr(FILE),
    'scene': NAME, 'workflow_id': ID, 'implementation_sha256': SHA},
    ('file', 'expected_sha256', 'resources', 'scene'))


def _fail(message):
    raise WorkflowError('INVALID_REQUEST', message)


def _validate(value, schema, path='$'):
    """Validate the JSON Schema subset emitted here without external packages."""
    if 'oneOf' in schema:
        matches = 0
        errors = []
        for branch in schema['oneOf']:
            try:
                _validate(value, branch, path)
                matches += 1
            except WorkflowError as exc:
                errors.append(str(exc))
        if matches != 1:
            _fail(path + ': expected exactly one schema branch; ' + ' | '.join(errors))
        return value
    if 'const' in schema and (type(value) is not type(schema['const']) or value != schema['const']):
        _fail(path + ': wrong constant')
    if 'enum' in schema and not any(type(value) is type(item) and value == item for item in schema['enum']):
        _fail(path + ': unsupported value')
    kind = schema.get('type')
    if kind == 'object':
        if not isinstance(value, dict):
            _fail(path + ': expected object')
        if any(not isinstance(key, str) for key in value):
            _fail(path + ': object keys must be strings')
        unknown = set(value) - set(schema['properties'])
        if unknown:
            _fail(path + ': unknown fields: ' + ', '.join(sorted(unknown)))
        missing = set(schema.get('required', ())) - set(value)
        if missing:
            _fail(path + ': missing fields: ' + ', '.join(sorted(missing)))
        if len(value) < schema.get('minProperties', 0):
            _fail(path + ': empty object')
        for key, item in value.items():
            _validate(item, schema['properties'][key], path + '.' + key)
    elif kind == 'array':
        if not isinstance(value, list):
            _fail(path + ': expected array')
        if len(value) < schema.get('minItems', 0) or ('maxItems' in schema and len(value) > schema['maxItems']):
            _fail(path + ': array length outside bounds')
        for i, item in enumerate(value):
            _validate(item, schema['items'], path + '[' + str(i) + ']')
    elif kind == 'string':
        if not isinstance(value, str):
            _fail(path + ': expected string')
        if '\x00' in value or not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', math.inf):
            _fail(path + ': invalid string length or NUL')
        if 'pattern' in schema and re.fullmatch(schema['pattern'], value) is None:
            _fail(path + ': invalid string format')
    elif kind in ('integer', 'number'):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _fail(path + ': expected finite number')
        # Arbitrarily large Python ints are finite, without float conversion.
        if isinstance(value, float) and not math.isfinite(value):
            _fail(path + ': expected finite number')
        if kind == 'number':
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                _fail(path + ': expected finite representable number')
        if kind == 'integer' and (not isinstance(value, int) and not value.is_integer()):
            _fail(path + ': expected integer')
        if value < schema.get('minimum', -math.inf) or value > schema.get('maximum', math.inf):
            _fail(path + ': number outside bounds')
        if 'exclusiveMinimum' in schema and value <= schema['exclusiveMinimum']:
            _fail(path + ': number must exceed lower bound')
    elif kind == 'boolean' and not isinstance(value, bool):
        _fail(path + ': expected boolean')
    return value


def _absolute(filename):
    path = Path(filename)
    if not path.is_absolute():
        _fail('Absolute path required: ' + filename)
    return str(path.resolve())


def _unique(values, label):
    if len(values) != len(set(values)):
        _fail('Duplicate ' + label)


def _canonical(value, schema):
    """JSON Schema integers include 1.0; turn accepted integer-valued floats into ints."""
    if 'oneOf' in schema:
        for branch in schema['oneOf']:
            try:
                _validate(value, branch)
            except WorkflowError:
                continue
            return _canonical(value, branch)
    if schema.get('type') == 'integer':
        return int(value)
    if schema.get('type') == 'object':
        return {k: _canonical(v, schema['properties'][k]) for k, v in value.items()}
    if schema.get('type') == 'array':
        return [_canonical(v, schema['items']) for v in value]
    return value


def _normalize_file(row):
    row['file'] = _absolute(row['file'])
    return row


def _normalize_source(source, kind=None):
    for row in source['files']:
        _normalize_file(row)
    _unique([r['file'].casefold() for r in source['files']], 'source file')
    source.setdefault('source_in', 0)
    source.setdefault('speed', 1)
    source.setdefault('freeze', False)
    source.setdefault('stream', 0)
    if kind in ('IMAGE', 'MOVIE'):
        source.setdefault('color_space', 'sRGB')
    if kind in ('MOVIE', 'SOUND') and len(source['files']) != 1:
        _fail(kind + ' requires exactly one source file')
    if kind == 'SOUND' and source['freeze']:
        _fail('SOUND cannot freeze')
    return source


def _sampling(source, duration, kind):
    # Still images can hold indefinitely; sequence indexes must address real members.
    if kind != 'IMAGE':
        return
    count = len(source['files'])
    if count == 1:
        if source['source_in'] != 0:
            _fail('Still IMAGE source_in must be zero')
        return
    last = source['source_in']
    if not source['freeze']:
        # Native sample floor is the last source frame visited, not duration*speed.
        advance = (duration - 1) * source['speed']
        try:
            finite = math.isfinite(advance)
        except OverflowError:
            finite = False
        if not finite:
            _fail('Source sampling exceeds finite range')
        last += math.floor(advance)
    if last >= count:
        _fail('IMAGE sequence source sampling exceeds member count')


def _normalize_fields(clip, kind=None, full=False):
    kind = kind or clip.get('kind')
    if 'source' in clip:
        _normalize_source(clip['source'], kind)
        if 'duration' in clip:
            _sampling(clip['source'], clip['duration'], kind)
    if 'font' in clip:
        _normalize_file(clip['font'])
    alternatives = clip.get('alternatives', [])
    _unique([a['candidate_id'] for a in alternatives], 'alternative candidate_id')
    for alternative in alternatives:
        _normalize_source(alternative['source'], kind)
        if 'duration' in clip:
            _sampling(alternative['source'], clip['duration'], kind)
    if full:
        clip.setdefault('shot_id', clip['id'])
        clip.setdefault('name', clip['id'])
        clip.setdefault('candidate_id', 'original')
        clip.setdefault('lock', False)
        clip.setdefault('mute', False)
        clip.setdefault('fade_in', 0)
        clip.setdefault('fade_out', 0)
        if kind == 'SOUND':
            clip.setdefault('volume', 1)
            clip.setdefault('pan', 0)
        else:
            clip.setdefault('opacity', 1)
        if kind == 'TEXT':
            clip.setdefault('font_size', 48)
            clip.setdefault('color', [1, 1, 1, 1])
    if clip.get('candidate_id') in {a['candidate_id'] for a in alternatives}:
        _fail('Current and alternative candidate IDs must be distinct')
    if 'transform' in clip:
        for key, default in [('x', 0), ('y', 0), ('scale_x', 1), ('scale_y', 1), ('rotation_degrees', 0)]:
            clip['transform'].setdefault(key, default)
    if 'crop' in clip:
        for key in ('left', 'right', 'top', 'bottom'):
            clip['crop'].setdefault(key, 0)
    if 'grade' in clip:
        clip['grade'].setdefault('multiply', 1)
        clip['grade'].setdefault('saturation', 1)
    if 'duration' in clip and clip.get('fade_in', 0) + clip.get('fade_out', 0) > clip['duration']:
        _fail('Fades exceed clip duration')
    return clip


def validate_clip(clip, timeline=None):
    """Validate a merged patch clip against kind and actual timeline before mutation."""
    _validate(clip, CLIP, 'clip')
    normalized = _normalize_fields(_canonical(deepcopy(clip), CLIP), full=True)
    end = normalized['start'] + normalized['duration']
    if end > FRAME_MAX + 1:
        _fail('Clip half-open end exceeds frame bounds')
    if timeline and (normalized['start'] < timeline['frame_start'] or end > timeline['frame_end_exclusive']):
        _fail('Clip is outside half-open timeline')
    return normalized


def normalize_manifest(value):
    _validate(value, MANIFEST_SCHEMA, 'manifest')
    value = _canonical(deepcopy(value), MANIFEST_SCHEMA)
    timeline = value.get('timeline')
    if timeline and timeline['frame_end_exclusive'] <= timeline['frame_start']:
        _fail('Timeline must be a nonempty half-open interval')
    if value['action'] == 'create':
        _unique([c['id'] for c in value['clips']], 'clip id')
        value['clips'] = [validate_clip(c, timeline) for c in value['clips']]
    else:
        adds = [e['clip']['id'] for e in value['edits'] if e['op'] == 'add']
        _unique(adds, 'added clip id')
        for edit in value['edits']:
            if edit['op'] == 'add':
                edit['clip'] = validate_clip(edit['clip'], timeline)
            elif edit['op'] == 'update':
                _normalize_fields(edit['changes'])
    return value


def output_shots(output, timeline, clips=None):
    """Select real composed-timeline shot ranges, shared by budgets and outputs.

    Default active IMAGE/MOVIE, or COLOR when none exist. Explicit selection
    matches shot_id (missing shot_id falls back to id), never TEXT/SOUND.
    Each selected identity must describe one continuous/overlapping interval;
    disjoint pieces are rejected before cropping to the output frame range.
    """
    _validate(output, OUTPUT_SCHEMA, 'output')
    _validate(timeline, TIMELINE, 'timeline')
    if 'shot_ids' in output:
        if output.get('shots') is not True:
            _fail('shot_ids requires shots=true')
        _unique(output['shot_ids'], 'shot_id selection')
    if not output.get('shots'):
        return []
    if not isinstance(clips, list):
        _fail('Shot output requires actual clips')
    start, end = output.get('frames', [timeline['frame_start'], timeline['frame_end_exclusive']])
    if not timeline['frame_start'] <= start < end <= timeline['frame_end_exclusive']:
        _fail('Output frames must be a nonempty half-open subset of timeline')
    active = [row for row in clips if isinstance(row, dict) and row.get('kind') in ('IMAGE', 'MOVIE', 'COLOR') and not row.get('mute', False)]
    if 'shot_ids' not in output:
        media = [row for row in active if row['kind'] in ('IMAGE', 'MOVIE')]
        active = media or [row for row in active if row['kind'] == 'COLOR']
    groups = {}
    for row in active:
        identity = row.get('shot_id', row.get('id'))
        _validate(identity, ID, 'shot_id')
        _validate(row.get('start'), FRAME, 'clip.start')
        _validate(row.get('duration'), DURATION, 'clip.duration')
        groups.setdefault(identity, []).append(row)
    requested = output.get('shot_ids', list(groups))
    missing = set(requested) - set(groups)
    if missing:
        _fail('Unknown, muted or nonvisual shot_ids: ' + ', '.join(sorted(missing)))
    result = []
    for identity in requested:
        rows = sorted(groups[identity], key=lambda row: (row['start'], row['duration']))
        left = rows[0]['start']; right = left + rows[0]['duration']
        for row in rows[1:]:
            if row['start'] > right:
                _fail('shot_id has disconnected intervals: ' + identity)
            right = max(right, row['start'] + row['duration'])
        left, right = max(start, left), min(end, right)
        if left < right:
            clip_ids = [row['id'] for row in rows if row.get('id') is not None and row['start'] < right and row['start'] + row['duration'] > left]
            result.append({'shot_id': identity, 'clip_ids': list(dict.fromkeys(clip_ids)), 'frames': [int(left), int(right)]})
    return sorted(result, key=lambda row: (row['frames'][0], row['shot_id']))


def validate_output_budget(output, timeline, clips=None):
    """Preflight complete main and additional visual-shot pixel frame count.

    Caller MUST supply actual post-patch timeline/clips. No shortening/truncation.
    """
    _validate(output, OUTPUT_SCHEMA, 'output')
    _validate(timeline, TIMELINE, 'timeline')
    start, end = output.get('frames', [timeline['frame_start'], timeline['frame_end_exclusive']])
    if not timeline['frame_start'] <= start < end <= timeline['frame_end_exclusive']:
        _fail('Output frames must be a nonempty half-open subset of timeline')
    width = output.get('width', timeline['width'])
    height = output.get('height', timeline['height'])
    # Changing the VSE canvas changes its composition. Output overrides are
    # downscaled delivery/preview sizes; another canvas is an explicit edit.
    percentage = width * 100 / timeline['width']
    if (not percentage.is_integer() or not 1 <= percentage <= 100 or
            height * 100 != timeline['height'] * int(percentage)):
        _fail('Output dimensions must be an exact 1..100 percent of the authored canvas; reframe a timeline version for another aspect or larger resolution')
    frame_count = end - start
    ranges = output_shots(output, timeline, clips)
    frame_count += sum(row['frames'][1] - row['frames'][0] for row in ranges)
    pixels = width * height * frame_count
    if pixels > output['max_pixel_frames']:
        raise WorkflowError('RESOURCE_LIMIT',
                            'Full output requires ' + str(pixels) + ' pixel-frames; budget is ' + str(output['max_pixel_frames']))
    return {'frames': [start, end], 'width': width, 'height': height, 'render_percentage': int(percentage),
            'frame_count': frame_count, 'pixel_frames': pixels,
            'max_pixel_frames': output['max_pixel_frames'], 'shots': ranges}


def normalize_output(value, timeline=None, clips=None):
    _validate(value, OUTPUT_SCHEMA, 'output')
    value = _canonical(deepcopy(value), OUTPUT_SCHEMA)
    value.setdefault('shots', False)
    value.setdefault('subtitles', False)
    value.setdefault('audio', True)
    value.setdefault('color_mode', 'SDR')
    if 'preview' in value:
        if value['color_mode'] != 'SDR':
            _fail('Proxy preview currently requires SDR')
        _unique([row['clip_id'] for row in value['preview']['proxies']], 'proxy clip_id')
        for row in value['preview']['proxies']:
            for desc in row['source_files'] + row['files']:
                _normalize_file(desc)
    if 'shot_ids' in value:
        if not value['shots']:
            _fail('shot_ids requires shots=true')
        _unique(value['shot_ids'], 'shot_id selection')
    if value['color_mode'] == 'HDR10_PQ':
        if value['format'] != 'MP4' or not all(key in value for key in ('hdr', 'ffmpeg', 'ffprobe')):
            _fail('HDR10_PQ requires MP4 and explicit hdr/ffmpeg/ffprobe descriptors')
        _normalize_file(value['hdr']['ocio'])
    elif 'hdr' in value:
        _fail('HDR policy is only valid with HDR10_PQ output')
    for key in ('ffmpeg', 'ffprobe'):
        if key in value:
            _normalize_file(value[key])
    if 'frames' in value and value['frames'][1] <= value['frames'][0]:
        _fail('Output frames must be nonempty and half-open')
    if timeline is not None:
        validate_output_budget(value, timeline, clips)
    elif all(k in value for k in ('frames', 'width', 'height')) and not value['shots']:
        if value['width'] * value['height'] * (value['frames'][1] - value['frames'][0]) > value['max_pixel_frames']:
            raise WorkflowError('RESOURCE_LIMIT', 'Explicit output exceeds full pixel-frame budget')
    return value


def _inputs(value, schema):
    _validate(value, schema, 'params')
    value = _canonical(deepcopy(value), schema)
    value['file'] = _absolute(value['file'])
    if Path(value['file']).suffix.lower() != '.blend':
        _fail('Source must be .blend')
    for row in value['resources']:
        _normalize_file(row)
    _unique([row['file'].casefold() for row in value['resources']], 'resource path')
    return value


def _descriptors(manifest):
    clips = manifest.get('clips', [])
    if manifest['action'] == 'patch':
        clips = [e['clip'] if e['op'] == 'add' else e['changes']
                 for e in manifest['edits'] if e['op'] in ('add', 'update')]
    for clip in clips:
        if 'font' in clip:
            yield clip['font']
        for source in ([clip['source']] if 'source' in clip else []) + [a['source'] for a in clip.get('alternatives', [])]:
            yield from source['files']


def normalize_params(value):
    value = _inputs(value, RUN_PARAMS_SCHEMA)
    value['manifest'] = normalize_manifest(value['manifest'])
    descriptors = list(_descriptors(value['manifest']))
    if 'media_probe' in value:
        _normalize_file(value['media_probe'])
        descriptors.append(value['media_probe'])
    if 'resume' in value:
        _normalize_file(value['resume'])
        # Resume is a prior JSON report anchor, independently locked/verified by
        # Host. It is not a media dependency and need not repeat in resources.
    if 'output' in value:
        manifest = value['manifest']
        value['output'] = normalize_output(value['output'],
            manifest.get('timeline') if manifest['action'] == 'create' else None,
            manifest.get('clips'))
        descriptors += [value['output'][k] for k in ('ffmpeg', 'ffprobe') if k in value['output']]
        if 'hdr' in value['output']:
            descriptors.append(value['output']['hdr']['ocio'])
        if 'preview' in value['output']:
            for proxy in value['output']['preview']['proxies']:
                descriptors.extend(proxy['source_files'] + proxy['files'])
    declared = {r['file'].casefold(): r['expected_sha256'] for r in value['resources']}
    for row in descriptors:
        if declared.get(row['file'].casefold()) != row['expected_sha256']:
            _fail('Resource closure/SHA mismatch: ' + row['file'])
    return value


def normalize_inspect_params(value):
    return _inputs(value, INSPECT_PARAMS_SCHEMA)
