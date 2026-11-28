"""Compact G·几何 workspace for native geometry and managed caches."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from uuid import uuid4

import bpy
from bpy.app.handlers import persistent
from bpy.props import EnumProperty, IntProperty, PointerProperty, StringProperty

from . import artist_recipes, cache_worker, core, session
from .contract import WorkflowError
from ._ui_kit import (
    BackgroundJobRunner, HUDManager, IconPack, SidebarDock,
    apply_collection_theme, apply_node_theme, draw_brand_header,
    ensure_object_mode, get_theme, primary_button, progress_card, prop_column,
    section_panel, status_banner,
)


DOMAIN = "geometry"
THEME = get_theme(DOMAIN)
_ICONS = IconPack(DOMAIN)
_RUNNER = BackgroundJobRunner(DOMAIN)
_WORKDIR = None
_JOB_SCENE = None
_UI_PROPERTY = "gnw_geometry_ui"

_RECIPES = (
    ("spiral_stair", "螺旋楼梯", "在游标处生成可调阶数与高度的螺旋楼梯"),
    ("curve_fence", "曲线护栏", "生成可跟随选定曲线的路径护栏"),
    ("surface_scatter", "草地散布", "生成带草簇与岩石的可调起伏地面"),
    ("simulation_translate", "位移模拟", "在当前网格上建立随帧推进的位移模拟"),
)
_TITLES = {key: title for key, title, _ in _RECIPES}
_TITLES.update(biome_scatter="生态散布", hair_curves="毛发曲线与卡片")
_LABELS = {
    "steps": "阶数", "height": "高度", "radius": "半径", "turn_angle": "旋转角",
    "column_radius": "立柱半径", "tread_thickness": "踏板厚度",
    "rail_height": "扶手高度", "rail_radius": "扶手半径", "post_radius": "护栏柱半径",
    "length": "路径长度", "amplitude": "路径起伏", "spacing": "间距",
    "path_object": "路径曲线", "use_external_path": "使用外部曲线", "size": "地块尺寸",
    "relief": "地表起伏", "density": "散布密度", "seed": "随机种子", "scale": "实例缩放",
    "max_slope": "最大坡角", "terrain": "散布表面", "species": "实例集合", "camera": "裁切相机",
    "terrain_material": "地形材质", "species_material": "实例材质", "include_terrain": "包含源表面",
    "camera_frustum": "限定相机画幅", "camera_distance": "限定相机距离", "distance_min": "最小间距",
    "density_max": "密度上限", "min_height": "最低高度", "max_height": "最高高度",
    "weight_attribute": "密度权重属性", "min_scale": "最小缩放", "max_scale": "最大缩放",
    "yaw_range": "随机转角范围", "max_camera_distance": "最大相机距离", "align_normals": "沿表面法线",
    "hair_surface": "毛发附着表面", "hair_material": "毛发材质", "hair_length": "毛发长度",
    "hair_density": "毛发密度", "hair_points": "曲线控制点", "hair_radius": "发丝半径",
    "hair_cards": "输出毛发卡片", "card_width": "卡片宽度", "speed": "向上速度",
}


def _settings(scene):
    return getattr(scene, _UI_PROPERTY)


def _report(scene, text, kind="INFO"):
    settings = _settings(scene)
    settings.last_report = text
    settings.last_report_kind = kind


def _failure(operator, context, message, error):
    _report(context.scene, f"{message}：{error}", "ERROR")
    operator.report({"ERROR"}, message)
    return {"CANCELLED"}


def _mesh_filter(_self, obj):
    return obj.type == "MESH"


def _curve_filter(_self, obj):
    return obj.type in {"CURVES", "CURVE"}


def _mesh_target(context, obj):
    if obj is None or obj.type != "MESH" or context.view_layer.objects.get(obj.name) != obj:
        raise ValueError("请先选择当前视图层中的网格物体")
    return obj


def _managed_modifiers(obj):
    if obj is None:
        return []
    return [mod for mod in obj.modifiers if mod.type == "NODES" and mod.node_group
            and mod.node_group.get(core.ROLE) == "wrapper"]


def _active_modifier(obj):
    mods = _managed_modifiers(obj)
    return next((mod for mod in mods if mod.is_active), mods[0] if mods else None)


def _unique_object_name(title):
    if bpy.data.objects.get(title) is None:
        return title
    index = 2
    while bpy.data.objects.get(f"{title} {index}") is not None:
        index += 1
    return f"{title} {index}"


def _place_generated(context, obj):
    root = context.scene.collection
    group = next((col for col in root.children if col.get("gnw_gui_collection")), None)
    if group is None or group.library or group.override_library or group.users > 1:
        group = bpy.data.collections.new("受管几何")
        group["gnw_gui_collection"] = True
        root.children.link(group)
    if obj.name not in group.objects:
        group.objects.link(obj)
    if context.view_layer.objects.get(obj.name) == obj:
        for old_group in tuple(obj.users_collection):
            if old_group != group:
                old_group.objects.unlink(obj)
    apply_collection_theme(group, DOMAIN)
    context.view_layer.update()


def _tint_tree(tree, visited):
    if tree in visited or tree.library or tree.users > 1:
        return
    visited.add(tree)
    for node in tree.nodes:
        apply_node_theme(node, DOMAIN)
        if child := getattr(node, "node_tree", None):
            _tint_tree(child, visited)


def _binding(obj, mod):
    return (core.tree_fingerprint(mod.node_group), core._parameter_state(mod),
            [(item.bake_id, item.bake_target, item.directory, item.frame_start, item.frame_end)
             for item in mod.bakes], mod.bake_target, mod.bake_directory)


def _release_job():
    global _WORKDIR, _JOB_SCENE
    if _WORKDIR is not None:
        _WORKDIR.cleanup()
        _WORKDIR = None
    _JOB_SCENE = None


@persistent
def _cancel_on_reload(_unused):
    _RUNNER.cleanup()
    _release_job()


class GNWSettings(bpy.types.PropertyGroup):
    recipe: EnumProperty(name="几何配方", items=_RECIPES)
    path: PointerProperty(name="路径曲线", type=bpy.types.Object, poll=_curve_filter)
    terrain: PointerProperty(name="散布表面", type=bpy.types.Object, poll=_mesh_filter)
    species: PointerProperty(name="实例集合", type=bpy.types.Collection)
    cache_directory: StringProperty(name="缓存输出位置", subtype="DIR_PATH")
    cache_start: IntProperty(name="起始帧", default=1, min=-1048574, max=1048574)
    cache_end: IntProperty(name="结束帧", default=120, min=-1048574, max=1048574)
    last_report: StringProperty()
    last_report_kind: StringProperty(default="INFO")


class GNW_OT_build_recipe(bpy.types.Operator):
    bl_idname = "gnw.build_recipe"
    bl_label = "创建几何配方"
    bl_description = "生成独立受管几何，并将可调输入保留在修改器中"
    bl_options = {"REGISTER", "UNDO"}
    recipe: StringProperty(options={"SKIP_SAVE"})

    def execute(self, context):
        settings = _settings(context.scene)
        recipe = self.recipe or settings.recipe
        try:
            title = _TITLES[recipe]
            params = {}
            target = {"action": "build", "recipe": recipe,
                      "id": f"{recipe}-{uuid4().hex[:12]}", "modifier": title}
            with ensure_object_mode(context):
                if recipe == "curve_fence" and settings.path:
                    params.update(use_external_path=True, path_object=core.plain(settings.path))
                elif recipe == "biome_scatter":
                    terrain = _mesh_target(context, settings.terrain or context.active_object)
                    if settings.species is None or not settings.species.all_objects:
                        raise ValueError("请选择包含实例物体的集合")
                    target["graph"] = artist_recipes.biome_graph()
                    params.update(terrain=core.plain(terrain), species=core.plain(settings.species))
                    if context.scene.camera:
                        params["camera"] = core.plain(context.scene.camera)
                elif recipe == "hair_curves":
                    surface = _mesh_target(context, context.active_object)
                    target["graph"] = artist_recipes.hair_graph(artist_recipes.load_hair_generator())
                    params["hair_surface"] = core.plain(surface)
                elif recipe == "simulation_translate":
                    mesh = _mesh_target(context, context.active_object)
                    target["graph"] = artist_recipes.simulation_graph()
                    target["object"] = mesh.name
                    next_index = 2
                    while mesh.modifiers.get(target["modifier"]):
                        target["modifier"] = f"{title} {next_index}"
                        next_index += 1

                if "object" not in target:
                    target.update(object=_unique_object_name(title), create=True)
                target["parameters"] = params
                result = core.apply_target(target)
                obj = bpy.data.objects[result["object"]]
                modifier = obj.modifiers[result["modifier"]]
                _tint_tree(modifier.node_group, set())
                if target.get("create"):
                    if recipe in {"spiral_stair", "surface_scatter"} or (recipe == "curve_fence" and not params):
                        obj.location = context.scene.cursor.location
                    _place_generated(context, obj)

            if context.mode == "OBJECT":
                for selected in context.selected_objects:
                    selected.select_set(False)
                obj.select_set(True)
                context.view_layer.objects.active = obj
            modifier.is_active = True
            _report(context.scene, f"已创建「{title}」", "OK")
            return {"FINISHED"}
        except Exception as error:
            return _failure(self, context, "无法创建几何配方", error)


class GNW_OT_choose_modifier(bpy.types.Operator):
    bl_idname = "gnw.choose_modifier"
    bl_label = "切换配方"
    bl_options = {"REGISTER", "UNDO"}
    modifier: StringProperty()

    def execute(self, context):
        for mod in _managed_modifiers(context.object):
            if mod.name == self.modifier:
                mod.is_active = True
                return {"FINISHED"}
        return {"CANCELLED"}


class GNW_OT_cache(bpy.types.Operator):
    bl_idname = "gnw.cache"
    bl_label = "处理模拟缓存"
    bl_options = {"REGISTER", "UNDO"}
    action: EnumProperty(items=(("bake", "烘焙", ""), ("export", "导出", ""), ("clear", "解除缓存", "")))

    def execute(self, context):
        global _WORKDIR, _JOB_SCENE
        scene = context.scene
        obj = context.object
        modifier = _active_modifier(obj)
        if _RUNNER.running:
            return _failure(self, context, "已有后台任务", "请等待完成或先取消")
        try:
            if modifier is None or not modifier.bakes or obj.library or obj.override_library:
                raise ValueError("请选择含模拟或烘焙节点的本地受管配方")
            if self.action == "clear":
                with ensure_object_mode(context):
                    token = core.stage_fresh_modifier(obj, modifier, modifier.node_group)
                    core.commit_fresh_modifier(token)
                    obj.update_tag()
                _report(scene, "已解除当前修改器的缓存绑定，磁盘缓存仍可复用", "OK")
                return {"FINISHED"}

            settings = _settings(scene)
            if not settings.cache_directory:
                raise ValueError("请选择缓存输出位置")
            base = Path(bpy.path.abspath(settings.cache_directory)).resolve()
            if not base.is_dir():
                raise ValueError("缓存输出位置必须是已存在的文件夹")
            if self.action == "bake" and settings.cache_start > settings.cache_end:
                raise ValueError("结束帧应大于或等于起始帧")

            rows = []
            for item in modifier.bakes:
                row = {"id": item.bake_id, "folder": f"node-{item.bake_id}",
                       "start": settings.cache_start, "end": settings.cache_end,
                       "simulation": item.node.bl_idname == "GeometryNodeSimulationOutput"}
                if self.action == "export":
                    source = Path(bpy.path.abspath(item.directory)).resolve() if item.use_custom_path and item.directory else None
                    if source is None or not (source / "meta").is_dir():
                        raise ValueError("当前节点没有可导出的磁盘缓存，请先烘焙")
                    row.update(source=str(source), start=item.frame_start, end=item.frame_end)
                rows.append(row)
            rows.sort(key=lambda row: not row["simulation"])
            root = Path(mkdtemp(prefix="GNW_", dir=base))
            request = {"action": self.action, "root": str(root), "bakes": rows,
                       "scene": scene.name, "view_layer": context.view_layer.name,
                       "object": obj.name, "modifier": modifier.name}
            request["binding"] = binding = _binding(obj, modifier)
            _WORKDIR = TemporaryDirectory(prefix="gnw_gui_", ignore_cleanup_errors=True)
            job = Path(_WORKDIR.name)
            req, status, result = (job / name for name in ("request.json", "status.json", "result.json"))
            cache_worker.write_json(req, request)
            command = [bpy.app.binary_path, "--background", "--factory-startup", "--offline-mode"]
            if self.action == "bake":
                snapshot = job / "geometry.blend"
                with ensure_object_mode(context):
                    if bpy.ops.wm.save_as_mainfile(filepath=str(snapshot), copy=True, check_existing=False) != {"FINISHED"}:
                        raise RuntimeError("无法保存烘焙快照")
                command.append(str(snapshot))
            command += ["--python-exit-code", "1", "--python", str(Path(cache_worker.__file__)),
                        "--", str(req), str(status), str(result)]
            scene_name, scene_uid = scene.name, scene.session_uid
            object_name, object_uid, modifier_name = obj.name, obj.session_uid, modifier.name
            _JOB_SCENE = scene

            def complete(ok, payload):
                try:
                    live = bpy.data.scenes.get(scene_name)
                    if live is None or live.session_uid != scene_uid:
                        return
                    if not ok:
                        error = payload.get("error") or payload.get("stderr_tail") or "后台进程未完成"
                        _report(live, f"缓存作业失败：{error}", "ERROR")
                    elif "archive" in payload:
                        _report(live, f"已导出：{payload['archive']}", "OK")
                    else:
                        target = live.objects.get(object_name)
                        current = target.modifiers.get(modifier_name) if target and target.session_uid == object_uid else None
                        if current is None or _binding(target, current) != binding:
                            _report(live, f"配方已经变化，缓存保存在 {root}", "WARN")
                        else:
                            with ensure_object_mode(bpy.context):
                                staged = core.stage_fresh_modifier(target, current, current.node_group)
                                try:
                                    cache_worker.bind_cache(staged["fresh"], root, rows)
                                    core.commit_fresh_modifier(staged)
                                except Exception:
                                    if staged["status"] == "staged":
                                        core.rollback_fresh_modifier(staged)
                                    raise
                            target.update_tag()
                            if bpy.context.scene == live and bpy.ops.ed.undo_push.poll():
                                bpy.ops.ed.undo_push(message="绑定几何节点缓存")
                            _report(live, f"已烘焙并绑定 {len(rows)} 个节点缓存", "OK")
                except Exception as error:
                    _report(live, f"缓存已保存，绑定失败：{error}", "WARN")
                finally:
                    _release_job()

            label = "几何模拟烘焙" if self.action == "bake" else "导出几何缓存"
            _RUNNER.start(command, label=label, status_file=status, result_file=result, on_complete=complete)
            _report(scene, f"正在后台执行{label}", "RUNNING")
            return {"FINISHED"}
        except Exception as error:
            _release_job()
            return _failure(self, context, "无法处理模拟缓存", error)


class GNW_OT_cancel_job(bpy.types.Operator):
    bl_idname = "gnw.cancel_job"
    bl_label = "取消后台任务"

    def execute(self, context):
        scene = _JOB_SCENE or context.scene
        if _RUNNER.cancel():
            _RUNNER.cleanup()
            _release_job()
            _report(scene, "已取消几何缓存任务", "WARN")
        return {"FINISHED"}


class GNW_OT_session_start(bpy.types.Operator):
    bl_idname = "gnw.session_start"
    bl_label = "启动现场会话"
    bl_description = "建立队列以接收外部工作流的现场交接"

    def execute(self, context):
        try:
            settings = context.window_manager
            session.start(settings.gnw_session_directory, settings.gnw_cli_runtime_directory or None)
            _report(context.scene, "现场会话已启动", "OK")
            return {"FINISHED"}
        except Exception as error:
            return _failure(self, context, "无法启动现场会话", error)


class GNW_OT_session_stop(bpy.types.Operator):
    bl_idname = "gnw.session_stop"
    bl_label = "停止现场会话"
    bl_description = "关闭队列并保留已生成的检查点和结果"

    def execute(self, context):
        session.stop()
        _report(context.scene, "现场会话已停止", "INFO")
        return {"FINISHED"}


class GNW_PT_workflow(bpy.types.Panel):
    bl_label = ""
    bl_idname = "GNW_PT_workflow"
    bl_space_type = THEME.editor_space
    bl_region_type = "UI"
    bl_category = THEME.tab_label
    bl_options = {"HEADER_LAYOUT_EXPAND"}

    def draw_header(self, context):
        draw_brand_header(self.layout, DOMAIN, _ICONS, fallback_icon="GEOMETRY_NODES")

    def draw(self, context):
        layout = self.layout
        settings = _settings(context.scene)
        obj = context.object
        modifier = _active_modifier(obj)
        mesh_active = bool(obj and obj.type == "MESH")

        if _RUNNER.running:
            progress_card(layout, DOMAIN, _ICONS, text=_RUNNER.detail, factor=_RUNNER.progress,
                          cancel_operator="gnw.cancel_job")

        create = prop_column(layout)
        create.prop(settings, "recipe", text="几何配方")
        if settings.recipe == "curve_fence":
            create.prop(settings, "path")
        primary_button(layout, "gnw.build_recipe", "创建配方", icon_value=_ICONS.action_id("node_params"),
                       icon="GEOMETRY_NODES", enabled=settings.recipe != "simulation_translate" or mesh_active)
        if modifier is not None:
            self._draw_inputs(layout, context, obj, modifier)

        _, scatter = section_panel(layout, "GNW_scatter", "表面散布与生态排布",
                                   icon_value=_ICONS.action_id("biome_scatter"), default_closed=True)
        if scatter is not None:
            source = prop_column(scatter)
            source.prop(settings, "terrain")
            source.prop(settings, "species")
            primary_button(scatter, "gnw.build_recipe", "创建生态散布",
                           icon_value=_ICONS.action_id("biome_scatter"),
                           enabled=bool((settings.terrain or mesh_active) and settings.species)).recipe = "biome_scatter"

        _, hair = section_panel(layout, "GNW_hair", "毛发曲线与卡片",
                                icon_value=_ICONS.action_id("hair_curves"), default_closed=True)
        if hair is not None:
            primary_button(hair, "gnw.build_recipe", "从所选网格生成毛发",
                           icon_value=_ICONS.action_id("hair_curves"), enabled=mesh_active).recipe = "hair_curves"

        _, cache = section_panel(layout, "GNW_cache", "模拟缓存与现场会话",
                                 icon_value=_ICONS.action_id("bake_cache"), default_closed=True)
        if cache is not None:
            self._draw_cache(cache, context, settings, modifier)

        status_banner(layout, context, settings.last_report, kind=settings.last_report_kind, icon_pack=_ICONS)

    def _draw_inputs(self, layout, context, obj, modifier):
        chosen = layout.row(align=True)
        icon = _ICONS.action_id("node_params")
        chosen.label(text=f"{obj.name} · {modifier.name}", icon_value=icon)
        mods = _managed_modifiers(obj)
        if len(mods) > 1:
            buttons = layout.grid_flow(row_major=True, columns=2, align=True)
            for mod in mods:
                buttons.operator("gnw.choose_modifier", text=mod.name,
                                 depress=mod == modifier).modifier = mod.name

        controls = prop_column(layout, align=True)
        controls.enabled = not (obj.library or obj.override_library)
        try:
            rows = core.interface_map(modifier.node_group)
        except (ValueError, WorkflowError):
            status_banner(layout, context, "公开节点接口已变更，请在节点编辑器中检查", kind="WARN", icon_pack=_ICONS)
            return
        for key, row in rows.items():
            if row["direction"] != "INPUT" or row["type"] == "NodeSocketGeometry":
                continue
            item = core._typed(modifier, row["identifier"])
            label = _LABELS.get(key, row["name"])
            if getattr(item, "type", "VALUE") == "ATTRIBUTE":
                controls.prop(item, "attribute_name", text=label)
            elif hasattr(item, "value"):
                if key == "weight_attribute":
                    terrain_row = rows.get("terrain")
                    terrain = core._typed(modifier, terrain_row["identifier"]).value if terrain_row else None
                    if terrain and terrain.type == "MESH":
                        controls.prop_search(item, "value", terrain.data, "attributes", text=label)
                        continue
                controls.prop(item, "value", text=label)

    def _draw_cache(self, layout, context, settings, modifier):
        if modifier is not None and modifier.bakes:
            cache = layout.column()
            cache.enabled = not _RUNNER.running
            cache.prop(settings, "cache_directory")
            frames = cache.row(align=True)
            frames.prop(settings, "cache_start")
            frames.prop(settings, "cache_end")
            primary_button(cache, "gnw.cache", "后台烘焙", icon_value=_ICONS.action_id("bake_cache")).action = "bake"
            row = cache.row(align=True)
            row.operator("gnw.cache", text="解除缓存", icon="UNLINKED").action = "clear"
            row.operator("gnw.cache", text="导出缓存", icon="EXPORT").action = "export"
        else:
            layout.label(text="选择含模拟或烘焙节点的受管配方", icon="INFO")

        layout.separator()
        info = session.status()
        if info["session"]:
            layout.operator("gnw.session_stop", icon="CANCEL")
        else:
            paths = prop_column(layout)
            paths.prop(context.window_manager, "gnw_cli_runtime_directory")
            paths.prop(context.window_manager, "gnw_session_directory")
            layout.operator("gnw.session_start", icon="PLAY")
        last = info["last"]
        if last.get("ok") is False:
            layout.label(text=str(last.get("error") or "交接失败")[:100], icon="ERROR")
        if last.get("candidate"):
            layout.label(text=Path(last["candidate"]).name, icon="FILE_BLEND")


_CLASSES = (
    GNWSettings, GNW_OT_build_recipe, GNW_OT_choose_modifier, GNW_OT_cache,
    GNW_OT_cancel_job, GNW_OT_session_start, GNW_OT_session_stop, GNW_PT_workflow,
)


def register():
    source_runtime = Path(__file__).resolve().parents[1] / "blenderctl"
    _ICONS.register()
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    setattr(bpy.types.Scene, _UI_PROPERTY, PointerProperty(type=GNWSettings))
    bpy.types.WindowManager.gnw_cli_runtime_directory = StringProperty(
        name="交接工具目录", subtype="DIR_PATH", default=str(source_runtime) if source_runtime.is_dir() else "")
    bpy.types.WindowManager.gnw_session_directory = StringProperty(name="新建队列目录", subtype="DIR_PATH")
    SidebarDock.register_plugin(DOMAIN, _ICONS.icons_dir)
    bpy.app.handlers.load_pre.append(_cancel_on_reload)


def unregister():
    bpy.app.handlers.load_pre.remove(_cancel_on_reload)
    _cancel_on_reload(None)
    SidebarDock.unregister_plugin(DOMAIN)
    HUDManager.clear(DOMAIN)
    _ICONS.unregister()
    del bpy.types.WindowManager.gnw_session_directory
    del bpy.types.WindowManager.gnw_cli_runtime_directory
    delattr(bpy.types.Scene, _UI_PROPERTY)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
