"""Explicit device, frame, output and bounded media contracts."""
from pathlib import Path
import copy
from protocol import Failure,read_json
from scene_contract import obj,array,enum,number,integer,NAME,BOOL,validate
from node_contract import FILE
FRAMES={**array(integer(1,100000),1,120),'uniqueItems':True}
DEVICE=obj({'backend':enum('CPU','CUDA','OPTIX','GRAPHICS'),'id':{'type':'string','minLength':1}},['backend'])
COLOR=obj({'view':enum('Standard','AgX','Raw'),'exposure':number(-10,10),'gamma':number(.25,4)})
RUN=obj({'scene':NAME,'view_layer':NAME,'camera':NAME,'engine':enum('CYCLES','BLENDER_EEVEE','BLENDER_WORKBENCH'),'device':DEVICE,'frames':FRAMES,'width':integer(16,2048),'height':integer(16,2048),'samples':integer(1,256),'format':enum('PNG','JPEG','OPEN_EXR','OPEN_EXR_MULTILAYER'),'depth':enum('8','16','32'),'transparent':BOOL,'color':COLOR,'compositor':BOOL,'passes':{**array(enum('Z','NORMAL','DIFFUSE_COLOR','EMISSION','OBJECT_INDEX'),0,5),'uniqueItems':True},'denoise':BOOL,'motion_blur':number(0,1)},['scene','view_layer','camera','engine','device','frames','width','height','samples','format','depth','transparent','color','compositor','passes','denoise','motion_blur'])
BASE={'name':NAME,'channel':integer(1,32),'start':integer(1,100000),'end':integer(2,100001)}
STRIPS={'COLOR':obj({'type':{'const':'COLOR'},**BASE,'color':array(number(0,1),3,3)}),'IMAGE':obj({'type':{'const':'IMAGE'},**BASE,'files':array(FILE,1,120)}),'MOVIE':obj({'type':{'const':'MOVIE'},**BASE,'file':FILE,'offset':integer(0,100000)}),'SOUND':obj({'type':{'const':'SOUND'},**BASE,'file':FILE,'offset':integer(0,100000),'volume':number(0,2)})}
MEDIA=obj({'scene':NAME,'fps':integer(1,60),'width':integer(16,1920),'height':integer(16,1080),'frame_start':integer(1,100000),'frame_end':integer(2,100000),'strips':array({'oneOf':list(STRIPS.values())},1,64)})
RENDER_PIXEL_FRAME_DEFAULT=32_000_000
RENDER_PIXEL_FRAME_MAX=512_000_000
RUN['properties']['pixel_frame_budget']={**integer(1,RENDER_PIXEL_FRAME_MAX),'default':RENDER_PIXEL_FRAME_DEFAULT}
MEDIA_PIXEL_FRAME_DEFAULT=32_000_000
MEDIA_PIXEL_FRAME_MAX=512_000_000
MEDIA['properties']['pixel_frame_budget']={**integer(1,MEDIA_PIXEL_FRAME_MAX),'default':MEDIA_PIXEL_FRAME_DEFAULT}
EXPORT=obj({'scene':NAME,'format':enum('PNG_SEQUENCE','MP4_H264_AAC','WAV_PCM16'),'frame_start':integer(1,100000),'frame_end':integer(2,100000),'color':COLOR})
def file_input(item):
    p=Path(item['file'])
    if not p.is_absolute():raise Failure('INVALID_REQUEST','Media/resource paths must be absolute')
    if not p.is_file():raise Failure('NOT_FOUND','Input missing: '+str(p))
    return item
def normalize_run(v):
    v=copy.deepcopy(v)
    validate(v,RUN)
    if v['frames']!=sorted(v['frames']):raise Failure('INVALID_REQUEST','Frames must be sorted and unique')
    v.setdefault('pixel_frame_budget',RENDER_PIXEL_FRAME_DEFAULT)
    if v['width']*v['height']*len(v['frames'])>v['pixel_frame_budget']:raise Failure('UNSUPPORTED','Render exceeds declared output pixel-frame budget: '+str(v['pixel_frame_budget']))
    d=v['device'];backend=d['backend']
    if v['engine']=='BLENDER_WORKBENCH' and v['samples']!=8:raise Failure('UNSUPPORTED','Workbench currently requires eight-sample antialiasing')
    if v['engine']=='CYCLES':
        if backend=='GRAPHICS' or (backend in ('CUDA','OPTIX'))!=('id' in d):raise Failure('INVALID_REQUEST','Cycles GPU requires exact device id; CPU has no device id')
    elif backend!='GRAPHICS' or 'id' in d:raise Failure('INVALID_REQUEST','Eevee/Workbench use the host graphics context')
    if v['engine']!='CYCLES' and (v['denoise'] or v['motion_blur'] or v['passes']):raise Failure('UNSUPPORTED','Denoise, motion blur and passes are scoped to Cycles')
    if v['format'] in ('PNG','JPEG'):
        if v['depth'] not in (('8',) if v['format']=='JPEG' else ('8','16')):raise Failure('INVALID_REQUEST','Invalid display format depth')
    elif v['depth'] not in ('16','32'):raise Failure('INVALID_REQUEST','EXR requires 16/32 bit float')
    if v['format']=='JPEG' and v['transparent']:raise Failure('INVALID_REQUEST','JPEG cannot preserve alpha')
    if v['passes'] and v['format']!='OPEN_EXR_MULTILAYER':raise Failure('INVALID_REQUEST','Render passes require multilayer EXR')
    if v['format']=='OPEN_EXR_MULTILAYER' and v['compositor']:raise Failure('UNSUPPORTED','Multilayer raw passes and composited image use separate runs')
    return v
def normalize_media(v, *, check_files=True):
    v=copy.deepcopy(v)
    validate(v,MEDIA)
    if not 1<=v['frame_end']-v['frame_start']<240:raise Failure('INVALID_REQUEST','Media timeline must be 2..240 frames')
    v.setdefault('pixel_frame_budget',MEDIA_PIXEL_FRAME_DEFAULT)
    if v['width']*v['height']*(v['frame_end']-v['frame_start']+1)>v['pixel_frame_budget']:raise Failure('UNSUPPORTED','Media exceeds declared output pixel-frame budget: '+str(v['pixel_frame_budget']))
    seen=set();ranges={}
    for s in v['strips']:
        if s['name'].casefold() in seen:raise Failure('CONFLICT','Duplicate strip name')
        seen.add(s['name'].casefold())
        if not v['frame_start']<=s['start']<s['end']<=v['frame_end']+1:raise Failure('INVALID_REQUEST','Strip half-open range must lie inside timeline')
        for lo,hi in ranges.setdefault(s['channel'],[]):
            if max(lo,s['start'])<min(hi,s['end']):raise Failure('CONFLICT','Same-channel strips overlap')
        ranges[s['channel']].append((s['start'],s['end']))
        if check_files:
            for f in s.get('files',[]) or ([s['file']] if 'file' in s else []):file_input(f)
        if s['type']=='IMAGE' and len(s['files']) not in (1,s['end']-s['start']):raise Failure('INVALID_REQUEST','Image list must be one still or exactly one image per timeline frame')
    return v
def normalize_export(v):
    validate(v,EXPORT)
    if not 1<=v['frame_end']-v['frame_start']<240:raise Failure('INVALID_REQUEST','Export must span 2..240 frames')
    return v
def input_documents(params):
    docs=list(params.get('resources',[]))
    for strip in params.get('manifest',{}).get('strips',[]):docs+=strip.get('files',[]) or ([strip['file']] if 'file' in strip else [])
    if 'simulation_receipt' in params:docs.append(params['simulation_receipt'])
    if 'cache_manifest' in params:
        from geometry_temporal import validate_cache
        from geometry_identity import implementation_sha256
        manifest=validate_cache(params['cache_manifest'],candidate={'file':params['file'],'sha256':params['expected_sha256']},identity=implementation_sha256())
        docs.append(params['cache_manifest'])
        root=Path(manifest['root']).resolve()
        docs.extend({'file':str((root/item['path']).resolve()),'expected_sha256':item['sha256']} for item in manifest['files'])
    for d in docs:file_input(d)
    return docs
