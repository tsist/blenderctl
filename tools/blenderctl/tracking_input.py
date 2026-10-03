# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit source-index windows decoded with the bundled Blender backend."""
import bpy,math,shutil,struct,zlib
from pathlib import Path
from protocol import Failure,atomic_json,digest
from tracking_contract import validate,INPUT_REPORT

def fail(message):raise Failure('VALIDATION_FAILED',message)

def validate_png(path,width,height):
    if Path(path).stat().st_size>width*height*8+16777216:fail('PNG compressed byte budget exceeded')
    with open(path,'rb') as stream:
        if stream.read(8)!=b'\x89PNG\r\n\x1a\n':fail('Invalid PNG signature')
        first=True;data=False
        while True:
            header=stream.read(8)
            if len(header)!=8:fail('Truncated PNG chunk header')
            length,kind=struct.unpack('>I4s',header)
            if length>width*height*8+16777216:fail('PNG chunk byte budget exceeded')
            value=stream.read(length);crc=stream.read(4)
            if len(value)!=length or len(crc)!=4 or zlib.crc32(kind+value)&0xffffffff!=struct.unpack('>I',crc)[0]:fail('PNG chunk truncated or CRC mismatch')
            if first:
                if kind!=b'IHDR' or len(value)!=13 or struct.unpack('>II',value[:8])!=(width,height):fail('PNG dimensions differ')
                depth,color,compression,filter_method,interlace=value[8:]
                legal_depths={0:(1,2,4,8,16),2:(8,16),3:(1,2,4,8),4:(8,16),6:(8,16)}
                if depth not in legal_depths.get(color,()) or compression or filter_method or interlace not in (0,1):fail('Invalid PNG pixel encoding')
                first=False
            if kind==b'IDAT':data=True
            if kind==b'IEND':
                if length or not data or stream.read(1):fail('Invalid PNG end or trailing data')
                break

def verify_report(spec,rows,report):
    if not isinstance(report,dict):fail('Missing managed input mapping')
    validate(report,INPUT_REPORT)
    source=spec['source']
    if report.get('adapter')!='CLIP_INPUT_V1' or report.get('source_fps')!=source['fps'] or report.get('scene_fps')!=spec['fps'] or report.get('scene_start')!=spec['scene_start'] or report.get('kind')!=source['kind'] or len(report.get('frames',[]))!=len(rows):fail('Input mapping header differs from recipe')
    for i,(row,mapping) in enumerate(zip(rows,report['frames'])):
        original=source if source['kind']=='MOVIE' else source['files'][i]
        f=source['first_frame']+i*source['frame_step']
        expected={'clip_frame':i+1,'source_frame':f,'scene_frame':spec['scene_start']+i,
                  'source_seconds':(f-1)/source['fps'],'source':{'file':original['file'],'sha256':original['expected_sha256']},
                  'decoded_sha256':row['expected_sha256']}
        if mapping!=expected:fail('Input mapping row differs from recipe or decoded bytes')

def prepare(spec,job):
    source=spec['source'];directory=job/'frames';directory.mkdir()
    indices=[source['first_frame']+i*source['frame_step'] for i in range(source['frame_count'])]
    rows=[];mapping=[]
    bpy.ops.wm.read_factory_settings(use_empty=True)
    s=bpy.context.scene
    if source['kind']=='MOVIE':
        if digest(source['file'])!=source['expected_sha256']:fail('Movie source SHA mismatch')
        s.render.resolution_x=spec['width'];s.render.resolution_y=spec['height'];s.render.resolution_percentage=100
        s.render.fps=round(source['fps']);s.render.fps_base=round(source['fps'])/source['fps']
        clip=bpy.data.movieclips.load(source['file'])
        if list(clip.size)!=[spec['width'],spec['height']] or clip.frame_duration<indices[-1]:fail('Movie dimensions or available frame range differs')
        if not math.isfinite(clip.fps) or abs(clip.fps-source['fps'])>1e-3:fail('Declared source fps differs from native movie metadata')
        st=s.sequence_editor_create().strips.new_movie('Decode',filepath=source['file'],channel=1,frame_start=1,fit_method='FIT')
        if st.frame_duration<indices[-1] or abs(st.fps-source['fps'])>1e-3:fail('Movie strip timing differs from native clip')
        if (st.elements[0].orig_width,st.elements[0].orig_height)!=(spec['width'],spec['height']):fail('Movie strip dimensions differ')
        s.render.use_sequencer=True;s.render.engine='CYCLES';s.cycles.device='CPU';s.cycles.samples=1
        s.view_settings.view_transform='Standard';s.view_settings.look='None';s.view_settings.exposure=0;s.view_settings.gamma=1
        fmt=s.render.image_settings;fmt.media_type='IMAGE';fmt.file_format='PNG';fmt.color_mode='RGB';fmt.color_depth='8'
    for i,source_frame in enumerate(indices,1):
        target=directory/f'frame{i:06d}.png'
        if source['kind']=='MOVIE':
            s.frame_set(source_frame);s.render.filepath=str(target)
            if bpy.ops.render.render(write_still=True,scene=s.name)!={'FINISHED'}:fail('Native movie decoding did not finish')
            original={'file':source['file'],'sha256':source['expected_sha256']}
        else:
            item=source['files'][i-1]
            if digest(item['file'])!=item['expected_sha256']:fail('PNG source SHA mismatch')
            shutil.copyfile(item['file'],target)
            original={'file':item['file'],'sha256':item['expected_sha256']}
        validate_png(target,spec['width'],spec['height'])
        # Force full native decode, rather than trusting only the PNG header.
        image=bpy.data.images.load(str(target),check_existing=False)
        try:
            if list(image.size)!=[spec['width'],spec['height']] or not image.has_data:fail('PNG pixel decoding failed')
            pixels=image.pixels
            if not len(pixels) or any(not math.isfinite(pixels[j]) for j in range(0,len(pixels),max(1,len(pixels)//4096))):fail('Decoded image contains invalid pixels')
        finally:bpy.data.images.remove(image)
        sha=digest(target);rows.append({'frame':i,'file':str(target.resolve()),'expected_sha256':sha})
        mapping.append({'clip_frame':i,'source_frame':source_frame,'scene_frame':spec['scene_start']+i-1,
                        'source_seconds':(source_frame-1)/source['fps'],'source':original,'decoded_sha256':sha})
        atomic_json(job/'tracking-input-progress.json',{'state':'decoding','completed':i,'total':len(indices)})
    report={'version':'1.0','adapter':'CLIP_INPUT_V1','kind':source['kind'],'source_fps':source['fps'],
            'scene_fps':spec['fps'],'scene_start':spec['scene_start'],'frames':mapping,
            'time_basis':'declared indexed source frames; movie nominal fps checked, presentation timestamps not certified',
            'decode':'native Blender VSE Standard RGB8 PNG' if source['kind']=='MOVIE' else 'byte-preserved PNG copies'}
    validate(report,INPUT_REPORT);atomic_json(job/'tracking-input.json',report)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    return rows,report
