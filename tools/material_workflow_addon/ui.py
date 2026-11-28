"""Current-scene draft editor; Apply uses the shared conflict-aware core."""
import copy, hashlib, json, uuid
from pathlib import Path
import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       FloatVectorProperty, IntProperty, StringProperty)
from .contract import normalize_manifest, CHANNELS
from . import core, core_v2, templates
from . import workbench_jobs
from ._ui_kit import (
    HUDManager, IconPack, SidebarDock, apply_node_theme, draw_brand_header,
    ensure_object_mode, get_theme, primary_button, progress_card, prop_column,
    section_panel, status_banner,
)

DOMAIN = "material"
THEME = get_theme(DOMAIN)
_ICONS = IconPack(DOMAIN)
_CHANNEL_LABELS = {
    "base_color": "基础色", "roughness": "粗糙度", "metallic": "金属度",
    "normal": "法线", "height": "高度", "alpha": "透明度",
    "emission": "发光", "mask": "遮罩",
}
_EFFECT_VALUES = {"NodeSocketFloat": "scalar", "NodeSocketVector": "vector", "NodeSocketColor": "color"}

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
    scene.mw_effect_bindings.clear()
    scene.mw_effect_binding_group = ''

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
    bl_options={'REGISTER','UNDO'}
    action:EnumProperty(items=[('ADD','新增',''),('DELETE','删除',''),('UP','上移',''),('DOWN','下移','')])
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
    bl_options={'REGISTER','UNDO'}
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
    bl_options={'REGISTER','UNDO'}
    remove:BoolProperty(default=False)
    structured:BoolProperty(default=False)
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
            if self.structured:
                if scene.mw_effect_binding_group != group.name:
                    raise ValueError('请先读取此节点组的效果参数')
                parameters = []
                for binding in scene.mw_effect_bindings:
                    if binding.enabled:
                        value = getattr(binding, binding.kind)
                        parameters.append({'socket': binding.name,
                                           'value': value if binding.kind == 'scalar' else list(value)})
            else:
                parameters = json.loads(scene.mw_effect_parameters)
            effect={'group':group.name,'expected_sha256':core_v2.group_fingerprint(group),'input_socket':inputs[0].name,'output_socket':outputs[0].name,'parameters':parameters}
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
        with ensure_object_mode(context):
            result=engine.apply_material(manifest,engine.existing_resource_paths(material,manifest))
        material = _material(context, manifest)
        for node in core._nodes(material).values():
            apply_node_theme(node, DOMAIN, accent=node.type == "FRAME")
        _store(context.scene,manifest); context.scene.mw_upgrade_authorized=False
        context.scene.mw_last_report = '已应用 ' + result['material_name']
        context.scene.mw_last_report_kind = 'OK'
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
    use_current_target:BoolProperty(default=False)
    def run(self,context):
        scene=context.scene
        template=templates.builtin_template(self.builtin) if self.builtin else templates.read_template(bpy.path.abspath(scene.mw_template_path))
        if scene.mw_bindings_path and not self.use_current_target:
            with open(bpy.path.abspath(scene.mw_bindings_path),encoding='utf-8-sig') as handle: bindings=json.load(handle)
        else:
            bindings = _current_template_bindings(context, template)
        draft = templates.instantiate_template(template,bindings)
        if self.builtin and self.use_current_target:
            for layer, label in zip(draft['layers'], ('基础层', '覆盖层')):
                layer['name'] = label
        _store(scene,draft); scene.mw_layer_index=0; scene.mw_upgrade_authorized=False; scene.mw_manual_notice=''; _load_layer(scene)


class MW_EffectBinding(bpy.types.PropertyGroup):
    kind: StringProperty()
    enabled: BoolProperty(name='使用此参数', default=True)
    scalar: FloatProperty()
    vector: FloatVectorProperty(size=3)
    color: FloatVectorProperty(size=4, subtype='COLOR', min=0, max=1)


class MW_TemplateBinding(bpy.types.PropertyGroup):
    file: StringProperty(subtype='FILE_PATH')
    group: StringProperty()
    color_space: StringProperty()


class MW_OT_prepare_effect(MWOperator, bpy.types.Operator):
    bl_idname = 'material_workflow.prepare_effect_controls'
    bl_label = '读取效果参数'

    def run(self, context):
        scene = context.scene
        group = bpy.data.node_groups.get(scene.mw_effect_group)
        if group is None or group.bl_idname != 'ShaderNodeTree':
            raise ValueError('请选择着色器节点组')
        parameters = {row['socket']: row['value'] for row in json.loads(scene.mw_effect_parameters)}
        scene.mw_effect_bindings.clear()
        scene.mw_effect_binding_group = group.name
        for socket in group.interface.items_tree:
            if socket.item_type != 'SOCKET' or socket.in_out != 'INPUT':
                continue
            kind = _EFFECT_VALUES.get(socket.socket_type)
            if kind is None or sum(item.item_type == 'SOCKET' and item.in_out == 'INPUT'
                                   and item.name == socket.name for item in group.interface.items_tree) != 1:
                continue
            binding = scene.mw_effect_bindings.add()
            binding.name = socket.name
            binding.kind = kind
            setattr(binding, kind, parameters.get(socket.name, socket.default_value))


class MW_OT_read_template_bindings(MWOperator, bpy.types.Operator):
    bl_idname = 'material_workflow.read_template_bindings'
    bl_label = '读取模板资源需求'

    def run(self, context):
        scene = context.scene
        path = Path(bpy.path.abspath(scene.mw_template_path)).resolve(strict=True)
        document = templates.read_template(path)
        scene.mw_template_bindings.clear()
        scene.mw_template_binding_path = str(path)
        for key, resource in document['resource_slots'].items():
            binding = scene.mw_template_bindings.add()
            binding.name = key
            binding.color_space = resource['color_space']
        for key in document['group_slots']:
            scene.mw_template_bindings.add().name = key


def _current_template_bindings(context, template):
    scene = context.scene
    obj = context.object
    if obj is None or obj.type != 'MESH' or obj.data.uv_layers.active is None:
        raise ValueError('请选择有 UV 的活动网格')
    engine = scene.render.engine
    if engine not in ('CYCLES', 'BLENDER_EEVEE'):
        raise ValueError('请选择 Cycles 或 Eevee 场景')
    resources, groups = {}, {}
    needs = template['resource_slots'] or template['group_slots']
    if needs:
        path = Path(bpy.path.abspath(scene.mw_template_path)).resolve(strict=True)
        if str(path) != scene.mw_template_binding_path:
            raise ValueError('请读取当前模板的资源需求并填写绑定')
        for name, slot in template['resource_slots'].items():
            binding = next((row for row in scene.mw_template_bindings
                            if row.name == name and row.color_space), None)
            if not binding or not binding.file:
                raise ValueError('请选择贴图资源：' + name)
            path = Path(bpy.path.abspath(binding.file)).resolve(strict=True)
            resources[name] = {'file': str(path), 'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                               'color_space': slot['color_space']}
        for name in template['group_slots']:
            binding = next((row for row in scene.mw_template_bindings
                            if row.name == name and not row.color_space), None)
            group = bpy.data.node_groups.get(binding.group) if binding else None
            if group is None:
                raise ValueError('请选择节点组：' + name)
            groups[name] = {'group': group.name, 'expected_sha256': core_v2.group_fingerprint(group)}
    return {
        'context': {'scene': scene.name, 'view_layer': context.view_layer.name, 'frame': scene.frame_current},
        'target': {'object': obj.name, 'material_slot': obj.active_material_index,
                   'uv_layer': obj.data.uv_layers.active.name},
        'material': {'id': 'material_' + uuid.uuid4().hex[:12], 'name': scene.mw_artist_name or '分层材质'},
        'preview': {'engine': engine, 'device': {'backend': 'GRAPHICS' if engine == 'BLENDER_EEVEE' else 'CPU'},
                    'views': [{'id': 'hero'}]},
        'resources': resources, 'groups': groups,
    }


class MW_OT_create_basic(MWOperator, bpy.types.Operator):
    bl_idname = 'material_workflow.create_basic'
    bl_label = '新建并分配受管材质'
    bl_options = {'REGISTER', 'UNDO'}

    def run(self, context):
        if bpy.ops.material_workflow.template_load(
                builtin=context.scene.mw_artist_preset, use_current_target=True) != {'FINISHED'}:
            raise ValueError('无法创建基础材质草稿')
        MW_OT_apply.run(self, context)

class MW_OT_flip_layer(MWOperator, bpy.types.Operator):
    bl_idname = 'material_workflow.flip_layer'
    bl_label = '切换覆盖层显隐'
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(min=1, max=7)

    def run(self, context):
        scene = context.scene
        manifest = _flush(scene)
        if not 0 < self.index < len(manifest['layers']):
            raise ValueError('当前覆盖层已不存在')
        layer = manifest['layers'][self.index]
        layer['enabled'] = not layer['enabled']
        _store(scene, manifest)
        if self.index == scene.mw_layer_index:
            scene.mw_enabled = layer['enabled']


def _draw_layers(layout, scene, manifest):
    layers = manifest['layers']
    index = min(scene.mw_layer_index, len(layers) - 1)
    for i, layer in enumerate(layers):
        row = layout.row(align=True)
        row.operator('material_workflow.select_layer', text=layer['name'],
                     depress=i == index).index = i
        if i:
            visible = scene.mw_enabled if i == index else layer['enabled']
            row.operator('material_workflow.flip_layer', text='',
                         icon='HIDE_OFF' if visible else 'HIDE_ON').index = i
    if manifest['schema_version'] == '1.0':
        layout.operator('material_workflow.upgrade', text='启用多层结构编辑')
    else:
        row = layout.row(align=True)
        row.operator('material_workflow.edit_layer', text='', icon='ADD').action = 'ADD'
        controls = row.row(align=True)
        controls.enabled = index > 0
        controls.operator('material_workflow.edit_layer', text='', icon='REMOVE').action = 'DELETE'
        controls.operator('material_workflow.edit_layer', text='', icon='TRIA_UP').action = 'UP'
        controls.operator('material_workflow.edit_layer', text='', icon='TRIA_DOWN').action = 'DOWN'
    col = prop_column(layout, align=True)
    col.prop(scene, 'mw_name', text='层名')
    col.prop(scene, 'mw_color', text='基础色')
    col.prop(scene, 'mw_metallic', text='金属度', slider=True)
    col.prop(scene, 'mw_roughness', text='粗糙度', slider=True)
    col.prop(scene, 'mw_normal', text='法线强度')
    if index:
        col.prop(scene, 'mw_opacity', text='混合权重', slider=True)
    primary_button(layout, 'material_workflow.apply', '应用图层修改',
                   icon_value=_ICONS.action_id('layer_stack'), icon='CHECKMARK')
    _, advanced = section_panel(layout, 'MW_SEC_layers', '贴图、混合与节点效果', default_closed=True)
    if advanced is not None:
        _draw_layer_details(advanced, scene, manifest)


def _draw_layer_details(layout, scene, manifest):
    col = prop_column(layout, align=True)
    for prop, keys in FIELDS.items():
        if prop in {'mw_name', 'mw_enabled', 'mw_opacity', 'mw_color', 'mw_metallic', 'mw_roughness', 'mw_normal'}:
            continue
        if manifest['schema_version'] == '1.0' and (keys[0] == 'adjustments' or keys[-1] in core_v2.GLAZE_SOCKETS):
            continue
        col.prop(scene, prop)
    row = layout.row(align=True)
    row.prop(scene, 'mw_channel')
    row.operator('material_workflow.channel', text='', icon='FILE_REFRESH')
    layout.prop(scene, 'mw_image_path')
    row = layout.row(align=True)
    row.operator('material_workflow.image', text='指定贴图')
    row.operator('material_workflow.image', text='移除贴图').remove = True
    if manifest['schema_version'] != '1.1':
        return
    layout.prop_search(scene, 'mw_effect_group', bpy.data, 'node_groups', text='效果节点组')
    layout.operator('material_workflow.prepare_effect_controls')
    if scene.mw_effect_binding_group == scene.mw_effect_group:
        for binding in scene.mw_effect_bindings:
            row = layout.row(align=True)
            row.prop(binding, 'enabled', text='')
            controls = row.row(align=True)
            controls.enabled = binding.enabled
            controls.prop(binding, binding.kind, text=binding.name)
    row = layout.row(align=True)
    row.operator('material_workflow.effect', text='绑定效果').structured = True
    row.operator('material_workflow.effect', text='移除效果').remove = True


def _draw_templates(layout, scene):
    layout.prop(scene, 'mw_template_path', text='分层模板文件')
    layout.operator('material_workflow.read_template_bindings')
    for binding in scene.mw_template_bindings:
        if binding.color_space:
            layout.prop(binding, 'file', text=binding.name)
        else:
            layout.prop_search(binding, 'group', bpy.data, 'node_groups', text=binding.name)
    layout.operator('material_workflow.template_load', text='将模板载入当前材质槽').use_current_target = True
    if scene.mw_manifest_json:
        layout.prop(scene, 'mw_template_name')
        layout.operator('material_workflow.template_save', text='另存应用后的分层模板')
    layout.prop(scene, 'mw_manifest_path', text='现有草稿文件')
    layout.operator('material_workflow.load', text='载入草稿文件')
    if scene.mw_manifest_json:
        row = layout.row(align=True)
        row.prop(scene, 'mw_preview_engine')
        row.operator('material_workflow.preview_engine', text='保存配置')


class MW_PT_panel(bpy.types.Panel):
    bl_label = ''
    bl_idname = 'MW_PT_panel'
    bl_space_type = THEME.editor_space
    bl_region_type = 'UI'
    bl_category = THEME.tab_label
    bl_options = {'HEADER_LAYOUT_EXPAND'}

    def draw_header(self, context):
        draw_brand_header(self.layout, DOMAIN, _ICONS, fallback_icon='MATERIAL')

    def draw(self, context):
        from . import procedural_ui, projection_ui, ui_batch, ui_handoff

        layout = self.layout
        scene = context.scene
        runner = workbench_jobs.RUNNER
        if runner.running:
            progress_card(layout, DOMAIN, _ICONS, text=runner.detail or runner.label, factor=runner.progress,
                          cancel_operator='material_workflow.cancel_workbench_job')
        obj = context.active_object
        if obj and obj.type == 'MESH':
            if obj.material_slots:
                layout.template_list('MATERIAL_UL_matslots', 'mw_artist_slots', obj, 'material_slots',
                                     obj, 'active_material_index', rows=2)
            else:
                layout.label(text='当前模型没有材质槽', icon='MATERIAL')
            row = layout.row(align=True)
            row.prop(scene, 'mw_artist_preset', text='')
            row.prop(scene, 'mw_artist_name', text='')
            row = layout.row(align=True)
            fresh = row.row()
            fresh.enabled = obj.data.uv_layers.active is not None
            fresh.operator('material_workflow.create_basic', text='新建并分配', icon='ADD')
            read = row.row()
            material = obj.active_material
            read.enabled = bool(material and (core.STATE in material or core_v2.STATE in material))
            read.operator('material_workflow.load_current', text='编辑受管材质', icon='MATERIAL')
        else:
            layout.label(text='选择网格以编辑材质槽', icon='MESH_DATA')
        if scene.mw_manifest_json:
            try:
                manifest = _manifest(scene)
                target = manifest['target']
                layout.label(text=f"目标 · {target['object']} / 材质槽 {target['material_slot'] + 1}")
                _draw_layers(layout, scene, manifest)
            except (KeyError, ValueError, TypeError, IndexError):
                status_banner(layout, context, '草稿无法读取，请重新载入', kind='WARN', icon_pack=_ICONS)
        if scene.mw_manual_notice:
            status_banner(layout, context, scene.mw_manual_notice, kind='WARN', icon_pack=_ICONS)

        _, procedural = section_panel(layout, 'MW_SEC_procedural', '程序化 PBR 与模板',
                                      icon_value=_ICONS.action_id('procedural_pbr'), default_closed=True)
        if procedural is not None:
            procedural_ui.MWP_PT_panel.draw_contents(procedural, context)
            _, templates_body = section_panel(procedural, 'MW_SEC_templates', '分层材质模板',
                                              default_closed=True)
            if templates_body is not None:
                _draw_templates(templates_body, scene)

        _, projection = section_panel(layout, 'MW_SEC_projection', '高低模投射与贴图烘焙',
                                      icon_value=_ICONS.action_id('gpu_bake'), default_closed=True)
        if projection is not None:
            projection_ui.MWP_PT_projection_panel.draw_contents(
                projection, context, _ICONS.action_id('gpu_bake'))

        _, batch = section_panel(layout, 'MW_SEC_batch', '批量指派与交接',
                                 icon_value=_ICONS.action_id('batch_assign'), default_closed=True)
        if batch is not None:
            ui_batch.MW_PT_batch.draw_contents(batch, context)
            _, handoff = section_panel(batch, 'MW_SEC_handoff', '现场交接与预览',
                                       default_closed=True)
            if handoff is not None:
                ui_handoff.MW_PT_handoff.draw_contents(handoff, context)

        if scene.mw_last_error:
            status_banner(layout, context, scene.mw_last_error, kind='ERROR', icon_pack=_ICONS)
        elif scene.mw_last_report:
            status_banner(layout, context, scene.mw_last_report, kind=scene.mw_last_report_kind,
                          icon_pack=_ICONS)

CLASSES=(MW_EffectBinding, MW_TemplateBinding, MW_OT_load, MW_OT_load_current, MW_OT_select_layer,
         MW_OT_edit_layer, MW_OT_upgrade, MW_OT_image, MW_OT_channel, MW_OT_effect,
         MW_OT_apply, MW_OT_preview_engine, MW_OT_template_save, MW_OT_template_load,
         MW_OT_prepare_effect, MW_OT_read_template_bindings, MW_OT_create_basic, MW_OT_flip_layer,
         workbench_jobs.MW_OT_cancel_job, MW_PT_panel)
PROPS = {
    'mw_manifest_path': StringProperty(name='草稿文件', subtype='FILE_PATH'),
    'mw_manifest_json': StringProperty(options={'HIDDEN'}),
    'mw_layer_index': IntProperty(default=0, min=0),
    'mw_name': StringProperty(name='图层名称'),
    'mw_enabled': BoolProperty(name='启用', default=True),
    'mw_opacity': FloatProperty(name='不透明度', min=0, max=1, default=1),
    'mw_image_path': StringProperty(name='贴图路径', subtype='FILE_PATH'),
    'mw_effect_group': StringProperty(name='当前文件节点组'),
    'mw_template_path': StringProperty(name='模板文件', subtype='FILE_PATH'),
    'mw_bindings_path': StringProperty(subtype='FILE_PATH', options={'HIDDEN'}),
    'mw_template_id': StringProperty(name='模板标识', default='custom_template'),
    'mw_template_name': StringProperty(name='模板名称', default='自定义模板'),
    'mw_template_version': StringProperty(name='模板版本', default='1.0.0'),
    'mw_manual_notice': StringProperty(options={'HIDDEN'}),
    'mw_last_error': StringProperty(options={'HIDDEN'}),
    'mw_upgrade_authorized': BoolProperty(options={'HIDDEN'}),
}
PROPS['mw_preview_engine']=EnumProperty(name='预览引擎',items=[('CYCLES','Cycles',''),('BLENDER_EEVEE','Eevee','')],default='CYCLES')
PROPS.update(
    mw_channel=EnumProperty(name='贴图通道', items=[(key, _CHANNEL_LABELS.get(key, key), '') for key in (*CHANNELS, 'mask')]),
    mw_effect_parameters=StringProperty(default='[]', options={'HIDDEN'}),
    mw_effect_bindings=CollectionProperty(type=MW_EffectBinding),
    mw_effect_binding_group=StringProperty(options={'HIDDEN'}),
    mw_template_bindings=CollectionProperty(type=MW_TemplateBinding),
    mw_template_binding_path=StringProperty(options={'HIDDEN'}),
    mw_artist_preset=EnumProperty(items=(('base_overlay', '基础与覆盖', ''), ('single_layer', '单层 PBR', ''))),
    mw_artist_name=StringProperty(name='材质名称', default='分层材质', maxlen=63),
    mw_last_report=StringProperty(options={'SKIP_SAVE'}),
    mw_last_report_kind=StringProperty(default='INFO', options={'SKIP_SAVE'}),
)
for prop,label,default in [('mw_color','颜色常量',(0.5,0.5,0.5,1)),('mw_emission','发光颜色',(0,0,0,1)),('mw_tint','贴图色调',(1,1,1,1))]: PROPS[prop]=FloatVectorProperty(name=label,size=4,subtype='COLOR',min=0,max=1,default=default)
for prop,label,low,high,default in [('mw_roughness','粗糙度常量',0,1,0.5),('mw_metallic','金属度',0,1,0),('mw_alpha','透明度',0,1,1),('mw_normal','法线强度',0,10,1),('mw_height','高度距离',0,1,0.001),('mw_emission_strength','发光强度',0,100,0),('mw_roughness_scale','粗糙度倍率',0,10,1),('mw_roughness_bias','粗糙度偏移',-1,1,0)]: PROPS[prop]=FloatProperty(name=label,min=low,max=high,default=default)
for key,label,low,high in [('ior','基底 IOR',1,4),('coat_weight','釉层权重',0,1),('coat_roughness','釉层粗糙度',0,1),('coat_ior','釉层 IOR',1,4)]: PROPS['mw_'+key]=FloatProperty(name=label,min=low,max=high,default=core_v2.GLAZE_DEFAULTS[key])
for prop,label,default,low,high in [('mw_scale','映射比例',(1,1,1),0.00001,10000),('mw_rotation','映射旋转',(0,0,0),-1000,1000),('mw_translation','映射位移',(0,0,0),-10000,10000)]: PROPS[prop]=FloatVectorProperty(name=label,size=3,default=default,min=low,max=high)

def register():
    _ICONS.register()
    for cls in CLASSES: bpy.utils.register_class(cls)
    for name,prop in PROPS.items(): setattr(bpy.types.Scene,name,prop)
    bpy.app.handlers.load_pre.append(workbench_jobs.reset)
    SidebarDock.register_plugin(DOMAIN, _ICONS.icons_dir)

def unregister():
    workbench_jobs.reset(None)
    if workbench_jobs.reset in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(workbench_jobs.reset)
    SidebarDock.unregister_plugin(DOMAIN)
    HUDManager.clear(DOMAIN)
    _ICONS.unregister()
    for name in PROPS:
        if hasattr(bpy.types.Scene,name): delattr(bpy.types.Scene,name)
    for cls in reversed(CLASSES): bpy.utils.unregister_class(cls)

