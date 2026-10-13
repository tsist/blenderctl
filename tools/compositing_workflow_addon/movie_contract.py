"""Pure Movie 1.3 persisted proof; no host protocol/filesystem or process dependency."""
import hashlib
import json
import os
import re
from fractions import Fraction
from pathlib import Path
from .contract import WorkflowError as Failure, MOVIE_SCHEMA, validate
METHOD='BT709_OETF_INVERSE_V1'
MAX_FRAME_PIXELS=4194304
PIXEL_FRAME_BUDGET=640000000

def read_json(path):
    def pairs(rows):
        result={}
        for key,value in rows:
            if key in result:raise Failure('INVALID_REQUEST','Duplicate Movie proof JSON field')
            result[key]=value
        return result
    return json.loads(Path(path).read_text(encoding='utf-8'),object_pairs_hook=pairs)

TRANSFORM = {'method': METHOD, 'matrix': 'BT709_FLOAT_KR_0.2126_KB_0.0722_V1',
    'range': 'LIMITED_8BIT_EXACT_FLOAT', 'chroma': 'LEFT_H0_V128_TO_H0_V0_EXPLICIT_FLOAT64_BILINEAR_CLAMP_V1',
    'decoder_output': 'YUV420P8_UNSCALED',
    'output': 'LINEAR_REC709_RGBA32F_EXR', 'alpha': 'OPAQUE_ONE', 'clamp': 'NONE_FINITE'}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def value_sha(value): return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def file_sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1048576),b''):h.update(block)
    return h.hexdigest()


def _integer(value,label,minimum=None):
    if type(value) is not int or (minimum is not None and value<minimum):
        raise Failure('VALIDATION_FAILED','Required integer '+label+' missing/invalid')
    return value


def _ratio(value,label):
    try:
        result=Fraction(value)
        if result<=0:raise ValueError()
        return result
    except (ValueError,TypeError,ZeroDivisionError):raise Failure('VALIDATION_FAILED','Invalid '+label+' rational')


def _unsupported_side_data(item):
    for side in item.get('side_data_list',[]):
        name=str(side.get('side_data_type','')).lower()
        if any(x in name for x in ('display matrix','displaymatrix','mastering','content light','hdr','dovi','dolby')):
            raise Failure('UNSUPPORTED','Rotation/HDR side data is outside explicit SDR movie scope')
    if 'rotate' in item.get('tags',{}):raise Failure('UNSUPPORTED','Movie rotation tag is outside coded orientation scope')


def validate_probe(spec,document):
    """Pure complete presentation/duration proof; no avg-rate or PTS guessing."""
    validate(spec,MOVIE_SCHEMA)
    mapping=spec['mapping'];count=mapping['frame_count']
    if mapping['source_start']!=1 or not 1<=count<=300 or mapping['rate']!='ONE_TO_ONE':
        raise Failure('INVALID_REQUEST','Movie consumes its complete1..N presentation domain')
    fps=Fraction(mapping['scene_fps']['numerator'],mapping['scene_fps']['denominator'])
    videos=[s for s in document.get('streams',[]) if s.get('codec_type')=='video']
    if len(videos)!=1:raise Failure('UNSUPPORTED','Exactly one video stream is required')
    stream=videos[0];index=_integer(stream.get('index'),'stream.index',0)
    if stream.get('codec_name')!='h264' or stream.get('pix_fmt')!='yuv420p' or stream.get('field_order')!='progressive':
        raise Failure('UNSUPPORTED','Only progressive H264/yuv420p8 is verified')
    expected={'color_primaries':'bt709','color_transfer':'bt709','color_space':'bt709','color_range':'tv','chroma_location':'left'}
    for key,value in expected.items():
        if stream.get(key)!=value:raise Failure('UNSUPPORTED','Explicit movie metadata missing/mismatched: '+key)
    if stream.get('sample_aspect_ratio')!='1:1':raise Failure('UNSUPPORTED','Only square coded pixels are supported')
    _unsupported_side_data(stream)
    width=_integer(stream.get('width'),'width',16);height=_integer(stream.get('height'),'height',16)
    if width>4096 or height>4096 or width%2 or height%2 or width*height>MAX_FRAME_PIXELS or width*height*count>PIXEL_FRAME_BUDGET:
        raise Failure('RESOURCE_LIMIT','Movie exceeds bounded dimensions/pixel-frame/memory domain')
    tb=_ratio(stream.get('time_base'),'time_base');duration=_integer(stream.get('duration_ts'),'duration_ts',1)
    start=_integer(stream.get('start_pts'),'start_pts')
    if _ratio(stream.get('r_frame_rate'),'r_frame_rate')!=fps or _ratio(stream.get('avg_frame_rate'),'avg_frame_rate')!=fps:
        raise Failure('CONFLICT','Declared rationalFPS differs from complete stream rates')
    if str(stream.get('nb_frames'))!=str(count) or duration*tb!=Fraction(count,1)/fps:
        raise Failure('CONFLICT','Declared count/container duration disagree')
    frames=[f for f in document.get('frames',[]) if f.get('media_type')=='video']
    if len(frames)!=count:raise Failure('CONFLICT','Full presentation frame count differs')
    rows=[]
    for i,frame in enumerate(frames):
        _unsupported_side_data(frame)
        pts=_integer(frame.get('pts'),'frame.pts');best=_integer(frame.get('best_effort_timestamp'),'best_effort_timestamp')
        dur=_integer(frame.get('duration'),'frame.duration',1)
        if (frame.get('stream_index')!=index or best!=pts or pts*tb!=start*tb+Fraction(i,1)/fps or dur*tb!=1/fps):
            raise Failure('CONFLICT','Full CFR PTS/duration/domain proof differs; no best-effort substitution')
        if (frame.get('width')!=width or frame.get('height')!=height or frame.get('pix_fmt')!='yuv420p' or frame.get('interlaced_frame')!=0):
            raise Failure('CONFLICT','Frame decoded format/dimensions/progressive status changes')
        for key,value in expected.items():
            if frame.get(key)!=value:raise Failure('CONFLICT','Frame color metadata changed/missing: '+key)
        rows.append({'source_frame':i+1,'stage_index':i+1,'scene_frame':mapping['scene_start']+i,'pts':pts,'duration':dur})
    return {'stream_index':index,'codec_name':'h264','pix_fmt':'yuv420p','width':width,'height':height,'metadata':expected,
        'time_base':{'numerator':tb.numerator,'denominator':tb.denominator},'first_pts':start,'duration_ts':duration,
        'scene_fps':mapping['scene_fps'],'frame_count':count,'frames':rows,
        'audio_policy':'IGNORE_NOT_COMPOSITED','ignored_audio_streams':sum(s.get('codec_type')=='audio' for s in document.get('streams',[]))}


def showinfo_frames(stderr):
    rows=[]
    for line in stderr.decode('utf-8','replace').splitlines():
        if 'showinfo' not in line:continue
        match=re.search(r'\bn:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:\S+\s+duration:\s*(\d+)',line)
        if match:rows.append({'index':int(match[1]),'pts':int(match[2]),'duration':int(match[3])})
    return rows


def validate_resolved_movie(spec,entry,*,digest=file_sha,expected_implementation_sha256=None):
    validate(spec,MOVIE_SCHEMA)
    required={'kind','version','declaration_sha256','owner_job_directory','staging_directory','mapping','original_movie','movie',
        'decoder_runtime','transform','conversion_implementation_sha256','decode_receipt','anchor','frames'}
    if not isinstance(entry,dict) or set(entry)!=required or entry['kind']!='decoded_movie' or type(entry['version']) is not int or entry['version']!=1:
        raise Failure('CONFLICT','Malformed internal decoded Movie envelope')
    if entry['declaration_sha256']!=value_sha(spec) or entry['mapping']!=spec['mapping'] or entry['decoder_runtime']!=spec['decoder_runtime']:
        raise Failure('CONFLICT','Original Movie declaration/runtime/mapping differs')
    if entry['original_movie']!={'file':spec['file'],'sha256':spec['expected_sha256']} or digest(spec['file'])!=spec['expected_sha256']:
        raise Failure('CONFLICT','Original Movie identity/SHA changed')
    for runtime in spec['decoder_runtime'].values():
        if digest(runtime['file'])!=runtime['expected_sha256']:raise Failure('CONFLICT','Movie decoder runtime changed')
    if entry['transform']!=TRANSFORM or (not re.fullmatch(r'[0-9a-f]{64}',str(entry['conversion_implementation_sha256'])) or
        (expected_implementation_sha256 is not None and entry['conversion_implementation_sha256']!=expected_implementation_sha256)):
        raise Failure('CONFLICT','Movie transform/implementation changed')
    owner=Path(entry['owner_job_directory']);directory=Path(entry['staging_directory']);receipt_desc=entry['decode_receipt']
    if not owner.is_absolute() or not directory.is_absolute() or not directory.resolve().is_relative_to(owner.resolve()/'media') or directory.is_symlink():
        raise Failure('CONFLICT','Movie namespace escaped original owned job')
    receipt_file=Path(receipt_desc['file'])
    if receipt_file.resolve().parent!=owner.resolve() or digest(receipt_file)!=receipt_desc['sha256']:
        raise Failure('CONFLICT','Owned Movie receipt path/SHA differs')
    receipt=read_json(receipt_file)
    if (receipt.get('status')!='complete' or receipt.get('complete_EOS') is not True or receipt.get('declaration_sha256')!=entry['declaration_sha256'] or
        receipt.get('frames')!=entry['frames'] or receipt.get('movie')!=entry['movie'] or receipt.get('transform')!=entry['transform'] or
        receipt.get('decoder_runtime')!=entry['decoder_runtime'] or receipt.get('conversion_implementation_sha256')!=entry['conversion_implementation_sha256'] or
        receipt.get('decode_exit')!=0 or receipt.get('decoded_frame_count')!=spec['mapping']['frame_count']):
        raise Failure('CONFLICT','Movie complete receipt/provenance differs')
    for label in ('full_probe','decoder_log'):
        row=receipt[label]
        if Path(row['file']).resolve().parent!=owner.resolve() or digest(row['file'])!=row['sha256']:
            raise Failure('CONFLICT','Movie '+label+' sidecar identity changed')
    proof=validate_probe(spec,read_json(receipt['full_probe']['file']))
    expected_show=[{'index':i,'pts':f['pts'],'duration':f['duration']} for i,f in enumerate(proof['frames'])]
    if proof!=entry['movie'] or receipt.get('showinfo')!=expected_show or showinfo_frames(Path(receipt['decoder_log']['file']).read_bytes())!=expected_show:
        raise Failure('CONFLICT','Movie persisted fullPTS/showinfo proof differs')
    frames=entry['frames'];count=spec['mapping']['frame_count']
    if len(frames)!=count or sorted(p.name for p in directory.iterdir())!=[f'frame-{i:06d}.exr' for i in range(1,count+1)]:
        raise Failure('CONFLICT','Movie staged namespace coverage differs')
    for i,frame in enumerate(frames,1):
        if (not isinstance(frame,dict) or set(frame)!={'source_frame','source_pts','duration','stage_index','scene_frame','file','sha256','decoded_yuv_sha256'} or
            not re.fullmatch(r'[0-9a-f]{64}',str(frame.get('decoded_yuv_sha256',''))) or
            not re.fullmatch(r'[0-9a-f]{64}',str(frame.get('sha256','')))):
            raise Failure('CONFLICT','Movie frame requires complete exact PTS/stage/EXR/raw-YUV provenance')
        path=Path(frame['file'])
        if (path.resolve().parent!=directory.resolve() or path.is_symlink() or path.name!=f'frame-{i:06d}.exr' or
            frame['source_frame']!=i or frame['stage_index']!=i or frame['source_pts']!=proof['frames'][i-1]['pts'] or
            frame['duration']!=proof['frames'][i-1]['duration'] or frame['scene_frame']!=spec['mapping']['scene_start']+i-1 or digest(path)!=frame['sha256']):
            raise Failure('CONFLICT','Movie originalPTS/stage/scene/fileSHA mapping differs')
    if entry['anchor']!={'file':frames[0]['file'],'sha256':frames[0]['sha256'],'stage_index':1}:
        raise Failure('CONFLICT','Movie anchor differs')
    return entry


