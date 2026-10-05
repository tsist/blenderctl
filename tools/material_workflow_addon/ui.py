# SPDX-License-Identifier: GPL-3.0-or-later
"""Current-scene draft editor; Apply uses the shared conflict-aware core."""
import copy, hashlib, json, uuid
from pathlib import Path
import bpy
from bpy.props import StringProperty, FloatProperty, FloatVectorProperty, BoolProperty, IntProperty, EnumProperty
from .contract import normalize_manifest, CHANNELS
from . import core, core_v2, templates

FIELDS = {'mw_name':('name',),'mw_enabled':('enabled',),'mw_opacity':('opacity',)}
for p,k in [('color','base_color'),('roughness','roughness'),('metallic','metallic'),('alpha','alpha'),('normal','normal_strength'),('height','height_distance'),('emission','emission_color'),('emission_strength','emission_strength')]: FIELDS['mw_'+p]=('values',k)
for key in core_v2.GLAZE_SOCKETS: FIELDS['mw_'+key]=('values',key)
for p,k in [('tint','color_tint'),('roughness_scale','roughness_scale'),('roughness_bias','roughness_bias')]: FIELDS['mw_'+p]=('adjustments',k)
for k in ('scale','rotation','translation'): FIELDS['mw_'+k]=('mapping',k)

def _manifest(scene):
    if not scene.mw_manifest_json: raise ValueError('请先载入清单或创建基础模板')
    return json.loads(scene.mw_manifest_json)
def _store(scene,manifest): scene.mw_manifest_json=json.dumps(manifest,ensure_ascii=False)
def _index(scene,manifest):
    scene.mw_layer_index=min(max(scene.mw_layer_index,0),len(manifest['layers'])-1)
    return scene.mw_layer_index

def _load_layer(scene):
    manifest=_manifest(scene); layer=manifest['layers'][_index(scene,manifest)]
    scene.mw_preview_engine=manifest['preview']['engine']
    for prop,keys in FIELDS.items():
        if manifest['schema_version']=='1.0' and (keys[0]=='adjustments' or keys[-1] in core_v2.GLAZE_SOCKETS): continue
        value=layer
        for key in keys: value=value[key]
        setattr(scene,prop,value)
    image=layer['mask'] if scene.mw_channel=='mask' else layer['channels'].get(scene.mw_channel)
    scene.mw_image_path=image['file'] if image else ''
    fx=layer.get('effect')
    scene.mw_effect_group=fx['group'] if fx else ''
    scene.mw_effect_parameters=json.dumps(fx['parameters']) if fx else '[]'

def _flush(scene):
    manifest=_manifest(scene); layer=manifest['layers'][_index(scene,manifest)]
    for prop,keys in FIELDS.items():
        if manifest['schema_version']=='1.0' and (keys[0]=='adjustments' or keys[-1] in core_v2.GLAZE_SOCKETS): continue
        data=layer
        for key in keys[:-1]: data=data[key]
        value=getattr(scene,prop)
        data[keys[-1]]=value if isinstance(value,(str,bool,int,float)) else list(value)
    if scene.mw_layer_index==0: layer.update(enabled=True,opacity=1,mask=None)
    _store(scene,manifest); return manifest

def _material(context,manifest=None):
    if manifest:
        obj=context.scene.objects.get(manifest['target']['object']); slot=manifest['target']['material_slot']
        return obj.material_slots[slot].material if obj and slot<len(obj.material_slots) else None
    return context.object.active_material if context.object else None

class MWOperator:
    def execute(self,context):
        try:
            self.run(context); context.scene.mw_last_error=''; return {'FINISHED'}
        except Exception as error:
            context.scene.mw_last_error=str(error); self.report({'ERROR'},str(error)); return {'CANCELLED'}

class MW_OT_load(MWOperator,bpy.types.Operator):
    bl_idname='material_workflow.load'; bl_label='载入清单'
    def run(self,context):
        with open(bpy.path.abspath(context.scene.mw_manifest_path),encoding='utf-8-sig') as handle: manifest=normalize_manifest(json.load(handle))
        _store(context.scene,manifest); context.scene.mw_layer_index=0; context.scene.mw_upgrade_authorized=False; context.scene.mw_manual_notice=''; _load_layer(context.scene)

class MW_OT_load_current(MWOperator,bpy.types.Operator):
    bl_idname='material_workflow.load_current'; bl_label='读取当前受管材质'
    def run(self,context):
        material=_material(context)
        if material is None: raise ValueError('当前对象没有材质')
        # The request baseline is separate from actual node values. Importing live
        # manual values here would turn them into requested edits and false conflicts.
        manifest=core_v2.get_stored_manifest(material)
        engine=core_v2 if core_v2.STATE in material else core
        state=engine.verify_managed_state(material)
        context.scene.mw_manual_notice=('检测到节点手改：'+', '.join(state['conflicts'])+'。草稿显示保存请求；无关手改在应用时保留，重叠修改会报告冲突。') if not state['ok'] else ''
        _store(context.scene,manifest); context.scene.mw_layer_index=0; context.scene.mw_upgrade_authorized=False; _load_layer(context.scene)

class MW_OT_select_layer(MWOperator,bpy.types.Operator):
    bl_idname='material_workflow.select_layer'; bl_label='选择图层'
    index:IntProperty()
    def run(self,context): _flush(context.scene); context.scene.mw_layer_index=self.index; _load_layer(context.scene)

class MW_OT_edit_layer(MWOperator,bpy.types.Operator):
    bl_idname='material_workflow.edit_layer'; bl_label='编辑图层草稿'
    action:EnumProperty(items=[(v,v,'') for v in ('ADD','DELETE','UP','DOWN')])
    def run(self,context):
        scene=context.scene; manifest=_flush(scene)
        if manifest['schema_version']!='1.1': raise ValueError('结构编辑需要显式升级到 v2')
        layers=manifest['layers']; index=_index(scene,manifest)
        if self.action=='ADD':
            if len(layers)>=8: raise ValueError('最多 8 个图层')
            layer=copy.deepcopy(layers[index]); layer.update(id='layer_'+uuid.uuid4().hex[:12],name='新图层',opacity=0.5,enabled=True)
            layers.insert(index+1,layer); index+=1
        elif self.action=='DELETE':
            if index==0: raise ValueError('基础层不可删除')
            layers.pop(index); index=min(index,len(layers)-1)
        else:
            other=index+(-1 if self.action=='UP' else 1)
            if index==0 or other<=0 or other>=len(layers): raise ValueError('基础层固定；只能排序覆盖层')
            layers[index],layers[other]=layers[other],layers[index]; index=other
        _store(scene,normalize_manifest(manifest)); scene.mw_layer_index=index; _load_layer(scene)

class MW_OT_upgrade(MWOperator,bpy.types.Operator):
    bl_idname='material_workflow.upgrade'; bl_label='显式升级为 v2 草稿'
    def run(self,context):
        scene=context.scene; manifest=_flush(scene)
        manifest['schema_version']='1.1'; manifest['material']['template']='pbr_layers_v2'
        for layer in manifest['layers']:
            layer.setdefault('adjustments',{'color_tint':[1,1,1,1],'roughness_scale':1,'roughness_bias':0}); layer.setdefault('effect',None)
        _store(scene,normalize_manifest(manifest)); scene.mw_upgrade_authorized=True; _load_layer(scene)
        self.report({'INFO'},'已创建 v2 草稿；应用时检测旧材质手改冲突')

class MW_OT_image(MWOperator,bpy.types.Operator):
    bl_idname='material_workflow.image'; bl_label='更新贴图草稿'
    remove:BoolProperty(default=False)
    def run(self,context):
        scene=context.scene; manifest=_flush(scene); layer=manifest['layers'][scene.mw_layer_index]; channel=scene.mw_channel
        if channel=='mask' and scene.mw_layer_index==0: raise ValueError('基础层不可添加遮罩')
        if self.remove: image=None
        else:
            path=Path(bpy.path.abspath(scene.mw_image_path)).resolve(strict=True)
            if not path.is_file(): raise ValueError('贴图路径不是文件')
            image={'file':str(path),'expected_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'color_space':'sRGB' if channel in ('base_color','emission') else 'Non-Color'}
        if channel=='mask': layer['mask']=image
        elif image: layer['channels'][channel]=image
        else: layer['channels'].pop(channel,None)
        _store(scene,normalize_manifest(manifest)); _load_layer(scene)

class MW_OT_channel(MWOperator,bpy.types.Operator):
    bl_idname='material_workflow.channel'; bl_label='读取所选通道路径'
    def run(self,context): _flush(context.scene); _load_layer(context.scene)

class MW_OT_effect(MWOperator,bpy.types.Operator):
    bl_idname='material_workflow.effect'; bl_label='绑定当前文件节点组'
    remove:BoolProperty(default=False)
    def run(self,context):
        scene=context.scene; manifest=_flush(scene)
        if manifest['schema_version']!='1.1': raise ValueError('节点组需要 v2')
        effect=None
        if not self.remove:
            group=bpy.data.node_groups.get(scene.mw_effect_group)
            if group is None: raise ValueError('当前文件没有该节点组')
            inputs=[s for s in group.interface.items_tree if s.item_type=='SOCKET' and s.in_out=='INPUT' and s.socket_type=='NodeSocketShader']
            outputs=[s for s in group.interface.items_tree if s.item_type=='SOCKET' and s.in_out=='OUTPUT' and s.socket_type=='NodeSocketShader']
            if len(inputs)!=1 or len(outputs)!=1: raise ValueError('节点组需要唯一 Shader 输入和输出')
            effect={'group':group.name,'expected_sha256':core_v2.group_fingerprint(group),'input_socket':inputs[0].name,'output_socket':outputs[0].name,'parameters':json.loads(scene.mw_effect_parameters)}
        manifest['layers'][scene.mw_layer_index]['effect']=effect
        _store(scene,normalize_manifest(manifest)); _load_layer(scene)

class MW_OT_apply(MWOperator,bpy.types.Operator):
    bl_idname='material_workflow.apply'; bl_label='应用草稿到当前场景'; bl_options={'REGISTER','UNDO'}
    def run(self,context):
        manifest=normalize_manifest(_flush(context.scene))
        if manifest['context']['scene']!=context.scene.name: raise ValueError('清单 Scene 与当前场景不同')
        material=_material(context,manifest)
        if material and core.STATE in material and manifest['schema_version']=='1.1' and not context.scene.mw_upgrade_authorized: raise ValueError('旧材质需要点击显式升级按钮后再应用')
        engine=core_v2 if manifest['schema_version']=='1.1' else core
        result=engine.apply_material(manifest,engine.existing_resource_paths(material,manifest))
        _store(context.scene,manifest); context.scene.mw_upgrade_authorized=False
        self.report({'INFO'},'已应用 '+result['material_name']+'；请另存工作副本')

class MW_OT_preview_engine(MWOperator,bpy.types.Operator):
    bl_idname='material_workflow.preview_engine'; bl_label='设置预览引擎草稿'
    def run(self,context):
        scene=context.scene; manifest=_flush(scene); preview=manifest['preview']
        if preview['engine']!=scene.mw_preview_engine:
            preview.update(engine=scene.mw_preview_engine,
                device={'backend':'GRAPHICS' if scene.mw_preview_engine=='BLENDER_EEVEE' else 'CPU'},
                denoise=scene.mw_preview_engine=='CYCLES')
        _store(scene,normalize_manifest(manifest)); _load_layer(scene)
        self.report({'INFO'},'预览草稿已设置；切换引擎时设备重置为 Eevee GRAPHICS / Cycles CPU')

class MW_OT_template_save(MWOperator,bpy.types.Operator):
    bl_idname='material_workflow.template_save'; bl_label='保存已应用材质模板'
    def run(self,context):
        scene=context.scene; material=_material(context,_manifest(scene))
        if material is None: raise ValueError('目标对象尚未应用材质')
        manifest=core_v2.export_current_manifest(material)
        template=templates.template_from_manifest(manifest,scene.mw_template_id,scene.mw_template_name,scene.mw_template_version)
        templates.write_template(bpy.path.abspath(scene.mw_template_path),template)
        self.report({'INFO'},'已从实际材质导出；未应用草稿不包含在模板中')

class MW_OT_template_load(MWOperator,bpy.types.Operator):
    bl_idname='material_workflow.template_load'; bl_label='模板＋绑定清单实例化'
    builtin:StringProperty(default='')
    def run(self,context):
        scene=context.scene
        template=templates.builtin_template(self.builtin) if self.builtin else templates.read_template(bpy.path.abspath(scene.mw_template_path))
        if scene.mw_bindings_path:
            with open(bpy.path.abspath(scene.mw_bindings_path),encoding='utf-8-sig') as handle: bindings=json.load(handle)
        elif self.builtin:
            obj=context.object
            if obj is None or obj.type!='MESH' or not obj.data.uv_layers: raise ValueError('基础模板需要有 UV 的活动网格')
            engine=scene.render.engine
            if engine not in ('CYCLES','BLENDER_EEVEE'): raise ValueError('基础模板仅支持 Cycles / Eevee 场景')
            bindings={'context':{'scene':scene.name,'view_layer':context.view_layer.name,'frame':scene.frame_current},'target':{'object':obj.name,'material_slot':obj.active_material_index,'uv_layer':obj.data.uv_layers.active.name},'material':{'id':'material_'+uuid.uuid4().hex[:12],'name':'Layered Material'},'preview':{'engine':engine,'device':{'backend':'GRAPHICS' if engine=='BLENDER_EEVEE' else 'CPU'},'views':[{'id':'hero'}]},'resources':{},'groups':{}}
        else: raise ValueError('请指定 JSON 绑定清单，包含对象、UV、资源和预览设置')
        _store(scene,templates.instantiate_template(template,bindings)); scene.mw_layer_index=0; scene.mw_upgrade_authorized=False; scene.mw_manual_notice=''; _load_layer(scene)

class MW_PT_panel(bpy.types.Panel):
    bl_label='Material Workflow'; bl_idname='MW_PT_panel'
    bl_space_type='VIEW_3D'; bl_region_type='UI'; bl_category='Material Workflow'
    def draw(self,context):
        layout=self.layout; scene=context.scene
        layout.prop(scene,'mw_manifest_path'); layout.operator('material_workflow.load'); layout.operator('material_workflow.load_current')
        layout.operator('material_workflow.template_load',text='创建基础＋覆盖层').builtin='base_overlay'
        layout.label(text='编辑草稿后点击应用；请另存工作副本',icon='INFO')
        if scene.mw_manual_notice:
            box=layout.box()
            box.label(text='节点中存在手改；草稿显示保存请求',icon='INFO')
            box.label(text='应用保留无关手改，重叠修改报告冲突')
        if scene.mw_manifest_json:
            try: manifest=_manifest(scene); index=_index(scene,manifest)
            except Exception: return
            layout.prop(scene,'mw_preview_engine'); layout.operator('material_workflow.preview_engine')
            layout.label(text='切换设备：Eevee GRAPHICS / Cycles CPU；仅编辑草稿')
            for i,layer in enumerate(manifest['layers']): layout.operator('material_workflow.select_layer',text=layer['name'],depress=i==index).index=i
            if manifest['schema_version']=='1.0': layout.operator('material_workflow.upgrade')
            row=layout.row()
            for action,label in [('ADD','增加'),('DELETE','删除'),('UP','上移'),('DOWN','下移')]: row.operator('material_workflow.edit_layer',text=label).action=action
            layout.prop(scene,'mw_name'); row=layout.row(); row.enabled=index>0; row.prop(scene,'mw_enabled'); row.prop(scene,'mw_opacity')
            for prop,keys in FIELDS.items():
                if prop in ('mw_name','mw_enabled','mw_opacity'): continue
                if manifest['schema_version']=='1.0' and (keys[0]=='adjustments' or keys[-1] in core_v2.GLAZE_SOCKETS): continue
                layout.prop(scene,prop)
            layout.label(text='贴图接入后常量为回退；v2 色调/粗糙度调节有效')
            layout.prop(scene,'mw_channel'); layout.operator('material_workflow.channel'); layout.prop(scene,'mw_image_path')
            row=layout.row(); row.operator('material_workflow.image',text='替换/添加'); row.operator('material_workflow.image',text='移除').remove=True
            if manifest['schema_version']=='1.1':
                layout.prop_search(scene,'mw_effect_group',bpy.data,'node_groups'); layout.prop(scene,'mw_effect_parameters')
                row=layout.row(); row.operator('material_workflow.effect'); row.operator('material_workflow.effect',text='移除效果').remove=True
            layout.operator('material_workflow.apply')
        for prop in ('mw_template_path','mw_template_id','mw_template_name','mw_template_version','mw_bindings_path'): layout.prop(scene,prop)
        layout.operator('material_workflow.template_save'); layout.operator('material_workflow.template_load')
        if scene.mw_last_error: layout.label(text=scene.mw_last_error,icon='ERROR')

CLASSES=(MW_OT_load,MW_OT_load_current,MW_OT_select_layer,MW_OT_edit_layer,MW_OT_upgrade,MW_OT_image,MW_OT_channel,MW_OT_effect,MW_OT_apply,MW_OT_preview_engine,MW_OT_template_save,MW_OT_template_load,MW_PT_panel)
PROPS={'mw_manifest_path':StringProperty(name='清单 JSON',subtype='FILE_PATH'),'mw_manifest_json':StringProperty(options={'HIDDEN'}),'mw_layer_index':IntProperty(default=0,min=0),'mw_name':StringProperty(name='图层名称'),'mw_enabled':BoolProperty(name='启用',default=True),'mw_opacity':FloatProperty(name='不透明度',min=0,max=1,default=1),'mw_channel':EnumProperty(name='通道',items=[(c,c,'') for c in (*CHANNELS,'mask')]),'mw_image_path':StringProperty(name='贴图路径',subtype='FILE_PATH'),'mw_effect_group':StringProperty(name='当前文件节点组'),'mw_effect_parameters':StringProperty(name='参数 JSON',default='[]'),'mw_template_path':StringProperty(name='模板 JSON',subtype='FILE_PATH'),'mw_bindings_path':StringProperty(name='绑定 JSON',subtype='FILE_PATH'),'mw_template_id':StringProperty(name='模板 ID',default='custom_template'),'mw_template_name':StringProperty(name='模板名称',default='自定义模板'),'mw_template_version':StringProperty(name='模板版本',default='1.0.0'),'mw_manual_notice':StringProperty(options={'HIDDEN'}),'mw_last_error':StringProperty(options={'HIDDEN'}),'mw_upgrade_authorized':BoolProperty(options={'HIDDEN'})}
PROPS['mw_preview_engine']=EnumProperty(name='预览引擎',items=[('CYCLES','Cycles',''),('BLENDER_EEVEE','Eevee','')],default='CYCLES')
for prop,label,default in [('mw_color','颜色常量',(0.5,0.5,0.5,1)),('mw_emission','发光颜色',(0,0,0,1)),('mw_tint','贴图色调',(1,1,1,1))]: PROPS[prop]=FloatVectorProperty(name=label,size=4,subtype='COLOR',min=0,max=1,default=default)
for prop,label,low,high,default in [('mw_roughness','粗糙度常量',0,1,0.5),('mw_metallic','金属度',0,1,0),('mw_alpha','透明度',0,1,1),('mw_normal','法线强度',0,10,1),('mw_height','高度距离',0,1,0.001),('mw_emission_strength','发光强度',0,100,0),('mw_roughness_scale','粗糙度倍率',0,10,1),('mw_roughness_bias','粗糙度偏移',-1,1,0)]: PROPS[prop]=FloatProperty(name=label,min=low,max=high,default=default)
for key,label,low,high in [('ior','基底 IOR',1,4),('coat_weight','釉层权重',0,1),('coat_roughness','釉层粗糙度',0,1),('coat_ior','釉层 IOR',1,4)]: PROPS['mw_'+key]=FloatProperty(name=label,min=low,max=high,default=core_v2.GLAZE_DEFAULTS[key])
for prop,label,default,low,high in [('mw_scale','映射比例',(1,1,1),0.00001,10000),('mw_rotation','映射旋转',(0,0,0),-1000,1000),('mw_translation','映射位移',(0,0,0),-10000,10000)]: PROPS[prop]=FloatVectorProperty(name=label,size=3,default=default,min=low,max=high)

def register():
    for cls in CLASSES: bpy.utils.register_class(cls)
    for name,prop in PROPS.items(): setattr(bpy.types.Scene,name,prop)
def unregister():
    for name in PROPS:
        if hasattr(bpy.types.Scene,name): delattr(bpy.types.Scene,name)
    for cls in reversed(CLASSES): bpy.utils.unregister_class(cls)
