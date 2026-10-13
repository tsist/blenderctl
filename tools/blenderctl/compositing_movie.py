"""1.3 candidate: immutable full CFR decode, explicit BT709 YUV→linear EXR.

No bpy state mutation, implicit retime/TRC inference, partial-frame success or
arbitrary decoder flags. Production integration must preserve FileGuard scope.
"""
import hashlib
import json
import math
import os
from pathlib import Path
from fractions import Fraction
from contextlib import ExitStack
import queue
import re
import subprocess
import sys
import threading
import time

from protocol import Failure, atomic_json, read_json
from filesystem import FileGuard

METHOD = 'BT709_OETF_INVERSE_V1'
VF = 'showinfo,format=yuv420p'
MAX_FRAME_PIXELS = 4194304
PIXEL_FRAME_BUDGET = 640000000
CHILD_PIDS = set()

def _failure(code,message,details):
    error=Failure(code,message)
    error.details=details
    return error



def implementation_sha():
    return value_sha({'helper':file_sha(__file__),'persisted_contract':file_sha(_movie_contract.__file__)})





def probe_args(path,frames=False):
    entries=('frame=media_type,stream_index,pts,best_effort_timestamp,duration,width,height,pix_fmt,interlaced_frame,'
       'color_primaries,color_transfer,color_space,color_range,chroma_location:frame_side_data') if frames else (
       'stream=index,codec_type,codec_name,pix_fmt,width,height,field_order,color_primaries,color_transfer,color_space,'
       'color_range,chroma_location,sample_aspect_ratio,time_base,r_frame_rate,avg_frame_rate,start_pts,duration_ts,nb_frames:'
       'stream_side_data:stream_tags=rotate')
    args=['-v','error','-show_entries',entries,'-of','json']
    args+=['-select_streams','v:0','-show_frames'] if frames else ['-show_streams']
    return [*args,str(path)]

def _process(exe,args,progress,label,timeout):
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    process=subprocess.Popen([str(exe),*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=flags)
    CHILD_PIDS.add(process.pid);started=time.monotonic();out=bytearray();err=bytearray();overrun=threading.Event()
    def read(pipe,buffer,budget):
        try:
            for part in iter(lambda:pipe.read(4096),b''):
                if len(buffer)+len(part)>budget:overrun.set();return
                buffer.extend(part)
        except OSError:overrun.set()
    readers=[threading.Thread(target=read,args=(process.stdout,out,8388608),name='movie-probe-stdout'),
        threading.Thread(target=read,args=(process.stderr,err,2097152),name='movie-probe-stderr')]
    for reader in readers:reader.start()
    try:
        while True:
            progress(label,pid=process.pid)
            if overrun.is_set():raise Failure('RESOURCE_LIMIT','Full probe exceeded bounded stdout/stderr budget')
            try:process.wait(timeout=.1);break
            except subprocess.TimeoutExpired:
                if time.monotonic()-started>timeout:raise Failure('TIMEOUT',label+' timeout')
        for reader in readers:reader.join(timeout=5)
        if overrun.is_set() or any(r.is_alive() for r in readers):raise Failure('RESOURCE_LIMIT','Full probe reader exceeded bounded budget')
        if process.returncode or err.strip():raise _failure('VALIDATION_FAILED',label+' failed',{'exit':process.returncode,'stderr':err.decode('utf-8','replace')[-3000:]})
        if len(out)>8388608:raise Failure('RESOURCE_LIMIT','Full probe JSON exceeded bounded budget')
        return json.loads(out)
    finally:
        _stop_owned(process)
        for reader in readers:reader.join(timeout=5)
        process.stdout.close();process.stderr.close();CHILD_PIDS.discard(process.pid)
        if any(r.is_alive() for r in readers):raise Failure('WORKER_FAILED','Owned probe readers did not terminate')

def _stop_owned(process):
    if process.pid not in CHILD_PIDS:raise Failure('CONFLICT','Cannot terminate a process outside this helper ownership')
    if process.poll() is None:
        process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)


def decode_args(spec,proof):
    return ['-hide_banner','-nostdin','-v','info','-xerror','-err_detect','explode','-ec','0','-max_error_rate','0','-noautorotate',
        '-copyts','-i',spec['file'],'-map','0:'+str(proof['stream_index']),'-an','-fps_mode','passthrough','-vf',VF,
        '-pix_fmt','yuv420p','-f','rawvideo','pipe:1']

class _Decoder:
    def __init__(self,exe,args,frame_bytes):
        flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        self.process=subprocess.Popen([str(exe),*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=flags)
        CHILD_PIDS.add(self.process.pid);self.bytes=frame_bytes;self.queue=queue.Queue(maxsize=2);self.stop=threading.Event();self.stderr=bytearray()
        self.reader=threading.Thread(target=self._read,name='movie-bounded-stdout');self.errors=threading.Thread(target=self._errors,name='movie-bounded-stderr')
        self.reader.start();self.errors.start()
    def _put(self,value):
        while not self.stop.is_set():
            try:self.queue.put(value,timeout=.1);return
            except queue.Full:pass
    def _read(self):
        try:
            while not self.stop.is_set():
                data=bytearray()
                while len(data)<self.bytes:
                    part=self.process.stdout.read(self.bytes-len(data))
                    if not part:break
                    data.extend(part)
                if not data:break
                self._put(bytes(data))
                if len(data)!=self.bytes:break
        except Exception as exc:self._put(exc)
        finally:self._put(None)
    def _errors(self):
        for part in iter(lambda:self.process.stderr.read(4096),b''):
            self.stderr.extend(part)
            if len(self.stderr)>16777216:
                self.stop.set();break
    def frames(self,progress,timeout):
        started=time.monotonic()
        while True:
            progress('movie_decode_wait',pid=self.process.pid)
            if time.monotonic()-started>timeout:raise Failure('TIMEOUT','Full movie decode exceeded bounded timeout')
            if self.stop.is_set():raise Failure('RESOURCE_LIMIT','Decoder log exceeded budget')
            try:item=self.queue.get(timeout=.1)
            except queue.Empty:continue
            if item is None:break
            if isinstance(item,Exception):raise item
            if len(item)!=self.bytes:raise Failure('VALIDATION_FAILED','Partial decoded frame at EOF')
            yield item
        self.process.wait(timeout=5);self.errors.join(timeout=5)
        if self.process.returncode:raise _failure('VALIDATION_FAILED','Strict decode failed',{'exit':self.process.returncode,'stderr':bytes(self.stderr).decode('utf-8','replace')[-3000:]})
    def close(self):
        self.stop.set()
        _stop_owned(self.process)
        self.reader.join(timeout=5);self.errors.join(timeout=5)
        self.process.stdout.close();self.process.stderr.close();CHILD_PIDS.discard(self.process.pid)
        if self.reader.is_alive() or self.errors.is_alive():raise Failure('WORKER_FAILED','Owned decoder reader did not terminate')

def _decoded_yuv_planes(data,width,height):
    """Exact 8-bit planes; left chroma sample centers=(2*x, 2*y+0.5)."""
    import numpy as np
    if len(data)!=width*height*3//2:raise Failure('VALIDATION_FAILED','Original decoded YUV420 frame length differs')
    values=np.frombuffer(data,dtype=np.uint8).astype(np.float64)
    size=width*height;chroma_size=size//4
    y=values[:size].reshape(height,width)
    xx=np.clip(np.arange(width,dtype=np.float64)/2,0,width//2-1)
    yy=np.clip((np.arange(height,dtype=np.float64)-.5)/2,0,height//2-1)
    x0=xx.astype(np.int64);y0=yy.astype(np.int64)
    x1=np.minimum(x0+1,width//2-1);y1=np.minimum(y0+1,height//2-1)
    fx=(xx-x0)[None,:];fy=(yy-y0)[:,None]
    output=[y]
    for start in (size,size+chroma_size):
        plane=values[start:start+chroma_size].reshape(height//2,width//2)
        top=plane[y0[:,None],x0[None,:]]*(1-fx)+plane[y0[:,None],x1[None,:]]*fx
        bottom=plane[y1[:,None],x0[None,:]]*(1-fx)+plane[y1[:,None],x1[None,:]]*fx
        output.append(top*(1-fy)+bottom*fy)
    return np.stack(output)

def yuv_to_linear(data,width,height):
    import numpy as np
    planes=_decoded_yuv_planes(data,width,height)
    y=(planes[0]-16)/219;cb=(planes[1]-128)/224;cr=(planes[2]-128)/224
    encoded=np.stack([y+1.5748*cr,y-.1873242729306488*cb-.4681242729306488*cr,y+1.8556*cb],axis=-1)
    linear=np.empty_like(encoded);low=encoded<.081
    linear[low]=encoded[low]/4.5;linear[~low]=((encoded[~low]+.099)/1.099)**(1/.45)
    rgba=np.concatenate([linear,np.ones((height,width,1))],axis=-1).astype(np.float32)
    if not np.isfinite(rgba).all():raise Failure('VALIDATION_FAILED','Explicit YUV/TRC produced nonfinite values')
    return rgba

def _write_exr(path,rgba):
    import OpenImageIO as oiio
    height,width,_=rgba.shape;spec=oiio.ImageSpec(width,height,4,oiio.FLOAT);spec.channelnames=['R','G','B','A'];spec.attribute('oiio:ColorSpace','Linear Rec.709')
    spec.attribute('colorInteropID','lin_rec709_scene')
    writer=oiio.ImageOutput.create(str(path))
    if writer is None:raise Failure('UNSUPPORTED','OpenEXR writer unavailable')
    try:
        if not writer.open(str(path),spec) or not writer.write_image(rgba):raise Failure('IO_ERROR','Cannot write owned movie EXR')
    finally:writer.close()

def read_linear_exr(path,width,height):
    import numpy as np
    import OpenImageIO as oiio
    reader=oiio.ImageInput.open(str(path))
    if reader is None:raise Failure('VALIDATION_FAILED','Cannot decode movie-derived EXR')
    try:
        spec=reader.spec();data=np.asarray(reader.read_image(format=oiio.FLOAT))
        if (reader.format_name().lower()!='openexr' or str(spec.format)!='float' or list(spec.channelnames)!=['R','G','B','A'] or
            (spec.width,spec.height)!=(width,height) or data.shape!=(height,width,4) or not np.isfinite(data).all() or
            not np.array_equal(data[:,:,3],np.ones((height,width))) or spec.get_string_attribute('oiio:ColorSpace') not in {'Linear Rec.709','lin_rec709_scene'} or
            spec.get_string_attribute('colorInteropID')!='lin_rec709_scene'):
            raise _failure('VALIDATION_FAILED','Movie EXR is not flat finite opaque LinearRec709RGBA32f',
                {'container':reader.format_name(),'format':str(spec.format),'channels':list(spec.channelnames),
                 'size':[spec.width,spec.height],'shape':list(data.shape),'finite':bool(np.isfinite(data).all()),
                 'alpha_min':float(data[:,:,3].min()),'alpha_max':float(data[:,:,3].max()),
                 'colorspace':spec.get_string_attribute('oiio:ColorSpace')})
        if reader.seek_subimage(1,0):raise Failure('UNSUPPORTED','Movie-derived EXR acquired extra subimage')
        return data
    finally:reader.close()

def _guard_inputs(spec,stack):
    rows=[{'file':spec['file'],'expected_sha256':spec['expected_sha256']},*spec['decoder_runtime'].values()]
    parents={};guards=[]
    for row in rows:
        path=Path(os.path.abspath(row['file']))
        if not Path(row['file']).is_absolute():raise Failure('INVALID_REQUEST','Movie/runtime paths must be absolute')
        for parent in reversed(path.parents):
            key=os.path.normcase(str(parent))
            if key not in parents:parents[key]=stack.enter_context(FileGuard(parent,directory=True))
        guard=stack.enter_context(FileGuard(path))
        if guard.sha256()!=row['expected_sha256']:raise _failure('CONFLICT','Movie/runtime SHA differs',{'file':str(path)})
        guards.append((row,guard,guard.identity()))
    return guards

def _live_inputs(guards):
    for row,guard,identity in guards:
        if not guard.handle or guard.identity()!=identity:raise Failure('CONFLICT','Movie/runtime live handle identity changed')
        with FileGuard(row['file']) as current:
            if current.identity()!=identity:raise Failure('CONFLICT','Movie/runtime path identity changed')

def prepare_movie(spec,binding,job,progress=lambda *_a,**_k:None,*,resume=None,timeout=300):
    job=Path(job).resolve();job.mkdir(parents=True,exist_ok=True)
    with ExitStack() as stack:
        guards=_guard_inputs(spec,stack)
        if resume is not None:
            validate_resolved_movie(spec,resume,decode_images=True)
            if resume['conversion_implementation_sha256']!=implementation_sha():raise Failure('CONFLICT','Movie conversion implementation changed')
            progress('movie_reuse_complete',owner=resume['owner_job_directory'])
            _live_inputs(guards);return resume
        directory=job/'media'/('primary' if binding=='$source' else 'node-'+binding)
        if directory.exists():raise Failure('CONFLICT','Owned movie namespace already exists; use a new attempt')
        directory.mkdir(parents=True,exist_ok=False);frames=[];decoder=None
        receipt_path=job/('movie-'+('primary' if binding=='$source' else binding)+'-decode-receipt.json')
        probe_path=job/('movie-'+('primary' if binding=='$source' else binding)+'-full-probe.json')
        log_path=job/('movie-'+('primary' if binding=='$source' else binding)+'-decoder.log')
        try:
            header=_process(spec['decoder_runtime']['ffprobe']['file'],probe_args(spec['file']),progress,'movie_full_header',timeout)
            presentation=_process(spec['decoder_runtime']['ffprobe']['file'],probe_args(spec['file'],True),progress,'movie_full_pts',timeout)
            document={'streams':header.get('streams',[]),'frames':presentation.get('frames',[])};proof=validate_probe(spec,document)
            atomic_json(probe_path,document);width,height=proof['width'],proof['height']
            decoder=_Decoder(spec['decoder_runtime']['ffmpeg']['file'],decode_args(spec,proof),width*height*3//2)
            for index,data in enumerate(decoder.frames(progress,timeout),1):
                if index>proof['frame_count']:raise Failure('CONFLICT','Decoder EOS contains undeclared extra frame')
                _live_inputs(guards);rgba=yuv_to_linear(data,width,height)
                temporary=directory/('frame-%06d.partial.exr'%index);destination=directory/('frame-%06d.exr'%index)
                _write_exr(temporary,rgba)
                with FileGuard(temporary,renameable=True) as output:
                    output_sha=output.sha256();output.rename_new(destination)
                read_linear_exr(destination,width,height)
                frames.append({'source_frame':index,'source_pts':proof['frames'][index-1]['pts'],'duration':proof['frames'][index-1]['duration'],
                    'stage_index':index,'scene_frame':spec['mapping']['scene_start']+index-1,'file':str(destination),'sha256':output_sha,
                    'decoded_yuv_sha256':hashlib.sha256(data).hexdigest()})
                progress('movie_stage_frame',stage_index=index,source_pts=frames[-1]['source_pts'])
            stderr=bytes(decoder.stderr);log_path.write_bytes(stderr)
            observed=showinfo_frames(stderr);expected=[{'index':i,'pts':f['pts'],'duration':f['duration']} for i,f in enumerate(proof['frames'])]
            if observed!=expected or len(frames)!=proof['frame_count']:
                raise Failure('CONFLICT','Strict decoder full EOS/showinfo presentation proof differs from independent ffprobe')
            _live_inputs(guards)
            import numpy as np
            import OpenImageIO as oiio
            receipt={'status':'complete','complete_EOS':True,'declaration_sha256':value_sha(spec),'movie':proof,'decoder_runtime':spec['decoder_runtime'],
                'transform':TRANSFORM,'conversion_implementation_sha256':implementation_sha(),'python':sys.version,
                'numpy':np.__version__,'openimageio':getattr(oiio,'VERSION_STRING',None),'decode_exit':0,'decoded_frame_count':len(frames),
                'showinfo':observed,'full_probe':{'file':str(probe_path),'sha256':file_sha(probe_path)},
                'decoder_log':{'file':str(log_path),'sha256':file_sha(log_path)},'frames':frames}
            atomic_json(receipt_path,receipt)
            entry={'kind':'decoded_movie','version':1,'declaration_sha256':value_sha(spec),'owner_job_directory':str(job),
                'staging_directory':str(directory),'mapping':spec['mapping'],'original_movie':{'file':spec['file'],'sha256':spec['expected_sha256']},
                'movie':proof,'decoder_runtime':spec['decoder_runtime'],'transform':TRANSFORM,'conversion_implementation_sha256':implementation_sha(),
                'decode_receipt':{'file':str(receipt_path),'sha256':file_sha(receipt_path)},
                'anchor':{'file':frames[0]['file'],'sha256':frames[0]['sha256'],'stage_index':1},'frames':frames}
            validate_resolved_movie(spec,entry,decode_images=True);return entry
        except BaseException as exc:
            if decoder is not None:log_path.write_bytes(bytes(decoder.stderr))
            atomic_json(receipt_path,{'status':'cancelled' if getattr(exc,'code',None)=='CANCELLED' else 'incomplete',
                'declaration_sha256':value_sha(spec),'frames':frames,'complete_EOS':False,'error':str(exc),'code':getattr(exc,'code',None)})
            raise
        finally:
            if decoder is not None:decoder.close()


def movie_documents(entry):
    receipt=read_json(entry['decode_receipt']['file'])
    return [entry['original_movie'],*entry['decoder_runtime'].values(),entry['decode_receipt'],receipt['full_probe'],receipt['decoder_log'],
        *({'file':f['file'],'sha256':f['sha256']} for f in entry['frames'])]

# Host wrappers translate the addon's pure contract errors into public CLI failures.
from compositing_workflow_addon import movie_contract as _movie_contract
TRANSFORM=_movie_contract.TRANSFORM
canonical=_movie_contract.canonical
value_sha=_movie_contract.value_sha
file_sha=_movie_contract.file_sha
showinfo_frames=_movie_contract.showinfo_frames

def validate_probe(spec,document):
    try:return _movie_contract.validate_probe(spec,document)
    except _movie_contract.Failure as exc:raise _failure(exc.code,str(exc),getattr(exc,'details',None)) from exc

def validate_resolved_movie(spec,entry,*,digest=file_sha,decode_images=False):
    try:_movie_contract.validate_resolved_movie(spec,entry,digest=digest,expected_implementation_sha256=implementation_sha())
    except _movie_contract.Failure as exc:raise _failure(exc.code,str(exc),getattr(exc,'details',None)) from exc
    if decode_images:
        for frame in entry['frames']:read_linear_exr(frame['file'],entry['movie']['width'],entry['movie']['height'])
    return entry
