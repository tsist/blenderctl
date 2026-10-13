"""Isolated Host HDR10 work unit. Public integration is intentionally absent."""
from pathlib import Path
from contextlib import ExitStack
from fractions import Fraction
import ctypes,hashlib,json,math,os,subprocess,sys,time,uuid
from ctypes import wintypes as w
import processes as owned_processes
from compositing_encode_contract import (EncodeError,require,normalize_params,anchor_documents,
                                        producer_documents,signature,from_row,mapping_policy)

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''):h.update(b)
    return h.hexdigest()
def describe(path):
    p=Path(path).resolve();return {'file':str(p),'sha256':digest(p),'bytes':p.stat().st_size}
def atomic_json(path,value):
    path=Path(path);temp=path.with_name(path.name+'.new.'+uuid.uuid4().hex)
    with temp.open('x',encoding='utf-8') as f:json.dump(value,f,indent=2,allow_nan=False)
    os.replace(temp,path)
def implementation_identity():
    files=[describe(Path(__file__).with_name(name)) for name in ('compositing_encode.py','compositing_encode_contract.py','processes.py','bootstrap.py')]
    require(Path(owned_processes.__file__).resolve()==Path(__file__).with_name('processes.py').resolve(),'Owned process module path differs','CONFLICT')
    return {'files':files,'sha256':signature(files)}

class Guard:
    """Windows deny-write/delete guard; include every ancestor to block path rebinding."""
    def __init__(self,path,directory=False):
        require(os.name=='nt','Windows guard required','UNSUPPORTED')
        self.path=Path(os.path.abspath(path));self.k=ctypes.WinDLL('kernel32',use_last_error=True)
        self.k.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,ctypes.c_void_p,w.DWORD,w.DWORD,w.HANDLE];self.k.CreateFileW.restype=w.HANDLE
        self.k.CloseHandle.argtypes=[w.HANDLE];self.k.CloseHandle.restype=w.BOOL
        self.k.GetFileInformationByHandle.argtypes=[w.HANDLE,ctypes.c_void_p];self.k.GetFileInformationByHandle.restype=w.BOOL
        name=str(self.path);name='\\\\?\\UNC\\'+name[2:] if name.startswith('\\\\') else '\\\\?\\'+name
        self.handle=self.k.CreateFileW(name,0 if directory else 0x80000000,3 if directory else 1,None,3,0x200000|(0x2000000 if directory else 0x80),None)
        require(self.handle!=ctypes.c_void_p(-1).value,'Cannot lock '+str(self.path)+':'+str(ctypes.WinError(ctypes.get_last_error())),'CONFLICT')
        class Info(ctypes.Structure):
            _fields_=[('attributes',w.DWORD),('created',w.FILETIME),('accessed',w.FILETIME),('written',w.FILETIME),('volume',w.DWORD),('size_high',w.DWORD),('size_low',w.DWORD),('links',w.DWORD),('index_high',w.DWORD),('index_low',w.DWORD)]
        info=Info()
        if not self.k.GetFileInformationByHandle(self.handle,ctypes.byref(info)) or info.attributes&0x400:
            self.close();raise EncodeError('CONFLICT','Reparse or invalid input guard:'+str(self.path))
    def close(self):
        if self.handle:self.k.CloseHandle(self.handle);self.handle=None
    def __enter__(self):return self
    def __exit__(self,*_):self.close()

class Inputs:
    def __init__(self,stack,checkpoint):self.stack=stack;self.checkpoint=checkpoint;self.files={};self.dirs=set()
    def protect(self,d):
        p=Path(os.path.abspath(d['file']));key=os.path.normcase(str(p));sha=d['expected_sha256']
        if key in self.files:require(self.files[key]['sha256']==sha,'Conflicting input descriptor','CONFLICT');return p
        for parent in reversed(p.parents):
            k=os.path.normcase(str(parent))
            if k not in self.dirs:self.stack.enter_context(Guard(parent,True));self.dirs.add(k)
        self.stack.enter_context(Guard(p));self.checkpoint();require(digest(p)==sha,'SHA mismatch:'+str(p),'CONFLICT')
        self.files[key]={'file':str(p),'sha256':sha};return p
    def recheck(self):
        for d in self.files.values():self.checkpoint();require(digest(d['file'])==d['sha256'],'Guarded input changed','CONFLICT')

def runtime_manifest(modules):
    result={}
    for name,module in modules.items():
        base=Path(module.__file__).parent
        result[name]=sorted(str(p.resolve()) for p in base.rglob('*') if p.is_file() and p.suffix.lower() in ['.py','.pyd','.dll'])
    base=Path(sys.executable).parent
    result['python_native']=sorted({str(p.resolve()) for directory in [base,base.parent,base.parent/'DLLs'] for p in directory.glob('*') if p.is_file() and p.suffix.lower() in ['.pyd','.dll']})
    return result

def _worker_command(p,job):
    return [p['runtime']['python']['file'],'-B',str(Path(__file__).resolve()),'--worker',str(job)]

def _owned_worker_lifetime(command,job,python,timeout_seconds,checkpoint,stdout,stderr):
    """One newly owned gated JobObject, independent first-error grace, no PID-name kill."""
    started=time.monotonic();first_error=None;cancel_started=None;tree=None;terminated=False;cleanup_errors=[]
    gate=job/'encode'/'owned-worker-gate';gate.mkdir(exist_ok=False)
    lifetime={'ownership':'new ProcessTree JobObject, bootstrap gate before business child creation','cancel_grace_seconds':10,'termination_wait_bound_seconds':10,'worker_report_required_for_cancel':False,'process_control_scope':'this new JobObject only','bootstrap':str(Path(__file__).with_name('bootstrap.py'))}
    def cancel(error):
        nonlocal first_error,cancel_started
        if first_error is None:
            first_error=error;cancel_started=time.monotonic()
            lifetime['first_error']={'code':getattr(error,'code','HOST_FAILED'),'type':type(error).__name__,'message':str(error)}
            lifetime['first_error_elapsed_seconds']=cancel_started-started
            try:(job/'encode.cancel.request').write_text('owned first-error cancellation',encoding='utf-8')
            except OSError as e:cleanup_errors.append('cancel marker:'+str(e))
    try:
        env=os.environ.copy();env['PYTHONDONTWRITEBYTECODE']='1'
        name='blenderctl-m4-'+uuid.uuid4().hex;lifetime['owned_job_name']=name
        # ProcessTree returns only after JobObject assignment. bootstrap is still waiting.
        tree=owned_processes.ProcessTree([python,'-B',str(Path(__file__).with_name('bootstrap.py')),str(gate),*command],job_name=name,stdout=stdout,stderr=stderr,env=env,shell=False)
        lifetime['bootstrap_pid']=tree.process.pid;lifetime['assignment_returned_elapsed_seconds']=time.monotonic()-started
        atomic_json(job/'encode'/'owned-process-identity.json',{'job_name':name,'bootstrap_pid':tree.process.pid,'ownership':'new task-owned JobObject; business gate still closed'})
        checkpoint()
        with (gate/'worker.gate').open('x',encoding='utf-8') as f:f.write('owned job assigned; child launch allowed')
        lifetime['gate_opened_elapsed_seconds']=time.monotonic()-started
        while tree.process.poll() is None:
            if first_error is None:
                try:
                    checkpoint();require(time.monotonic()-started<=timeout_seconds+10,'Host runtime timeout','TIMEOUT')
                except KeyboardInterrupt:cancel(EncodeError('CANCELLED','Owned supervisor interrupted'))
                except Exception as error:cancel(error)
            if cancel_started is not None and time.monotonic()-cancel_started>=10:
                terminated=True;lifetime['forced_termination_elapsed_seconds']=time.monotonic()-started
                try:tree.terminate()
                except Exception as error:cleanup_errors.append('owned terminate:'+str(error))
                break
            time.sleep(.05)
        lifetime['bootstrap_returncode']=tree.process.poll()
    except KeyboardInterrupt:cancel(EncodeError('CANCELLED','Owned supervisor interrupted before gate'))
    except Exception as error:
        if first_error is None:cancel(error)
        else:cleanup_errors.append('lifecycle:'+str(error))
    finally:
        if tree is not None:
            try:tree.close()  # KILL_ON_JOB_CLOSE also removes surviving owned descendants on normal/fault root exit.
            except Exception as error:cleanup_errors.append('owned close:'+str(error))
        lifetime.update(seconds=time.monotonic()-started,forced_termination=terminated,cleanup_errors=cleanup_errors,first_error_to_return=None if first_error is None else getattr(first_error,'code','HOST_FAILED'))
        if cancel_started is not None:lifetime['first_error_to_cleanup_end_seconds']=time.monotonic()-cancel_started
        try:atomic_json(job/'encode-lifecycle.json',lifetime)
        except Exception as error:
            # Diagnostic IO cannot turn first CANCELLED/TIMEOUT into WORKER_FAILED.
            if first_error is None:first_error=error
            try:(job/'encode-lifecycle-write-error.log').write_text(str(error),encoding='utf-8')
            except OSError:pass
    return lifetime,first_error

def _run_owned(params,job,*,cancel_parent=None):
    """Pure Host entry point; numeric runtime is explicit and SHA-bound, no bpy import."""
    p=normalize_params(params);job=Path(job).resolve();require(job.is_dir(),'Runner-owned existing job directory required')
    target=job/'encode';require(not target.exists() and not (job/'encode-report.json').exists(),'Fresh owned attempt required','CONFLICT');target.mkdir()
    identity=implementation_identity();binding={'request':p,'request_signature':signature({k:v for k,v in p.items() if k!='resume'}),'implementation':identity}
    atomic_json(target/'binding.json',binding)
    with ExitStack() as stack:
        def host_checkpoint():
            if cancel_parent:cancel_parent()
            require(not (job/'cancel.request').exists(),'Cancellation requested','CANCELLED')
        inputs=Inputs(stack,host_checkpoint)
        for d in anchor_documents(p)+[from_row(d) for d in identity['files']]:inputs.protect(d)
        with (target/'worker.stdout.log').open('x',encoding='utf-8') as stdout,(target/'worker.stderr.log').open('x',encoding='utf-8') as stderr:
            lifetime,first_error=_owned_worker_lifetime(_worker_command(p,job),job,p['runtime']['python']['file'],p['budget']['timeout_seconds'],host_checkpoint,stdout,stderr)
        # First owned cancellation/timeout is authority even with missing/malformed worker report.
        if first_error is not None:raise first_error
        report_path=job/'encode-report.json';require(report_path.is_file(),'Worker returned no diagnostic report','WORKER_FAILED')
        inputs.recheck();report=json.loads(report_path.read_text(encoding='utf-8'))
        require(report['request_signature']==binding['request_signature'] and report['implementation']==identity,'Worker binding differs','CONFLICT')
        require(lifetime.get('bootstrap_returncode')==0 and report['status']=='passed',report.get('error',{}).get('message','Encode validation failed'),report.get('error',{}).get('code','VALIDATION_FAILED'))
        return report

def run(params,job,*,cancel_parent=None):
    """Keep diagnostic IO secondary to first cancellation/timeout authority."""
    try:return _run_owned(params,job,cancel_parent=cancel_parent)
    except Exception as error:
        owned=Path(job).resolve();target=owned/'encode-report.json'
        if owned.is_dir() and (owned/'encode').is_dir() and not target.exists():
            try:
                p=normalize_params(params)
                atomic_json(target,{'encode_report_version':'1.0-candidate','status':'failed','request_signature':signature({k:v for k,v in p.items() if k!='resume'}),'implementation':implementation_identity(),'original_request':{k:v for k,v in p.items() if k!='resume'},'error':{'code':getattr(error,'code','HOST_FAILED'),'type':type(error).__name__,'message':str(error)},'partial_files':[str(x) for x in (owned/'encode').iterdir() if x.is_file()]})
            except Exception as diagnostic_error:
                try:(owned/'encode-host-diagnostic-error.log').write_text(str(diagnostic_error),encoding='utf-8')
                except OSError:pass
        raise


def worker(job):
    import numpy as np
    import PyOpenColorIO as ocio
    import OpenImageIO as oiio
    job=Path(job).resolve();out=job/'encode';binding=json.loads((out/'binding.json').read_text(encoding='utf-8'));p=normalize_params(binding['request']);started=time.monotonic()
    report={'encode_report_version':'1.0-candidate','status':'failed','request_signature':binding['request_signature'],'original_request':{k:v for k,v in p.items() if k!='resume'},'implementation':binding['implementation'],'commands':[],'frames':[],'checks':{},'viewing':'not_run','real_mastering':'not_run','complete_hdr10_normative_certification':'not_run'}
    def checkpoint():
        require(not (job/'cancel.request').exists() and not (job/'encode.cancel.request').exists(),'Owned cancellation requested','CANCELLED')
        require(time.monotonic()-started<=p['budget']['timeout_seconds'],'Worker timeout','TIMEOUT')
    def command(argv,label):
        checkpoint();begin=time.monotonic()
        with (out/(label+'.stdout.log')).open('x',encoding='utf-8') as stdout,(out/(label+'.stderr.log')).open('x',encoding='utf-8') as stderr:
            child=subprocess.Popen([str(x) for x in argv],stdout=stdout,stderr=stderr,shell=False)
            try:
                while child.poll() is None:checkpoint();time.sleep(.1)
            except BaseException:
                child.terminate();child.wait(timeout=10);raise
        report['commands'].append({'label':label,'argv':[str(x) for x in argv],'returncode':child.returncode,'seconds':time.monotonic()-begin})
        require(child.returncode==0,'Subprocess failed:'+label+'; see owned stderr','ENCODER_FAILED')
    def eotf(signal,upper=True):
        x=np.clip(signal,0,1) if upper else np.maximum(signal,0)
        t=np.asarray(x,dtype=np.float64)**(32/2523);den=2413/128-2392/128*t
        require(bool(np.all(den>0)),'Positive PQ extrapolation denominator invalid')
        return 10000*(np.maximum(t-3424/4096,0)/den)**(16384/2610)
    def oetf(light):
        x=(light/10000)**(2610/16384)
        return ((3424/4096+2413/128*x)/(1+2392/128*x))**(2523/32)
    def stats(rgb):
        physical=eotf(rgb);positive=eotf(rgb,False);maxrgb=np.max(physical,axis=-1)
        high=rgb>1;low=rgb<0;peak=int(np.argmax(rgb));bottom=int(np.argmin(rgb))
        return {'max_cll_nits':float(maxrgb.max()),'frame_average_nits':float(maxrgb.mean()),'positive_pq_extrapolated_peak_nits':float(positive.max()),'raw_min':float(rgb.min()),'raw_max':float(rgb.max()),'above_one_channel_samples':int(high.sum()),'below_zero_channel_samples':int(low.sum()),'excursion_pixels':int(np.any(high|low,axis=-1).sum()),'max_location_y_x_channel':[int(x) for x in np.unravel_index(peak,rgb.shape)],'min_location_y_x_channel':[int(x) for x in np.unravel_index(bottom,rgb.shape)],'statistics_input_policy':'maxRGB, normalizedPQ0..1 clipping only for statistics; not CTA mandate; raw output retained'}
    def compare(ref,actual,rois):
        weights=np.array([.2627,.678,.0593]);r=eotf(ref);a=eotf(actual);delta=actual-ref
        expected=float(np.mean(r@weights));observed=float(np.mean(a@weights));tol=.01 if expected<.0001 else max(.05,.03*expected)
        rows=[]
        for roi in rois:
            x,y,wid,hei=roi['rect'];require(x+wid<=ref.shape[1] and y+hei<=ref.shape[0],'ROI outside reference')
            sr=r[y:y+hei,x:x+wid];sa=a[y:y+hei,x:x+wid]
            require(float(np.max(np.ptp(sr,axis=-1)))<max(.05,float(sr.max())*1e-5),'Gray ROI reference is chromatic; not a gray test')
            ex=float(np.mean(sr@weights));ob=float(np.mean(sa@weights));t=.01 if ex<.0001 else max(.05,.03*ex)
            rows.append({'label':roi['label'],'reference_nits':ex,'decoded_nits':ob,'absolute_error':abs(ex-ob),'relative_error':abs(ex-ob)/ex if ex>.0001 else None,'tolerance_nits':t,'status':'pass' if abs(ex-ob)<=t else 'fail'})
        edge=np.zeros(ref.shape[:2],bool);dx=np.max(np.abs(ref[:,1:]-ref[:,:-1]),axis=-1)>.02;dy=np.max(np.abs(ref[1:]-ref[:-1]),axis=-1)>.02
        edge[:,1:]|=dx;edge[:,:-1]|=dx;edge[1:]|=dy;edge[:-1]|=dy
        return {'full_frame_mean_luminance':{'reference_nits':expected,'decoded_nits':observed,'absolute_error':abs(expected-observed),'tolerance_nits':tol,'status':'pass' if abs(expected-observed)<=tol else 'fail'},'gray':{'status':'pass' if rows and all(r['status']=='pass' for r in rows) else 'not_applicable' if not rows else 'fail','rows':rows,'thresholds':{'relative':.03,'abs_floor_nits':.05,'black_upper_nits':.01}},'pq_channel_mae':float(np.mean(np.abs(delta))),'pq_channel_max_abs':float(np.max(np.abs(delta))),'edge_threshold_pq':.02,'edge_pixels':int(edge.sum()),'edge_pq_mae':float(np.mean(np.abs(delta[edge]))) if edge.any() else None,'edge_pq_max_abs':float(np.max(np.abs(delta[edge]))) if edge.any() else None,'luminance_mae_nits':float(np.mean(np.abs((a-r)@weights)))}
    try:
        with ExitStack() as stack:
            inputs=Inputs(stack,checkpoint)
            for d in anchor_documents(p)+[from_row(x) for x in binding['implementation']['files']]+[{'file':str(out/'binding.json'),'expected_sha256':digest(out/'binding.json')}]:inputs.protect(d)
            require(implementation_identity()==binding['implementation'],'Implementation changed','CONFLICT')
            discovered=runtime_manifest({'PyOpenColorIO':ocio,'OpenImageIO':oiio,'numpy':np})
            for name,paths in discovered.items():require(set(map(os.path.normcase,paths))==set(os.path.normcase(str(Path(x['file']).resolve())) for x in p['runtime']['modules'][name]),'Runtime module closure differs:'+name,'CONFLICT')
            require(Path(sys.executable).resolve()==Path(p['runtime']['python']['file']).resolve(),'Python runtime path differs','CONFLICT')
            producer=json.loads(Path(p['producer']['file']).read_text(encoding='utf-8'));documents,selected=producer_documents(producer,p)
            for d in documents:inputs.protect(d)
            report['producer']=p['producer'];report['producer_candidate']=producer['candidate'];report['source_color_identity']=producer['source_color_identity'];report['encode_color']=p['encode_color'];report['mastering']=p['mastering'];report['mastering_provenance']='synthetic technical_target only; not measured Mastering display'
            report['input_guarded_files']=list(inputs.files.values());report['runtime_versions']={'python':sys.version,'OCIO':ocio.__version__,'OIIO':oiio.VERSION_STRING,'numpy':np.__version__}
            width,height=selected[0]['width'],selected[0]['height'];count=len(selected) if p['select']['mode']=='produced_frames' else p['select']['repeat_count']
            require(type(width)==int and type(height)==int and 16<=width<=4096 and 16<=height<=4096 and width%2==0 and height%2==0 and width*height*count<=p['budget']['max_pixel_frames'],'420 dimensions or declared pixel-frame budget invalid')
            report['timing']={'fps':p['fps'],'frame_count':count,'mode':p['select']['mode'],'source_frames':p['select']['frames'],'motion_claim':'none; explicitly repeated still' if p['select']['mode']=='still_repeat' else 'producer frames only'}
            cs=selected[0]['output_color_space'];cfg=ocio.Config.CreateFromFile(producer['source_color_identity']['ocio']['file']);cfg.validate()
            require(cfg.getColorSpace(cs['name']) is not None and cfg.getColorSpace(cs['name']).getInteropID()==cs['interop_id'],'Output OCIO space/interop differs')
            working=producer['source_color_identity']['working_space'];require(cfg.getColorSpace(ocio.ROLE_SCENE_LINEAR).getName()==working['name'] and cfg.getColorSpace(working['name']).getInteropID()==working['interop_id'],'Producer working-role differs from actual OCIO')
            policy=mapping_policy(p);hdr=policy['kind']=='OCIO_DISPLAY_VIEW'
            if hdr:
                require(ocio.__version__=='2.5.0','This exact AgX mapping currently qualifies OCIO2.5.0 only','UNSUPPORTED')
                processor=cfg.getProcessor(cs['name'],policy['display'],policy['view'],ocio.TRANSFORM_DIR_FORWARD)
            else:processor=cfg.getProcessor(cs['name'],'Linear Rec.2020')
            cpu=processor.getOptimizedCPUProcessor(ocio.BIT_DEPTH_F32,ocio.BIT_DEPTH_F32,ocio.OPTIMIZATION_NONE)
            consumed=list(processor.getProcessorMetadata().getFiles());looks=list(processor.getProcessorMetadata().getLooks())
            closure={os.path.normcase(str(Path(d['file']).resolve())) for d in producer['color_immutable_inputs']['files']}
            require(not looks and all(os.path.normcase(str(Path(f).resolve())) in closure for f in consumed),'Processor look or consumed file outside guarded OCIO closure','CONFLICT')
            report['mapping_policy']=policy
            report['reference_transform']={'source_actual_output_space':cs,'target_linear_space':None if hdr else 'Linear Rec.2020','method':'Exact OCIO display/view CPU F32 OPTIMIZATION_NONE, explicit EV before view, alreadyPQ, no white gain' if hdr else 'OCIO CPU F32 OPTIMIZATION_NONE then analytic ST2084, explicit reference white and EV','processor_cache_id':processor.getCacheID(),'processor_files':consumed,'processor_looks':looks,'source_view_preserved_record_only':producer['source_color_identity']['view'],'source_exposure_recorded_only':producer['source_color_identity']['view']['exposure'],'encode_exposure_applied_only':p['encode_color']['exposure'],'source_display_policy':'EXPLICIT_OVERRIDE; no source style or perceptual equivalence acceptance'}
            if hdr:
                neutral=np.ones((1,3),np.float32);cpu.applyRGB(neutral)
                report['reference_transform'].update(declared_display_reference_white_nits=100,extra_gain='NONE',scene_unity_nits_channels=eotf(neutral)[0].tolist(),scene_unity_weighted_Y_nits=float(eotf(neutral)[0]@np.array([.2627,.678,.0593])),view_nominal_peak_nits=1000,strict_decoded_peak_guarantee=False)
            if 'resume' in p:
                old=json.loads(Path(p['resume']['file']).read_text(encoding='utf-8'))
                require(old.get('status')=='passed' and old['request_signature']==report['request_signature'] and old['implementation']==report['implementation'],'Only identical passed previous output can be reused','CONFLICT')
                for d in old['artifacts']:inputs.protect(from_row(d))
                report.update(old);report['resumed_from']=p['resume'];report['reused_validated_output']=True;report['commands']=[]
                inputs.recheck();report['resume_input_sha_recheck']='pass';report['seconds']=time.monotonic()-started
                atomic_json(job/'encode-report.json',report);return
            refs=[];source_decode=[];alpha_rows=[];raw=out/'reference-planar-gbr.f32';reference_stats=[]
            with raw.open('xb') as stream:
                for index in range(count):
                    checkpoint();row=selected[index] if p['select']['mode']=='produced_frames' else selected[0]
                    reader=oiio.ImageInput.open(row['file']);require(reader is not None,'Cannot open producer EXR')
                    try:
                        spec=reader.spec();require(reader.format_name().lower()=='openexr' and str(spec.format)=='float' and all(str(t)=='float' for t in spec.channelformats) and list(spec.channelnames)==['R','G','B','A'] and (spec.width,spec.height)==(width,height),'Actual RGBA32f EXR container differs')
                        require((spec.x,spec.y,spec.full_x,spec.full_y)==(0,0,0,0) and (spec.full_width,spec.full_height)==(width,height) and spec.get_int_attribute('Orientation',1)==1,'EXR data/display window or orientation unsupported')
                        rgba=np.asarray(reader.read_image(format=oiio.FLOAT));require(rgba.shape==(height,width,4) and np.isfinite(rgba).all(),'Malformed/nonfinite producer frame')
                        require(not reader.seek_subimage(1,0),'Multilayer EXR not qualified')
                        interop=spec.get_string_attribute('colorInteropID');require(interop==cs['interop_id'],'Independent EXR interop metadata missing/differs')
                        alpha=rgba[:,:,3];require(float(alpha.min())>=0 and float(alpha.max())<=1,'Alpha outside0..1')
                        rgb=rgba[:,:,:3].copy()
                        if p['alpha']['mode']=='require_opaque':require(bool(np.all(alpha==1)),'Actual frame nonopaque; cannot drop Alpha')
                        else:rgb=rgb+(1-alpha[:,:,None])*np.array(p['alpha']['matte_rgb_output_linear'],dtype=np.float32)
                        alpha_rows.append({'index':index,'min':float(alpha.min()),'max':float(alpha.max()),'policy':p['alpha'],'association':'opaque' if p['alpha']['mode']=='require_opaque' else 'explicit caller PREMUL assertion; physical association not inferable from pixels','formula':'RGBpremul+(1-A)*matte in actual output-linear space' if p['alpha']['mode']=='composite_matte' else 'all A exactly1'})
                        if index==0:
                            inspection_view='AgX' if hdr else 'Standard'
                            display=cfg.getProcessor(cs['name'],'sRGB',inspection_view,ocio.TRANSFORM_DIR_FORWARD).getOptimizedCPUProcessor(ocio.BIT_DEPTH_F32,ocio.BIT_DEPTH_F32,ocio.OPTIMIZATION_NONE)
                            preview=rgb.copy()*np.float32(2**p['encode_color']['exposure']);display.applyRGB(preview)
                            step=max(1,math.ceil(max(width,height)/1600));preview=np.clip(preview[::step,::step],0,1)
                            image=oiio.ImageOutput.create(str(out/'standard-sdr-inspection.png'));ps=oiio.ImageSpec(preview.shape[1],preview.shape[0],3,oiio.UINT8);require(image.open(str(out/'standard-sdr-inspection.png'),ps),'Cannot write SDR inspection preview');require(image.write_image((preview*255+.5).astype(np.uint8)),'SDR preview write failed');image.close()
                            report['sdr_preview']={**describe(out/'standard-sdr-inspection.png'),'width':preview.shape[1],'height':preview.shape[0],'view':inspection_view,'scope':inspection_view+' sRGB SDR inspection only; not HDR viewing or style acceptance'}
                            inputs.protect(from_row(report['sdr_preview']))
                        if hdr:
                            rgb*=np.float32(2**p['encode_color']['exposure']);cpu.applyRGB(rgb)
                            require(bool(np.isfinite(rgb).all() and np.all((rgb>=0)&(rgb<=1))),'OCIO view output outside finite physical PQ domain; no implicit clipping')
                            pq=rgb
                        else:
                            cpu.applyRGB(rgb);light=rgb.astype(np.float64)*p['encode_color']['reference_white_nits']*(2**p['encode_color']['exposure'])
                            require(bool(np.all((light>=0)&(light<=10000))),'RETAIN_REFERENCE physical PQ input domain exceeded; no implicit clipping')
                            pq=oetf(light).astype(np.float32)
                        ref=out/f'reference-{index+1:06d}.npy';np.save(ref,pq,allow_pickle=False);refs.append(describe(ref));inputs.protect(from_row(refs[-1]));reference_stats.append(stats(pq));np.stack([pq[:,:,1],pq[:,:,2],pq[:,:,0]]).astype('<f4').tofile(stream)
                        source_decode.append({'index':index+1,'producer_frame':row['frame'],'source':from_row(row),'actual_output_linear':cs,'alpha':alpha_rows[-1]})
                    finally:reader.close()
            report['references']=refs;report['source_decode']=source_decode;report['reference_stats']=reference_stats
            inputs.protect(from_row(describe(raw)))
            fps=str(p['fps'][0])+'/'+str(p['fps'][1]);size=str(width)+'x'+str(height);ffmpeg=p['runtime']['ffmpeg']['file'];ffprobe=p['runtime']['ffprobe']['file'];yuv=out/'prepared-limited42010.yuv'
            vf='zscale=matrixin=gbr:matrix=bt2020nc:primariesin=bt2020:primaries=bt2020:transferin=smpte2084:transfer=smpte2084:rangein=full:range=limited:npl='+str(100 if hdr else p['encode_color']['reference_white_nits'])+':agamma=0:dither=error_diffusion,format=yuv420p10le'
            command([ffmpeg,'-v','error','-f','rawvideo','-pixel_format','gbrpf32le','-video_size',size,'-framerate',fps,'-i',raw,'-vf',vf,'-f','rawvideo','-pix_fmt','yuv420p10le','-n',yuv],'prepare-yuv')
            inputs.protect(from_row(describe(yuv)))
            pending={'max_cll':math.ceil(max(s['max_cll_nits'] for s in reference_stats)),'max_fall':math.ceil(max(s['frame_average_nits'] for s in reference_stats))};attempts=[]
            for attempt in range(1,3):
                video=out/f'attempt-{attempt:02d}.mp4';master='G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)' if hdr else 'G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)L(100000000,1)'
                params=':'.join(['hdr10=1','hdr10-opt=0','master-display='+master,f"max-cll={pending['max_cll']},{pending['max_fall']}",'repeat-headers=1','colorprim=bt2020','transfer=smpte2084','colormatrix=bt2020nc','range=limited','pools=none','frame-threads=1','wpp=0','bframes=0','keyint='+str(max(count,3)),'log-level=1'])
                cicp=['-color_primaries','bt2020','-color_trc','smpte2084','-colorspace','bt2020nc','-color_range','tv']
                command([ffmpeg,'-v','error','-f','rawvideo','-pixel_format','yuv420p10le','-video_size',size,'-framerate',fps,*cicp,'-i',yuv,'-c:v','libx265','-preset',p['quality']['preset'],'-crf',str(p['quality']['crf']),'-profile:v','main10','-pix_fmt','yuv420p10le',*cicp,'-x265-params',params,'-tag:v','hvc1','-movflags','+faststart+write_colr','-metadata','comment=Virtual technical target only; no measured mastering display','-an','-n',video],f'encode-{attempt}')
                inputs.protect(from_row(describe(video)))
                command([ffprobe,'-v','error','-select_streams','v:0','-count_frames','-show_streams','-show_frames','-show_format','-of','json',video],f'probe-{attempt}')
                probe=json.loads((out/f'probe-{attempt}.stdout.log').read_text(encoding='utf-8'));atomic_json(out/f'probe-{attempt}.json',probe)
                inputs.protect(from_row(describe(out/f'probe-{attempt}.json')))
                stream=probe['streams'][0];frames=probe.get('frames',[])
                frame_cicp={'pix_fmt':'yuv420p10le','color_range':'tv','color_primaries':'bt2020','color_transfer':'smpte2084','color_space':'bt2020nc','width':width,'height':height}
                formatok=all(stream.get(k)==v for k,v in {'codec_name':'hevc','profile':'Main 10','codec_tag_string':'hvc1',**frame_cicp,'nb_read_frames':str(count)}.items()) and Fraction(stream['avg_frame_rate'])==Fraction(*p['fps']) and len(frames)==count and all(all(f.get(k)==v for k,v in frame_cicp.items()) for f in frames)
                duration=Fraction(count*p['fps'][1],p['fps'][0]);timebase=Fraction(stream['time_base']);timingok=abs(Fraction(stream['duration'])-duration)<=timebase
                for i,f in enumerate(frames):timingok=timingok and abs(Fraction(f['best_effort_timestamp_time'])-Fraction(i*p['fps'][1],p['fps'][0]))<=timebase
                expected_master={'red_x':Fraction(35400,50000),'red_y':Fraction(14600,50000),'green_x':Fraction(8500,50000),'green_y':Fraction(39850,50000),'blue_x':Fraction(6550,50000),'blue_y':Fraction(2300,50000),'white_point_x':Fraction(15635,50000),'white_point_y':Fraction(16450,50000),'min_luminance':Fraction(1,10000),'max_luminance':Fraction(10000)}
                if hdr:expected_master.update(red_x=Fraction(34000,50000),red_y=Fraction(16000,50000),green_x=Fraction(13250,50000),green_y=Fraction(34500,50000),blue_x=Fraction(7500,50000),blue_y=Fraction(3000,50000),max_luminance=Fraction(1000))
                metadataok=True
                for f in frames:
                    masters=[s for s in f.get('side_data_list',[]) if s.get('side_data_type')=='Mastering display metadata'];clls=[s for s in f.get('side_data_list',[]) if s.get('side_data_type')=='Content light level metadata']
                    metadataok=metadataok and len(masters)==1 and len(clls)==1 and all(Fraction(masters[0].get(k,'-1'))==v for k,v in expected_master.items()) and clls[0].get('max_content')==pending['max_cll'] and clls[0].get('max_average')==pending['max_fall']
                decoded=out/f'decoded-{attempt}.gbr.f32';dvf='zscale=matrixin=bt2020nc:matrix=gbr:primariesin=bt2020:primaries=bt2020:transferin=smpte2084:transfer=smpte2084:rangein=limited:range=full:agamma=0,format=gbrpf32le'
                command([ffmpeg,'-v','error','-i',video,'-vf',dvf,'-f','rawvideo','-pix_fmt','gbrpf32le','-n',decoded],f'decode-{attempt}')
                inputs.protect(from_row(describe(decoded)))
                require(decoded.stat().st_size==count*width*height*3*4,'Full decode byte count differs')
                mem=np.memmap(decoded,dtype='<f4',mode='r',shape=(count,3,height,width));observed=[]
                for i in range(count):
                    checkpoint();rgb=np.stack([mem[i,2],mem[i,0],mem[i,1]],axis=-1);require(np.isfinite(rgb).all(),'Nonfinite decoded pixels');ref=np.load(refs[i]['file'],allow_pickle=False)
                    observed.append({'index':i+1,'pts':frames[i]['best_effort_timestamp_time'],'stats':stats(rgb),'fidelity':compare(ref,rgb,p['quality']['gray_rois'])})
                del mem
                decoded_yuv=out/f'decoded-{attempt}.yuv420p10'
                command([ffmpeg,'-v','error','-i',video,'-f','rawvideo','-pix_fmt','yuv420p10le','-n',decoded_yuv],f'decode-yuv-{attempt}')
                inputs.protect(from_row(describe(decoded_yuv)))
                require(decoded_yuv.stat().st_size==count*width*height*3,'Full10bit420 YUV byte count differs')
                ys=np.memmap(decoded_yuv,dtype='<u2',mode='r',shape=(count,width*height*3//2));n=width*height;rangeok=True
                for i in range(count):
                    planes={'Y':ys[i,:n],'Cb':ys[i,n:n+n//4],'Cr':ys[i,n+n//4:]};code_rows={}
                    for name,v in planes.items():
                        lo,hi=(64,940) if name=='Y' else (64,960);outside=int(np.sum((v<4)|(v>1019)));rangeok=rangeok and outside==0
                        code_rows[name]={'min':int(v.min()),'max':int(v.max()),'outside_storage_0_1023':int(np.sum(v>1023)),'below_nominal':int(np.sum(v<lo)),'above_nominal':int(np.sum(v>hi)),'outside_bt2100_video_data_4_1019':outside}
                    observed[i]['yuv10_codes']=code_rows
                del planes,v,ys
                actual={'max_cll':math.ceil(max(f['stats']['max_cll_nits'] for f in observed)),'max_fall':math.ceil(max(f['stats']['frame_average_nits'] for f in observed))}
                attempts.append({'attempt':attempt,'video':describe(video),'decoded':describe(decoded),'decoded_yuv':describe(decoded_yuv),'probe':describe(out/f'probe-{attempt}.json'),'format':'pass' if formatok else 'fail','bt2100_video_data':'pass' if rangeok else 'fail','all_pts_duration':'pass' if timingok else 'fail','every_frame_metadata':'pass' if metadataok else 'fail','metadata_bound_to_decode':pending==actual,'expected_cll_fall':pending,'decoded_cll_fall':actual,'frames':observed})
                if pending==actual:break
                pending=actual
            last=attempts[-1];report['attempts']=attempts;report['final_video']=last['video'];report['frames']=last['frames']
            if hdr:report['strict_decoded_1000nit_diagnostic']={'status':'pass' if max(f['stats']['positive_pq_extrapolated_peak_nits'] for f in last['frames'])<=1000.001 else 'fail','is_signal_or_fidelity_gate':False,'reason':'view name/nominal reference target does not promise reconstructed decoded peak; no normativeHDR10 claim'}
            fidelityok=all(f['fidelity']['full_frame_mean_luminance']['status']=='pass' and f['fidelity']['gray']['status'] in ['pass','not_applicable'] for f in last['frames'])
            report['checks'].update(format=last['format'],bt2100_video_data=last['bt2100_video_data'],full_pts_duration=last['all_pts_duration'],every_frame_metadata=last['every_frame_metadata'],final_decode_metadata_binding='pass' if last['metadata_bound_to_decode'] else 'fail',full_frame_reference_fidelity='pass' if fidelityok else 'fail',zero_rgb_excursions='diagnostic_only_not_normative_gate',alpha='pass',all_inputs_sha='pending')
            inputs.recheck();report['input_guarded_files']=list(inputs.files.values());report['checks']['all_inputs_sha']='pass';require(all(report['checks'][k]=='pass' for k in ['format','bt2100_video_data','full_pts_duration','every_frame_metadata','final_decode_metadata_binding','full_frame_reference_fidelity','alpha','all_inputs_sha']),'Bounded encode verification failed')
            report['status']='passed';report['reused_validated_output']=False
            report['limits']=[('AgXHDR exact view override; source exposure recorded, explicit encode EV only, no white gain or perceptual match acceptance.' if hdr else 'Standard explicit override changes sourceAgX appearance; no artistic acceptance.'),'No measured mastering display or HDR viewing.','NormalizedPQ statistics clipping is explicit policy, not established CTA mandate.','RGB excursions retained as diagnostics, not automatically normativeFAIL.','NativeHIGH/LOSSLESS3%FAIL and strict1000 experimentFAIL remain unchanged.']
            report['artifacts']=[describe(x) for x in out.iterdir() if x.is_file() and x.name not in ['worker.stdout.log','worker.stderr.log','binding.json']]
    except Exception as e:
        report['error']={'code':getattr(e,'code','WORKER_FAILED'),'type':type(e).__name__,'message':str(e)}
        report['partial_files']=[str(x) for x in out.iterdir() if x.is_file()]
    report['seconds']=time.monotonic()-started;atomic_json(job/'encode-report.json',report)
    if report['status']!='passed':raise EncodeError(report['error']['code'],report['error']['message'])

if __name__=='__main__':
    try:
        require(len(sys.argv)==3 and sys.argv[1]=='--worker','Worker invocation required');worker(sys.argv[2])
    except Exception as e:print(type(e).__name__+': '+str(e),file=sys.stderr);sys.exit(1)
