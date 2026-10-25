"""Current-scene UI for public procedural recipes, live controls and PBR export.

No scene loading, factory reset, preference editing or current-blend save occurs
in this module. Disk operations own fresh UUID subdirectories of a user-selected
absolute directory. Shader construction and baking stay in the shared backends.
"""
import copy
import hashlib
import json
from pathlib import Path
import uuid

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, IntProperty, StringProperty

from .contract import WorkflowError
from . import procedural_core as core
from .procedural_examples import make_recipe
from .procedural_contract import normalize_manifest, EXPORT_CHANNELS
from .procedural_bake import bake_channels, rebuild_material

FAMILY_ITEMS = [
    ('painted_metal', '漆面金属', '金属底材、涂漆、边缘掉漆、局部锈和积灰'),
    ('wood', '木材', '方向木纹、多尺度粗糙度、微凹凸与少量清漆'),
    ('glass_clear', '清透玻璃', '保持Alpha 1，以透射和IOR表达玻璃'),
    ('glass_tinted', '着色玻璃', '淡绿色表面透射着色'),
    ('glass_frosted', '磨砂玻璃', '透射、粗糙度及微凹凸表达磨砂'),
]
SIZE_ITEMS = [(str(n), str(n) + (' (2K)' if n == 2048 else ''), '') for n in (128, 256, 512, 1024, 2048)]


def _absolute_path(raw, description, *, directory=False):
    if not raw or not Path(raw).is_absolute():
        raise WorkflowError('ui_path', description + '必须是已选择的绝对路径')
    path = Path(raw).resolve(strict=True)
    if directory and not path.is_dir():
        raise WorkflowError('ui_path', description + '不是目录')
    if not directory and not path.is_file():
        raise WorkflowError('ui_path', description + '不是文件')
    return path


def _write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def _bindings(context):
    obj = context.object
    if not obj or obj.type != 'MESH' or obj.mode != 'OBJECT' or context.mode != 'OBJECT':
        raise WorkflowError('ui_target', '请选择Object模式下有UV的活动网格')
    if obj.name not in context.view_layer.objects or not obj.data.polygons:
        raise WorkflowError('ui_target', '活动网格必须属于当前视图层且包含面')
    name = context.scene.mwp_ui_uv or (obj.data.uv_layers.active.name if obj.data.uv_layers.active else '')
    if name not in obj.data.uv_layers:
        raise WorkflowError('missing_uv', '请选择当前对象已有的UV层')
    slot = context.scene.mwp_ui_slot
    if slot not in {p.material_index for p in obj.data.polygons}:
        raise WorkflowError('ui_target', '该材质槽尚无面使用；请先在Blender中完成面分配')
    return ({'scene': context.scene.name, 'view_layer': context.view_layer.name,
             'frame': context.scene.frame_current},
            {'object': obj.name, 'material_slot': slot, 'uv_layer': name})


def current_material(context):
    obj = context.object
    slot = context.scene.mwp_ui_slot
    return obj.material_slots[slot].material if obj and obj.type == 'MESH' and slot < len(obj.material_slots) else None


def _managed(context):
    material = current_material(context)
    if not material or core.MANIFEST not in material or core.STATE not in material:
        raise WorkflowError('ui_material', '所选槽没有程序化PBR受管材质')
    return material


def visible_controls(material, scope='ALL', text=''):
    """Return the actual live root sockets, never detached property copies."""
    if not material or not material.use_nodes: return []
    root = material.node_tree.nodes.get('MWP Controls')
    if not root: return []
    keys = json.loads(material.get('mwp_controls', '{}'))
    result = []
    for socket in root.inputs:
        if socket.name not in keys or not hasattr(socket, 'default_value'): continue
        if scope == 'LAYER' and not socket.name.startswith('L/'): continue
        if scope == 'SIGNAL' and not socket.name.startswith('S/'): continue
        if text and text.casefold() not in socket.name.casefold(): continue
        result.append(socket)
    return result


class _ProceduralOperator:
    def new_run(self, context, purpose):
        root = _absolute_path(context.scene.mwp_ui_work_dir, '工作目录', directory=True)
        run = root / ('procedural-' + purpose + '-' + uuid.uuid4().hex)
        run.mkdir(exist_ok=False)
        self._owned_run = str(run)
        context.scene.mwp_ui_result_dir = str(run)
        return run

    def finish(self, context, run, receipt, message):
        _write_json(run / 'ui-result.json', receipt)
        context.scene.mwp_ui_last_report = str(run / 'ui-result.json')
        context.scene.mwp_ui_status = message
        self.report({'INFO'}, message + '：' + str(run))

    def execute(self, context):
        self._owned_run = ''
        context.scene.mwp_ui_error = ''
        try:
            self.run(context)
            return {'FINISHED'}
        except Exception as error:
            message = getattr(error, 'code', 'procedural_ui') + ': ' + str(error)
            context.scene.mwp_ui_error = message
            context.scene.mwp_ui_status = '操作未完成；查看错误及本次目录'
            if getattr(error, 'report_path', None):
                context.scene.mwp_ui_last_report = error.report_path
            if self._owned_run:
                try:
                    _write_json(Path(self._owned_run) / 'ui-error.json',
                                {'status': 'failed', 'error': message,
                                 'backend_report': getattr(error, 'report_path', None)})
                except OSError:
                    pass
            self.report({'ERROR'}, message)
            return {'CANCELLED'}


class MWP_OT_create(_ProceduralOperator, bpy.types.Operator):
    bl_idname = 'material_workflow.procedural_create'
    bl_label = '按预设新建并分配'
    bl_description = '为当前对象/UV/槽创建新的程序化材质ID；请先确认目标槽'
    bl_options = {'REGISTER', 'UNDO'}

    def run(self, context):
        binding, target = _bindings(context)
        scene = context.scene
        recipe = make_recipe(scene.mwp_ui_family, binding, target,
                'mwp_' + uuid.uuid4().hex[:20], scene.mwp_ui_name or None,
                seed=scene.mwp_ui_seed, weathering=scene.mwp_ui_weathering,
                grain_axis=scene.mwp_ui_grain_axis, metal_region=scene.mwp_ui_metal_region,
                timber_scale=tuple(scene.mwp_ui_timber_scale))
        recipe['mesh_map_size'] = int(scene.mwp_ui_mesh_size)
        recipe = normalize_manifest(recipe)
        run = self.new_run(context, 'create')
        _write_json(run / 'request.json', recipe)
        report = core.apply_material(recipe, work_dir=run)
        self.finish(context, run, {'status': 'complete', 'operation': 'create', 'result': report}, '已创建程序化材质')


class MWP_OT_update(_ProceduralOperator, bpy.types.Operator):
    bl_idname = 'material_workflow.procedural_update'
    bl_label = '更新当前材质，保留实际参数'
    bl_description = '读取当前受管材质与手改参数，以核心三方合并更新；预设栏不覆盖当前参数'
    bl_options = {'REGISTER', 'UNDO'}

    def run(self, context):
        binding, target = _bindings(context)
        material = _managed(context)
        live = core.get_stored_manifest(material)
        if live['target'] != target:
            raise WorkflowError('managed_conflict', '当前对象、UV或槽与受管材质原绑定不同；请用模板载入到新目标')
        # The core preserves edits only when incoming control values still equal
        # the saved request baseline. Feeding live edits back as new requested
        # edits would incorrectly produce a three-way overlap conflict.
        recipe = json.loads(material[core.MANIFEST])
        recipe['context'] = binding
        run = self.new_run(context, 'update')
        _write_json(run / 'live-before.json', live)
        _write_json(run / 'request.json', recipe)
        identity = live['material']['id']
        report = core.apply_material(recipe, work_dir=run)
        updated = current_material(context)
        actual = core.get_stored_manifest(updated)
        if actual['material']['id'] != identity:
            raise WorkflowError('managed_conflict', '更新后材质身份意外改变')
        self.finish(context, run, {'status': 'complete', 'operation': 'update', 'result': report,
                                  'live_after': actual}, '已更新并保留实际参数')


class MWP_OT_load(_ProceduralOperator, bpy.types.Operator):
    bl_idname = 'material_workflow.procedural_load'
    bl_label = '从JSON新建到当前目标'
    bl_description = '读取原始清单或程序模板；显式重绑当前对象/UV/槽，并生成新的材质ID'
    bl_options = {'REGISTER', 'UNDO'}

    def run(self, context):
        binding, target = _bindings(context)
        path = _absolute_path(context.scene.mwp_ui_json_path, '模板JSON')
        if path.stat().st_size > 8 * 1024 * 1024:
            raise WorkflowError('ui_template', '模板JSON超过8MiB上限')
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if isinstance(value, dict) and value.get('schema') == 'material-procedural-template-1.0':
            if set(value) != {'schema', 'manifest'}:
                raise WorkflowError('ui_template', '模板封装含未知字段')
            value = value['manifest']
        recipe = normalize_manifest(value)
        recipe['context'] = binding
        recipe['target'] = target
        recipe['material']['id'] = 'mwp_' + uuid.uuid4().hex[:20]
        recipe['material']['name'] = (context.scene.mwp_ui_name or recipe['material']['name'])[:63]
        recipe['mesh_map_size'] = int(context.scene.mwp_ui_mesh_size)
        recipe = normalize_manifest(recipe)
        run = self.new_run(context, 'load')
        _write_json(run / 'request.json', recipe)
        report = core.apply_material(recipe, work_dir=run)
        self.finish(context, run, {'status': 'complete', 'operation': 'load', 'template': str(path), 'result': report}, '已将模板新建到当前目标')


class MWP_OT_template_save(_ProceduralOperator, bpy.types.Operator):
    bl_idname = 'material_workflow.procedural_template_save'
    bl_label = '保存当前实际参数为模板'

    def run(self, context):
        material = _managed(context)
        run = self.new_run(context, 'template')
        report = core.save_template(material, run / 'template.json')
        context.scene.mwp_ui_json_path = report['file']
        self.finish(context, run, {'status': 'complete', 'operation': 'template_save', 'result': report}, '已保存实际参数模板')


class MWP_OT_export(_ProceduralOperator, bpy.types.Operator):
    bl_idname = 'material_workflow.procedural_export'
    bl_label = '导出PBR贴图与重建材质'
    bl_description = '写入新目录，保留作者材质绑定；另创建仅贴图材质和独立材质blend'

    def run(self, context):
        binding, target = _bindings(context)
        material = _managed(context)
        core.get_stored_manifest(material)
        obj = context.object
        run = self.new_run(context, 'export')
        report = bake_channels(obj, material, run, {
            'channels': list(EXPORT_CHANNELS), 'size': int(context.scene.mwp_ui_export_size),
            'margin': 16, 'samples': context.scene.mwp_ui_bake_samples, 'bit_depth': '16'},
            uv_layer=target['uv_layer'])
        rebuilt = rebuild_material(report, name=material.name + ' Baked')
        rebuilt.use_fake_user = True
        library = run / 'baked-material.blend'
        if library.exists():
            raise WorkflowError('ui_output_exists', '重建材质输出已存在')
        bpy.data.libraries.write(str(library), {rebuilt}, path_remap='RELATIVE_ALL', fake_user=True, compress=True)
        context.scene.mwp_ui_rebuilt_material = rebuilt.name
        receipt = {'status': 'complete', 'operation': 'export', 'report_file': report['report_file'],
                   'output_dir': report['output_dir'], 'rebuilt_material': rebuilt.name,
                   'rebuilt_library': {'file': str(library), 'sha256': hashlib.sha256(library.read_bytes()).hexdigest()},
                   'author_material': material.name, 'author_binding_retained': obj.material_slots[target['material_slot']].material == material}
        self.finish(context, run, receipt, '已导出贴图并保留新的重建材质')


class MWP_PT_panel(bpy.types.Panel):
    bl_idname = 'MWP_PT_procedural'
    bl_label = '程序化PBR'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Material Workflow'

    def draw(self, context):
        layout, scene = self.layout, context.scene
        layout.label(text='当前活动网格 · 明确槽与UV', icon='MATERIAL')
        layout.prop(scene, 'mwp_ui_slot')
        obj = context.object
        if obj and obj.type == 'MESH':
            layout.prop_search(scene, 'mwp_ui_uv', obj.data, 'uv_layers')
            if not scene.mwp_ui_uv and obj.data.uv_layers.active:
                layout.label(text='当前UV：' + obj.data.uv_layers.active.name)
        else:
            layout.label(text='请选择有UV的网格', icon='INFO')
        layout.prop(scene, 'mwp_ui_work_dir')
        preset = layout.box()
        preset.label(text='新建预设')
        for prop in ('mwp_ui_family', 'mwp_ui_name', 'mwp_ui_seed', 'mwp_ui_weathering', 'mwp_ui_mesh_size'):
            preset.prop(scene, prop)
        if scene.mwp_ui_family == 'wood':
            preset.prop(scene, 'mwp_ui_grain_axis')
            preset.prop(scene, 'mwp_ui_timber_scale')
            preset.label(text='按局部模型单位与物理采样尺度明确填写倍率')
        elif scene.mwp_ui_family == 'painted_metal':
            preset.prop(scene, 'mwp_ui_metal_region')
        preset.label(text='局部坐标纹理；方向与高度带可在参数中调节')
        preset.operator('material_workflow.procedural_create')
        material = current_material(context)
        managed = bool(material and core.MANIFEST in material)
        row = layout.row(); row.enabled = managed
        row.operator('material_workflow.procedural_update')
        if managed:
            layout.label(text='实际材质：' + material.name)
            box = layout.box(); box.prop(scene, 'mwp_ui_show_controls', text='展开实际节点参数')
            if scene.mwp_ui_show_controls:
                box.prop(scene, 'mwp_ui_control_scope'); box.prop(scene, 'mwp_ui_control_filter')
                try:
                    controls = visible_controls(material, scene.mwp_ui_control_scope, scene.mwp_ui_control_filter)
                    for socket in controls:
                        row = box.row(); row.enabled = not socket.is_linked
                        row.prop(socket, 'default_value', text=socket.name)
                    if not controls: box.label(text='没有匹配的参数；可按层ID、源ID或通道筛选')
                except (ValueError, TypeError, KeyError):
                    box.label(text='参数结构异常，请检查材质节点', icon='ERROR')
        box = layout.box(); box.prop(scene, 'mwp_ui_json_path')
        box.operator('material_workflow.procedural_load')
        row = box.row(); row.enabled = managed
        row.operator('material_workflow.procedural_template_save')
        box = layout.box(); box.label(text='PBR导出')
        box.prop(scene, 'mwp_ui_export_size'); box.prop(scene, 'mwp_ui_bake_samples')
        row = box.row(); row.enabled = managed
        row.operator('material_workflow.procedural_export')
        if scene.mwp_ui_rebuilt_material: box.label(text='重建材质：' + scene.mwp_ui_rebuilt_material)
        if scene.mwp_ui_status: layout.label(text=scene.mwp_ui_status, icon='INFO')
        if scene.mwp_ui_result_dir:
            row = layout.row(); row.enabled = False; row.prop(scene, 'mwp_ui_result_dir')
        if scene.mwp_ui_last_report:
            row = layout.row(); row.enabled = False; row.prop(scene, 'mwp_ui_last_report')
        if scene.mwp_ui_error:
            box = layout.box(); box.alert = True
            for start in range(0, len(scene.mwp_ui_error), 65):
                box.label(text=scene.mwp_ui_error[start:start+65])


CLASSES = (MWP_OT_create, MWP_OT_update, MWP_OT_load, MWP_OT_template_save, MWP_OT_export, MWP_PT_panel)
PROPS = {
    'mwp_ui_family': EnumProperty(name='材质族', items=FAMILY_ITEMS, default='painted_metal'),
    'mwp_ui_name': StringProperty(name='新建材质名称'),
    'mwp_ui_seed': IntProperty(name='新建随机种子', default=7, min=0, max=100000),
    'mwp_ui_weathering': FloatProperty(name='新建旧化程度', default=0.4, min=0, max=1),
    'mwp_ui_grain_axis': EnumProperty(name='木材局部长轴', items=[(axis, axis, '共享木轴坐标映射到内部X方向') for axis in ('X','Y','Z')], default='X'),
    'mwp_ui_timber_scale': FloatVectorProperty(name='木材局部采样倍率 XYZ', size=3, default=(1, 1, 1),
        min=1e-6, description='按局部模型单位和所需物理采样尺度填写三轴正倍率；先缩放后木轴旋转，不自动推断对象缩放或单位'),
    'mwp_ui_metal_region': EnumProperty(name='金属旧化区域', items=[('lower','下部接触区','局部Z下部与接触边缘'),('edges','边缘与凹部','盖板/边缘区域'),('seam','卷边接缝','已分件卷边或接缝；不自动识别几何')], default='lower'),
    'mwp_ui_work_dir': StringProperty(name='绝对工作目录', subtype='DIR_PATH'),
    'mwp_ui_slot': IntProperty(name='材质槽（从0计）', default=0, min=0, max=63),
    'mwp_ui_uv': StringProperty(name='UV层（空=活动UV）'),
    'mwp_ui_mesh_size': EnumProperty(name='几何辅助图尺寸', items=SIZE_ITEMS, default='512'),
    'mwp_ui_export_size': EnumProperty(name='贴图尺寸', items=SIZE_ITEMS, default='2048'),
    'mwp_ui_bake_samples': IntProperty(name='烘焙采样', default=16, min=1, max=256),
    'mwp_ui_json_path': StringProperty(name='清单或模板JSON', subtype='FILE_PATH'),
    'mwp_ui_show_controls': BoolProperty(name='展开实际节点参数', default=False),
    'mwp_ui_control_scope': EnumProperty(name='参数范围', items=[('ALL','全部',''),('LAYER','图层',''),('SIGNAL','源信号','')], default='LAYER'),
    'mwp_ui_control_filter': StringProperty(name='层ID / 源ID / 通道筛选', default='roughness'),
    'mwp_ui_result_dir': StringProperty(name='结果目录', subtype='DIR_PATH'),
    'mwp_ui_last_report': StringProperty(name='本次报告', subtype='FILE_PATH'),
    'mwp_ui_rebuilt_material': StringProperty(name='重建材质'),
    'mwp_ui_status': StringProperty(options={'HIDDEN'}),
    'mwp_ui_error': StringProperty(options={'HIDDEN'}),
}


def register():
    for cls in CLASSES: bpy.utils.register_class(cls)
    for name, prop in PROPS.items(): setattr(bpy.types.Scene, name, prop)


def unregister():
    for name in PROPS:
        if hasattr(bpy.types.Scene, name): delattr(bpy.types.Scene, name)
    for cls in reversed(CLASSES): bpy.utils.unregister_class(cls)
