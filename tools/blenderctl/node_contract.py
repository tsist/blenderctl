# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage-9 declarative shader/compositor/geometry node and image contracts."""
import re
from pathlib import Path
from protocol import Failure
from scene_contract import obj,enum,array,number,integer,NAME,BOOL,VEC,CONTEXT,validate
from model_contract import MANIFEST as MODEL_MANIFEST,normalize as model_normalize,OPS as MODEL_OPS,SCENE_OPS,SHA,SCOPE,SELECTION

TEXT={'type':'string','minLength':1,'maxLength':1024}
FILE=obj({'file':TEXT,'expected_sha256':SHA})
TREE=obj({'kind':enum('MATERIAL','WORLD','GROUP'),'name':NAME,'scope':enum('reject_shared','shared')})
SOCKET={'oneOf':[obj({'identifier':TEXT}),obj({'name':NAME})]}
IDREF=obj({'id_type':enum('Material','Object','Collection','Image'),'name':NAME})
VALUE={'oneOf':[number(),BOOL,{'type':'string','maxLength':1024},array(number(),2,4),IDREF]}
NODE_TYPES={
 'ShaderNodeTree':['ShaderNodeOutputMaterial','ShaderNodeOutputWorld','ShaderNodeBsdfPrincipled','ShaderNodeEmission','ShaderNodeBackground','ShaderNodeTexImage','ShaderNodeTexEnvironment','ShaderNodeTexChecker','ShaderNodeTexNoise','ShaderNodeTexCoord','ShaderNodeUVMap','ShaderNodeMapping','ShaderNodeNormalMap','ShaderNodeBump','ShaderNodeSeparateColor','ShaderNodeCombineColor','ShaderNodeValToRGB','ShaderNodeMath','ShaderNodeVectorMath','ShaderNodeMixRGB','ShaderNodeValue','ShaderNodeRGB','ShaderNodeGroup','NodeGroupInput','NodeGroupOutput'],
 'GeometryNodeTree':['NodeGroupInput','NodeGroupOutput','GeometryNodeTransform','GeometryNodeSetMaterial','GeometryNodeMeshCube','GeometryNodeInputPosition','ShaderNodeMath','ShaderNodeVectorMath','ShaderNodeCombineXYZ','ShaderNodeSeparateXYZ','GeometryNodeGroup'],
 'CompositorNodeTree':['NodeGroupInput','NodeGroupOutput','CompositorNodeRLayers','CompositorNodeImage','ShaderNodeMix','ShaderNodeMath','CompositorNodeRGB','CompositorNodeGroup','CompositorNodeInvert'],
}
OPS={}
def operation(name,props,required=None):OPS[name]=obj({'op':{'const':name},**props},['op',*(props if required is None else required)])
operation('material.create',{'name':NAME,'preset':enum('EMPTY','PRINCIPLED')})
operation('material.copy',{'material':NAME,'name':NAME})
operation('material.assign',{'object':NAME,'data_scope':SCOPE,'material':NAME,'slot':integer(0,63),'mode':enum('append','replace')})
operation('material.faces',{'object':NAME,'data_scope':SCOPE,'selection':SELECTION,'slot':integer(0,63)})
operation('world.assign',{'scene':NAME,'world':NAME})
operation('tree.create',{'name':NAME,'type':enum(*NODE_TYPES)})
operation('tree.interface',{'tree':TREE,'name':NAME,'direction':enum('INPUT','OUTPUT'),'socket_type':enum('NodeSocketFloat','NodeSocketInt','NodeSocketBool','NodeSocketVector','NodeSocketColor','NodeSocketGeometry','NodeSocketShader','NodeSocketMaterial','NodeSocketObject','NodeSocketImage'),'default':VALUE},['tree','name','direction','socket_type'])
operation('node.add',{'tree':TREE,'name':NAME,'type':enum(*sorted({n for ns in NODE_TYPES.values() for n in ns}))})
operation('node.remove',{'tree':TREE,'node':NAME})
operation('node.input',{'tree':TREE,'node':NAME,'socket':SOCKET,'value':VALUE})
operation('node.output',{'tree':TREE,'node':NAME,'socket':SOCKET,'value':VALUE})
operation('node.properties',{'tree':TREE,'node':NAME,'properties':{'type':'object','minProperties':1}})
operation('node.resource',{'tree':TREE,'node':NAME,'property':enum('image','node_tree','scene'),'name':NAME})
operation('node.link',{'tree':TREE,'from_node':NAME,'from_socket':SOCKET,'to_node':NAME,'to_socket':SOCKET,'replace':BOOL})
operation('node.unlink',{'tree':TREE,'node':NAME,'socket':SOCKET})
operation('node.ramp',{'tree':TREE,'node':NAME,'interpolation':enum('LINEAR','CONSTANT','EASE'),'elements':array(obj({'position':number(0,1),'color':array(number(0,1),4,4)}),2,32)})
operation('geometry.bind',{'object':NAME,'name':NAME,'group':NAME})
operation('geometry.input',{'object':NAME,'modifier':NAME,'socket':SOCKET,'value':VALUE})
operation('compositor.bind',{'scene':NAME,'group':NAME,'enabled':BOOL})
operation('image.load',{'name':NAME,'source':enum('FILE','TILED','SEQUENCE'),'files':array(obj({**FILE['properties'],'number':integer(0,9999)}),1,100),'template':TEXT,'colorspace':enum('sRGB','Non-Color','Linear Rec.709'),'alpha_mode':enum('STRAIGHT','PREMUL','CHANNEL_PACKED','NONE'),'pack':BOOL},['name','source','files','colorspace','alpha_mode','pack'])
operation('image.configure',{'image':NAME,'scope':enum('reject_shared','shared'),'colorspace':enum('sRGB','Non-Color','Linear Rec.709'),'alpha_mode':enum('STRAIGHT','PREMUL','CHANNEL_PACKED','NONE')})
operation('image.sequence',{'tree':TREE,'node':NAME,'frame_start':integer(-100000,100000),'frame_offset':integer(-10000,10000),'frame_duration':integer(1,100),'cyclic':BOOL})
from zone_contract import operations as zone_operations
OPS.update(zone_operations(TREE))
MANIFEST=obj({'initial_scene':NAME,'context':CONTEXT,'deformation_guard':obj({'frames':array(integer(-100000,100000),1,32),'evaluated_uv_tolerance':number(0,0.00001)},['frames']),'operations':array({'oneOf':[*MODEL_MANIFEST['properties']['operations']['items']['oneOf'],*OPS.values()]},1,500)},['context','operations'])
PREVIEW=obj({'file':TEXT,'expected_sha256':SHA,'material':NAME,'resolution':integer(64,1024),'samples':integer(1,128),'frame':integer(1,100000),'resources':array(FILE,0,100)},['file','expected_sha256','material','resolution','samples','frame'])
BAKE=obj({'file':TEXT,'expected_sha256':SHA,'object':NAME,'scene':NAME,'view_layer':NAME,'uv_layer':NAME,'type':enum('EMIT','DIFFUSE_COLOR','NORMAL'),'resolution':integer(16,2048),'samples':integer(1,128),'margin':integer(0,64),'frame':integer(1,100000),'resources':array(FILE,0,100)},['file','expected_sha256','object','scene','view_layer','uv_layer','type','resolution','samples','margin','frame'])

def file_contract(item):
    p=Path(item['file']).resolve()
    if not p.is_file():raise Failure('NOT_FOUND','Texture input missing: '+str(p))
    if p.suffix.lower() not in ('.png','.jpg','.jpeg','.exr','.tif','.tiff','.hdr','.bmp'):raise Failure('UNSUPPORTED','Unsupported image file extension')
    item['file']=str(p)

def normalize(manifest):
    validate(manifest,MANIFEST)
    if 'deformation_guard' in manifest:
        if 'initial_scene' in manifest or any(op['op'] not in ('material.create','material.assign','material.faces') for op in manifest['operations']):
            raise Failure('UNSUPPORTED','deformation_guard requires an existing file and material.create/assign/faces only')
        frames=manifest['deformation_guard']['frames']
        if frames!=sorted(set(frames)):
            raise Failure('INVALID_REQUEST','Deformation sample frames must be unique and increasing')
    prior=[op for op in manifest['operations'] if op['op'] in MODEL_OPS or op['op'] in SCENE_OPS]
    if prior:model_normalize({**manifest,'operations':prior})
    for op in manifest['operations']:
        if op['op']=='node.properties' and not op['properties']:raise Failure('INVALID_REQUEST','Empty properties')
        if op['op']=='node.ramp':
            ps=[e['position'] for e in op['elements']]
            if ps!=sorted(set(ps)):raise Failure('INVALID_REQUEST','Ramp positions must be unique and increasing')
        if op['op']=='image.load':
            for f in op['files']:file_contract(f)
            numbers=[f['number'] for f in op['files']]
            if len(numbers)!=len(set(numbers)):raise Failure('INVALID_REQUEST','Duplicate image member number')
            if op['source']=='FILE':
                if len(numbers)!=1 or numbers!=[0] or 'template' in op:raise Failure('INVALID_REQUEST','FILE requires exactly member 0 and no template')
            else:
                if 'template' not in op:raise Failure('INVALID_REQUEST','UDIM/sequence requires explicit template')
                template=str(Path(op['template']).resolve());op['template']=template
                token='<UDIM>' if op['source']=='TILED' else '####'
                if template.count(token)!=1:raise Failure('INVALID_REQUEST','Template requires exactly '+token)
                if op['source']=='TILED' and (numbers!=sorted(numbers) or numbers[0]!=1001 or max(numbers)>1999):raise Failure('INVALID_REQUEST','UDIM members must be sorted, start at 1001 and end at most 1999')
                if op['source']=='SEQUENCE' and (numbers!=list(range(1,len(numbers)+1)) or op['pack']):raise Failure('UNSUPPORTED','Sequence must explicitly list frames 1..N and remain external')
                for f in op['files']:
                    if Path(template.replace(token,str(f['number']) if token=='<UDIM>' else f"{f['number']:04d}")).resolve()!=Path(f['file']):raise Failure('INVALID_REQUEST','Member differs from declared template')
    return manifest

def input_documents(params):return [*params.get('resources',[]),*(f for op in params.get('manifest',{}).get('operations',[]) if op['op']=='image.load' for f in op['files'])]
