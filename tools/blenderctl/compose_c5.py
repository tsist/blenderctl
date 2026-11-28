"""Assemble and render one C5 shot through the candidate compositor template."""
from argparse import ArgumentParser
import hashlib
import json
from pathlib import Path
import sys
import time

import bpy
import numpy as np
import OpenImageIO as oiio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compositing_workflow_addon.c5 import bind_frame, bind_sequence, build_template, job_output
from compositing_workflow_addon.c5_ocio import bind_profile, verify_files
from compositing_workflow_addon.core import managed_output_scope, guard_output_node


def sha256(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle,'sha256').hexdigest()


def layers(path, include, *, decode=False):
    reader=oiio.ImageInput.open(str(path))
    if not reader:
        raise ValueError(f'Cannot open image: {path}')
    out={}
    try:
        index=0
        while reader.seek_subimage(index,0):
            spec=reader.spec()
            name=spec.get_string_attribute('oiio:subimagename')
            if include is None or name in include:
                out[name]={'width':spec.width,'height':spec.height,'type':str(spec.format),
                           'channels':list(spec.channelnames),'color':spec.get_string_attribute('colorInteropID')}
                if decode:
                    pixels=reader.read_image(format=oiio.FLOAT)
                    if pixels is None or not np.isfinite(pixels).all():
                        raise ValueError(f'Nonfinite or absent pixels in {name}')
                    out[name]['pixels']=pixels
            index+=1
    finally:
        reader.close()
    return out


def validate_contract(handoff, shot):
    expected=('shot_id','view_layer','frame_start','frame_end','fps','resolution','output_passes','compositor_enabled')
    if any(handoff[key]!=shot[key] for key in expected):
        raise ValueError('Delivery parameters disagree with the C5 shot table')
    if handoff['delivery_profile']!='MASTER_EXR32' or handoff['alpha']!='PREMUL':
        raise ValueError('C5 finishing expects a premultiplied MASTER_EXR32 input')
    if [frame['frame'] for frame in handoff['frames']] != list(range(shot['frame_start'],shot['frame_end']+1)):
        raise ValueError('Incorrect or discontinuous source frame range')


def load_verified(record, layer, shape, working_interop):
    path=Path(record['path'])
    if not path.is_absolute() or not path.is_file() or sha256(path)!=record['sha256']:
        raise ValueError(f'Source frame changed or is absent: {path}')
    primary=(layer+'.Combined','Composite.Combined')
    images=layers(path,primary)
    if primary[0] not in images or any((item['width'],item['height'])!=shape or item['type']!='float' or item['color']!=working_interop for item in images.values()):
        raise ValueError(f'Source beauty dimensions, depth or working space drifted: {path}')
    image=bpy.data.images.load(str(path),check_existing=False)
    image.alpha_mode='PREMUL'
    return image


def report_pixels(path, master, layer, gains, bloom):
    images=layers(path,None,decode=True)
    graded=images['Graded']['pixels']
    mixed=images['Mixed']['pixels']
    matte=images['Matte']['pixels'][:,:,0]
    selected=matte>0.5
    background=matte==0
    if not selected.any() or not background.any():
        raise ValueError('Cryptomatte selection is empty or covers the entire image')
    delta=np.max(np.abs(graded[:,:,:3]-mixed[:,:,:3]),axis=-1)
    combined=layer+'.Combined'
    needed={combined,*[layer+'.Combined_'+group for group in gains]}
    if bloom:
        needed.add('Composite.Combined')
    source=layers(master,needed,decode=True)
    raw=source[combined]['pixels'][:,:,:3]
    beauty=source['Composite.Combined']['pixels'][:,:,:3] if bloom else raw
    expected=beauty.copy()
    for group,gain in gains.items():
        expected+=(gain-1)*source[layer+'.Combined_'+group]['pixels'][:,:,:3]
    mix_error=float(np.abs(expected-mixed[:,:,:3]).max())
    mix_tolerance=max(1e-5,2e-6*float(np.abs(expected).max()))
    result={'selected_pixels':int(selected.sum()),'clear_background_pixels':int(background.sum()),
            'selected_max_rgb_difference':float(delta[selected].max()),
            'background_max_rgb_difference':float(delta[background].max()),
            'max_alpha_difference':float(np.abs(graded[:,:,3]-mixed[:,:,3]).max()),
            'light_group_mix_max_rgb_error':mix_error,'light_group_mix_rgb_tolerance':mix_tolerance,
            'bloom_max_rgb_difference':float(np.abs(beauty-raw).max())}
    if result['selected_max_rgb_difference']<=1e-3 or result['background_max_rgb_difference']>1e-6 or result['max_alpha_difference']>1e-6:
        raise ValueError(f'Cryptomatte isolation proof did not pass: {result}')
    if mix_error>mix_tolerance:
        raise ValueError(f'Light group mix proof did not pass: {result}')
    return result


def run(handoff, shot, output, color_config=None, finishing=None):
    validate_contract(handoff,shot)
    width,height=shot['resolution']
    scene=bpy.context.scene
    scene.name=f"{shot['shot_id']} finishing"
    scene.render.engine='CYCLES'
    scene.cycles.samples=1
    scene.cycles.device='CPU'
    scene.cycles.use_denoising=False
    scene.render.resolution_percentage=100
    scene.render.resolution_x=width
    scene.render.resolution_y=height
    scene.render.fps=shot['fps'][0]
    scene.render.fps_base=shot['fps'][1]
    scene.frame_start=shot['frame_start']
    scene.frame_end=shot['frame_end']
    scene.render.use_sequencer=False
    scene.render.compositor_device='CPU'
    scene.render.compositor_precision='FULL'
    scene.render.image_settings.file_format='PNG'
    scene.render.image_settings.color_mode='RGBA'
    scene.render.image_settings.color_depth='16'
    scene.render.dither_intensity=0
    for obj in list(scene.objects):
        if obj.type!='CAMERA':
            bpy.data.objects.remove(obj,do_unlink=True)
    scene.display_settings.display_device=color_config['display'] if color_config else 'sRGB'
    scene.view_settings.view_transform=color_config['view'] if color_config else handoff['view_transform']
    working_interop=color_config['working_interop'] if color_config else 'lin_rec709_scene'
    saved_view=handoff.get('view_settings',{}) if not color_config else {}
    scene.view_settings.look=saved_view.get('look','None')
    scene.view_settings.exposure=saved_view.get('exposure',0)
    scene.view_settings.gamma=saved_view.get('gamma',1)
    layer=shot['view_layer']
    master=handoff['frames'][0]
    first=load_verified(master,layer,(width,height),working_interop)
    finishing=finishing or {}
    if not isinstance(finishing,dict) or not set(finishing)<={'light_gains','grade','denoise'}:
        raise ValueError('Finishing accepts light_gains, grade and denoise')
    denoise=finishing.get('denoise',True)
    if type(denoise) is not bool:
        raise ValueError('Denoise must be Boolean')
    gains=finishing.get('light_gains',{group: (1.08 if index==0 else .82) for index,group in enumerate(shot['light_groups'])})
    if set(gains)!=set(shot['light_groups']):
        raise ValueError('Light gains must match the declared light groups')
    tree=build_template(scene,first,layer=layer,selection=shot['crypto_target'],light_gains=gains,
                        use_bloom_layer=handoff['compositor_enabled'],grade=finishing.get('grade'),denoise=denoise)
    filter_node=tree.nodes.get('Linear beauty denoise')
    guides=[] if filter_node is None else [port.name for port in filter_node.inputs if port.is_linked and port.name!='Image']
    qa=job_output(tree,output,'c5-check-####')
    guard_output_node(qa)
    result={'schema_version':'1.0','shot_id':shot['shot_id'],'fps':shot['fps'],'resolution':shot['resolution'],
            'view_transform':scene.view_settings.view_transform,'color_space':'sRGB','alpha':'STRAIGHT',
            'working_interop':working_interop,'ocio_config':color_config, 'source_sha256':handoff['source_sha256'],
            'light_groups':gains,'cryptomatte_selection':shot['crypto_target'],
            'denoise':{'enabled':denoise,'guides':guides,'method':'Blender native compositor, scene linear',
                       'device':scene.render.compositor_device},
            'grade':finishing.get('grade',{'mode':'LIFT_GAMMA_GAIN','gain':[1.09,.93,.86]}),'frames':[]}
    current=first
    for index,record in enumerate(handoff['frames']):
        frame=record['frame']
        if index:
            current=load_verified(record,layer,(width,height),working_interop)
            previous=tree.nodes['Raw view layer'].image
            bind_frame(tree,current,layer)
            if previous!=first:
                bpy.data.images.remove(previous)
        scene.frame_set(frame)
        dest=output/f'frame-{frame:06d}.png'
        scene.render.filepath=str(dest)
        start=time.perf_counter()
        if dest.exists() or 'FINISHED' not in bpy.ops.render.render(write_still=True,scene=scene.name):
            raise ValueError(f'Cannot publish finished frame {frame}')
        elapsed=time.perf_counter()-start
        decoded=next(iter(layers(dest,None).values()))
        if (decoded['width'],decoded['height'],decoded['type'])!=(width,height,'uint16'):
            raise ValueError(f'Finished image dimensions or depth differ: {dest}')
        if index==0:
            files=list(output.glob('c5-check-*.exr'))
            if len(files)!=1:
                raise ValueError(f'Expected an owned multilayer proof: {files}')
            result['pixel_verification']={'file':str(files[0]),'sha256':sha256(files[0]),
                                          **report_pixels(files[0],record['path'],layer,gains,handoff['compositor_enabled'])}
            tree.nodes.remove(qa)
        result['frames'].append({'frame':frame,'path':str(dest),'sha256':sha256(dest),'source_sha256':record['sha256'],
                                 'render_seconds':elapsed})
    bind_frame(tree,first,layer)
    if current!=first:
        bpy.data.images.remove(current)
    if len(handoff['frames'])>1:
        bind_sequence(tree,first,scene.frame_start,scene.frame_end-scene.frame_start+1)
    scene.frame_set(scene.frame_start)
    scene.render.filepath='//render/finished-'
    candidate=output/f"{shot['shot_id']}-comp.blend"
    bpy.context.preferences.filepaths.save_version=0
    bpy.ops.wm.save_as_mainfile(filepath=str(candidate),copy=True,relative_remap=True)
    result['candidate']={'file':str(candidate),'sha256':sha256(candidate)}
    if color_config:
        verify_files([color_config['config'],*color_config['dependencies']])
    temporary=output/'handoff.tmp'
    temporary.write_text(json.dumps(result,indent=2),encoding='utf-8')
    temporary.replace(output/'handoff.json')


if __name__=='__main__':
    parser=ArgumentParser(description=__doc__)
    parser.add_argument('handoff',type=Path)
    parser.add_argument('table',type=Path)
    parser.add_argument('output',type=Path)
    parser.add_argument('--ocio-profile',type=Path)
    parser.add_argument('--finishing',type=Path)
    args=parser.parse_args(sys.argv[sys.argv.index('--')+1:])
    handoff=json.loads(args.handoff.read_text(encoding='utf-8-sig'))
    table=json.loads(args.table.read_text(encoding='utf-8-sig'))
    shot=next(row for row in table['shots'] if row['shot_id']==handoff['shot_id'])
    color_config=bind_profile(json.loads(args.ocio_profile.read_text(encoding='utf-8-sig'))) if args.ocio_profile else None
    finishing=json.loads(args.finishing.read_text(encoding='utf-8-sig')) if args.finishing else None
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    with managed_output_scope(output):
        run(handoff,shot,output,color_config,finishing)
