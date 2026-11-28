"""Owned bundled-Python decode/CPU compute/encode stages. No bpy/Torch/model."""
import hashlib, json, struct, sys, time, types, zlib
from pathlib import Path

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1048576), b''): h.update(block)
    return h.hexdigest()

def need(condition, message, code='VALIDATION_FAILED'):
    if not condition:
        exc=RuntimeError(message);exc.code=code;raise exc

def run(job):
    target=job/'depth';b=json.loads((target/'binding.json').read_text(encoding='utf-8'));started=time.monotonic()
    need(sys.flags.isolated and sys.flags.dont_write_bytecode, 'Isolated no-pyc runtime required')
    need(Path(sys.executable).resolve()==Path(b['runtime']['python']['file']).resolve(), 'Numeric executable differs')
    need(digest(sys.executable)==b['runtime']['python']['sha256'], 'Numeric executable SHA differs')
    need(Path(sys.pycache_prefix).is_dir() and not any(Path(sys.pycache_prefix).iterdir()), 'Fresh empty pycache prefix required')
    def checkpoint():
        need(not (job/'depth.cancel.request').exists(), 'Owned stage cancelled', 'CANCELLED')
        need(time.monotonic()-started<=b['timeout_seconds'], 'Owned stage timed out', 'TIMEOUT')
    def offline(event,args):
        if event in ('socket.connect','socket.bind','socket.getaddrinfo','urllib.Request','http.client.connect'):
            raise RuntimeError('Network forbidden')
    sys.addaudithook(offline)
    protected={}
    for r in b['input_files']:
        checkpoint();need(digest(r['file'])==r['sha256'], 'Protected input SHA differs', 'CONFLICT');protected[str(Path(r['file']).resolve())]=r['sha256']
    codes={r['role']:r for r in b['codes']}
    def pin(name, role):
        r=codes[role];data=Path(r['file']).read_bytes();need(hashlib.sha256(data).hexdigest()==r['sha256'], 'Frozen source changed')
        m=types.ModuleType(name);m.__file__=r['file'];exec(compile(data,r['file'],'exec'),m.__dict__);return m
    io=pin('relight_owned_io','depth_io');np=io.np;kernel=pin('relight_owned_kernel','relight_kernel');colorio=pin('relight_owned_color_io','color_io')
    need(digest(codes['depth_worker']['file'])==digest(__file__), 'Worker differs from binding')
    rows=[]
    def member(path, role, array):
        return {'file':str(path),'sha256':digest(path),'bytes':path.stat().st_size,'role':role,'shape':list(array.shape),'dtype':str(array.dtype)}
    def save(name,role,array):
        checkpoint();array=np.ascontiguousarray(array);path=target/name
        with path.open('xb') as f: np.save(f,array,allow_pickle=False)
        restored=io.read_npy(path,array.shape,(array.dtype,));need(np.array_equal(array,restored),'NPY readback differs')
        r=member(path,role,array);rows.append(r);return r
    def array(r,shape,dtype):
        checkpoint();need(protected.get(str(Path(r['file']).resolve()))==r['sha256'], 'Array absent from protected closure')
        return io.read_npy(r['file'],shape,(dtype,))
    mode=b['mode'];params=b['params'];domain=b['domain'];hw=(domain['height'],domain['width'])
    metadata=[]
    if mode=='pack':
        bundle=b['bundle'];original=bundle['request']['params'];members={r['role']:r for r in bundle['members']}
        derived={name:array(members[name],hw+(3,) if name=='normal' else hw,
                            np.bool_ if name in ('validity','normal_validity') else np.float32)
                 for name in ('scalar','validity','normal','normal_validity')}
        io.validate_derived(derived,original)
        if params['source']['kind']=='bundle_RGB':
            pixels,meta=colorio.rgb(io,original['rgb'],domain);metadata.append(meta)
            srgb=pixels[...,:3].astype(np.float64)/255
            linear=np.where(srgb<=.04045,srgb/12.92,((srgb+.055)/1.055)**2.4).astype(np.float32)
            alpha=pixels[...,3:4].astype(np.float32)/255 if pixels.shape[2]==4 else np.ones(hw+(1,),np.float32)
            rgba=np.ascontiguousarray(np.concatenate((linear,alpha),axis=-1))
        else:
            source=params['source'];d=source['image'];need(protected.get(str(Path(d['file']).resolve()))==d['expected_sha256'],'Override unprotected')
            if source['format']=='NPY': rgba=io.read_npy(d['file'],hw+(4,),(np.float32,))
            else:
                rgba,meta=colorio.read(io,Path(d['file']),domain,'openexr',4);metadata.append(meta)
            need(np.isfinite(rgba).all() and ((rgba[...,3]>=0)&(rgba[...,3]<=1)).all(), 'Override finite RGBA with alpha0..1 required')
        arrays=[rgba,derived['scalar'],derived['normal'],derived['validity'],derived['normal_validity']]
        for kind in ('light','highlight','line'):
            d=params['masks'][kind]
            if d is None: mask=np.full(hw,0 if kind=='line' else 1,np.float32)
            else:
                mask=io.read_npy(d['file'],hw,(np.float32,))
                need(np.isfinite(mask).all() and ((mask>=0)&(mask<=1)).all(), 'Mask finite float32 0..1 required')
            arrays.append(mask)
        for i,(name,x) in enumerate(zip(('rgba','scalar','normal','depth_valid','normal_valid','light_mask','highlight_mask','line_mask'),arrays)):
            save(str(i)+'-'+name+'.npy',name,x)
        stats={'color_decode':'exact inverse sRGB PNG8 or declared scene-linear straight override',
               'alpha_preserved':True,'source_is_albedo':False,'validity_is_confidence':False,'derived_validation':'complete float32/bool readback'}
    elif mode=='compute':
        shapes=[hw+(4,),hw,hw+(3,),hw,hw,hw,hw,hw];dtypes=[np.float32,np.float32,np.float32,np.bool_,np.bool_,np.float32,np.float32,np.float32]
        arrays=[array(r,s,d) for r,s,d in zip(b['packed'],shapes,dtypes)]
        t=time.perf_counter();out,delta,parts=kernel.render(*arrays,params['config'],kernel.Backend(np));elapsed=time.perf_counter()-t
        save('output.npy','output',out);save('delta.npy','delta',delta)
        for i,p in enumerate(parts):
            for name in ('diffuse','specular','visibility','delta'): save(str(i)+'-'+name+'.npy','light:'+str(i)+':'+name,p[name])
        stats={'device':'CPU_NUMPY','fallback':False,'kernel_seconds':elapsed,'light_ids':[p['id'] for p in parts]}
    elif mode=='encode':
        output=next(r for r in b['computed'] if r['role']=='output');rgba=array(output,hw+(4,),np.float32)
        original=array(b['packed'][0],hw+(4,),np.float32)
        need(np.array_equal(rgba[...,3],original[...,3]), 'Alpha changed during light computation')
        need(np.isfinite(rgba).all(),'Nonfinite output')
        for r in b['computed']:
            x=array(r,tuple(r['shape']),np.float32);save(Path(r['file']).name,r['role'],x)
        path=target/'output-linear-straight.exr';need(not path.exists(),'EXR target exists','CONFLICT')
        spec=io.oiio.ImageSpec(hw[1],hw[0],4,io.oiio.FLOAT);spec.channelnames=['R','G','B','A']
        spec.attribute('compression','zip');spec.attribute('oiio:UnassociatedAlpha',1);spec.attribute('oiio:ColorSpace','Linear')
        writer=io.oiio.ImageOutput.create(str(path));need(writer is not None and writer.open(str(path),spec),'EXR output open failed')
        try: need(writer.write_image(rgba),'EXR output failed')
        finally: writer.close()
        restored,meta=colorio.read(io,path,domain,'openexr',4);need(np.array_equal(restored,rgba),'Float32 EXR pixel readback differs');metadata.append(meta)
        rows.append(member(path,'linear_straight_exr',restored))
        linear=np.clip(rgba[...,:3].astype(np.float64),0,1)
        encoded=np.where(linear<=.0031308,linear*12.92,1.055*linear**(1/2.4)-.055)
        pixels=np.concatenate((np.rint(encoded*255).astype(np.uint8),np.rint(rgba[...,3:4]*255).astype(np.uint8)),axis=-1)
        def chunk(tag,data): return struct.pack('>I',len(data))+tag+data+struct.pack('>I',zlib.crc32(tag+data)&0xffffffff)
        data=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',hw[1],hw[0],8,6,0,0,0))+chunk(b'sRGB',b'\x00')
        data+=chunk(b'IDAT',zlib.compress(b''.join(b'\x00'+r.tobytes() for r in pixels)))+chunk(b'IEND',b'')
        png=target/'output-standard-srgb.png'
        with png.open('xb') as f:f.write(data)
        restored,meta=colorio.read(io,png,domain,'png',4);need(np.array_equal(restored,pixels),'Display PNG readback differs');metadata.append(meta)
        rows.append(member(png,'display_srgb_png',restored))
        stats={'float_output_unclipped':True,'alpha_bit_exact':True,'EXR_float32_lossless_readback':True,
               'display_transform':'Standard exact sRGB transfer, RGB clamped0..1 for display only',
               'display_RGB_clipped_pixels':int(((rgba[...,:3]<0)|(rgba[...,:3]>1)).any(axis=-1).sum()),'tone_map':False}
    else: raise RuntimeError('Unknown stage')
    for path,sha in protected.items():checkpoint();need(digest(path)==sha,'Protected input changed','CONFLICT')
    checkpoint()
    report={'status':'passed','mode':mode,'request_signature':b['request_signature'],'members':rows,'stats':stats,
            'seconds':time.monotonic()-started,'input_decode_metadata':metadata,'parent_success_required':True}
    with (target/'worker-report.json').open('x',encoding='utf-8') as f:json.dump(report,f,indent=2,allow_nan=False)
    return report

if __name__=='__main__':
    job=Path(sys.argv[1])
    try: run(job)
    except BaseException as exc:
        code=getattr(exc,'code','VALIDATION_FAILED');path=job/'depth/worker-report.json'
        failure={'status':'failed','error':{'code':code,'message':str(exc)},'type':type(exc).__name__,'partial_eligible':False}
        if not path.exists():
            with path.open('x',encoding='utf-8') as f:json.dump(failure,f,indent=2)
        raise SystemExit({'CANCELLED':7,'TIMEOUT':6,'CONFLICT':4}.get(code,8))
