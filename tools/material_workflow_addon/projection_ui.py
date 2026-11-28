"""Material Workflow panel for confirmed high-to-low projection."""
import re
from pathlib import Path

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty,
                       PointerProperty, StringProperty)
from bpy.types import Operator, Panel, PropertyGroup, UIList

from .contract import WorkflowError
from .projection_bake import CHANNELS, bake_projection
from . import workbench_jobs
from ._ui_kit import apply_node_theme, ensure_object_mode, get_theme, primary_button, prop_column
from .projection_delivery import deliver_project
from .projection_material import (EFFECT_CONTROLS, PROJECTION_NODE, attach_projection, effect_controls,
                                  validate_material_slots, verify_projection_binding,
                                  verify_projection_inputs)


ROLE = re.compile(r'^(.*?)[ _.-]+(high|low|hp|lp)(?:[ _.-]+\d+)?$', re.IGNORECASE)
OPTION_LABELS = {
    'wear': ('边缘磨损', ('启用', '强度', '范围', '露出底色')),
    'dust': ('凹处积灰', ('启用', '强度', '范围', '灰尘颜色')),
}


def _mesh(_, obj):
    return obj.type == 'MESH'


def _unconfirm(pair, context):
    pair.confirmed = False


def _source_changed(source, context):
    for pair in source.id_data.mwp_projection_pairs:
        if any(row == source for row in pair.sources):
            pair.confirmed = False
            break


class MWP_ProjectionSource(PropertyGroup):
    object: PointerProperty(name='高模', type=bpy.types.Object, poll=_mesh, update=_source_changed)


class MWP_ProjectionPair(PropertyGroup):
    target: PointerProperty(name='低模', type=bpy.types.Object, poll=_mesh, update=_unconfirm)
    sources: CollectionProperty(type=MWP_ProjectionSource)
    uv_layer: StringProperty(name='目标 UV', update=_unconfirm)
    confirmed: BoolProperty(name='已检查这组配对')
    cage: PointerProperty(name='自定义投射笼', type=bpy.types.Object, poll=_mesh, update=_unconfirm)
    extrusion: FloatProperty(name='投射笼外扩', min=0, precision=5, subtype='DISTANCE')
    distance: FloatProperty(name='最大射线距离', min=0, precision=5, subtype='DISTANCE')
    ao_distance: FloatProperty(name='AO 距离', min=0, precision=5, subtype='DISTANCE')
    curvature_radius: FloatProperty(name='曲率参考半径', min=0, precision=5, subtype='DISTANCE')
    manifest: StringProperty(name='已烘焙清单', subtype='FILE_PATH', options={'PATH_SUPPORTS_BLEND_RELATIVE'})
    hit_rate: FloatProperty(name='命中率', min=0, max=1)


def suggest_pairs(objects):
    by_name = {}
    for obj in objects:
        suffix = ROLE.fullmatch(obj.name)
        if obj.type != 'MESH' or suffix is None:
            continue
        base, role = suffix.groups()
        rows = by_name.setdefault(base.casefold(), {'low': [], 'high': []})
        rows['low' if role.lower() in {'low', 'lp'} else 'high'].append(obj)
    return [(lows[0], sorted(highs, key=lambda item: item.name))
            for row in by_name.values()
            for lows, highs in [(row['low'], row['high'])]
            if len(lows) == 1 and highs]


def _pair(scene):
    return (scene.mwp_projection_pairs[scene.mwp_projection_index]
            if 0 <= scene.mwp_projection_index < len(scene.mwp_projection_pairs) else None)


def _populate(scene, target, sources):
    pair = scene.mwp_projection_pairs.add()
    pair.target = target
    for obj in sources:
        pair.sources.add().object = obj
    uv = target.data.uv_layers.active
    pair.uv_layer = uv.name if uv else ''
    scale = max(max(target.dimensions), 0.00001)
    pair.extrusion, pair.distance = scale * .025, scale * .06
    pair.ao_distance, pair.curvature_radius = scale * .07, scale * .015
    scene.mwp_projection_index = len(scene.mwp_projection_pairs) - 1
    return pair


def _absolute(value):
    if not value:
        raise ValueError('请先指定路径')
    if value.startswith('//') and not bpy.data.filepath:
        raise ValueError('请先保存工程，再使用项目相对路径')
    path = Path(bpy.path.abspath(value))
    if not path.is_absolute():
        raise ValueError('请指定绝对路径，或以 // 开头的项目相对路径')
    return path.resolve()


def _sources(pair):
    values = [row.object for row in pair.sources]
    if any(obj is None for obj in values):
        raise ValueError('请选择所有高模对象')
    return values


class _ProjectionOperator:
    def execute(self, context):
        try:
            message = self.run(context)
            context.scene.mwp_projection_status = message
            self.report({'INFO'}, message)
            return {'FINISHED'}
        except (WorkflowError, ValueError, TypeError, KeyError, OSError, RuntimeError) as error:
            context.scene.mwp_projection_status = str(error)
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}


class MWP_OT_suggest_projection(_ProjectionOperator, Operator):
    bl_idname = 'material_workflow.projection_suggest'
    bl_label = '按名称建议配对'
    bl_description = '在当前视图层匹配同名的 _low 与 _high，可为一件低模匹配多个高模'
    bl_options = {'UNDO'}

    def run(self, context):
        scene = context.scene
        existing = {pair.target for pair in scene.mwp_projection_pairs}
        suggestions = [(target, sources) for target, sources in suggest_pairs(context.view_layer.objects)
                       if target not in existing]
        for target, sources in suggestions:
            _populate(scene, target, sources)
        if not suggestions:
            return '没有新的同名配对，可用当前选择手动添加'
        return f'新增 {len(suggestions)} 组建议，请逐组检查后确认'


class MWP_OT_add_projection(_ProjectionOperator, Operator):
    bl_idname = 'material_workflow.projection_add'
    bl_label = '用当前选择添加'
    bl_description = '活动网格作为低模，其他选中的网格作为高模'
    bl_options = {'UNDO'}

    def run(self, context):
        target = context.active_object
        if not target or target.type != 'MESH':
            raise ValueError('先让低模成为活动网格')
        if any(pair.target == target for pair in context.scene.mwp_projection_pairs):
            raise ValueError('已有该低模的配对，请在列表中编辑')
        _populate(context.scene, target, [obj for obj in context.selected_objects
                                          if obj != target and obj.type == 'MESH'])
        return '已添加配对，请检查高模和目标 UV'


class MWP_OT_remove_projection(_ProjectionOperator, Operator):
    bl_idname = 'material_workflow.projection_remove'
    bl_label = '移除配对'
    bl_options = {'UNDO'}

    def run(self, context):
        scene = context.scene
        if _pair(scene) is None:
            raise ValueError('未选中配对')
        scene.mwp_projection_pairs.remove(scene.mwp_projection_index)
        scene.mwp_projection_index = max(0, scene.mwp_projection_index - 1)
        return '已从配对列表移除'


class MWP_OT_projection_source(_ProjectionOperator, Operator):
    bl_idname = 'material_workflow.projection_source'
    bl_label = '编辑高模列表'
    bl_options = {'UNDO'}
    index: IntProperty(default=-1)

    def run(self, context):
        pair = _pair(context.scene)
        if pair is None:
            raise ValueError('未选中配对')
        if self.index == -1:
            pair.sources.add()
        elif 0 <= self.index < len(pair.sources):
            pair.sources.remove(self.index)
        pair.confirmed = False
        return '请复核更新后的配对'


class MWP_OT_bake_projection(_ProjectionOperator, Operator):
    bl_idname = 'material_workflow.projection_bake'
    bl_label = '烘焙并接入材质'
    bl_description = '对当前确认的配对烘切线法线、曲率、AO，并接入低模材质'
    bl_options = {'UNDO'}

    def run(self, context):
        scene = context.scene
        pair = _pair(scene)
        if pair is None or not pair.target or not pair.confirmed:
            raise ValueError('请先检查并确认当前配对')
        sources = _sources(pair)
        if not sources:
            raise ValueError('请选择所有高模对象')
        validate_material_slots(pair.target)
        size = scene.mwp_projection_size
        manifest = bake_projection(
            context, pair.target, sources,
            pair.uv_layer, _absolute(scene.mwp_projection_workdir),
            size=size, cage=pair.cage, extrusion=pair.extrusion, distance=pair.distance,
            ao_distance=pair.ao_distance, curvature_radius=pair.curvature_radius,
            samples=scene.mwp_projection_samples, margin=min(12, max(2, size // 128)))
        # Store the verified bake before binding so it remains available if
        # the post-bake material operation encounters a changed input.
        pair.manifest = bpy.path.relpath(str(manifest)) if bpy.data.filepath else str(manifest)
        report = attach_projection(context, pair.target, manifest, sources=sources, cage=pair.cage)
        pair.hit_rate = report['witness']['coverage_fraction']
        return f'贴图已接入 {pair.target.name}，射线命中 {pair.hit_rate:.1%}'


class MWP_OT_attach_projection(_ProjectionOperator, Operator):
    bl_idname = 'material_workflow.projection_attach'
    bl_label = '接入已有烘焙'
    bl_description = '从已完成的 projection.json 接回贴图，可使用后台作业结果'
    bl_options = {'UNDO'}

    def run(self, context):
        pair = _pair(context.scene)
        if pair is None or not pair.target:
            raise ValueError('请选中带低模的配对')
        with ensure_object_mode(context):
            report = attach_projection(context, pair.target, _absolute(pair.manifest),
                                       sources=_sources(pair) or None, cage=pair.cage)
        for slot in pair.target.material_slots:
            tree = slot.material.node_tree if slot.material else None
            node = tree.nodes.get(PROJECTION_NODE) if tree else None
            if node and node.node_tree:
                apply_node_theme(node, 'material')
                for managed in node.node_tree.nodes:
                    apply_node_theme(managed, 'material')
        pair.hit_rate = report['witness']['coverage_fraction']
        return f'已接入 {pair.target.name}，沿用已有烘焙图'


class MWP_OT_bake_background(_ProjectionOperator, Operator):
    bl_idname = 'material_workflow.projection_bake_background'
    bl_label = '后台烘焙投射贴图'

    def run(self, context):
        scene = context.scene
        pair = _pair(scene)
        if pair is None or not pair.target or not pair.confirmed or not pair.uv_layer:
            raise ValueError('请先确认低模、高模和目标 UV')
        sources = _sources(pair)
        if not sources:
            raise ValueError('请选择至少一个高模对象')
        channels = [name for name in CHANNELS if getattr(scene, 'mwp_projection_' + name)]
        if not channels:
            raise ValueError('请选择至少一个烘焙通道')
        complete = len(channels) == len(CHANNELS)
        if complete:
            validate_material_slots(pair.target)
        target = pair.target.name
        source_names = [obj.name for obj in sources]
        cage_name = pair.cage.name if pair.cage else None
        size = scene.mwp_projection_size
        raw_output = scene.mwp_projection_workdir
        output = (Path(bpy.app.tempdir) / 'Blender AI Mesh Maps'
                  if raw_output == '//MeshMaps' and not bpy.data.filepath else _absolute(raw_output))
        request = {
            'job': 'projection', 'target': target, 'sources': source_names,
            'cage': cage_name, 'uv_layer': pair.uv_layer, 'output': str(output),
            'device': scene.mwp_projection_device, 'channels': channels,
            'settings': {**{name: getattr(pair, name) for name in
                            ('extrusion', 'distance', 'ao_distance', 'curvature_radius')},
                         'size': size, 'samples': scene.mwp_projection_samples,
                         'margin': min(12, max(2, size // 128))},
        }

        def complete_bake(destination, ok, result):
            if not ok:
                workbench_jobs.finish(destination, False, result, '')
                return
            destination.mwp_projection_result_path = result['manifest']
            message = f'贴图完成（{result["device"]}），命中 {result["coverage"]:.1%}'
            if complete:
                for saved in destination.mwp_projection_pairs:
                    if ((saved.target.name if saved.target else None) == target
                            and [row.object.name if row.object else None for row in saved.sources] == source_names
                            and (saved.cage.name if saved.cage else None) == cage_name
                            and saved.uv_layer == request['uv_layer'] and saved.confirmed):
                        saved.manifest = result['manifest']
                        saved.hit_rate = result['coverage']
                        message += '，可接入已烘焙贴图'
                        break
            destination.mwp_projection_status = message
            workbench_jobs.finish(destination, True, result, message)

        workbench_jobs.start(context, '投射烘焙', request, complete_bake)
        return '已启动后台投射烘焙'


class MWP_OT_projection_delivery(_ProjectionOperator, Operator):
    bl_idname = 'material_workflow.projection_delivery'
    bl_label = '另存交付工程与贴图'
    bl_description = '写入新目录，工程用相对路径引用贴图'

    def run(self, context):
        pairs = [pair for scene in bpy.data.scenes for pair in scene.mwp_projection_pairs if pair.manifest]
        originals = [(pair, pair.manifest, _absolute(pair.manifest)) for pair in pairs]
        for pair, _, path in originals:
            report = verify_projection_binding(pair.target, path)
            if context.view_layer.objects.get(pair.target.name) == pair.target:
                verify_projection_inputs(context, pair.target, report,
                                         sources=_sources(pair) or None, cage=pair.cage)
        paths = list(dict.fromkeys(path for _, _, path in originals))

        def set_relative_paths(remapping):
            for pair, _, path in originals:
                pair.manifest = remapping[path]

            def restore():
                for pair, original, _ in originals:
                    pair.manifest = original

            return restore

        blend = deliver_project(context, paths, _absolute(context.scene.mwp_projection_output),
                                set_relative_paths=set_relative_paths)
        return '已交付 ' + str(blend)


class MWP_UL_projection_pairs(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        name = item.target.name if item.target else '选择低模'
        layout.label(text=name, icon='CHECKMARK' if item.confirmed else 'MESH_DATA')


class MWP_PT_projection_panel(Panel):
    bl_idname = 'MWP_PT_projection'
    bl_label = '高低模烘焙'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = get_theme('material').tab_label

    def draw(self, context):
        self.draw_contents(self.layout, context)

    @staticmethod
    def draw_contents(layout, context, icon_value=0):
        scene = context.scene
        row = layout.row(align=True)
        row.operator('material_workflow.projection_suggest', icon='VIEWZOOM')
        row.operator('material_workflow.projection_add', text='', icon='ADD')
        row.operator('material_workflow.projection_remove', text='', icon='REMOVE')
        layout.template_list('MWP_UL_projection_pairs', '', scene, 'mwp_projection_pairs',
                             scene, 'mwp_projection_index', rows=3)
        pair = _pair(scene)
        obj = pair.target if pair and pair.target else context.active_object
        controls = effect_controls(obj.active_material if obj and obj.type == 'MESH' else None)
        if controls:
            if len(obj.material_slots) > 1:
                layout.label(text='材质槽 · ' + obj.active_material.name)
            for effect, sockets in EFFECT_CONTROLS.items():
                title, labels = OPTION_LABELS[effect]
                box = layout.box()
                box.prop(controls[sockets[0]], 'default_value', text=title, toggle=True)
                if controls[sockets[0]].default_value:
                    for name, label in zip(sockets[1:], labels[1:]):
                        box.prop(controls[name], 'default_value', text=label)
        if pair is not None:
            box = layout.box()
            box.prop(pair, 'target')
            for index, source in enumerate(pair.sources):
                row = box.row(align=True)
                row.prop(source, 'object')
                row.operator('material_workflow.projection_source', text='', icon='X').index = index
            box.operator('material_workflow.projection_source', text='添加高模', icon='ADD')
            if pair.target and pair.target.type == 'MESH':
                box.prop_search(pair, 'uv_layer', pair.target.data, 'uv_layers')
            box.prop(pair, 'confirmed')
            col = prop_column(box, align=True)
            col.prop(scene, 'mwp_projection_size', text='分辨率')
            col.prop(scene, 'mwp_projection_device', text='计算设备')
            col.prop(scene, 'mwp_projection_workdir', text='烘焙目录')
            flags = box.row(align=True)
            for key, title in (('normal', '法线'), ('curvature', '曲率'), ('ao', 'AO')):
                flags.prop(scene, 'mwp_projection_' + key, text=title, toggle=True)
            box.prop(scene, 'mwp_projection_advanced', toggle=True)
            if scene.mwp_projection_advanced:
                for option in ('cage', 'extrusion', 'distance', 'ao_distance', 'curvature_radius'):
                    box.prop(pair, option)
                box.prop(scene, 'mwp_projection_samples')
            primary_button(box, 'material_workflow.projection_bake_background', '后台烘焙',
                           icon_value=icon_value, icon='RENDER_STILL',
                           enabled=bool(pair.confirmed and pair.target and pair.uv_layer and
                                        not workbench_jobs.RUNNER.running))
            box.prop(pair, 'manifest')
            row = box.row()
            row.enabled = bool(pair.manifest)
            row.operator('material_workflow.projection_attach')
            if pair.hit_rate:
                text = '存在漏烘，命中率' if pair.hit_rate < 1 else '来源射线命中'
                box.label(text=f'{text} {pair.hit_rate:.2%}', icon='ERROR' if pair.hit_rate < 1 else 'INFO')

        layout.prop(scene, 'mwp_projection_output')
        layout.operator('material_workflow.projection_delivery', icon='FILE_FOLDER')
        if scene.mwp_projection_result_path:
            row = layout.row()
            row.enabled = False
            row.prop(scene, 'mwp_projection_result_path', text='最近烘焙报告')
        if scene.mwp_projection_status:
            layout.label(text=scene.mwp_projection_status, icon='INFO')


CLASSES = (MWP_ProjectionSource, MWP_ProjectionPair, MWP_OT_suggest_projection,
           MWP_OT_add_projection, MWP_OT_remove_projection, MWP_OT_projection_source,
           MWP_OT_bake_projection, MWP_OT_bake_background, MWP_OT_attach_projection,
           MWP_OT_projection_delivery, MWP_UL_projection_pairs)
PROPERTIES = {
    'mwp_projection_pairs': CollectionProperty(type=MWP_ProjectionPair),
    'mwp_projection_index': IntProperty(min=0),
    'mwp_projection_workdir': StringProperty(name='烘焙目录', default='//MeshMaps', subtype='DIR_PATH',
                                             options={'PATH_SUPPORTS_BLEND_RELATIVE'}),
    'mwp_projection_output': StringProperty(name='交付目录', default='//Delivery', subtype='DIR_PATH',
                                            options={'PATH_SUPPORTS_BLEND_RELATIVE'}),
    'mwp_projection_size': IntProperty(name='贴图尺寸', default=1024, min=64, max=2048),
    'mwp_projection_samples': IntProperty(name='AO 采样', default=16, min=1, max=128),
    'mwp_projection_advanced': BoolProperty(name='高级投射设置'),
    'mwp_projection_status': StringProperty(options={'SKIP_SAVE'}),
    'mwp_projection_normal': BoolProperty(default=True),
    'mwp_projection_curvature': BoolProperty(default=True),
    'mwp_projection_ao': BoolProperty(default=True),
    'mwp_projection_device': EnumProperty(items=(
        ('AUTO', '自动选择 GPU', '优先使用可用 GPU，随后回退 CPU'),
        ('CPU', 'CPU', '使用 CPU 烘焙'),
    )),
    'mwp_projection_result_path': StringProperty(subtype='FILE_PATH', options={'SKIP_SAVE'}),
}


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    for name, prop in PROPERTIES.items():
        setattr(bpy.types.Scene, name, prop)


def unregister():
    for name in PROPERTIES:
        delattr(bpy.types.Scene, name)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
