"""Artist controls for editable Blender 5.3 compositor effects."""

from pathlib import Path
import tempfile

import bpy
from bpy.props import CollectionProperty, EnumProperty, FloatProperty, FloatVectorProperty, PointerProperty, StringProperty

from . import c5, scene_effects
from .contract import WorkflowError
from ._ui_kit import (
    BackgroundJobRunner, IconPack, apply_node_theme, ensure_object_mode, primary_button,
    progress_card, prop_column, section_panel, status_banner,
)


DOMAIN = "compositing"
PROPERTY = "compositing_workflow_ui"
ICONS = IconPack(DOMAIN)
RENDER = BackgroundJobRunner(DOMAIN)
_OWNER = "compositing_workbench_tool_v1"
_NAMES = {"LIGHT": "合成 · 光组混光", "GRADE": "合成 · 遮罩调色", "BLOOM": "合成 · 辉光",
          "HALO": "合成 · 镜头光晕", "FOCUS": "合成 · 景深", "LINES": "合成 · 线色叠合"}


def settings(scene):
    return getattr(scene, PROPERTY)


def report(scene, message, kind="OK"):
    state = settings(scene)
    state.last_report, state.last_report_kind = message, kind


def fail(operator, context, error):
    label = f"操作未完成。{error}"
    report(context.scene, label, "ERROR")
    operator.report({"ERROR"}, label)
    return {"CANCELLED"}


def selected_target(state):
    return state.matte_object if state.matte_type == "OBJECT" else state.matte_material


class CF_LightGroup(bpy.types.PropertyGroup):
    group_name: StringProperty(name="光组")
    gain: FloatProperty(name="亮度倍率", default=1, min=0, max=8, soft_max=3)
    tint: FloatVectorProperty(name="光色倍率", subtype="COLOR", size=3, default=(1, 1, 1), min=0, max=4)


class CF_Settings(bpy.types.PropertyGroup):
    last_report: StringProperty(name="操作结果")
    last_report_kind: StringProperty(name="状态级别", default="INFO")
    light_groups: CollectionProperty(type=CF_LightGroup)
    light_layer: StringProperty(name="光组所属视图层")
    matte_type: EnumProperty(
        name="遮罩类型", items=[("OBJECT", "对象", ""), ("MATERIAL", "材质", "")],
    )
    matte_object: PointerProperty(name="对象", type=bpy.types.Object)
    matte_material: PointerProperty(name="材质", type=bpy.types.Material)
    grade_offset: FloatProperty(name="偏移", default=0, min=-1, max=1)
    grade_power: FloatVectorProperty(name="幂次颜色", subtype="COLOR", size=3,
                                    default=(1, 1, 1), min=0.01, max=4)
    grade_slope: FloatVectorProperty(name="斜率颜色", subtype="COLOR", size=3,
                                    default=(1, 1, 1), min=0, max=4)
    grade_contrast: FloatProperty(name="对比度", default=0, min=-100, max=100)
    grade_saturation: FloatProperty(name="饱和度", default=1, min=0, max=2)
    bloom_strength: FloatProperty(name="辉光强度", default=0.35, min=0, max=10)
    bloom_threshold: FloatProperty(name="亮部阈值", default=1, min=0, max=100)
    focus_fstop: FloatProperty(name="景深光圈", default=4, min=0.1, max=128)
    focus_blur: FloatProperty(name="模糊上限（像素）", default=16, min=0, max=256)
    line_image: PointerProperty(name="线稿图像", type=bpy.types.Image)
    line_interaction: PointerProperty(name="线色遮盖通道", type=bpy.types.Image)
    line_strength: FloatProperty(name="线稿强度", subtype="FACTOR", default=1, min=0, max=1)
    output_path: StringProperty(name="输出文件", subtype="FILE_PATH")
    output_format: EnumProperty(name="输出格式", items=[("PNG", "PNG", ""), ("OPEN_EXR", "EXR", "")])
    depth_input: EnumProperty(name="素材来源", items=[("IMAGES", "原图与深度", ""), ("BUNDLE", "已核验深度包", "")])
    relight_rgb: StringProperty(name="原图 PNG", subtype="FILE_PATH")
    relight_depth: StringProperty(name="深度 NPY 或 EXR", subtype="FILE_PATH")
    depth_channel: StringProperty(name="深度通道", description="EXR 中的标量通道名，单通道 EXR 可留空")
    depth_strength: FloatProperty(name="相对深度强度", default=0.12, min=0.01, max=1)
    near_order: EnumProperty(name="深度值方向", items=[
        ("larger_nearer", "越大越近", ""), ("smaller_nearer", "越小越近", ""),
    ])
    depth_bundle: StringProperty(name="深度包", subtype="FILE_PATH")
    relight_mode: EnumProperty(name="补光风格", items=[("REAL", "写实", ""), ("ANIME", "动漫", "")])


def _node(tree, kind, label, x, y=0):
    node = c5.named_node(tree, kind, label, (x, y))
    apply_node_theme(node, DOMAIN)
    return node


def _connect(tree, output, node, input_id):
    tree.links.new(output, c5.socket(node.inputs, input_id))


def _render_layers(tree, scene, layer):
    node = _node(tree, "CompositorNodeRLayers", "当前视图层通道", -900, -280)
    node.scene = scene
    node.layer = layer.name
    return node


def _light_mix(tree, image, scene, layer, state):
    if scene.render.engine != "CYCLES":
        raise ValueError("光组通道需要 Cycles 渲染引擎")
    if state.light_layer != layer.name or not state.light_groups:
        raise ValueError("请先读取当前视图层的光组")
    available = {group.name for group in layer.lightgroups}
    if {row.group_name for row in state.light_groups} != available:
        raise ValueError("视图层光组已修改，请重新读取")
    passes = _render_layers(tree, scene, layer)
    source = image
    for index, group in enumerate(state.light_groups):
        port = c5.socket(passes.outputs, "Combined_" + group.group_name)
        delta = _node(tree, "ShaderNodeVectorMath", f"{group.group_name} · 光色差值", -610, -220 * index)
        delta.operation = "MULTIPLY"
        _connect(tree, port, delta, "Vector")
        c5.socket(delta.inputs, "Vector_001").default_value = tuple(group.gain * tint - 1 for tint in group.tint)
        addition = _node(tree, "ShaderNodeVectorMath", f"{group.group_name} · 加入混光", -310 + 180 * index)
        addition.operation = "ADD"
        _connect(tree, source, addition, "Vector")
        _connect(tree, c5.socket(delta.outputs, "Vector"), addition, "Vector_001")
        source = c5.socket(addition.outputs, "Vector")
    alpha = _node(tree, "CompositorNodeSetAlpha", "保留输入透明度", 120 + 180 * len(state.light_groups))
    c5.socket(alpha.inputs, "Type").default_value = "Replace Alpha"
    _connect(tree, source, alpha, "Image")
    separate = _node(tree, "CompositorNodeSeparateColor", "读取输入透明度", -600, 260)
    _connect(tree, image, separate, "Image")
    _connect(tree, c5.socket(separate.outputs, "Alpha"), alpha, "Alpha")
    return c5.socket(alpha.outputs, "Image")


def _matte_grade(tree, image, scene, layer, state):
    target = selected_target(state)
    if target is None:
        raise ValueError("请选择要调色的对象或材质")
    node = _node(tree, "CompositorNodeCryptomatteV2", "制作选区遮罩", -580, -200)
    node.source = "RENDER"
    node.scene = scene
    suffix = "CryptoObject" if state.matte_type == "OBJECT" else "CryptoMaterial"
    node.layer_name = f"{layer.name}.{suffix}"
    node.matte_id = target.name
    balance = _node(tree, "CompositorNodeColorBalance", "ASC CDL 色彩平衡", -340)
    c5.configure_grade(balance, {
        "mode": "ASC_CDL", "base_offset": state.grade_offset,
        "power": tuple(state.grade_power), "slope": tuple(state.grade_slope),
    })
    _connect(tree, image, balance, "Image")
    contrast = _node(tree, "CompositorNodeBrightContrast", "对比度", -120)
    c5.socket(contrast.inputs, "Contrast").default_value = state.grade_contrast
    _connect(tree, c5.socket(balance.outputs, "Image"), contrast, "Image")
    saturation = _node(tree, "CompositorNodeHueSat", "饱和度", 100)
    c5.socket(saturation.inputs, "Saturation").default_value = state.grade_saturation
    _connect(tree, c5.socket(contrast.outputs, "Image"), saturation, "Image")
    mix = _node(tree, "ShaderNodeMix", "按遮罩合并", 340)
    mix.data_type = "RGBA"
    _connect(tree, c5.socket(node.outputs, "Matte"), mix, "Factor_Float")
    _connect(tree, image, mix, "A_Color")
    _connect(tree, c5.socket(saturation.outputs, "Image"), mix, "B_Color")
    return c5.socket(mix.outputs, "Result_Color")


def _soft_light(tree, image, state, halo):
    node = _node(tree, "CompositorNodeGlare", "镜头光晕" if halo else "柔和辉光", 0)
    c5.socket(node.inputs, "Type").default_value = "Fog Glow" if halo else "Bloom"
    c5.socket(node.inputs, "Highlights Threshold").default_value = state.bloom_threshold
    c5.socket(node.inputs, "Strength").default_value = state.bloom_strength
    _connect(tree, image, node, "Image")
    return c5.socket(node.outputs, "Image")


def _depth_focus(tree, image, scene, layer, state):
    if scene.camera is None:
        raise ValueError("请先为当前场景指定相机")
    lobe = _node(tree, "CompositorNodeDefocus", "由 Z 通道控制景深", 0)
    lobe.scene = scene
    lobe.use_zbuffer = True
    lobe.f_stop = state.focus_fstop
    lobe.blur_max = state.focus_blur
    passes = _render_layers(tree, scene, layer)
    _connect(tree, image, lobe, "Image")
    _connect(tree, c5.socket(passes.outputs, "Depth"), lobe, "Z")
    return c5.socket(lobe.outputs, "Image")


def _line_composite(tree, beauty, state):
    if state.line_image is None:
        raise ValueError("请在图像编辑器打开线稿并在此选择")
    line = _node(tree, "CompositorNodeImage", "独立线稿", -740, -220)
    line.image = state.line_image
    line_out = c5.socket(line.outputs, "Image")
    if state.line_interaction is None:
        overlay = _node(tree, "CompositorNodeAlphaOver", "以线稿透明度叠合", 100)
        c5.socket(overlay.inputs, "Fac").default_value = state.line_strength
        _connect(tree, beauty, overlay, "Background")
        _connect(tree, line_out, overlay, "Foreground")
        return c5.socket(overlay.outputs, "Image")
    interaction = _node(tree, "CompositorNodeImage", "色块线稿遮盖通道", -740, -450)
    interaction.image = state.line_interaction
    difference = c5.vector_math(tree, "SUBTRACT", "线稿减去遮盖", (-460, -200),
                               line_out, c5.socket(interaction.outputs, "Image"))
    delta = c5.vector_math(tree, "SCALE", "线稿强度", (-220, -120), difference, factor=state.line_strength)
    color = c5.vector_math(tree, "ADD", "线色重建", (20, 0), beauty, delta)
    alpha = _node(tree, "CompositorNodeSetAlpha", "保留色块透明度", 260)
    c5.socket(alpha.inputs, "Type").default_value = "Replace Alpha"
    _connect(tree, color, alpha, "Image")
    separate = _node(tree, "CompositorNodeSeparateColor", "读取色块透明度", 20, -220)
    _connect(tree, beauty, separate, "Image")
    _connect(tree, c5.socket(separate.outputs, "Alpha"), alpha, "Alpha")
    return c5.socket(alpha.outputs, "Image")


def _attach(scene, name, tree, key):
    tree[_OWNER] = key
    tree.allow_usage_in_scene_compositor_effect = True
    scene_effects._validate_image_effect(tree)
    existing = scene.compositor_effects.get(name)
    if existing is None:
        scene_effects.add_effect(scene.name, name, source={"kind": "LOCAL", "name": tree.name},
                                 preview=True, render=True)
        return
    previous = existing.node_group
    if not previous or previous.get(_OWNER) != key or previous.users != 1 or previous.animation_data:
        raise ValueError(f"「{name}」的名称已占用或节点组被共享，请先在合成编辑器保留原效果")
    scene_effects._editable_effect(scene, name)
    scene_effects._guard_animation(scene, existing, remove=True)
    existing.node_group = tree
    if existing.node_group != tree:
        raise ValueError("合成效果组未能更新")
    if previous.users == 0:
        bpy.data.node_groups.remove(previous)


def apply(scene, layer, state, key):
    old_passes = (layer.use_pass_z, layer.use_pass_cryptomatte_object, layer.use_pass_cryptomatte_material)
    tree = bpy.data.node_groups.new(_NAMES[key], "CompositorNodeTree")
    try:
        incoming = tree.interface.new_socket(name="图像", in_out="INPUT", socket_type="NodeSocketColor")
        outgoing = tree.interface.new_socket(name="图像", in_out="OUTPUT", socket_type="NodeSocketColor")
        source = _node(tree, "NodeGroupInput", "输入画面", -940)
        result = _node(tree, "NodeGroupOutput", "合成画面", 700)
        color = c5.socket(source.outputs, incoming.identifier)
        if key == "LIGHT":
            output = _light_mix(tree, color, scene, layer, state)
        elif key == "GRADE":
            if state.matte_type == "OBJECT":
                layer.use_pass_cryptomatte_object = True
            else:
                layer.use_pass_cryptomatte_material = True
            output = _matte_grade(tree, color, scene, layer, state)
        elif key == "FOCUS":
            layer.use_pass_z = True
            output = _depth_focus(tree, color, scene, layer, state)
        elif key == "LINES":
            output = _line_composite(tree, color, state)
        else:
            output = _soft_light(tree, color, state, key == "HALO")
        _connect(tree, output, result, outgoing.identifier)
        for node in tree.nodes:
            apply_node_theme(node, DOMAIN)
        _attach(scene, _NAMES[key], tree, key)
        scene.render.use_compositing = True
    except Exception:
        layer.use_pass_z, layer.use_pass_cryptomatte_object, layer.use_pass_cryptomatte_material = old_passes
        if tree.users == 0:
            bpy.data.node_groups.remove(tree)
        raise


class CF_OT_read_light_groups(bpy.types.Operator):
    bl_idname = "compositing_workflow.read_light_groups"
    bl_label = "读取视图层光组"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        state = settings(context.scene)
        existing = {row.group_name: (row.gain, tuple(row.tint)) for row in state.light_groups}
        state.light_groups.clear()
        state.light_layer = context.view_layer.name
        for group in context.view_layer.lightgroups:
            row = state.light_groups.add()
            row.group_name = group.name
            if group.name in existing:
                row.gain, row.tint = existing[group.name]
        report(context.scene, f"已读取 {len(state.light_groups)} 个光组" if state.light_groups
               else "当前视图层没有光组，请在灯光工作台指派光组", "OK" if state.light_groups else "INFO")
        return {"FINISHED"}


class CF_OT_apply_effect(bpy.types.Operator):
    bl_idname = "compositing_workflow.apply_effect"
    bl_label = "应用合成效果"
    bl_options = {"REGISTER", "UNDO"}
    effect: EnumProperty(items=[(key, label, "") for key, label in _NAMES.items()])

    def execute(self, context):
        try:
            apply(context.scene, context.view_layer, settings(context.scene), self.effect)
            report(context.scene, f"已应用「{_NAMES[self.effect]}」")
            return {"FINISHED"}
        except (RuntimeError, ValueError, WorkflowError) as error:
            return fail(self, context, error)


class CF_OT_export(bpy.types.Operator):
    bl_idname = "compositing_workflow.export_frame"
    bl_label = "后台导出当前帧"
    bl_description = "以当前相机和合成效果栈渲染当前帧，当前工程保持可编辑"

    def execute(self, context):
        from .relight_preview_ui import _LAUNCHER

        scene = context.scene
        state = settings(scene)
        try:
            if RENDER.running or _LAUNCHER.running:
                raise ValueError("请等候当前合成后台任务或先取消")
            if scene.camera is None or not state.output_path:
                raise ValueError("请指定当前场景相机和新输出文件")
            output = Path(bpy.path.abspath(state.output_path)).resolve()
            output = output.with_suffix(".png" if state.output_format == "PNG" else ".exr")
            if not output.parent.is_dir() or output.exists():
                raise ValueError("请选择存在的输出目录和新文件名")
            work = Path(tempfile.mkdtemp(prefix="blender_comp_"))
            snapshot = work / "scene.blend"
            with ensure_object_mode(context):
                saved = bpy.ops.wm.save_as_mainfile(filepath=str(snapshot), copy=True, relative_remap=True)
            if "FINISHED" not in saved:
                raise ValueError("无法建立当前工程的后台快照")
            result = work / "result.json"
            worker = Path(__file__).with_name("workbench_render_worker.py")
            scene_name, render_format = scene.name, state.output_format

            def finish(ok, payload):
                current = bpy.data.scenes.get(scene_name)
                if current is not None:
                    message = f"已导出 {output.name}" if ok else f"导出未完成。{payload.get('error', '请查看作业日志')}"
                    report(current, message, "OK" if ok else "ERROR")

            RENDER.start([
                bpy.app.binary_path, "--background", "--factory-startup", "--offline-mode", str(snapshot),
                "--python-exit-code", "1", "--python", str(worker), "--",
                scene_name, str(scene.frame_current), render_format, str(output), str(result),
            ], label="导出合成静帧", status_file=work / "status.json", result_file=result, on_complete=finish)
            report(scene, "正在后台渲染合成静帧", "RUNNING")
            return {"FINISHED"}
        except (OSError, RuntimeError, ValueError) as error:
            return fail(self, context, error)


class CF_OT_cancel_render(bpy.types.Operator):
    bl_idname = "compositing_workflow.cancel_render"
    bl_label = "取消导出"

    def execute(self, context):
        if RENDER.cancel():
            report(context.scene, "已取消导出", "WARN")
        return {"FINISHED"}


def draw_finish(layout, context, *, launching=False):
    scene = context.scene
    state = settings(scene)
    _, body = section_panel(layout, "CF_light_mix", "光组后期混光", icon_value=ICONS.action_id("light_mix"),
                            icon="LIGHT", default_closed=True)
    if body is not None:
        body.operator("compositing_workflow.read_light_groups", icon="FILE_REFRESH")
        for group in state.light_groups:
            row = body.row(align=True)
            row.label(text=group.group_name)
            row.prop(group, "gain", text="")
            row.prop(group, "tint", text="")
        primary_button(body, "compositing_workflow.apply_effect", "应用光组混光",
                       icon_value=ICONS.action_id("light_mix"), enabled=bool(state.light_groups)).effect = "LIGHT"
    _, body = section_panel(layout, "CF_matte_grade", "Cryptomatte 与 CDL 调色",
                            icon_value=ICONS.action_id("crypto_cdl"), icon="COLOR", default_closed=True)
    if body is not None:
        body.row(align=True).prop(state, "matte_type", expand=True)
        col = prop_column(body, align=True)
        col.prop(state, "matte_object" if state.matte_type == "OBJECT" else "matte_material")
        for field in ("grade_offset", "grade_power", "grade_slope", "grade_contrast", "grade_saturation"):
            col.prop(state, field)
        primary_button(body, "compositing_workflow.apply_effect", "应用选区调色",
                       icon_value=ICONS.action_id("crypto_cdl"), enabled=selected_target(state) is not None).effect = "GRADE"
    _, body = section_panel(layout, "CF_scene_effects", "辉光、景深与三渲二线色",
                            icon_value=ICONS.action_id("scene_effects"), icon="NODE_COMPOSITING", default_closed=True)
    if body is not None:
        col = prop_column(body, align=True)
        col.prop(state, "bloom_strength")
        col.prop(state, "bloom_threshold")
        row = body.row(align=True)
        row.operator("compositing_workflow.apply_effect", text="应用辉光").effect = "BLOOM"
        row.operator("compositing_workflow.apply_effect", text="应用镜头光晕").effect = "HALO"
        col = prop_column(body, align=True)
        col.prop(state, "focus_fstop")
        col.prop(state, "focus_blur")
        body.operator("compositing_workflow.apply_effect", text="应用相机景深").effect = "FOCUS"
        col = prop_column(body, align=True)
        col.prop(state, "line_image")
        col.prop(state, "line_interaction")
        col.prop(state, "line_strength")
        body.operator("compositing_workflow.apply_effect", text="叠合线色通道").effect = "LINES"
        row = body.row(align=True)
        row.prop(state, "output_format", text="")
        row.prop(state, "output_path", text="")
        primary_button(body, "compositing_workflow.export_frame", "后台导出当前帧",
                       icon_value=ICONS.action_id("scene_effects"), enabled=not (RENDER.running or launching))


def draw_progress(layout):
    if RENDER.running:
        progress_card(layout, DOMAIN, ICONS, text=RENDER.detail, factor=RENDER.progress,
                      cancel_operator="compositing_workflow.cancel_render")


def draw_status(layout, context):
    state = settings(context.scene)
    status_banner(layout, context, state.last_report, kind=state.last_report_kind, icon_pack=ICONS)


CLASSES = (CF_LightGroup, CF_Settings, CF_OT_read_light_groups, CF_OT_apply_effect, CF_OT_export, CF_OT_cancel_render)
