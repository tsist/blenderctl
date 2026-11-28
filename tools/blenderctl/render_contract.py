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
RUN['properties']['denoiser']=enum('OPENIMAGEDENOISE','OPTIX')
RUN['properties']['denoising_use_gpu']=BOOL
SOURCE_SETTINGS_MODE='SOURCE_V1'
C5_SETTINGS_MODE='SOURCE_C5'
RUN['properties']['settings_mode']=enum(SOURCE_SETTINGS_MODE,C5_SETTINGS_MODE)
RUN['properties']['width']=integer(16,8192)
RUN['properties']['height']=integer(16,8192)
RUN['properties']['output_passes']={**array(NAME,0,64),'uniqueItems':True}
RUN['properties']['exr_codec']=enum('ZIP','DWAA')
RUN['properties']['adaptive_sampling']=obj({'enabled':BOOL,'threshold':number(0,1),'min_samples':integer(0,4096)},['enabled','threshold','min_samples'])
COLOR['properties']['look']=enum('None','AgX - Very High Contrast','AgX - High Contrast','AgX - Medium High Contrast','AgX - Base Contrast','AgX - Medium Low Contrast','AgX - Low Contrast','AgX - Very Low Contrast','AgX - Punchy','AgX - Greyscale')
MEDIA_PIXEL_FRAME_DEFAULT=32_000_000
MEDIA_PIXEL_FRAME_MAX=512_000_000
MEDIA['properties']['pixel_frame_budget']={**integer(1,MEDIA_PIXEL_FRAME_MAX),'default':MEDIA_PIXEL_FRAME_DEFAULT}
EXPORT=obj({'scene':NAME,'format':enum('PNG_SEQUENCE','MP4_H264_AAC','WAV_PCM16'),'frame_start':integer(1,100000),'frame_end':integer(2,100000),'color':COLOR})
def is_source_run(v):
    return isinstance(v,dict) and v.get('settings_mode') in (SOURCE_SETTINGS_MODE,C5_SETTINGS_MODE)
def is_c5_run(v):
    return isinstance(v,dict) and v.get('settings_mode')==C5_SETTINGS_MODE
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
    if is_c5_run(v):
        if v['passes'] or (v.get('output_passes') and v['format']!='OPEN_EXR_MULTILAYER'):
            raise Failure('INVALID_REQUEST','C5 output_passes identify native saved EXR passes')
        if v.get('adaptive_sampling') and v['engine']!='CYCLES':
            raise Failure('INVALID_REQUEST','Adaptive sampling requires Cycles')
    elif is_source_run(v):
        # SOURCE_V1 keeps the saved engine/sampling/color state. Only these
        # output declarations are request-owned in the first version.
        if v['format']!='PNG' or v['depth'] not in ('8','16'):raise Failure('UNSUPPORTED','SOURCE_V1 supports PNG 8/16 output only')
        if v['compositor']:raise Failure('UNSUPPORTED','SOURCE_V1 does not apply compositor graphs; use explicit render.run for compositing')
        if v['passes']:raise Failure('UNSUPPORTED','SOURCE_V1 does not accept render passes')
    d=v['device'];backend=d['backend']
    if v['color'].get('look','None')!='None' and v['color']['view']!='AgX':raise Failure('INVALID_REQUEST','AgX look requires the AgX view')
    if 'denoiser' in v or 'denoising_use_gpu' in v:
        if v['engine']!='CYCLES' or not v['denoise']:raise Failure('INVALID_REQUEST','Explicit denoising settings require Cycles denoise=true')
        if v.get('denoising_use_gpu') and backend not in ('CUDA','OPTIX'):raise Failure('INVALID_REQUEST','GPU denoising requires an explicit GPU backend')
        if v.get('denoiser')=='OPTIX' and (backend!='OPTIX' or v.get('denoising_use_gpu') is not True):raise Failure('INVALID_REQUEST','OptiX denoising requires the exact OptiX GPU backend and denoising_use_gpu=true')
    if v['engine']=='BLENDER_WORKBENCH' and v['samples']!=8:raise Failure('UNSUPPORTED','Workbench currently requires eight-sample antialiasing')
    if v['engine']=='CYCLES':
        if backend=='GRAPHICS' or (backend in ('CUDA','OPTIX'))!=('id' in d):raise Failure('INVALID_REQUEST','Cycles GPU requires exact device id; CPU has no device id')
    elif backend!='GRAPHICS' or 'id' in d:raise Failure('INVALID_REQUEST','Eevee/Workbench use the host graphics context')
    source_eevee_blur=v['engine']=='BLENDER_EEVEE' and is_source_run(v)
    if v['engine']!='CYCLES' and (v['denoise'] or v['passes'] or (v['motion_blur'] and not source_eevee_blur)):
        raise Failure('UNSUPPORTED','Denoise, motion blur and passes are scoped to Cycles')
    if v['format'] in ('PNG','JPEG'):
        if v['depth'] not in (('8',) if v['format']=='JPEG' else ('8','16')):raise Failure('INVALID_REQUEST','Invalid display format depth')
    elif v['depth'] not in ('16','32'):raise Failure('INVALID_REQUEST','EXR requires 16/32 bit float')
    if v['format']=='JPEG' and v['transparent']:raise Failure('INVALID_REQUEST','JPEG cannot preserve alpha')
    if v['passes'] and v['format']!='OPEN_EXR_MULTILAYER':raise Failure('INVALID_REQUEST','Render passes require multilayer EXR')
    if v['format']=='OPEN_EXR_MULTILAYER' and v['compositor'] and not is_c5_run(v):raise Failure('UNSUPPORTED','Select C5 to retain raw passes and a composited layer')
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
