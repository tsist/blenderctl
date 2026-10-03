# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded VSE timeline candidates and in-process Blender media exports."""
import json,time,wave,math
from pathlib import Path
from contextlib import ExitStack
import bpy
import scenes,modeling,rendering
from protocol import Failure,atomic_json,digest,read_json
from render_contract import normalize_media,normalize_export
from filesystem import FileGuard
KEY='_blenderctl_media_v1'
def state(s):
    rows=[]
    for x in sorted(s.sequence_editor.strips,key=lambda x:(x.channel,x.frame_final_start,x.name)):
        r={'name':x.name,'type':x.type,'channel':x.channel,'start':int(x.frame_final_start),'end':int(x.frame_final_end),'mute':x.mute,'blend_type':x.blend_type,'blend_alpha':x.blend_alpha}
        if x.type=='COLOR':r['color']=list(x.color)
        elif x.type=='IMAGE':r['files']=[str(Path(bpy.path.abspath(x.directory))/e.filename) for e in x.elements]
        elif x.type=='MOVIE':r['file']=str(Path(bpy.path.abspath(x.filepath)).resolve());r['offset']=int(x.frame_offset_start)
        elif x.type=='SOUND':r['file']=str(Path(bpy.path.abspath(x.sound.filepath)).resolve());r['offset']=int(x.frame_offset_start);r['volume']=x.volume
        else:raise Failure('UNSUPPORTED','Unsupported VSE strip: '+x.type)
        if x.modifiers:raise Failure('UNSUPPORTED','VSE strip modifiers are outside this workflow')
        rows.append(r)
    return {'scene':s.name,'fps':s.render.fps,'fps_base':s.render.fps_base,'width':s.render.resolution_x,'height':s.render.resolution_y,'start':s.frame_start,'end':s.frame_end,'strips':rows}
def guard_resources(files,stack):
    hashes={}
    for f in files:
        path=str(Path(f['file']).resolve());g=stack.enter_context(FileGuard(path));h=g.sha256()
        if h!=f['expected_sha256']:raise Failure('CONFLICT','Media resource hash changed')
        hashes[path]=h
    return hashes
def construct(spec):
    bpy.ops.wm.read_factory_settings(use_empty=True);s=bpy.context.scene;scenes.named(s,spec['scene']);s.render.fps=spec['fps'];s.render.fps_base=1;s.frame_start=spec['frame_start'];s.frame_end=spec['frame_end'];s.render.resolution_x=spec['width'];s.render.resolution_y=spec['height'];s.render.resolution_percentage=100;s.render.pixel_aspect_x=s.render.pixel_aspect_y=1
    ed=s.sequence_editor_create();resources=[]
    for st in spec['strips']:resources+=st.get('files',[]) or ([st['file']] if 'file' in st else [])
    for st in spec['strips']:
        common={'name':st['name'],'channel':st['channel'],'frame_start':st['start']};duration=st['end']-st['start'];kind=st['type']
        if kind=='COLOR':x=ed.strips.new_effect(**common,type='COLOR',length=duration);x.color=st['color']
        elif kind=='IMAGE':
            paths=[Path(f['file']) for f in st['files']]
            if len({p.parent for p in paths})!=1:raise Failure('UNSUPPORTED','One image strip requires files in the same directory')
            x=ed.strips.new_image(**common,filepath=str(paths[0]),fit_method='FIT')
            for p in paths[1:]:x.elements.append(p.name)
            x.frame_final_end=st['end']
        else:
            path=st['file']['file'];x=ed.strips.new_sound(**common,filepath=path) if kind=='SOUND' else ed.strips.new_movie(**common,filepath=path,fit_method='FIT')
            available=x.frame_duration
            if st['offset']+duration>available:raise Failure('INVALID_REQUEST','Trim extends beyond decoded source duration')
            x.frame_start=st['start']-st['offset'];x.frame_offset_start=st['offset'];x.frame_final_end=st['end']
            if kind=='SOUND':x.volume=st['volume']
        scenes.named(x,st['name']);x.mute=False
        if kind!='SOUND':x.blend_type='REPLACE';x.blend_alpha=1
        if (int(x.frame_final_start),int(x.frame_final_end))!=(st['start'],st['end']):raise Failure('VALIDATION_FAILED','Strip timing clamped by Blender')
    return s,resources
def prepare(params,job):
    spec=normalize_media(params['manifest'])
    resources=[f for st in spec['strips'] for f in (st.get('files',[]) or ([st['file']] if 'file' in st else []))]
    with ExitStack() as stack:
        hashes=guard_resources(resources,stack);s,_=construct(spec)
        expected=state(s);s[KEY]=json.dumps({'manifest':spec,'state':expected,'resources':resources},ensure_ascii=False,sort_keys=True);path=job/'media-candidate.blend';bpy.context.preferences.filepaths.save_version=0;bpy.ops.wm.save_as_mainfile(filepath=str(path),relative_remap=False,check_existing=False)
        bpy.ops.wm.open_mainfile(filepath=str(path),load_ui=False,use_scripts=False);observed=state(bpy.context.scene)
        if scenes.compare(expected,observed):raise Failure('VALIDATION_FAILED','Media candidate reopen mismatch')
        report={'media_report_version':'1.0','state':observed,'resource_hashes':hashes,'reopen':'pass'};atomic_json(job/'media-report.json',report)
        return {'candidate':str(path),'candidate_sha256':digest(path),'report':str(job/'media-report.json'),'resources':resources,'reopen':'pass'}
def export(params,job):
    spec=normalize_export(params['manifest']);started=time.monotonic();bpy.ops.wm.open_mainfile(filepath=params['file'],load_ui=False,use_scripts=False);s=scenes.find(bpy.data.scenes,spec['scene'])
    if not s.get(KEY) or not s.sequence_editor:raise Failure('UNSUPPORTED','Export requires a managed media.prepare candidate')
    if tuple(bpy.data.version[:2])!=tuple(bpy.app.version[:2]):raise Failure('UNSUPPORTED','Media requires matching Blender major/minor')
    stored=json.loads(s[KEY]);normalize_media(stored['manifest'])
    resources=[f for st in stored['manifest']['strips'] for f in (st.get('files',[]) or ([st['file']] if 'file' in st else []))]
    if resources!=stored['resources']:raise Failure('CONFLICT','Media resource manifest differs')
    if scenes.compare(stored['state'],state(s)):raise Failure('CONFLICT','VSE timeline differs from its authored contract; prepare a new candidate')
    if not s.frame_start<=spec['frame_start']<=spec['frame_end']<=s.frame_end:raise Failure('INVALID_REQUEST','Export range outside timeline')
    declared={str(Path(d['file']).resolve()):d['expected_sha256'] for d in params.get('resources',[])}
    if any(declared.get(str(Path(f['file']).resolve()))!=f['expected_sha256'] for f in stored['resources']):raise Failure('INVALID_REQUEST','Export must declare every media resource and SHA')
    with ExitStack() as stack:
        hashes=guard_resources(stored['resources'],stack);s,_=construct(stored['manifest']);
        if scenes.compare(stored['state'],state(s)):raise Failure('CONFLICT','Rebuilt media manifest differs from stored timeline')
        bpy.context.window.scene=s;rendering.disable_external_writes(s);s.render.use_sequencer=True;s.render.engine='CYCLES';s.cycles.device='CPU';s.cycles.samples=1;s.render.use_file_extension=True;s.render.resolution_percentage=100;s.render.use_border=False;s.render.use_multiview=False;s.render.film_transparent=False;s.frame_start=spec['frame_start'];s.frame_end=spec['frame_end'];rendering.color_settings(s,spec['color']);outputs=[]
        atomic_json(job/'media-progress.json',{'state':'exporting','start':s.frame_start,'end':s.frame_end})
        if spec['format']=='WAV_PCM16':
            # SOUND_OT_mixdown starts a native job even in background mode; its
            # FINISHED result is not a completion barrier. Decode bounded strips
            # synchronously with Blender's Audaspace, then write exact PCM frames.
            import aud,numpy as np
            count=round((s.frame_end-s.frame_start+1)*48000/s.render.fps);pcm=np.zeros((count,2),dtype=np.float64)
            for st in stored['manifest']['strips']:
                if st['type']!='SOUND':continue
                lo=max(st['start'],s.frame_start);hi=min(st['end'],s.frame_end+1)
                if lo>=hi:continue
                begin=(st['offset']+lo-st['start'])/s.render.fps;end=(st['offset']+hi-st['start'])/s.render.fps
                sound=aud.Sound(st['file']['file']).limit(begin,end).resample(48000,3).rechannel(2)
                samples=np.asarray(sound.data());offset=round((lo-s.frame_start)*48000/s.render.fps);length=min(len(samples),count-offset,round((hi-lo)*48000/s.render.fps))
                if abs(len(samples)-round((hi-lo)*48000/s.render.fps))>2:raise Failure('VALIDATION_FAILED','Decoded sound duration differs from strip')
                pcm[offset:offset+length]+=samples[:length]*st['volume']
            if not np.isfinite(pcm).all():raise Failure('VALIDATION_FAILED','Nonfinite audio samples')
            path=job/'audio.wav'
            with wave.open(str(path),'wb') as w:
                w.setparams((2,2,48000,0,'NONE','not compressed'));w.writeframes(np.rint(np.clip(pcm,-1,32767/32768)*32768).astype('<i2').tobytes())
            with wave.open(str(path),'rb') as w:
                if w.getnchannels()!=2 or w.getsampwidth()!=2 or w.getframerate()!=48000:raise Failure('VALIDATION_FAILED','Unexpected PCM format')
                expected=(s.frame_end-s.frame_start+1)/s.render.fps
                if abs(w.getnframes()/48000-expected)>1/48000:raise Failure('VALIDATION_FAILED','Audio duration differs')
                details={'channels':2,'rate':48000,'sample_frames':w.getnframes(),'seconds':w.getnframes()/48000}
            outputs.append({'file':str(path),'sha256':digest(path),'bytes':path.stat().st_size,'audio':details})
        elif spec['format']=='MP4_H264_AAC':
            if s.render.resolution_x%2 or s.render.resolution_y%2:raise Failure('INVALID_REQUEST','H264 requires even dimensions')
            fmt=s.render.image_settings;fmt.media_type='VIDEO';fmt.file_format='FFMPEG';fmt.color_mode='RGB';ff=s.render.ffmpeg;ff.format='MPEG4';ff.codec='H264';ff.constant_rate_factor='HIGH';ff.ffmpeg_preset='GOOD';ff.audio_codec='AAC';ff.audio_mixrate=48000;ff.audio_channels='STEREO';ff.audio_bitrate=192;path=job/'video.mp4';s.render.filepath=str(path);modeling.finished(bpy.ops.render.render(animation=True,scene=s.name))
            clip=bpy.data.movieclips.load(str(path));expected=s.frame_end-s.frame_start+1
            if clip.frame_duration!=expected or list(clip.size)!=[s.render.resolution_x,s.render.resolution_y]:raise Failure('VALIDATION_FAILED','Encoded movie timing/size differs')
            outputs.append({'file':str(path),'sha256':digest(path),'bytes':path.stat().st_size,'video':{'frames':clip.frame_duration,'width':clip.size[0],'height':clip.size[1],'fps':s.render.fps,'codec_request':'H264/AAC','audio_verification':'independent acceptance decoder; runtime video reload only'}})
        else:
            fmt=s.render.image_settings;fmt.media_type='IMAGE';fmt.file_format='PNG';fmt.color_mode='RGBA';fmt.color_depth='8'
            for frame in range(s.frame_start,s.frame_end+1):
                s.frame_set(frame);path=job/f'media-{frame:06d}.png';s.render.filepath=str(path);modeling.finished(bpy.ops.render.render(write_still=True,scene=s.name));r=rendering.image_report(path);r['frame']=frame;outputs.append(r)
        report={'media_export_version':'1.0','settings':spec,'outputs':outputs,'resource_hashes':hashes,'seconds':time.monotonic()-started,'source_saved':False,'timeline_authority':'stored manifest rebuilt privately; unmanaged strip edits are not exported'};atomic_json(job/'media-export.json',report);atomic_json(job/'media-progress.json',{'state':'verified'});return report
