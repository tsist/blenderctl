import sys,pathlib,json,hashlib,bpy
up,out=map(pathlib.Path,sys.argv[sys.argv.index('--')+1:])
sys.path.insert(0,str(up/'tools'))
from material_workflow_addon import core
from material_workflow_addon.contract import normalize_manifest
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add()
cube=bpy.context.object; cube.name='Fixture'
m=normalize_manifest(dict(schema_version='1.1',context=dict(scene=bpy.context.scene.name,view_layer=bpy.context.view_layer.name,frame=1),target=dict(object='Fixture',uv_layer=cube.data.uv_layers.active.name),material=dict(id='Studio',name='Studio',template='pbr_layers_v2'),layers=[dict(id='Base',name='Base',values=dict(roughness=0.375,metallic=0.25,base_color=[0.25,0.5,0.75,1]))],preview=dict(engine='CYCLES',device=dict(backend='CPU'),width=64,height=64,samples=4,denoise=False,views=[dict(id='Front',azimuth=45,elevation=20)])))
core.apply_material(m)
f=out/'managed.blend'; bpy.ops.wm.save_as_mainfile(filepath=str(f))
(out/'resources.json').write_text('[]')
(out/'request.json').write_text(json.dumps(dict(schema_version='1.0',command='material.template-save',params=dict(file=str(f),expected_sha256=hashlib.sha256(f.read_bytes()).hexdigest(),material_id='Studio',template_id='StudioExport',name='Studio Export',version='1.0.0',resources=[]))))
