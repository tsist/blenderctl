"""Technical Standard-100 HDR10/PQ output; no measured HDR mastering claim.

Requires an explicitly SHA-bound active OCIO configuration. Native VSE exports
RGBA32f EXR in a temporary Scene copy. CPU OCIO performs the primaries transform;
analytic ST2084 and explicit zscale produce limited Main10. Original Scene and
candidate bytes are never saved or rebuilt. SDR output module remains frozen.
"""
import contextlib
import json
import math
import os
import uuid
from fractions import Fraction
from pathlib import Path

import bpy
import numpy as np
import OpenImageIO as oiio
import PyOpenColorIO as ocio
from protocol import Failure, atomic_json, digest
from video_editing_workflow_addon import contract
import video_outputs as sdr


def _require(condition, message, code='VALIDATION_FAILED'):
    if not condition:
        raise Failure(code, message)


def pq_oetf(nits):
    light = np.asarray(nits, dtype=np.float64)
    _require(np.isfinite(light).all() and np.all((light >= 0) & (light <= 10000)),
             'PQ input outside physical 0..10000nit; no implicit clipping')
    power = (light / 10000) ** (2610 / 16384)
    return ((3424 / 4096 + 2413 / 128 * power) / (1 + 2392 / 128 * power)) ** (2523 / 32)


def pq_eotf(signal, statistics=False):
    signal = np.asarray(signal, dtype=np.float64)
    # Only metadata statistics normalize reconstructed excursions. Actual video
    # and raw decoded pixels retain excursions and diagnostics.
    signal = np.clip(signal, 0, 1) if statistics else np.maximum(signal, 0)
    power = signal ** (32 / 2523)
    denominator = 2413 / 128 - 2392 / 128 * power
    _require(np.all(denominator > 0), 'PQ extrapolation denominator invalid')
    return 10000 * (np.maximum(power - 3424 / 4096, 0) / denominator) ** (16384 / 2610)


def _stats(pq):
    nits = pq_eotf(pq, statistics=True)
    maximum = nits.max(axis=-1)
    raw_positive = pq_eotf(pq)
    return {'max_cll_nits': float(maximum.max()), 'maxrgb_frame_average_nits': float(maximum.mean()),
            'positive_extrapolated_peak_nits': float(raw_positive.max()),
            'below_zero_channels': int(np.count_nonzero(pq < 0)),
            'above_one_channels': int(np.count_nonzero(pq > 1)),
            'raw_pq_min': float(pq.min()), 'raw_pq_max': float(pq.max()),
            'policy': 'maxRGB of normalizedPQ0..1 for metadata statistics only; not a CTA certification claim'}


def _color_identity(params):
    policy = params['output'].get('hdr')
    _require(isinstance(policy, dict), 'Explicit output.hdr policy required', 'INVALID_REQUEST')
    _require(policy.get('mapping') == 'STANDARD_100' and policy.get('reference_white_nits') == 100
             and policy.get('exposure') == 0 and policy.get('mastering') == 'VIRTUAL_BT2020_10000NIT'
             and policy.get('alpha') == 'REQUIRE_OPAQUE',
             'First HDR qualification requires explicit STANDARD_100/100nit/EV0/virtualMaster/opaque policy', 'UNSUPPORTED')
    descriptor = policy.get('ocio', {})
    path = Path(descriptor.get('file', ''))
    _require(path.is_absolute() and path.is_file() and descriptor.get('expected_sha256'),
             'Explicit HDR OCIO FILE descriptor required', 'INVALID_REQUEST')
    active = os.environ.get('OCIO')
    _require(active and Path(active).resolve() == path.resolve(),
             'Worker must launch with OCIO environment bound to explicit HDR descriptor', 'UNSUPPORTED')
    declared = {str(Path(r['file']).resolve()).casefold(): r['expected_sha256'] for r in params['resources']}
    _require(declared.get(str(path.resolve()).casefold()) == descriptor['expected_sha256']
             and digest(path) == descriptor['expected_sha256'], 'HDR OCIO resource closure/SHA differs', 'CONFLICT')
    config = ocio.Config.CreateFromFile(str(path)); config.validate()
    working = config.getColorSpace(ocio.ROLE_SCENE_LINEAR)
    _require(working is not None and working.getName() == 'Linear Rec.709'
             and working.getInteropID() == 'lin_rec709_scene',
             'Current VSE HDR qualification requires observed Linear Rec.709 working role', 'UNSUPPORTED')
    processor = config.getProcessor(working.getName(), 'Linear Rec.2020')
    files = list(processor.getProcessorMetadata().getFiles())
    _require(not list(processor.getProcessorMetadata().getLooks()), 'Unexpected HDR transform looks', 'CONFLICT')
    for filename in files:
        consumed = Path(config.getCurrentContext().resolveFileLocation(filename)).resolve()
        _require(declared.get(str(consumed).casefold()) == digest(consumed),
                 'Consumed OCIO LUT must be declared with SHA', 'CONFLICT')
    cpu = processor.getOptimizedCPUProcessor(ocio.BIT_DEPTH_F32, ocio.BIT_DEPTH_F32, ocio.OPTIMIZATION_NONE)
    identity = {'config_sha256': descriptor['expected_sha256'], 'working_space': working.getName(),
                'working_interop_id': working.getInteropID(), 'output_linear': 'Linear Rec.2020',
                'target_interop_id': config.getColorSpace('Linear Rec.2020').getInteropID(),
                'processor_cache_id': processor.getCacheID(), 'processor_consumed_sha256': [digest(f) for f in files],
                'OCIO': ocio.__version__, 'OIIO': oiio.VERSION_STRING, 'numpy': np.__version__,
                'mapping': 'STANDARD_100', 'reference_white_nits': 100, 'exposure': 0,
                'source_display_view': 'recorded only; explicit standard linear mapping, no source view baking',
                'device': 'CPU OCIO/NumPy + CPU libx265 Main10; no GPU claim'}
    return identity, cpu


def _source_state(scene):
    return {'basic': [values for _, values in sdr._capture(scene)],
            'linear_space': scene.render.image_settings.linear_colorspace_settings.name,
            'color_management': scene.render.image_settings.color_management,
            'view': scene.view_settings.view_transform, 'look': scene.view_settings.look,
            'exposure': scene.view_settings.exposure, 'gamma': scene.view_settings.gamma,
            'sequencer_space': scene.sequencer_colorspace_settings.name,
            'display': scene.display_settings.display_device,
            'scenes': sorted(s.name for s in bpy.data.scenes)}


@contextlib.contextmanager
def _export_copy(scene):
    before = _source_state(scene)
    window = bpy.context.window
    original = window.scene if window else None
    temp = scene.copy()
    temp.name = '__video_hdr_export_' + uuid.uuid4().hex
    try:
        if window: window.scene = temp
        yield temp
    finally:
        if window and original: window.scene = original
        bpy.data.scenes.remove(temp)
        _require(_source_state(scene) == before, 'HDR export modified original Scene state')


def _linear_frames(scene, interval, width, height, identity, cpu, root, progress):
    raw = root / 'reference-gbr.f32'
    references, images = [], []
    with _export_copy(scene) as temp, raw.open('xb') as stream:
        temp.render.resolution_percentage = int(width * 100 / temp.render.resolution_x)
        temp.render.use_sequencer = True
        temp.render.use_border = False; temp.render.use_multiview = False
        temp.render.use_stamp = False; temp.render.use_file_extension = True
        threads = os.environ.get('VWE_RENDER_THREADS')
        if threads:
            _require(threads.isdigit() and int(threads) > 0, 'Invalid Host render thread bound', 'INVALID_REQUEST')
            temp.render.threads_mode = 'FIXED'; temp.render.threads = int(threads)
        for key in ('use_render_cache', 'use_texture_cache', 'use_auto_generate_texture_cache'):
            if hasattr(temp.render, key): setattr(temp.render, key, False)
        fmt = temp.render.image_settings
        fmt.media_type = 'IMAGE'; fmt.file_format = 'OPEN_EXR'; fmt.color_mode = 'RGBA'
        fmt.color_depth = '32'; fmt.exr_codec = 'ZIP'; fmt.color_management = 'FOLLOW_SCENE'
        fmt.linear_colorspace_settings.name = identity['working_space']
        _require(fmt.has_linear_colorspace, 'Native EXR output has no linear-space RNA')
        for index, frame in enumerate(range(*interval)):
            sdr._emit(progress, 'hdr_linear_frame', frame=frame, completed=index, total=interval[1]-interval[0])
            path = root / ('linear-' + str(index).zfill(8) + '.exr')
            if temp.sequence_editor:
                for strip in temp.sequence_editor.strips_all:
                    strip.invalidate_cache('RAW'); strip.invalidate_cache('COMPOSITE')
            temp.frame_set(frame); temp.render.filepath = str(path)
            _require('FINISHED' in bpy.ops.render.render(write_still=True, scene=temp.name) and path.is_file(),
                     'Native linear EXR render did not complete')
            reader = oiio.ImageInput.open(str(path)); _require(reader is not None, 'Cannot open native EXR')
            try:
                spec = reader.spec()
                _require(reader.format_name().lower() == 'openexr' and str(spec.format) == 'float'
                         and list(spec.channelnames) == ['R','G','B','A']
                         and (spec.width,spec.height) == (width,height)
                         and spec.get_string_attribute('colorInteropID') == identity['working_interop_id'],
                         'Native EXR dimensions/32f/interop differ')
                rgba = np.asarray(reader.read_image(format=oiio.FLOAT))
                _require(rgba.shape == (height,width,4) and np.isfinite(rgba).all(), 'Invalid native EXR pixels')
                _require(not reader.seek_subimage(1,0), 'Multilayer EXR not qualified', 'UNSUPPORTED')
            finally:
                reader.close()
            _require(np.all(rgba[:,:,3] == 1), 'HDR alpha REQUIRE_OPAQUE failed', 'UNSUPPORTED')
            rgb = rgba[:,:,:3].copy(); cpu.applyRGB(rgb)
            pq = pq_oetf(rgb.astype(np.float64)*100).astype(np.float32)
            reference = root / ('pq-reference-' + str(index).zfill(8) + '.npy')
            np.save(reference,pq,allow_pickle=False)
            np.stack([pq[:,:,1],pq[:,:,2],pq[:,:,0]]).astype('<f4').tofile(stream)
            images.append(sdr._file(path, kind='LINEAR_EXR',frame=frame,linear_space=identity['working_space'],
                                    interop_id=identity['working_interop_id']))
            references.append({'file':str(reference),'sha256':digest(reference),'stats':_stats(pq),'frame':frame})
            atomic_json(root/'linear-checkpoint.json',{'status':'partial','completed':index+1,
                        'total':interval[1]-interval[0],'settings_restoration':'pending_copy_disposal','frame_reuse':False})
    atomic_json(root/'linear-checkpoint.json',{'status':'passed','completed':len(references),
                'settings_restoration':'passed','frame_reuse':False})
    return raw,references,images


def _fidelity(reference, decoded):
    weights=np.array([.2627,.678,.0593])
    ref=pq_eotf(reference,statistics=True); actual=pq_eotf(decoded,statistics=True)
    expected=float(np.mean(ref@weights));observed=float(np.mean(actual@weights))
    tolerance=.01 if expected<.0001 else max(.05,.03*expected)
    delta=np.abs(decoded-reference)
    return {'status':'passed' if abs(expected-observed)<=tolerance else 'failed',
            'reference_mean_Y_nits':expected,'decoded_mean_Y_nits':observed,
            'mean_Y_error_nits':abs(expected-observed),'mean_Y_tolerance_nits':tolerance,
            'pq_channel_mae':float(delta.mean()),'pq_channel_max_abs':float(delta.max()),
            'scope':'3%/.05nit applies only full-frame mean Y; RGB/edge errors are diagnostics'}


def _encode_hdr(params, references, raw, audio, interval, width, height, fps, root, progress):
    ffmpeg,ffprobe=sdr._tool(params,'ffmpeg'),sdr._tool(params,'ffprobe')
    count=len(references);size=f'{width}x{height}';yuv=root/'prepared-42010.yuv'
    vf='zscale=matrixin=gbr:matrix=bt2020nc:primariesin=bt2020:primaries=bt2020:transferin=smpte2084:transfer=smpte2084:rangein=full:range=limited:npl=100:agamma=0:dither=error_diffusion,format=yuv420p10le'
    sdr._external([ffmpeg,'-nostdin','-v','error','-f','rawvideo','-pixel_format','gbrpf32le',
                  '-video_size',size,'-framerate',str(fps),'-i',str(raw),'-vf',vf,
                  '-f','rawvideo','-pix_fmt','yuv420p10le','-n',str(yuv)],root,'hdr_prepare_yuv',progress)
    _require(yuv.stat().st_size==count*width*height*3,'Prepared complete YUV size differs')
    pending={'cll':math.ceil(max(r['stats']['max_cll_nits'] for r in references)),
             'fall':math.ceil(max(r['stats']['maxrgb_frame_average_nits'] for r in references))}
    attempts=[];cicp=['-color_primaries','bt2020','-color_trc','smpte2084','-colorspace','bt2020nc','-color_range','tv']
    for attempt in range(1,3):
        movie=root/f'hdr-attempt-{attempt}.mp4';master='G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(100000000,1)'
        settings=':'.join(['hdr10=1','hdr10-opt=0','master-display='+master,
             f"max-cll={pending['cll']},{pending['fall']}",'repeat-headers=1','colorprim=bt2020',
             'transfer=smpte2084','colormatrix=bt2020nc','range=limited','pools=none',
             'frame-threads=1','wpp=0','bframes=0','keyint='+str(max(count,3)),'log-level=1'])
        command=[ffmpeg,'-nostdin','-v','error','-f','rawvideo','-pixel_format','yuv420p10le',
                 '-video_size',size,'-framerate',str(fps),*cicp,'-i',str(yuv)]
        if audio:command+=['-i',audio['file']]
        command+=['-map','0:v:0']
        if audio:command+=['-map','1:a:0','-c:a','aac','-ar','48000','-b:a','192k']
        else:command+=['-an']
        command+=['-c:v','libx265','-preset','medium','-crf','14','-profile:v','main10',
                  '-pix_fmt','yuv420p10le',*cicp,'-x265-params',settings,'-tag:v','hvc1',
                  '-movflags','+faststart+write_colr','-metadata',
                  'comment=Virtual technical target only; no measured mastering display','-n',str(movie)]
        sdr._external(command,root,f'hdr_encode_{attempt}',progress)
        output=sdr._external([ffprobe,'-v','error','-select_streams','v:0','-count_frames','-show_streams',
                 '-show_frames','-show_format','-of','json',str(movie)],root,f'hdr_probe_{attempt}',progress)
        probe=json.loads(output.read_text(encoding='utf-8'));stream=probe['streams'][0];frames=probe.get('frames',[])
        fields={'pix_fmt':'yuv420p10le','color_range':'tv','color_primaries':'bt2020',
                'color_transfer':'smpte2084','color_space':'bt2020nc','width':width,'height':height}
        _require(all(stream.get(k)==v for k,v in {**fields,'codec_name':'hevc','profile':'Main 10',
                  'codec_tag_string':'hvc1','nb_read_frames':str(count)}.items())
                  and Fraction(stream['avg_frame_rate'])==fps and len(frames)==count,
                  'HDR Main10/profile/CICP/full frame count differs')
        timebase=Fraction(stream['time_base']);duration=Fraction(count,1)/fps
        _require(abs(Fraction(stream['duration'])-duration)<=timebase,'HDR duration differs')
        expected_master={'red_x':Fraction(35400,50000),'red_y':Fraction(14600,50000),
             'green_x':Fraction(8500,50000),'green_y':Fraction(39850,50000),
             'blue_x':Fraction(6550,50000),'blue_y':Fraction(2300,50000),
             'white_point_x':Fraction(15635,50000),'white_point_y':Fraction(16450,50000),
             'min_luminance':Fraction(1,10000),'max_luminance':Fraction(10000)}
        for i,frame in enumerate(frames):
            _require(all(frame.get(k)==v for k,v in fields.items()),'Decoded frame CICP differs')
            _require(abs(Fraction(frame['best_effort_timestamp_time'])-Fraction(i,1)/fps)<=timebase,'HDR frame PTS differs')
            masters=[d for d in frame.get('side_data_list',[]) if d['side_data_type']=='Mastering display metadata']
            clls=[d for d in frame.get('side_data_list',[]) if d['side_data_type']=='Content light level metadata']
            _require(len(masters)==1 and all(Fraction(masters[0].get(k,'-1'))==v for k,v in expected_master.items())
                     and len(clls)==1 and clls[0].get('max_content')==pending['cll']
                     and clls[0].get('max_average')==pending['fall'],'Every-frame HDR metadata differs')
        decoded=root/f'decoded-{attempt}.gbr.f32';dvf='zscale=matrixin=bt2020nc:matrix=gbr:primariesin=bt2020:primaries=bt2020:transferin=smpte2084:transfer=smpte2084:rangein=limited:range=full:agamma=0,format=gbrpf32le'
        sdr._external([ffmpeg,'-nostdin','-v','error','-xerror','-i',str(movie),'-map','0:v:0','-vf',dvf,
                      '-f','rawvideo','-pix_fmt','gbrpf32le','-n',str(decoded)],root,f'hdr_decode_{attempt}',progress)
        _require(decoded.stat().st_size==count*width*height*3*4,'Complete HDR decoded bytes differ')
        mem=np.memmap(decoded,dtype='<f4',mode='r',shape=(count,3,height,width));rows=[]
        for i in range(count):
            sdr._emit(progress,'hdr_numeric_check',completed=i,total=count)
            rgb=np.stack([mem[i,2],mem[i,0],mem[i,1]],axis=-1)
            _require(np.isfinite(rgb).all(),'Nonfinite decoded HDR pixels')
            ref=np.load(references[i]['file'],allow_pickle=False)
            rows.append({'frame':interval[0]+i,'stats':_stats(rgb),'fidelity':_fidelity(ref,rgb)})
        del mem
        decoded_yuv=root/f'decoded-{attempt}.yuv420p10'
        sdr._external([ffmpeg,'-nostdin','-v','error','-xerror','-i',str(movie),'-map','0:v:0',
                      '-f','rawvideo','-pix_fmt','yuv420p10le','-n',str(decoded_yuv)],root,f'hdr_decode_yuv_{attempt}',progress)
        _require(decoded_yuv.stat().st_size==count*width*height*3,'Complete HDR YUV bytes differ')
        codes=np.memmap(decoded_yuv,dtype='<u2',mode='r',shape=(count,width*height*3//2));n=width*height
        for i in range(count):
            details={}
            for name,plane in [('Y',codes[i,:n]),('Cb',codes[i,n:n+n//4]),('Cr',codes[i,n+n//4:])]:
                outside=int(np.count_nonzero((plane<4)|(plane>1019)))
                _require(outside==0,'Decoded HDR exceeds BT2100 video-data code range')
                details[name]={'min':int(plane.min()),'max':int(plane.max()),'outside_video_data_4_1019':outside}
            rows[i]['yuv10_codes']=details
        del plane,codes
        actual={'cll':math.ceil(max(r['stats']['max_cll_nits'] for r in rows)),
                'fall':math.ceil(max(r['stats']['maxrgb_frame_average_nits'] for r in rows))}
        attempts.append({'attempt':attempt,'declared':pending,'decoded':actual,
                         'bound_to_decode':pending==actual,'frames':rows})
        if pending==actual:break
        pending=actual
    _require(attempts[-1]['bound_to_decode'],'HDR metadata did not converge within 2 attempts')
    _require(all(r['fidelity']['status']=='passed' for r in attempts[-1]['frames']),
             'HDR reference full-frame mean fidelity failed')
    av_output=sdr._external([ffprobe,'-v','error','-show_streams','-of','json',str(movie)],
                  root,'hdr_probe_av',progress)
    av=json.loads(av_output.read_text(encoding='utf-8'))['streams']
    audio_streams=[v for v in av if v.get('codec_type')=='audio']
    _require(len([v for v in av if v.get('codec_type')=='video'])==1
             and len(audio_streams)==(1 if audio else 0),'HDR final AV stream count differs')
    if audio:
        audio_stream=audio_streams[0]
        _require(audio_stream.get('codec_name')=='aac' and int(audio_stream.get('sample_rate',0))==48000
                 and int(audio_stream.get('channels',0))==2,'HDR AAC rate/channel/codec differs')
        encoded_duration=float(audio_stream.get('duration','nan'))
        expected_duration=audio['audio']['sample_frames']/48000
        _require(math.isfinite(encoded_duration) and abs(encoded_duration-expected_duration)<=1024/48000,
                 'HDR audio duration exceeds one AAC frame tolerance')
    sdr._external([ffmpeg,'-nostdin','-v','error','-xerror','-i',str(movie),'-map','0','-f','null','-'],
                  root,'hdr_full_decode',progress)
    qa={'status':'passed','frames':count,'fps':[fps.numerator,fps.denominator],
        'width':width,'height':height,'codec':'HEVC Main10','bit_depth':10,'subsampling':'4:2:0',
        'CICP':'BT2020/PQ/BT2020nc/limited','all_pts':'passed','full_decode':'passed',
        'every_frame_mastering_cll':'passed','decoded_metadata_binding':'passed','attempts':attempts,
        'audio_streams':len(audio_streams),'AAC_duration_tolerance_samples':1024 if audio else None,
        'mastering_provenance':'virtual BT2020 D65 10000nit/.0001nit technical target; not measured display',
        'HDR_device_viewing':'not_run','real_mastering':'not_run','normative_HDR10_certification':'not_run',
        'decoded_pixels':sdr._file(decoded,kind='RAW_PQ_GBR_FLOAT32')}
    return sdr._file(movie,kind='HDR10_MP4'),qa


def render_hdr_outputs(params,job,report,progress):
    output=params['output']
    _require(output.get('color_mode')=='HDR10_PQ' and output.get('format')=='MP4',
             'HDR module only handles explicitly requested HDR10_PQ MP4','UNSUPPORTED')
    identity,cpu=_color_identity(params)
    inspected=sdr._inspection(params,report);clips=sdr._actual_clips(inspected);timeline=inspected['timeline']
    # Parent integrates hdr policy into its strict schema. Budget calculation
    # remains identical and includes every additional visual clip range.
    budget_fields={k:v for k,v in output.items() if k in contract.OUTPUT_SCHEMA['properties'] and k!='hdr'}
    try:budget=contract.validate_output_budget(budget_fields,timeline,clips)
    except contract.WorkflowError as ex:raise Failure(ex.code,str(ex)) from ex
    width,height=budget['width'],budget['height']
    _require(width>=16 and height>=16 and width%2==0 and height%2==0,
             'Qualified Main10 dimensions must be even and at least16','INVALID_REQUEST')
    scene=bpy.data.scenes.get(inspected.get('scene') or params['manifest']['scene'])
    _require(scene is not None,'HDR candidate scene not loaded','NOT_FOUND')
    sdr._no_external_compositor(scene);sdr._tool(params,'ffmpeg');sdr._tool(params,'ffprobe')
    fps=Fraction(*timeline['fps']);root=Path(job).resolve()/('video-hdr-'+uuid.uuid4().hex);root.mkdir()
    ranges=[('main',budget['frames'],None)]
    for i,row in enumerate(contract.output_shots(output,timeline,clips)):
        ranges.append((f'shot-{i:06d}',row['frames'],{'clip_ids':row['clip_ids'],'shot_id':row['shot_id']}))
    result={'status':'running','outputs':[],'audio_qa':[],'video_qa':[],'color_identity':identity,
            'source_view_record':{'view':scene.view_settings.view_transform,'look':scene.view_settings.look,
                                  'exposure':scene.view_settings.exposure,'gamma':scene.view_settings.gamma},
            'budget':budget,'color_mode':'HDR10_PQ','timeline_authority':'current candidate, no manifest rebuild',
            'resume':{'frame_reuse':False},'checks':{'HDR_device_viewing':'not_run','real_mastering':'not_run',
                          'perceptual_view_match':'not_run','normative_HDR10_certification':'not_run'}}
    before=_source_state(scene)
    for label,interval,shot in ranges:
        directory=root/label;directory.mkdir();audio=None
        if output.get('audio',True):
            audio,audio_qa=sdr._mix_audio(scene,clips,params,interval,fps,directory,progress)
            result['audio_qa'].append({'range':label,**audio_qa})
            if audio_qa['status']!='passed':
                result['status']='failed';atomic_json(root/'hdr-report.json',result);return result
            result['outputs'].append({**audio,'range':label,'shot':shot})
        raw,references,images=_linear_frames(scene,interval,width,height,identity,cpu,directory,progress)
        result['outputs'].extend({**item,'range':label,'shot':shot} for item in images)
        movie,qa=_encode_hdr(params,references,raw,audio,interval,width,height,fps,directory,progress)
        result['outputs'].append({**movie,'range':label,'shot':shot,'frames':interval})
        result['video_qa'].append({'range':label,**qa})
        if output.get('subtitles'):result['outputs'].append({**sdr._subtitles(clips,interval,fps,directory),'range':label})
        atomic_json(root/'hdr-report.json',result)
    _require(_source_state(scene)==before,'HDR output changed original scene state')
    result['status']='passed';result['checks']['settings_restored']='passed'
    atomic_json(root/'hdr-report.json',result);result['report']=sdr._file(root/'hdr-report.json',kind='HDR_OUTPUT_REPORT')
    return result
