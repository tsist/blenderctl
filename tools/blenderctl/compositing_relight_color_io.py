"""Exact straight-alpha reader for PNG8/float32 EXR. No implicit association."""
import os,struct,uuid,zlib

LINEAR_EXR_CONTRACT='linear-straight-exr-v1'
LINEAR_EXR_SEMANTICS={'color_space':'scene_linear_Rec709','alpha_mode':'STRAIGHT',
                      'precision':'FLOAT32','compression':'ZIP','display_transform':'NONE',
                      'rgb_clamped':False,'source_role':'output'}

def write_linear_straight_exr(io,path,rgba,domain,checkpoint=lambda:None):
    """Write only an owned new FLOAT32 ZIP EXR; leave every failure partial.

    The caller must guard, independently decode and bind it before publication.
    A short same-directory staging filename retains format detection and avoids
    Windows MAX_PATH growth. Windows rename refuses an existing destination.
    """
    np=io.np;oiio=io.oiio;need=io.need;h,w=domain['height'],domain['width']
    need(path.is_absolute() and path.suffix.lower()=='.exr','Absolute EXR path required')
    need(not path.exists(),'EXR target exists','CONFLICT')
    need(type(rgba) is np.ndarray and rgba.shape==(h,w,4) and rgba.dtype==np.float32 and
         rgba.flags.c_contiguous and np.isfinite(rgba).all() and
         ((rgba[...,3]>=0)&(rgba[...,3]<=1)).all(),'Finite contiguous float32 RGBA with Alpha0..1 required')
    checkpoint()
    for _ in range(8):
        temp=path.with_name('.e'+uuid.uuid4().hex[:8]+'.exr')
        try:
            with temp.open('xb'):pass
            break
        except FileExistsError:continue
    else:raise FileExistsError('EXR staging collisions exhausted')
    spec=oiio.ImageSpec(w,h,4,oiio.FLOAT);spec.channelnames=['R','G','B','A']
    spec.attribute('compression','zip');spec.attribute('oiio:UnassociatedAlpha',1)
    spec.attribute('oiio:ColorSpace','Linear');spec.attribute('wm:color_space','scene_linear_Rec709')
    spec.attribute('wm:alpha_mode','STRAIGHT')
    writer=oiio.ImageOutput.create(str(temp));need(writer is not None,'EXR writer unavailable')
    try:
        need(writer.open(str(temp),spec),'EXR output open failed')
        need(writer.write_image(rgba),'EXR image write failed')
    finally:closed=writer.close()
    need(closed,'EXR close failed')
    with temp.open('r+b') as stream:stream.flush();os.fsync(stream.fileno())
    checkpoint();need(not path.exists(),'EXR target appeared','CONFLICT')
    os.rename(temp,path)
    checkpoint()

def read_linear_straight_exr(io,path,domain):
    """Final-output-only metadata check, then a complete independent decode.

    Generic read() continues to accept its existing input formats/compressions.
    This canonical colorInteropID mapping is qualified for bundled OIIO3.1.13.1.
    UnassociatedAlpha is decoder configuration, not an EXR stored-alpha proof.
    """
    config=io.oiio.ImageSpec();config.attribute('oiio:UnassociatedAlpha',1)
    inp=io.oiio.ImageInput.open(str(path),config);io.need(inp is not None,'EXR output cannot open')
    try:
        spec=inp.spec()
        metadata={'compression':spec.get_string_attribute('compression'),
                  'colorInteropID':spec.get_string_attribute('colorInteropID'),
                  'color_space':spec.get_string_attribute('oiio:ColorSpace'),
                  'declared_color_space':spec.get_string_attribute('wm:color_space'),
                  'declared_alpha_mode':spec.get_string_attribute('wm:alpha_mode')}
        io.need(metadata=={'compression':'zip','colorInteropID':'lin_rec709_scene','color_space':'lin_rec709_scene',
                           'declared_color_space':'scene_linear_Rec709','declared_alpha_mode':'STRAIGHT'},
                'Final EXR ZIP/Rec709/straight declaration differs')
    finally:inp.close()
    pixels,decoded=read(io,path,domain,'openexr',4)
    return pixels,decoded|metadata

def read(io,path,domain,container,channels):
    np=io.np;oiio=io.oiio;need=io.need;h,w=domain['height'],domain['width']
    if container=='png':
        with path.open('rb') as f:
            header=f.read(33);need(len(header)==33 and header[:8]==b'\x89PNG\r\n\x1a\n' and header[12:16]==b'IHDR','PNG8 header required')
            iw,ih,bits,colour,compression,filt,interlace=struct.unpack('>IIBBBBB',header[16:29])
            need((iw,ih)==(w,h) and bits==8 and colour=={3:2,4:6}[channels] and compression==filt==0 and interlace in (0,1),'PNG8 domain/type differs')
            f.seek(8);first=True;saw_idat=False
            while True:
                head=f.read(8);need(len(head)==8,'PNG truncated before IEND');length,tag=struct.unpack('>I4s',head)
                need(length<=path.stat().st_size-f.tell()-4,'PNG chunk out of bounds')
                need(not first or tag==b'IHDR' and length==13,'PNG first chunk differs');need(first or tag!=b'IHDR','Duplicate PNG IHDR')
                need(tag not in (b'acTL',b'fcTL',b'fdAT'),'Animated PNG unsupported');first=False
                crc=zlib.crc32(tag);remaining=length
                while remaining:
                    block=f.read(min(1048576,remaining));need(bool(block),'PNG truncated payload');crc=zlib.crc32(block,crc);remaining-=len(block)
                stored=f.read(4);need(len(stored)==4 and struct.unpack('>I',stored)[0]==crc&0xffffffff,'PNG CRC differs');saw_idat|=tag==b'IDAT'
                if tag==b'IEND':need(length==0 and saw_idat and f.read(1)==b'','PNG tail/IDAT differs');break
    config=oiio.ImageSpec();config.attribute('oiio:UnassociatedAlpha',1)
    inp=oiio.ImageInput.open(str(path),config);need(inp is not None,'Image open failed')
    try:
        s=inp.spec();names=['R','G','B']+(['A'] if channels==4 else [])
        formats=list(s.channelformats) or [s.format]*s.nchannels;fmt='uint8' if container=='png' else 'float'
        need(inp.format_name()==container and (s.width,s.height,s.depth)==(w,h,1) and s.x==s.y==s.z==0 and
             (s.full_width,s.full_height,s.full_x,s.full_y)==(w,h,0,0) and list(s.channelnames)==names and
             all(str(x)==fmt for x in formats),'Image actual window/channels/precision differ')
        pixels=inp.read_image(format=oiio.UINT8 if container=='png' else oiio.FLOAT)
        need(pixels is not None and pixels.shape==(h,w,channels) and pixels.dtype==(np.uint8 if container=='png' else np.float32),'Full pixel dtype/shape differs')
        need(not inp.seek_subimage(1,0),'Multipart unsupported')
        return np.ascontiguousarray(pixels),{'container':container,'shape':list(pixels.shape),'dtype':str(pixels.dtype),'alpha_read':'explicit unassociated','channels':names,'full_pixel_readback':True}
    finally:inp.close()

def rgb(io,desc,domain):
    path=io._path(desc)
    with path.open('rb') as f:header=f.read(33)
    io.need(len(header)==33 and header[25] in (2,6),'RGB8/RGBA8 required')
    pixels,metadata=read(io,path,domain,'png',3 if header[25]==2 else 4)
    io.need(io.sha(path)==desc['expected_sha256'],'Source changed during decode','INPUT_CHANGED')
    return pixels,metadata
