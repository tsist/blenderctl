"""Editable compositor template for C5 multilayer OpenEXR masters."""
import math
from pathlib import Path
import re

import bpy


def socket(sockets, identifier):
    selected = next((item for item in sockets if item.identifier == identifier and item.enabled), None)
    if selected is None:
        raise ValueError(f'Unavailable compositor socket: {identifier}')
    return selected


def named_node(tree, kind, name, position):
    node = tree.nodes.new(kind)
    node.name = node.label = name
    node.location = position
    return node


def vector_math(tree, operation, name, position, first, other=None, *, factor=None):
    node = named_node(tree,'ShaderNodeVectorMath',name,position)
    node.operation = operation
    tree.links.new(first,socket(node.inputs,'Vector'))
    if other is not None:
        tree.links.new(other,socket(node.inputs,'Vector_001'))
    if factor is not None:
        socket(node.inputs,'Scale').default_value=factor
    return socket(node.outputs,'Vector')


def input_node(tree, image, layer, name, position):
    node = named_node(tree,'CompositorNodeImage',name,position)
    node.image = image
    node.layer = layer
    return node


def configure_grade(node, grade):
    modes={
        'LIFT_GAMMA_GAIN':('Lift/Gamma/Gain',{'lift','gamma','gain'}),
        'ASC_CDL':('Offset/Power/Slope (ASC-CDL)',{'offset','power','slope'}),
    }
    grade=grade or {'mode':'LIFT_GAMMA_GAIN','gain':(1.09,0.93,0.86)}
    if not isinstance(grade,dict) or grade.get('mode') not in modes:
        raise ValueError('Choose LIFT_GAMMA_GAIN or ASC_CDL color balance')
    menu, controls=modes[grade['mode']]
    if any(key!='mode' and key.removeprefix('base_') not in controls for key in grade):
        raise ValueError('Color balance control does not belong to the selected mode')
    socket(node.inputs,'Type').default_value=menu
    for key,value in grade.items():
        if key=='mode':
            continue
        if key.startswith('base_'):
            if type(value) not in (int,float) or not math.isfinite(value):
                raise ValueError('A base color balance control requires a finite number')
            socket(node.inputs,'Base '+key[5:].title()).default_value=value
        else:
            if not isinstance(value,(list,tuple)) or len(value)!=3 or any(type(item) not in (int,float) or not math.isfinite(item) for item in value):
                raise ValueError('A color balance control requires three finite RGB components')
            socket(node.inputs,'Color '+key.title()).default_value=(*value,1)


def build_template(scene, image, *, layer, selection, light_gains, use_bloom_layer, grade=None, denoise=True):
    """Mix light deltas into beauty, then grade only the Cryptomatte selection."""
    if not selection or ',' in selection or not layer:
        raise ValueError('Select one exact Cryptomatte object and its view layer')
    if not light_gains or any(not math.isfinite(gain) or not 0 <= gain <= 8 for gain in light_gains.values()):
        raise ValueError('Light group multipliers must be finite values between 0 and 8')
    if scene.compositor_effects.get('C5 finishing') is not None:
        raise ValueError('The scene already has a C5 finishing effect')
    tree = bpy.data.node_groups.new('C5 finishing', 'CompositorNodeTree')
    tree.interface.new_socket(name='Image',in_out='OUTPUT',socket_type='NodeSocketColor')
    layer_image = input_node(tree,image,layer,'Raw view layer',(-1000,120))
    beauty_image = input_node(tree,image,'Composite','Bloom beauty',(-1000,430)) if use_bloom_layer else layer_image
    beauty = socket(beauty_image.outputs,'Combined')
    alpha = socket(beauty_image.outputs,'Alpha')
    mixed = beauty
    for index,(group,gain) in enumerate(light_gains.items()):
        contribution = socket(layer_image.outputs,'Combined_'+group)
        delta = vector_math(tree,'SCALE',f'{group} adjustment',(-750,-140*index),contribution,factor=gain-1)
        mixed = vector_math(tree,'ADD',f'Add {group} adjustment',(-500+index*190,380),mixed,delta)
    mixed_alpha = named_node(tree,'CompositorNodeSetAlpha','Restore beauty alpha',(-50,360))
    socket(mixed_alpha.inputs,'Type').default_value='Replace Alpha'
    tree.links.new(mixed,socket(mixed_alpha.inputs,'Image'))
    tree.links.new(alpha,socket(mixed_alpha.inputs,'Alpha'))
    pick = named_node(tree,'CompositorNodeCryptomatteV2','Object selection',(-310,-120))
    pick.source='IMAGE'
    pick.image=image
    pick.layer_name=layer+'.CryptoObject'
    pick.matte_id=selection
    grade_node = named_node(tree,'CompositorNodeColorBalance','Local color balance',(210,380))
    configure_grade(grade_node,grade)
    tree.links.new(socket(mixed_alpha.outputs,'Image'),socket(grade_node.inputs,'Image'))
    tree.links.new(socket(pick.outputs,'Matte'),socket(grade_node.inputs,'Fac'))
    final=socket(grade_node.outputs,'Image')
    if denoise:
        filter_node=named_node(tree,'CompositorNodeDenoise','Linear beauty denoise',(430,380))
        tree.links.new(final,socket(filter_node.inputs,'Image'))
        guides={'Normal':'Denoising Normal','Albedo':'Denoising Albedo'}
        pass_sockets={port.identifier:port for port in layer_image.outputs if port.enabled}
        for guide,pass_name in guides.items():
            if pass_name in pass_sockets:
                tree.links.new(pass_sockets[pass_name],socket(filter_node.inputs,guide))
        final=socket(filter_node.outputs,'Image')
    output = named_node(tree,'NodeGroupOutput','Finished image',(660,380))
    tree.links.new(final,next(port for port in output.inputs if port.name=='Image'))
    effect=scene.compositor_effects.new('C5 finishing')
    effect.node_group=tree
    effect.enable_for_render=True
    effect.enable_for_preview=True
    scene.render.use_compositing=True
    return tree


def bind_frame(tree,image,layer):
    """Bind every image consumer to the same source frame."""
    raw=tree.nodes['Raw view layer']
    raw.image=image
    raw.layer=layer
    if (bloom:=tree.nodes.get('Bloom beauty')) is not None:
        bloom.image=image
        bloom.layer='Composite'
    crypto=tree.nodes['Object selection']
    crypto.image=image
    crypto.layer_name=layer+'.CryptoObject'


def bind_sequence(tree, image, start, duration):
    """Use the actual EXR file numbering when the shot begins after frame 1."""
    number=re.search(r'(\d+)$',Path(image.filepath).stem)
    if not number or int(number.group(1))<1:
        raise ValueError('Native EXR sequences must begin with a positive numbered filename')
    image.source='SEQUENCE'
    for name in ('Raw view layer','Bloom beauty','Object selection'):
        node=tree.nodes.get(name)
        if node is not None:
            node.frame_start=start
            node.frame_duration=duration
            node.frame_offset=int(number.group(1))-1
            node.use_auto_refresh=True


def job_output(tree, directory, filename):
    """Create a File Output owned by a fresh job directory with fixed items."""
    directory=Path(directory)
    if not directory.is_absolute() or not directory.is_dir() or directory.resolve()!=directory:
        raise ValueError('File Output requires the owned canonical absolute job directory')
    if not isinstance(filename,str) or not re.fullmatch(r'[A-Za-z0-9_-]+#{4}',filename):
        raise ValueError('File Output name must be a simple basename with four frame digits')
    if any(directory.glob(filename[:-4]+'*.exr')):
        raise ValueError('The owned proof File Output already exists')
    output = named_node(tree,'CompositorNodeOutputFile','Owned QC layers',(455,-85))
    output.directory=str(directory)
    output.file_name=filename
    output.save_as_render=False
    output.format.media_type='MULTI_LAYER_IMAGE'
    output.format.file_format='OPEN_EXR_MULTILAYER'
    output.format.color_depth='32'
    output.format.exr_codec='ZIP'
    for socket_type, name, source in (
        ('RGBA','Graded',socket(tree.nodes['Local color balance'].outputs,'Image')),
        ('RGBA','Mixed',socket(tree.nodes['Restore beauty alpha'].outputs,'Image')),
        ('FLOAT','Matte',socket(tree.nodes['Object selection'].outputs,'Matte')),
    ):
        output.file_output_items.new(socket_type,name)
        tree.links.new(source,next(port for port in output.inputs if port.name==name))
    return output
