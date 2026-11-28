"""Owned orthographic control scene for external relight sessions (no Torch).

Entry: register(); setup(absolute_session_ready_json). IPC and bpy stay on
Blender's main thread. The external job owns all compute and cancellation.
"""
import copy
import importlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

import bpy
from mathutils import Quaternion, Vector
from bpy.props import StringProperty, EnumProperty, PointerProperty
from bpy.app.handlers import persistent

from . import workbench
from ._ui_kit import (
    BackgroundJobRunner, HUDManager, SidebarDock, apply_collection_theme, draw_brand_header, ensure_object_mode,
    get_theme, primary_button, progress_card, prop_column, section_panel, status_banner,
)

DOMAIN = "compositing"
THEME = get_theme(DOMAIN)
_ICONS = workbench.ICONS
_LAUNCHER = BackgroundJobRunner(DOMAIN)
_launch_directory = None

OWNER = "relight_preview_owner_v1"
_sessions = {}
_registered = False
LOCAL_V1 = "relight-local-edits-v1"
LOCAL_VERSION = "relight-local-edits-v2"
LOCAL_OPERATIONS = {"light": ("restrict", "attenuate"), "highlight": ("restrict", "attenuate"),
                    "line": ("add", "attenuate"), "depth": ("affine",),
                    "roughness": ("set",), "receiver": ("add", "attenuate"),
                    "caster": ("restrict", "attenuate")}
TARGET_LABELS = {"light": "漫反射", "highlight": "高光", "line": "线稿", "depth": "深度",
                 "roughness": "粗糙度", "receiver": "阴影接收", "caster": "阴影投射"}
OPERATION_LABELS = {"restrict": "限制范围", "attenuate": "减弱", "add": "增加区域",
                    "affine": "倍率与偏移", "set": "设置粗糙度"}
ANIME_VERSION = "relight-kernel-draft-0.4"
ANIME_VIEWS = ("RESULT", "PARA", "FORM", "DARK_SIDE", "RIM", "LINE", "DELTA", "DEPTH", "NORMAL", "VALIDITY")
ANIME_VIEW_LABELS = {"RESULT": "结果", "PARA": "二维光形", "FORM": "深度形体", "DARK_SIDE": "未受光影色",
                     "RIM": "内轮廓", "LINE": "线稿恢复", "DELTA": "总差分", "DEPTH": "深度",
                     "NORMAL": "法线", "VALIDITY": "有效域"}
ANIME_NUMBER_LIMITS = {
    "form": (0., 1.), "wrap": (0., 4.), "para_spread": (.05, 8.), "back_dark": (0., 1.),
    "back_span": (.001, 10.), "dark_side_reach": (0., 8.), "shadow_lo": (0., 1.), "shadow_hi": (0., 1.),
    "shadow_tint": (0., 1.), "cel": (0., 1.), "cel_steps": (2, 8), "cel_soft": (0., 1.),
    "rim_width": (0., 128.), "rim_edge0": (0., 1.), "rim_edge1": (0., 1.), "rim_gain": (0., 8.),
    "rim_white_mix": (0., 1.), "line_core_lo": (0., 1.), "line_core_hi": (0., 1.), "delta_gain": (.01, 100.),
}
ANIME_FIELD_LABELS = {
    "form": "深度形体混合", "wrap": "法线包裹", "para_spread": "二维光形范围", "back_dark": "逆光压暗",
    "back_span": "前后深度范围", "dark_side_reach": "影色形体范围", "shadow_lo": "影色阈值低",
    "shadow_hi": "影色阈值高", "shadow_tint": "独立影色强度", "cel": "Cel 光形量化",
    "cel_steps": "Cel 阶数", "cel_soft": "Cel 软边", "rim_width": "轮廓宽度（源像素）",
    "rim_edge0": "轮廓深度阈值低", "rim_edge1": "轮廓深度阈值高", "rim_gain": "轮廓强度",
    "rim_white_mix": "暗色轮廓白混合", "line_core_lo": "线芯阈值低", "line_core_hi": "线芯阈值高",
    "delta_gain": "差分诊断倍数",
}


def _native_continuous(value):
    """Continuous RNA controls need float IDProperties, even for JSON ints.

    JSON templates/original recipes remain untouched; _stable restores their
    exact numeric representation when a native control has not changed.
    """
    if isinstance(value, (list, tuple)):
        return [_native_continuous(element) for element in value]
    if type(value) not in (int, float):
        raise ValueError("连续参数必须是真实数值")
    return float(value)


def _empty_local_edits(version=LOCAL_VERSION):
    return {"version": version, "regions": []}


def _local_version(scene):
    # Saved v1 scenes have no explicit version property; never infer upgrade
    # from a persisted ready snapshot. Upgrade happens only on explicit bind.
    version = scene.get("rp_local_version")
    if version is None:
        version = json.loads(scene.get("rp_applied_local_edits", _json(_empty_local_edits(LOCAL_V1))))["version"]
    if version not in (LOCAL_V1, LOCAL_VERSION):
        raise ValueError("Unsupported saved local recipe version")
    return version


def _supports_local(ready):
    return ready.get("local_edits_version") in (LOCAL_V1, LOCAL_VERSION)


def _supports_material(ready):
    return ready.get("local_edits_version") == LOCAL_VERSION


def _upgrade_config(config):
    config = copy.deepcopy(config)
    if config["version"] == "relight-kernel-draft-0.1":
        config["version"] = "relight-kernel-draft-0.2"
        metric = config["frame"]["kind"] == "metric_perspective"
        config["projection"] = {"enabled": False, "point": [0., -1., -4.] if metric else [0., 0., 0.],
                                "normal": [0., 1., 0.] if metric else [0., 0., 1.],
                                "strength": 1., "transmission": [0., 0., 0.],
                                "light_ids": [light["id"] for light in config["lights"]]}
    if config["version"] == "relight-kernel-draft-0.2":
        config["version"] = "relight-kernel-draft-0.3"
        config["base"] = {"exposure": 0., "contrast": 1., "saturation": 1., "mask": "ALL"}
    return config


def _projection_properties(scene, config):
    if "projection" not in config:
        return
    projection = config["projection"]
    for field in ("enabled", "point", "normal", "strength", "transmission"):
        scene["rp_projection_"+field] = (bool(projection[field]) if field == "enabled"
                                         else _native_continuous(projection[field]))
    for field in ("point", "normal"):
        scene.id_properties_ui("rp_projection_"+field).update(description="控制空间 XYZ；平面为可编辑假设")
    scene.id_properties_ui("rp_projection_strength").update(min=0., max=1.)
    scene.id_properties_ui("rp_projection_transmission").update(subtype="COLOR", min=0., max=1.)
    for obj in scene.objects:
        if obj.get(OWNER) == scene.get(OWNER) and obj.get("relight_id"):
            obj["rp_projection_selected"] = obj["relight_id"] in projection["light_ids"]


def _anime_properties(scene, config):
    if config["version"] != ANIME_VERSION:
        return
    anime = config["anime"]
    for field, (lo, hi) in ANIME_NUMBER_LIMITS.items():
        key = "rp_anime_" + field
        scene[key] = int(anime[field]) if field == "cel_steps" else _native_continuous(anime[field])
        scene.id_properties_ui(key).update(min=lo, max=hi)
    scene["rp_anime_shadow_color"] = _native_continuous(anime["shadow_color"])
    scene.id_properties_ui("rp_anime_shadow_color").update(subtype="COLOR", min=0., max=1.)
    for field in ("solo_light_id", "subject_source", "view"):
        scene["rp_anime_" + field] = anime[field]
    for obj in scene.objects:
        if obj.get(OWNER) == scene.get(OWNER) and obj.get("relight_id"):
            obj["rp_anime_rim_selected"] = obj["relight_id"] in anime["rim_light_ids"]
    environment = config.setdefault("environment", {
        "subject_tint": [1., 1., 1.], "subject_exposure": 0.,
        "background_tint": [1., 1., 1.], "background_exposure": 0.})
    for region in ("subject", "background"):
        scene["rp_environment_" + region + "_tint"] = _native_continuous(environment[region + "_tint"])
        scene.id_properties_ui("rp_environment_" + region + "_tint").update(subtype="COLOR", min=0., max=4.)
        scene["rp_environment_" + region + "_exposure"] = _native_continuous(environment[region + "_exposure"])
        scene.id_properties_ui("rp_environment_" + region + "_exposure").update(min=-4., max=4.)


def _submit(runtime, config, *, kind="preview", reason="edit", local_edits=None):
    kwargs = {"kind": kind, "reason": reason}
    if _supports_local(runtime["client"].ready):
        recipe = local_edits or _empty_local_edits(runtime["client"].ready["local_edits_version"])
        if recipe["version"] == LOCAL_VERSION and not _supports_material(runtime["client"].ready):
            raise ValueError("当前会话不支持 v2 局部配方；绑定支持该功能的新会话")
        kwargs["local_edits"] = copy.deepcopy(recipe)
    elif local_edits and local_edits["regions"]:
        raise ValueError("当前热会话未提供局部编辑；绑定支持该功能的新会话")
    return runtime["client"].submit(config, **kwargs)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


# This is a source-root selection, never a path supplied by session-ready JSON.
_ADDON_ID = __package__
_CLIENT_MODULES = (
    "compositing_relight_session_client", "compositing_relight_session_contract",
    "compositing_relight_contract", "compositing_relight_kernel",
    "compositing_relight_local_edits", "compositing_depth_contract",
)


def _cli_selection():
    preferences = getattr(bpy.context, "preferences", None)
    addon = preferences.addons.get(_ADDON_ID) if preferences is not None else None
    configured = getattr(getattr(addon, "preferences", None), "cli_root", "").strip()
    if configured:
        return configured, "扩展设置"
    configured = os.environ.get("BLENDERCTL_ROOT", "").strip()
    if configured:
        return configured, "BLENDERCTL_ROOT"
    source = Path(__file__).resolve().parents[2]
    for root in (source, *Path(bpy.app.binary_path).resolve().parents):
        if (root / "tools/blenderctl/compositing_relight_session_client.py").is_file():
            return str(root), "本机 Blender 项目"
    return str(source), "源码兄弟目录"


def _cli_files():
    raw, selected_by = _cli_selection()
    root = Path(raw)
    if not root.is_absolute():
        raise ValueError(f"{selected_by}必须是配套 CLI 源码根的绝对路径（包含 tools/blenderctl）：{raw}")
    root = root.resolve()
    folder = root / "tools" / "blenderctl"
    modules = {name: (folder / (name + ".py")).resolve() for name in _CLIENT_MODULES}
    contract = (root / "tools/compositing_workflow_addon/depth_contract.py").resolve()
    missing = [str(path) for path in (*modules.values(), contract) if not path.is_file()]
    if missing:
        raise ValueError(
            f"{selected_by}的配套 CLI 源码不完整：{', '.join(missing)}。"
            "请在扩展设置选择包含 tools/blenderctl 的匹配源码根；扩展 ZIP 不包含计算运行环境。"
        )
    # The depth bridge resolves this source from its actual module location.
    if contract != (modules["compositing_depth_contract"].parent.parent /
                    "compositing_workflow_addon/depth_contract.py").resolve():
        raise ValueError("配套 CLI 与深度合同必须保持同一 tools 目录布局")
    return folder.resolve(), modules, contract


def _check_client_module(name, module, expected):
    actual = getattr(module, "__file__", None)
    if not actual or Path(actual).resolve() != expected:
        raise RuntimeError(
            f"配套 CLI 模块来源冲突：{name}；期望 {expected}，已加载 {actual!r}。"
            "请确认配套 CLI 根目录并重启 Blender；不会替换或删除已加载的全局模块。"
        )


def _check_client_sources(modules, contract, *, require_all=False):
    for name, expected in modules.items():
        if name in sys.modules:
            _check_client_module(name, sys.modules[name], expected)
        elif require_all:
            raise RuntimeError(f"配套 CLI 未加载必要模块：{name}")
    bridge = sys.modules.get("compositing_depth_contract")
    if bridge is not None:
        _check_client_module("compositing_depth_contract._module",
                             getattr(bridge, "_module", None), contract)


def _client(ready_file):
    # Only the stdlib client/contract closure is imported, never a GPU worker.
    folder, modules, contract = _cli_files()
    _check_client_sources(modules, contract)
    previous_path = list(sys.path)
    sys.path.insert(0, str(folder))
    try:
        importlib.invalidate_caches()
        for name, expected in modules.items():
            if name not in sys.modules:
                spec = importlib.util.find_spec(name)
                origin = getattr(spec, "origin", None)
                if not origin or Path(origin).resolve() != expected:
                    raise RuntimeError(f"配套 CLI 模块解析来源不符：{name}；期望 {expected}，实际 {origin!r}")
        client = importlib.import_module("compositing_relight_session_client")
        _check_client_sources(modules, contract, require_all=True)
    except ImportError as exc:
        raise RuntimeError(f"无法加载所选配套 CLI 客户端（{folder}）：{exc}") from exc
    finally:
        sys.path[:] = previous_path
    # Existing ready/parent/live/heartbeat validation stays in the real client.
    return client.SessionClient(ready_file)


class RP_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = _ADDON_ID

    cli_root: StringProperty(
        name="补光工具根目录",
        description="与 Blender 安装配套的项目目录，包含 tools/blenderctl",
        subtype="DIR_PATH", default="",
    )
    runtime_profile: StringProperty(name="本机 GPU 环境档案", subtype="FILE_PATH")

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "cli_root")
        layout.prop(self, "runtime_profile")
        layout.label(text="使用本机配套补光环境创建或绑定会话")


def _assert_main():
    import threading
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Relight UI must run on Blender main thread")


def frame_geometry(ready):
    """Image corners are pixel boundaries, pixel centers are (u+.5,v+.5)."""
    domain = ready.get("source_domain", ready["domain"])
    width, height = int(domain["width"]), int(domain["height"])
    if min(width, height) <= 0:
        raise ValueError("Positive source domain required")
    frame = ready["initial_config"]["frame"]
    if frame["kind"] == "screen_heightfield":
        return {"center": [0., 0., 0.], "width": width / height,
                "height": 1., "source_width": width, "source_height": height}
    if frame["kind"] != "metric_perspective":
        raise ValueError("Unsupported control frame")
    fx, fy = float(frame["fx"]), float(frame["fy"])
    if fx != fy:
        raise ValueError("Metric orthographic UI currently requires fx == fy")
    depth = float(ready["reference_depth"])
    if not math.isfinite(depth) or depth <= 0:
        raise ValueError("Positive finite metric reference_depth required")
    return {"center": [(width / 2 - frame["cx"]) * depth / fx,
                        (frame["cy"] - height / 2) * depth / fy, -depth],
            "width": width * depth / fx, "height": height * depth / fy,
            "source_width": width, "source_height": height}


def pixel_to_control(ready, u, v):
    g = frame_geometry(ready)
    return (g["center"][0] + (u / g["source_width"] - .5) * g["width"],
            g["center"][1] + (.5 - v / g["source_height"]) * g["height"],
            g["center"][2])


def _direction(obj):
    # Owned proxies prohibit parents/constraints. Local rotation avoids stale
    # evaluated matrix_world between native transform and depsgraph refresh.
    return list((obj.rotation_quaternion @ Vector((0., 0., -1.))).normalized())


def _stable(new, old):
    # RNA stores float32. Preserve exact untouched JSON fields, preventing an
    # artificial initial revision caused solely by native RNA round-tripping.
    if isinstance(new, (int, float)) and isinstance(old, (int, float)):
        return old if math.isclose(new, old, rel_tol=1e-6, abs_tol=2e-7) else new
    if isinstance(new, list) and isinstance(old, list) and len(new) == len(old):
        return [_stable(a, b) for a, b in zip(new, old)]
    return new


def _tag(block, owner):
    block[OWNER] = owner
    return block


def _new_light(scene, collection, owner, light, geometry):
    data = _tag(bpy.data.lights.new("Relight " + light["id"], light["type"]), owner)
    obj = _tag(bpy.data.objects.new("Relight " + light["id"], data), owner)
    collection.objects.link(obj)
    obj["relight_id"] = light["id"]
    obj["relight_type"] = light["type"]
    obj["relight_original"] = _json(light)
    obj.location = light.get("position", light.get("anchor"))
    obj.rotation_mode = "QUATERNION"
    if light["type"] == "SUN":
        direction = Vector(light["direction"])
    elif light["type"] in ("SPOT", "AREA"):
        direction = Vector(light["aim"]) - obj.location
        obj["rp_aim_distance"] = direction.length
    else:
        direction = Vector((0., 0., -1.))
    if direction.length <= 1e-10:
        raise ValueError("Zero light direction")
    obj.rotation_quaternion = direction.to_track_quat("-Z", "Y")
    obj.lock_scale = (True, True, True)
    obj.lock_location = (False, False, True)
    if light["type"] == "POINT":
        obj.lock_rotation = (True, True, True)
    obj["rp_height"] = float(obj.location.z)
    obj.id_properties_ui("rp_height").update(description="控制空间 Z；不改变图像平面", min=-10000., max=10000.)
    data.energy = 0.  # Proxy only: Watts never mean supplemental intensity.
    for field in ("intensity", "diffuse", "specular", "color", "samples", "softening", "reference_distance"):
        if field in light:
            obj["rp_" + field] = int(light[field]) if field == "samples" else _native_continuous(light[field])
    for field, maximum in (("intensity", 32.), ("diffuse", 8.), ("specular", 8.)):
        obj.id_properties_ui("rp_" + field).update(min=0., max=maximum)
    obj.id_properties_ui("rp_color").update(subtype="COLOR", min=0., max=16.)
    if light["type"] in ("POINT", "SPOT"):
        data.shadow_soft_size = light["radius"]
    if light["type"] == "SPOT":
        data.spot_size = math.radians(light["cone_deg"])
        data.spot_blend = light["blend"]
    if light["type"] == "AREA":
        data.shape, data.size, data.size_y = light["shape"], light["size"], light["size_y"]
    if light["type"] == "SUN":
        data.angle = math.radians(light["angle_deg"])
    return obj


def extract_config(scene):
    """Complete config; only owned proxy fields replace the saved template."""
    _assert_main()
    owner = scene.get(OWNER)
    if not owner:
        raise ValueError("Not an owned relight preview scene")
    config = json.loads(scene["rp_initial_config"])
    expected = {l["id"]: l for l in config["lights"]}
    found = {}
    for obj in scene.objects:
        if obj.get(OWNER) != owner or not obj.get("relight_id"):
            continue
        ident = obj["relight_id"]
        if ident not in expected or ident in found or obj.type != "LIGHT":
            raise ValueError("Owned light identity differs")
        light = copy.deepcopy(expected[ident])
        if obj.data.type != light["type"] or obj.parent or obj.constraints or obj.rotation_mode != "QUATERNION":
            raise ValueError("Changing proxy type/parent/constraints is unsupported")
        if obj.data.get(OWNER) != owner or obj.data.users != 1:
            raise ValueError("Proxy Light datablock is no longer independently owned")
        if any(abs(s - 1.) > 1e-6 for s in obj.scale):
            raise ValueError("Proxy scale must be one; edit native light size")
        # Height property is editable despite native Z transform lock.
        obj.location.z = float(obj["rp_height"])
        for field in ("intensity", "diffuse", "specular", "color", "samples", "softening", "reference_distance"):
            if field in light:
                value = obj["rp_" + field]
                light[field] = _stable(list(value) if field == "color" else value, light[field])
        location = list(obj.location)
        if light["type"] == "SUN":
            light["anchor"] = _stable(location, light["anchor"])
            d = _direction(obj)
            old = Vector(light["direction"]).normalized()
            if Vector(d).angle(old) > 1e-6:
                light["direction"] = d
            light["angle_deg"] = _stable(math.degrees(obj.data.angle), light["angle_deg"])
        else:
            light["position"] = _stable(location, light["position"])
        if light["type"] in ("POINT", "SPOT"):
            light["radius"] = _stable(obj.data.shadow_soft_size, light["radius"])
        if light["type"] in ("SPOT", "AREA"):
            aim = Vector(location) + Vector(_direction(obj)) * float(obj["rp_aim_distance"])
            light["aim"] = _stable(list(aim), light["aim"])
        if light["type"] == "SPOT":
            light["cone_deg"] = _stable(math.degrees(obj.data.spot_size), light["cone_deg"])
            light["blend"] = _stable(obj.data.spot_blend, light["blend"])
        if light["type"] == "AREA":
            light["shape"] = obj.data.shape
            light["size"] = _stable(obj.data.size, light["size"])
            light["size_y"] = (_stable(obj.data.size_y, light["size_y"])
                               if obj.data.shape in ("RECTANGLE", "ELLIPSE") else light["size"])
        found[ident] = light
    if set(found) != set(expected):
        raise ValueError("An owned proxy was deleted; retaining previous preview")
    config["lights"] = [found[l["id"]] for l in config["lights"]]
    config["mix"] = _stable(float(scene["rp_mix"]), config["mix"])
    config["bypass"] = bool(scene["rp_bypass"])
    config["line_protect"] = _stable(float(scene["rp_line_protect"]), config["line_protect"])
    if "rp_roughness" in scene:
        config["material"]["roughness"] = _stable(float(scene["rp_roughness"]), config["material"]["roughness"])
    if "rp_f0" in scene:
        config["material"]["f0"] = _stable(list(scene["rp_f0"]), config["material"]["f0"])
    if "base" in config:
        for field in ("exposure", "contrast", "saturation"):
            config["base"][field] = _stable(float(scene["rp_base_"+field]), config["base"][field])
        config["base"]["mask"] = "LIGHT" if bool(scene["rp_base_use_light_mask"]) else "ALL"
    if "projection" in config:
        projection = config["projection"]
        for field in ("enabled", "point", "normal", "strength", "transmission"):
            value = scene["rp_projection_"+field]
            if field in ("point", "normal", "transmission"):
                value = list(value)
            projection[field] = _stable(value, projection[field])
        selected = {ident for ident in found if bool(next(o for o in scene.objects
                    if o.get(OWNER) == owner and o.get("relight_id") == ident).get("rp_projection_selected", False))}
        # Retain exact saved light ordering until the selection actually changes.
        if selected != set(projection["light_ids"]):
            projection["light_ids"] = [light["id"] for light in config["lights"] if light["id"] in selected]
    if config["version"] == ANIME_VERSION:
        anime = config["anime"]
        for field in ANIME_NUMBER_LIMITS:
            value = scene["rp_anime_" + field]
            value = int(value) if field == "cel_steps" else float(value)
            anime[field] = _stable(value, anime[field])
        anime["shadow_color"] = _stable(list(scene["rp_anime_shadow_color"]), anime["shadow_color"])
        for field in ("solo_light_id", "subject_source", "view"):
            anime[field] = scene["rp_anime_" + field]
        selected = {ident for ident in found if bool(next(o for o in scene.objects
                    if o.get(OWNER) == owner and o.get("relight_id") == ident).get("rp_anime_rim_selected", False))}
        if selected != set(anime["rim_light_ids"]):
            anime["rim_light_ids"] = [light["id"] for light in config["lights"] if light["id"] in selected]
        environment = config.setdefault("environment", {})
        for region in ("subject", "background"):
            environment[region + "_tint"] = _stable(list(scene["rp_environment_" + region + "_tint"]), environment.get(region + "_tint", [1., 1., 1.]))
            environment[region + "_exposure"] = _stable(float(scene["rp_environment_" + region + "_exposure"]), environment.get(region + "_exposure", 0.))
    return config


def extract_local_edits(scene):
    """Pending primitive recipe in original full image coordinates; no arrays."""
    _assert_main()
    owner = scene.get(OWNER)
    if not owner:
        raise ValueError("Not an owned relight preview scene")
    geometry = json.loads(scene["rp_geometry"])
    version = _local_version(scene)
    ordered = json.loads(scene.get("rp_region_order", "[]"))
    applied_rows = {r["id"]: r for r in json.loads(scene.get("rp_applied_local_edits", _json(_empty_local_edits())))["regions"]}
    if type(ordered) is not list or len(ordered) > 8 or len(set(ordered)) != len(ordered):
        raise ValueError("Invalid local region order")
    found = {}
    for obj in scene.objects:
        if obj.get(OWNER) != owner or not obj.get("rp_region_id"):
            continue
        ident = obj["rp_region_id"]
        if ident not in ordered or ident in found or obj.type not in ("EMPTY", "MESH"):
            raise ValueError("Owned local region identity differs")
        if obj.type == "MESH":
            if obj.data.get(OWNER) != owner or obj.data.users != 1 or obj.modifiers:
                raise ValueError("Local outline Mesh is no longer independently owned")
            vertices, edges = _region_geometry(obj["rp_shape"])
            mesh = obj.data
            if len(mesh.vertices) != len(vertices) or len(mesh.edges) != len(edges) or mesh.polygons:
                raise ValueError("Local outline topology differs")
            if any((v.co-Vector(expected)).length > 1e-6 for v, expected in zip(mesh.vertices, vertices)) or [tuple(e.vertices) for e in mesh.edges] != edges:
                raise ValueError("Local outline geometry differs; use object G/S/R")
        if obj.parent or obj.constraints or obj.rotation_mode != "XYZ" or len(obj.users_collection) != 1 or obj.users != 1:
            raise ValueError("Local region parent/constraints/shared identity is unsupported")
        collection = obj.users_collection[0]
        if collection.get(OWNER) != owner or collection.users != 1 or collection not in list(scene.collection.children):
            raise ValueError("Local region collection is no longer independently owned")
        for field in ("feather", "strength", "scale", "offset"):
            if type(obj["rp_"+field]) not in (int, float):
                raise ValueError("Local numeric properties must be real numbers, not bool/text")
        if abs(obj.location.z-geometry["center"][2]) > 1e-6 or abs(obj.rotation_euler.x) > 1e-6 or abs(obj.rotation_euler.y) > 1e-6 or abs(obj.scale.z-1.) > 1e-6:
            raise ValueError("Local region must stay in its image plane")
        if any(not math.isfinite(float(x)) or x <= 0 for x in obj.scale[:2]):
            raise ValueError("Positive finite local region scale required")
        region = {"id": ident, "enabled": obj["rp_enabled"], "target": obj["rp_target"],
                  "operation": obj["rp_operation"], "shape": obj["rp_shape"],
                  "center": [.5+(obj.location.x-geometry["center"][0])/geometry["width"],
                             .5-(obj.location.y-geometry["center"][1])/geometry["height"]],
                  "size": [2*obj.scale.x/geometry["width"], 2*obj.scale.y/geometry["height"]],
                  "rotation": -float(obj.rotation_euler.z),
                  "feather": float(obj["rp_feather"]), "strength": float(obj["rp_strength"]),
                  "scale": float(obj["rp_scale"]), "offset": float(obj["rp_offset"])}
        allowed = LOCAL_OPERATIONS if version == LOCAL_VERSION else {k: LOCAL_OPERATIONS[k] for k in ("light", "highlight", "line", "depth")}
        if region["target"] == "roughness":
            if type(obj.get("rp_value")) not in (int, float) or not .02 <= obj["rp_value"] <= 1.:
                raise ValueError("局部粗糙度必须是 .02 到 1 的真实数值")
            region["value"] = float(obj["rp_value"])
        if type(region["enabled"]) is not bool or region["target"] not in allowed or region["operation"] not in allowed[region["target"]] or region["shape"] not in ("ellipse", "box"):
            raise ValueError("Local region target/operation/type differs")
        expected_display = "CIRCLE" if region["shape"] == "ellipse" else "CUBE"
        if obj.type == "EMPTY" and (obj.empty_display_type != expected_display or abs(obj.empty_display_size-1.) > 1e-6):
            raise ValueError("Local region display geometry differs")
        values = region["center"]+region["size"]+[region[k] for k in ("rotation", "feather", "strength", "scale", "offset")]
        if not all(math.isfinite(v) for v in values) or not 0 <= region["feather"] <= 1 or not 0 <= region["strength"] <= 1 or not .01 <= region["scale"] <= 10 or not -10 <= region["offset"] <= 10:
            raise ValueError("Local region finite/range constraint differs")
        stabilized = _stable_region(region, json.loads(obj["rp_region_original"]))
        # Match the last applied exact snapshot too: RNA float32 arithmetic on
        # G/Undo can return a value a few ulps from its prior transformed state.
        found[ident] = _stable_region(stabilized, applied_rows.get(ident, {}))
    if set(found) != set(ordered):
        raise ValueError("Local region was deleted; use the owned remove operator")
    return {"version": version, "regions": [found[ident] for ident in ordered]}


def _stable_region(region, original):
    return {k: _stable(v, original.get(k)) for k, v in region.items()}


def _region_geometry(shape):
    # Blender native Empty CIRCLE is an XZ ring. Explicit XY wire geometry
    # keeps object G/S/R and full-image recipe axes identical in orthographic UI.
    if shape == "ellipse":
        vertices = [(math.cos(i*2*math.pi/64), math.sin(i*2*math.pi/64), 0.) for i in range(64)]
    elif shape == "box":
        vertices = [(-1., -1., 0.), (1., -1., 0.), (1., 1., 0.), (-1., 1., 0.)]
    else:
        raise ValueError("Invalid local outline shape")
    return vertices, [(i, (i+1) % len(vertices)) for i in range(len(vertices))]


def add_region(scene, target="depth", operation=None, shape="ellipse"):
    """Create only an independently owned pending outline; explicit Apply commits."""
    _assert_main()
    owner = scene.get(OWNER)
    ready = json.loads(scene["rp_ready_snapshot"])
    if not owner or not _supports_local(ready):
        raise ValueError("需要支持局部编辑的独立预览会话")
    recipe = extract_local_edits(scene)
    if len(recipe["regions"]) >= 8:
        raise ValueError("局部区域最多 8 个")
    default = {target: operations[0] for target, operations in LOCAL_OPERATIONS.items()}
    if target not in default or shape not in ("ellipse", "box"):
        raise ValueError("Invalid local target/shape")
    if target in ("roughness", "receiver", "caster") and (not _supports_material(ready) or recipe["version"] != LOCAL_VERSION):
        raise ValueError("新材质与投影区域需要显式绑定 v2 热会话")
    operation = operation or default[target]
    if operation not in LOCAL_OPERATIONS[target]:
        raise ValueError("Invalid local target/operation")
    region = {"id": uuid.uuid4().hex, "enabled": True, "target": target, "operation": operation,
              "shape": shape, "center": [.5, .5], "size": [.3, .3], "rotation": 0.,
              "feather": .2, "strength": 1., "scale": 1., "offset": 0.}
    if target == "roughness":
        region["value"] = float(scene.get("rp_roughness", .5))
    geometry = json.loads(scene["rp_geometry"])
    collection = next(c for c in scene.collection.children if c.get(OWNER) == owner)
    name = "Relight Local " + target + " " + region["id"][:8]
    mesh = _tag(bpy.data.meshes.new(name + " XY Outline"), owner)
    vertices, edges = _region_geometry(shape)
    mesh.from_pydata(vertices, edges, [])
    mesh.update()
    obj = _tag(bpy.data.objects.new(name, mesh), owner)
    collection.objects.link(obj)
    obj["rp_region_id"] = region["id"]
    obj["rp_region_original"] = _json(region)
    for field in ("enabled", "target", "operation", "shape", "feather", "strength", "scale", "offset"):
        obj["rp_"+field] = (_native_continuous(region[field]) if field in ("feather", "strength", "scale", "offset")
                            else bool(region[field]) if field == "enabled" else region[field])
    if target == "roughness":
        obj["rp_value"] = _native_continuous(region["value"])
        obj.id_properties_ui("rp_value").update(min=.02, max=1., description="本区域设置的高光粗糙度")
    obj.location = geometry["center"]
    obj.rotation_mode = "XYZ"
    obj.scale = (.15*geometry["width"], .15*geometry["height"], 1.)
    obj.display_type = "WIRE"
    obj.show_wire = True
    obj.show_all_edges = True
    obj.show_in_front = True
    obj.lock_location = (False, False, True)
    obj.lock_rotation = (True, True, False)
    obj.lock_scale = (False, False, True)
    for field, low, high in (("feather", 0., 1.), ("strength", 0., 1.), ("scale", .01, 10.), ("offset", -10., 10.)):
        obj.id_properties_ui("rp_"+field).update(min=low, max=high)
    ordered = [r["id"] for r in recipe["regions"]]+[region["id"]]
    scene["rp_region_order"] = _json(ordered)
    scene["rp_pending_local_edits"] = _json(extract_local_edits(scene))
    scene["rp_local_status"] = "局部轮廓待应用；G/S/R 修改后点击应用"
    return obj


def remove_region(scene, obj):
    _assert_main()
    owner = scene.get(OWNER)
    if obj.get(OWNER) != owner or not obj.get("rp_region_id") or obj.type not in ("EMPTY", "MESH"):
        raise ValueError("Only an owned local region can be removed")
    recipe = extract_local_edits(scene)
    ident = obj["rp_region_id"]
    mesh = obj.data if obj.type == "MESH" else None
    scene["rp_region_order"] = _json([r["id"] for r in recipe["regions"] if r["id"] != ident])
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and mesh.get(OWNER) == owner and mesh.users == 0:
        bpy.data.meshes.remove(mesh)
    pending = extract_local_edits(scene)
    scene["rp_pending_local_edits"] = _json(pending)
    applied = json.loads(scene.get("rp_applied_local_edits", _json(_empty_local_edits(_local_version(scene)))))
    scene["rp_local_status"] = ("轮廓与已提交配方一致" if pending == applied else "局部轮廓待应用")


def apply_local_edits(scene):
    _assert_main()
    runtime = _sessions.get(scene.get(OWNER))
    if not runtime:
        raise ValueError("先绑定正在运行的热会话")
    recipe, config = extract_local_edits(scene), extract_config(scene)
    ticket = _submit(runtime, config, reason="apply_local_edits", local_edits=recipe)
    runtime["latest"] = copy.deepcopy(ticket)
    runtime["last_config"] = copy.deepcopy(config)
    runtime["applied_local_edits"] = copy.deepcopy(recipe)
    scene["rp_applied_local_edits"] = _json(recipe)
    scene["rp_pending_local_edits"] = _json(recipe)
    scene["rp_current_config"] = _json(config)
    scene["rp_applied_config"] = _json(config)
    scene["rp_latest_revision"] = ticket["revision"]
    scene["rp_local_status"] = "局部应用已提交；等待读回验证"
    _event(runtime, "submitted_local", **ticket)
    return ticket


def _descriptor_path(descriptor):
    if isinstance(descriptor, str):
        return descriptor
    return descriptor.get("file", descriptor.get("path"))


def _load_image(scene, descriptor):
    owner = scene[OWNER]
    background = next(o for o in scene.objects if o.get(OWNER) == owner and o.get("rp_background"))
    path = _descriptor_path(descriptor)
    if not path or not Path(path).is_absolute() or not Path(path).is_file():
        raise ValueError("Selected display requires an existing absolute path")
    image = bpy.data.images.load(path, check_existing=False)
    _tag(image, owner)
    image.colorspace_settings.name = "sRGB"
    background.data = image
    background.color = (1., 1., 1., 1.)
    # Immutable per-revision file: no reload of a shared latest.png.
    previous = scene.get("rp_image_name")
    scene["rp_image_name"] = image.name
    if previous:
        old = bpy.data.images.get(previous)
        if old and old.get(OWNER) == owner and old.users == 0:
            bpy.data.images.remove(old)


def _refresh_display(scene, manifest=None):
    manifest = manifest or json.loads(scene["rp_shown_manifest"])
    if scene.get("rp_display_mode", "RESULT") == "ORIGINAL":
        baseline = json.loads(scene.get("rp_baseline_manifest", "null"))
        if not baseline:
            raise ValueError("当前会话没有可对照的 Original 基线")
        descriptor, displayed = baseline["display"], "ORIGINAL"
    else:
        view = manifest.get("view", "RESULT")
        descriptor = manifest.get("view_display") if view != "RESULT" else manifest["display"]
        if descriptor is None:
            descriptor, displayed = manifest["display"], "RESULT"
        else:
            displayed = view
    _load_image(scene, descriptor)
    scene["rp_displayed_view"] = displayed


def _show(scene, manifest):
    _refresh_display(scene, manifest)
    scene["rp_shown_manifest"] = _json(manifest)
    scene["rp_shown_maps_summary"] = _json({"derived_cache_id": manifest.get("derived_cache_id"),
                                           "generation": manifest.get("generation")})
    scene["rp_shown_revision"] = int(manifest["revision"])
    scene["rp_shown_at_ns"] = str(time.time_ns())


def _event(runtime, kind, **fields):
    runtime["events"].append({"event": kind, "time_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns(), **fields})
    if len(runtime["events"]) > 4096:
        runtime["events"].pop(0)


def tick(owner):
    """One nonblocking timer step; callable explicitly by an acceptance probe."""
    _assert_main()
    runtime = _sessions.get(owner)
    if not runtime:
        return None
    scene = next((s for s in bpy.data.scenes if s.get(OWNER) == owner), None)
    if not scene:
        stop(owner)
        return None
    if runtime.get("window_pointer") is not None and not any(
            w.as_pointer() == runtime["window_pointer"] and w.scene == scene
            for w in bpy.context.window_manager.windows):
        stop(owner)
        return None
    try:
        config = extract_config(scene)
        pending = extract_local_edits(scene)
        applied = runtime.get("applied_local_edits", _empty_local_edits())
        scene["rp_pending_local_edits"] = _json(pending)
        if pending != applied:
            scene["rp_local_status"] = "局部轮廓有未应用修改；调灯继续使用已应用区域"
        if config != runtime["last_config"]:
            ticket = _submit(runtime, config, kind="preview", reason="proxy_edit_or_restore", local_edits=applied)
            runtime["latest"] = copy.deepcopy(ticket)
            runtime["last_config"] = copy.deepcopy(config)
            scene["rp_latest_revision"] = int(ticket["revision"])
            scene["rp_current_config"] = _json(config)
            scene["rp_applied_config"] = _json(config)
            scene["rp_status"] = "计算中；继续显示上一张通过结果"
            _event(runtime, "submitted", **ticket)
        manifest = runtime["client"].poll()
        if manifest:
            latest, ready = runtime["latest"], runtime["client"].ready
            identity = manifest.get("request", manifest)
            valid = latest and all(identity.get(k) == latest.get(k) for k in ("revision", "parameters_sha256"))
            valid = valid and all(identity.get(k) == ready.get(k) for k in ("session_id", "incarnation_id", "cache_id"))
            manifest = dict(manifest, revision=identity.get("revision"), parameters_sha256=identity.get("parameters_sha256"))
            if valid and manifest["revision"] != scene.get("rp_shown_revision", -1):
                _show(scene, manifest)
                _event(runtime, "image_loaded", revision=manifest["revision"], parameters_sha256=manifest["parameters_sha256"], timings=manifest.get("timings"))
                scene["rp_status"] = "已显示通过结果；图像加载时间不等于屏幕可见时间"
                scene["rp_shown_local_edits"] = _json(applied)
                scene["rp_shown_config"] = _json(runtime["last_config"])
                if pending == applied:
                    scene["rp_local_status"] = "已显示应用结果；局部准备与调灯预览分别计时"
            elif not valid:
                _event(runtime, "rejected_stale", revision=manifest.get("revision"))
        # The public Host/client decides geometry validity. Failed revisions
        # retain the last accepted image and remain editable for recovery.
        last_reason = getattr(runtime["client"], "last_reason", "")
        if last_reason and ("reject" in str(last_reason).lower() or "projection" in str(last_reason).lower()):
            scene["rp_status"] = "本次修订被拒绝；保留上一张通过图像，请修正平面位置/法线或参数：" + str(last_reason)[:150]
        for window in bpy.context.window_manager.windows:
            if window.scene == scene:
                for area in window.screen.areas:
                    if area.type == "VIEW_3D":
                        region = area.spaces.active.region_3d
                        region.view_perspective = "ORTHO"
                        region.view_rotation = Quaternion((1., 0., 0., 0.))
                    area.tag_redraw()
    except Exception as exc:
        if isinstance(exc, RuntimeError) and str(exc) == "Session is not running":
            # The public client reports a terminal/mismatched parent here.
            # End this timer instead of rereading the dead session at 20 Hz.
            _event(runtime, "session_unavailable", message=str(exc))
            stop(owner)
            scene["rp_status"] = "会话连接不可用；绑定新会话继续，保留最后图像与灯参数"
            return None
        if getattr(runtime["client"], "last_reason", "") == "rejected":
            scene["rp_status"] = f"本次修订被拒绝；保留上一张通过图像，请修正平面或参数：{exc}"
        else:
            scene["rp_status"] = f"{type(exc).__name__}: {exc}"
        _event(runtime, "error", message=scene["rp_status"])
    return .05


def _configure_preview_window(window, scene):
    geometry = json.loads(scene["rp_geometry"])
    area = max(window.screen.areas, key=lambda a: a.width * a.height)
    area.type = "VIEW_3D"
    space = area.spaces.active
    space.shading.type = "SOLID"
    space.overlay.show_floor = False
    space.overlay.show_axis_x = False
    space.overlay.show_axis_y = False
    space.show_region_ui = True
    space.region_3d.view_perspective = "ORTHO"
    space.region_3d.view_rotation = Quaternion((1., 0., 0., 0.))
    space.region_3d.view_location = geometry["center"]
    space.region_3d.view_distance = max(geometry["width"], geometry["height"]) * 1.5
    space.region_3d.view_camera_zoom = 0.
    area.tag_redraw()


def open_preview_window(scene):
    _assert_main()
    if bpy.app.background:
        return None
    before = {w.as_pointer() for w in bpy.context.window_manager.windows}
    bpy.ops.wm.window_new()
    window = next(w for w in bpy.context.window_manager.windows if w.as_pointer() not in before)
    window.scene = scene
    _configure_preview_window(window, scene)
    scene["rp_window_created"] = True
    runtime = _sessions[scene[OWNER]]
    runtime["window_pointer"] = window.as_pointer()
    return window


def setup(ready_file, owned_scene_name=None, *, client_factory=None, open_window=True):
    """Create fresh owned scene; never replace/reuse a named user datablock."""
    _assert_main()
    if not _registered:
        register()
    if not Path(ready_file).is_absolute():
        raise ValueError("Absolute ready file path required")
    client = (client_factory or _client)(str(ready_file))
    ready = client.ready
    geometry = frame_geometry(ready)
    config = (_upgrade_config(ready["initial_config"]) if _supports_material(ready)
              else copy.deepcopy(ready["initial_config"]))
    local_version = LOCAL_VERSION if _supports_material(ready) else LOCAL_V1
    owner = uuid.uuid4().hex
    if owned_scene_name and bpy.data.scenes.get(owned_scene_name):
        raise ValueError("Scene name already exists; no user scene may be adopted")
    scene = _tag(bpy.data.scenes.new(owned_scene_name or "Relight Preview " + owner[:8]), owner)
    collection = _tag(bpy.data.collections.new("Relight Controls " + owner[:8]), owner)
    scene.collection.children.link(collection)
    apply_collection_theme(collection, DOMAIN)
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"
    scene.view_settings.exposure = 0.
    scene.view_settings.gamma = 1.
    scene.display_settings.display_device = "sRGB"
    scene["rp_ready_file"] = str(ready_file)
    scene["rp_initial_config"] = _json(config)
    scene["rp_current_config"] = _json(config)
    scene["rp_applied_config"] = _json(config)
    scene["rp_geometry"] = _json(geometry)
    scene["rp_ready_snapshot"] = _json(ready)
    scene["rp_status"] = "初始化；结果尚未显示"
    scene["rp_shown_revision"] = -1
    scene["rp_display_mode"] = "RESULT"
    scene["rp_displayed_view"] = "RESULT"
    scene["rp_region_order"] = "[]"
    scene["rp_local_version"] = local_version
    scene["rp_applied_local_edits"] = _json(_empty_local_edits(local_version))
    scene["rp_pending_local_edits"] = _json(_empty_local_edits(local_version))
    scene["rp_local_status"] = "未设置局部区域"
    scene["rp_save_path"] = str(Path(ready_file).parent / ("relight-preview-" + owner[:8] + ".blend"))
    for field in ("mix", "bypass", "line_protect"):
        scene["rp_" + field] = bool(config[field]) if field == "bypass" else _native_continuous(config[field])
    for field in ("mix", "line_protect"):
        scene.id_properties_ui("rp_" + field).update(min=0., max=1.)
    scene["rp_roughness"] = _native_continuous(config["material"]["roughness"])
    scene["rp_f0"] = _native_continuous(config["material"]["f0"])
    scene.id_properties_ui("rp_roughness").update(min=.02, max=1.)
    scene.id_properties_ui("rp_f0").update(subtype="COLOR", min=0., max=1.)
    if "base" in config:
        for field in ("exposure", "contrast", "saturation"):
            scene["rp_base_"+field] = _native_continuous(config["base"][field])
        scene["rp_base_use_light_mask"] = config["base"]["mask"] == "LIGHT"
        scene.id_properties_ui("rp_base_exposure").update(min=-4., max=4.)
        scene.id_properties_ui("rp_base_contrast").update(min=.25, max=2.)
        scene.id_properties_ui("rp_base_saturation").update(min=0., max=2.)
    for light in config["lights"]:
        _new_light(scene, collection, owner, light, geometry)
    _projection_properties(scene, config)
    _anime_properties(scene, config)
    background = _tag(bpy.data.objects.new("Relight Image " + owner[:8], None), owner)
    collection.objects.link(background)
    background["rp_background"] = True
    background.empty_display_type = "IMAGE"
    background.empty_display_size = max(geometry["width"], geometry["height"])
    background.empty_image_depth = "BACK"
    background.empty_image_side = "DOUBLE_SIDED"
    background.location = geometry["center"]
    background.hide_select = True
    background.lock_location = (True, True, True)
    background.lock_rotation = (True, True, True)
    background.lock_scale = (True, True, True)
    text = _tag(bpy.data.texts.new("Relight Session " + owner[:8]), owner)
    text.write(_json({"ready_file": str(ready_file), "ready": ready, "initial_config": config,
                      "reopen_policy": "No auto-bind; explicitly create a new external session and bind it."}))
    runtime = {"client": client, "last_config": extract_config(scene), "latest": None, "events": [], "timer": None,
               "applied_local_edits": _empty_local_edits(local_version)}
    _sessions[owner] = runtime
    if ready.get("baseline_display"):
        baseline = {"display": ready["baseline_display"], "revision": -1,
                    "baseline": True, "session_id": ready["session_id"], "view": "RESULT"}
        scene["rp_baseline_manifest"] = _json(baseline)
        _show(scene, baseline)
    ticket = _submit(runtime, runtime["last_config"], kind="preview", reason="setup", local_edits=_empty_local_edits(local_version))
    runtime["latest"] = copy.deepcopy(ticket)
    scene["rp_latest_revision"] = ticket["revision"]
    _event(runtime, "submitted", **ticket)
    callback = lambda: tick(owner)
    runtime["timer"] = callback
    bpy.app.timers.register(callback, first_interval=.05, persistent=False)
    if open_window:
        open_preview_window(scene)
    return scene


def bind(scene, ready_file, *, client_factory=None):
    """Explicitly rebind saved proxies to a new alive session with same frame."""
    _assert_main()
    config = extract_config(scene)
    client = (client_factory or _client)(str(ready_file))
    if config["frame"] != client.ready["initial_config"]["frame"] or json.loads(scene["rp_geometry"]) != frame_geometry(client.ready):
        raise ValueError("New session frame/domain differs; create fresh preview")
    applied = json.loads(scene.get("rp_applied_local_edits", _json(_empty_local_edits())))
    extract_local_edits(scene)  # Validate pending independently; don't apply it.
    if applied["regions"] and not _supports_local(client.ready):
        raise ValueError("新会话不支持已保存的局部配方")
    if applied["version"] == LOCAL_VERSION and not _supports_material(client.ready):
        raise ValueError("v2 配方不能绑定旧 v1 会话；新建兼容预览或绑定 v2 会话")
    owner = scene[OWNER]
    old_pointer = _sessions.get(owner, {}).get("window_pointer")
    window = next((w for w in bpy.context.window_manager.windows if w.as_pointer() == old_pointer and w.scene == scene), None)
    if window is None and bpy.context.window and bpy.context.window.scene == scene:
        window = bpy.context.window
    if not bpy.app.background and window is None:
        raise ValueError("在目标预览窗口绑定新会话")
    stop(owner)
    if _supports_material(client.ready):
        config = _upgrade_config(config)
        applied["version"] = LOCAL_VERSION
        scene["rp_local_version"] = LOCAL_VERSION
        scene["rp_initial_config"] = _json(config)
        _projection_properties(scene, config)
    _anime_properties(scene, config)
    scene["rp_applied_local_edits"] = _json(applied)
    scene["rp_pending_local_edits"] = _json(extract_local_edits(scene))
    scene["rp_current_config"] = _json(config)
    scene["rp_applied_config"] = _json(config)
    scene["rp_ready_file"] = str(ready_file)
    scene["rp_ready_snapshot"] = _json(client.ready)
    if client.ready.get("baseline_display"):
        baseline = {"display": client.ready["baseline_display"], "revision": -1,
                    "baseline": True, "session_id": client.ready["session_id"], "view": "RESULT"}
        scene["rp_baseline_manifest"] = _json(baseline)
    elif "rp_baseline_manifest" in scene:
        del scene["rp_baseline_manifest"]
    scene["rp_display_mode"] = "RESULT"
    scene["rp_shown_revision"] = -1
    runtime = {"client": client, "last_config": copy.deepcopy(config), "latest": None, "events": [], "timer": None,
               "applied_local_edits": copy.deepcopy(applied)}
    ticket = _submit(runtime, config, kind="preview", reason="explicit_rebind", local_edits=applied)
    runtime["latest"] = copy.deepcopy(ticket)
    scene["rp_latest_revision"] = ticket["revision"]
    _sessions[owner] = runtime
    if window is not None:
        runtime["window_pointer"] = window.as_pointer()
    _event(runtime, "submitted", **ticket)
    callback = lambda: tick(owner)
    runtime["timer"] = callback
    bpy.app.timers.register(callback, first_interval=.05, persistent=False)
    return scene


def telemetry(scene):
    runtime = _sessions.get(scene.get(OWNER))
    return copy.deepcopy(runtime["events"]) if runtime else json.loads(scene.get("rp_event_log", "[]"))


def request_final(scene):
    """Request original-domain output through the same guarded hot session."""
    _assert_main()
    runtime = _sessions.get(scene.get(OWNER))
    if not runtime:
        raise ValueError("先绑定正在运行的热会话")
    config = extract_config(scene)
    applied = runtime.get("applied_local_edits", _empty_local_edits())
    if extract_local_edits(scene) != applied:
        raise ValueError("局部区域有未应用修改；先应用局部编辑，再输出原尺寸")
    if config["version"] == ANIME_VERSION:
        config["anime"]["view"] = "RESULT"
        scene["rp_anime_view"] = "RESULT"
    scene["rp_display_mode"] = "RESULT"
    ticket = _submit(runtime, config, kind="final", reason="user_final", local_edits=applied)
    runtime["latest"] = copy.deepcopy(ticket)
    runtime["last_config"] = copy.deepcopy(config)
    scene["rp_latest_revision"] = ticket["revision"]
    scene["rp_current_config"] = _json(config)
    scene["rp_applied_config"] = _json(config)
    scene["rp_status"] = "计算原尺寸结果；继续显示上一张通过图像"
    _event(runtime, "submitted_final", **ticket)
    return ticket


def stop(owner):
    _assert_main()
    runtime = _sessions.pop(owner, None)
    if runtime:
        _event(runtime, "stopped")
        callback = runtime["timer"]
        if callback and bpy.app.timers.is_registered(callback):
            bpy.app.timers.unregister(callback)
        try:
            runtime["client"].stop()
            _event(runtime, "stop_requested")
        except Exception as exc:
            # An already-terminal parent cannot accept a new stop message.
            # Local timer/Scene cleanup still completes and preserves error.
            _event(runtime, "stop_request_failed", message=f"{type(exc).__name__}: {exc}")
    for scene in bpy.data.scenes:
        if scene.get(OWNER) == owner:
            if runtime:
                scene["rp_event_log"] = _json(runtime["events"])
            scene["rp_status"] = "会话已停止；保留最后图像和代理"


@persistent
def _load_pre(_):
    _cancel_launch()
    _LAUNCHER.cleanup()
    workbench.RENDER.cleanup()
    for owner in list(_sessions):
        stop(owner)


@persistent
def _save_pre(_):
    for scene in bpy.data.scenes:
        if scene.get(OWNER):
            scene["rp_current_config"] = _json(extract_config(scene))
            scene["rp_pending_local_edits"] = _json(extract_local_edits(scene))
            text = next((t for t in bpy.data.texts if t.get(OWNER) == scene[OWNER]), None)
            if text:
                text.clear()
                text.write(_json({"ready_file": scene["rp_ready_file"], "initial_config": json.loads(scene["rp_initial_config"]),
                                  "current_config": json.loads(scene["rp_current_config"]), "telemetry": telemetry(scene),
                                  "applied_config": json.loads(scene.get("rp_applied_config", scene["rp_current_config"])),
                                  "shown_config": json.loads(scene.get("rp_shown_config", "null")),
                                  "applied_local_edits": json.loads(scene.get("rp_applied_local_edits", _json(_empty_local_edits()))),
                                  "pending_local_edits": json.loads(scene["rp_pending_local_edits"]),
                                  "shown_local_edits": json.loads(scene.get("rp_shown_local_edits", "null")),
                                  "shown_manifest": json.loads(scene.get("rp_shown_manifest", "null")),
                                  "shown_maps_summary": json.loads(scene.get("rp_shown_maps_summary", "null")),
                                  "reopen_policy": "Explicitly start and bind a new external session."}))


class RP_OT_start(bpy.types.Operator):
    bl_idname = "relight_preview.start"
    bl_label = "建立独立拖灯预览"
    ready_file: StringProperty(name="已就绪的补光会话", subtype="FILE_PATH")

    def execute(self, context):
        try:
            setup(bpy.path.abspath(self.ready_file))
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class RP_OT_bind(bpy.types.Operator):
    bl_idname = "relight_preview.bind"
    bl_label = "绑定新热会话"
    ready_file: StringProperty(name="新补光会话", subtype="FILE_PATH")

    def execute(self, context):
        try:
            bind(context.scene, bpy.path.abspath(self.ready_file))
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


def _input_path(raw, label):
    if not raw:
        raise ValueError(f"请选择{label}")
    result = Path(bpy.path.abspath(raw)).resolve()
    if not result.is_file():
        raise ValueError(f"找不到{label}")
    return str(result)


def _default_profile(folder):
    addon = bpy.context.preferences.addons.get(_ADDON_ID)
    selected = getattr(getattr(addon, "preferences", None), "runtime_profile", "")
    if selected:
        return _input_path(selected, "扩展设置中的 GPU 环境档案")
    profile = Path(os.environ.get("BLENDER_RELIGHT_PROFILE") or str(folder.parents[1] / "work/relight-profile/PROFILE.json"))
    if not profile.is_file():
        raise ValueError("请在扩展设置选择已配置的 GPU 环境档案")
    return str(profile)


def _cancel_launch_job(directory):
    (directory / "cancel").touch(exist_ok=True)
    active = directory / "active.json"
    try:
        path = Path(json.loads(active.read_text(encoding="utf-8"))["directory"]).resolve()
        if path.parent == (directory / "j").resolve():
            (path / "cancel.request").touch(exist_ok=True)
    except (OSError, ValueError, KeyError):
        # The worker also watches the cancellation marker if its job receipt is not yet readable.
        pass


def _cancel_launch():
    if _launch_directory and _LAUNCHER.running:
        _cancel_launch_job(_launch_directory)


class RP_OT_launch(bpy.types.Operator):
    bl_idname = "relight_preview.launch"
    bl_label = "启动实时深度补光"

    def execute(self, context):
        global _launch_directory
        scene = context.scene
        state = workbench.settings(scene)
        try:
            if _LAUNCHER.running or workbench.RENDER.running:
                raise ValueError("请等候当前合成后台任务或先取消")
            folder, _, _ = _cli_files()
            profile = _default_profile(folder)
            blend = Path(bpy.app.binary_path)
            version = ".".join(str(value) for value in bpy.app.version[:2])
            python = blend.parent / version / "python/bin/python.exe"
            if not python.is_file():
                raise ValueError("当前 Blender 缺少配套运算 Python")
            raw = {}
            if state.depth_input == "BUNDLE":
                bundle = _input_path(state.depth_bundle, "深度包")
            else:
                bundle = ""
                raw = {
                    "rgb": _input_path(state.relight_rgb, "原图 PNG"),
                    "depth": _input_path(state.relight_depth, "标量深度图"),
                    "depth_channel": state.depth_channel.strip(), "depth_strength": state.depth_strength,
                    "near_order": state.near_order,
                }
            _launch_directory = directory = Path(tempfile.mkdtemp(prefix="cfr_"))
            request = directory / "request.json"
            request.write_text(_json({
                "tools": str(folder), "runtime_profile": profile, "blender": str(blend),
                "bundle": bundle, "mode": state.relight_mode, **raw,
            }), encoding="utf-8")
            scene_name, scene_pointer = scene.name, scene.as_pointer()
            window_pointer = context.window.as_pointer() if context.window else None
            mode = state.relight_mode

            def completed(ok, payload):
                target = bpy.data.scenes.get(scene_name)
                window = next((w for w in bpy.context.window_manager.windows if w.as_pointer() == window_pointer), None)
                if ((directory / "cancel").exists() or target is None or target.as_pointer() != scene_pointer
                        or (window is None and not bpy.app.background)):
                    _cancel_launch_job(directory)
                    return
                if not ok:
                    workbench.report(target, "补光未能启动。" + payload.get("error", "后台作业失败"), "ERROR")
                    return
                try:
                    with bpy.context.temp_override(window=window) if window else bpy.context.temp_override():
                        preview = setup(payload["ready"], open_window=not bpy.app.background)
                    preview_state = workbench.settings(preview)
                    preview_state.depth_input = "BUNDLE"
                    preview_state.depth_bundle = payload["bundle"]
                    preview_state.relight_mode = mode
                    workbench.report(target, "已打开交互补光窗口")
                except Exception as error:
                    Path(payload["ready"]).with_name("cancel.request").touch(exist_ok=True)
                    workbench.report(target, "补光窗口未能打开。" + str(error), "ERROR")

            _LAUNCHER.start(
                [str(python), "-B", str(Path(__file__).with_name("relight_launch_worker.py")), str(request)],
                label="启动实时深度补光", status_file=directory / "status.json",
                result_file=directory / "result.json", on_complete=completed,
            )
            workbench.report(scene, "正在准备深度补光", "RUNNING")
            return {"FINISHED"}
        except (OSError, RuntimeError, ValueError) as error:
            return workbench.fail(self, context, error)


class RP_OT_cancel_launch(bpy.types.Operator):
    bl_idname = "relight_preview.cancel_launch"
    bl_label = "取消补光启动"

    def execute(self, context):
        _cancel_launch()
        workbench.report(context.scene, "已请求取消补光启动", "WARN")
        return {"FINISHED"}


class RP_OT_stop(bpy.types.Operator):
    bl_idname = "relight_preview.stop"
    bl_label = "停止热会话"

    def execute(self, context):
        stop(context.scene.get(OWNER))
        return {"FINISHED"}


class RP_OT_final(bpy.types.Operator):
    bl_idname = "relight_preview.final"
    bl_label = "输出原尺寸结果"

    def execute(self, context):
        try:
            request_final(context.scene)
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class RP_OT_save(bpy.types.Operator):
    bl_idname = "relight_preview.save"
    bl_label = "保存工程副本"

    def execute(self, context):
        path = Path(context.scene["rp_save_path"])
        if not path.is_absolute() or path.exists():
            self.report({"ERROR"}, "需要新的绝对路径；不覆盖已有工程")
            return {"CANCELLED"}
        bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True)
        return {"FINISHED"}


class RP_OT_add_region(bpy.types.Operator):
    bl_idname = "relight_preview.add_region"
    bl_label = "添加局部区域"
    bl_options = {"REGISTER", "UNDO"}
    target: EnumProperty(items=[("depth", "深度修正", ""), ("light", "漫反射范围", ""),
                                ("highlight", "高光范围", ""), ("line", "线稿保护", ""),
                                ("roughness", "局部粗糙度", ""), ("receiver", "阴影接收区域", ""),
                                ("caster", "阴影投射区域", "")], default="depth")
    shape: EnumProperty(items=[("ellipse", "椭圆", ""), ("box", "矩形", "")], default="ellipse")

    def execute(self, context):
        try:
            select_new = context.mode == "OBJECT"
            with ensure_object_mode(context):
                obj = add_region(context.scene, self.target, shape=self.shape)
                if select_new:
                    for selected in context.selected_objects:
                        selected.select_set(False)
                    obj.select_set(True)
                    context.view_layer.objects.active = obj
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class RP_OT_region_action(bpy.types.Operator):
    bl_idname = "relight_preview.region_action"
    bl_label = "修改局部区域"
    bl_options = {"REGISTER", "UNDO"}
    action: EnumProperty(items=[("toggle", "启用/禁用", ""), ("remove", "删除区域", ""),
                               ("operation", "切换影响方式", "")], default="toggle")

    def execute(self, context):
        try:
            scene, obj = context.scene, context.active_object
            if not obj or obj.get(OWNER) != scene.get(OWNER) or not obj.get("rp_region_id"):
                raise ValueError("选择本预览的局部区域")
            extract_local_edits(scene)
            if self.action == "remove":
                remove_region(scene, obj)
            elif self.action == "toggle":
                obj["rp_enabled"] = not obj["rp_enabled"]
            else:
                alternatives = LOCAL_OPERATIONS[obj["rp_target"]]
                obj["rp_operation"] = alternatives[(alternatives.index(obj["rp_operation"])+1) % len(alternatives)]
            scene["rp_pending_local_edits"] = _json(extract_local_edits(scene))
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class RP_OT_apply_local(bpy.types.Operator):
    bl_idname = "relight_preview.apply_local"
    bl_label = "应用局部编辑"

    def execute(self, context):
        try:
            apply_local_edits(context.scene)
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class RP_OT_display_mode(bpy.types.Operator):
    bl_idname = "relight_preview.display_mode"
    bl_label = "切换原图或结果"
    mode: StringProperty(default="RESULT")

    def execute(self, context):
        try:
            if self.mode not in ("ORIGINAL", "RESULT"):
                raise ValueError("未知显示对照模式")
            if self.mode == "ORIGINAL" and "rp_baseline_manifest" not in context.scene:
                raise ValueError("当前会话没有原图可供对照")
            context.scene["rp_display_mode"] = self.mode
            _refresh_display(context.scene)
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class RP_OT_anime_mode(bpy.types.Operator):
    bl_idname = "relight_preview.anime_mode"
    bl_label = "切换动画补光视图或独显光源"
    field: StringProperty(default="view")
    value: StringProperty(default="RESULT")

    def execute(self, context):
        try:
            scene = context.scene
            config = json.loads(scene["rp_initial_config"])
            if config["version"] != ANIME_VERSION:
                raise ValueError("请选择动漫补光会话")
            if self.field == "view":
                if self.value not in ANIME_VIEWS:
                    raise ValueError("未知诊断视图")
                scene["rp_display_mode"] = "RESULT"
            elif self.field == "subject_source":
                if self.value not in ("LIGHT_MASK", "DEPTH_VALID", "ALPHA"):
                    raise ValueError("未知主体来源")
            elif self.field == "solo_light_id":
                ids = {light["id"] for light in config["lights"]}
                if self.value and self.value not in ids:
                    raise ValueError("独显的补光灯不存在")
            else:
                raise ValueError("动漫补光不支持此控件")
            scene["rp_anime_" + self.field] = self.value
            scene.update_tag()
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class RP_PT_panel(bpy.types.Panel):
    bl_label = ""
    bl_space_type = THEME.editor_space
    bl_region_type = "UI"
    bl_category = THEME.tab_label
    bl_options = {"HEADER_LAYOUT_EXPAND"}

    def draw_header(self, context):
        draw_brand_header(self.layout, DOMAIN, _ICONS, fallback_icon="NODE_COMPOSITING")

    def draw(self, context):
        layout, scene = self.layout, context.scene
        workbench.draw_progress(layout)
        if _LAUNCHER.running:
            progress_card(layout, DOMAIN, _ICONS, text=_LAUNCHER.detail, factor=_LAUNCHER.progress,
                          cancel_operator="relight_preview.cancel_launch")
        owner = scene.get(OWNER)
        if owner:
            data = json.loads(scene["rp_initial_config"])
            if scene.get("rp_baseline_manifest"):
                row = layout.row(align=True)
                for mode, title in (("ORIGINAL", "原图"), ("RESULT", "补光结果")):
                    operator = row.operator("relight_preview.display_mode", text=title,
                                            depress=scene.get("rp_display_mode", "RESULT") == mode)
                    operator.mode = mode
            row = layout.row(align=True)
            row.prop(scene, '["rp_mix"]', text="补光混合")
            row.prop(scene, '["rp_bypass"]', text="旁路", toggle=True)
            if data["version"] == ANIME_VERSION:
                row = layout.row(align=True)
                for view in ("RESULT", "FORM", "RIM"):
                    operator = row.operator("relight_preview.anime_mode", text=ANIME_VIEW_LABELS[view],
                                            depress=scene.get("rp_anime_view", "RESULT") == view)
                    operator.field, operator.value = "view", view
            row = layout.row(align=True)
            row.operator("relight_preview.bind", text="绑定新会话", icon="LINKED")
            if owner in _sessions:
                row.operator("relight_preview.final", text="原尺寸输出", icon="RENDER_STILL")
                row.operator("relight_preview.stop", text="停止", icon="CANCEL")
            row = layout.row(align=True)
            row.prop(scene, '["rp_save_path"]', text="")
            row.operator("relight_preview.save", text="保存副本", icon="FILE_TICK")
            if data["version"] == ANIME_VERSION or scene.get("rp_ready_snapshot"):
                _, advanced = section_panel(
                    layout, "CF_relight_controls", "光形、区域与补光诊断",
                    icon_value=_ICONS.action_id("relight_preview"), default_closed=True,
                )
                if advanced is not None:
                    self._draw_relight_controls(advanced, context)
        else:
            state = workbench.settings(scene)
            layout.row(align=True).prop(state, "depth_input", expand=True)
            fields = prop_column(layout, align=True)
            if state.depth_input == "BUNDLE":
                fields.prop(state, "depth_bundle")
            else:
                fields.prop(state, "relight_rgb")
                fields.prop(state, "relight_depth")
                if state.relight_depth.lower().endswith(".exr"):
                    fields.prop(state, "depth_channel")
                fields.prop(state, "near_order")
                fields.prop(state, "depth_strength")
            layout.row(align=True).prop(state, "relight_mode", expand=True)
            primary_button(
                layout, "relight_preview.launch", "启动实时深度补光",
                icon_value=_ICONS.action_id("relight_preview"), icon="PLAY",
                enabled=not (_LAUNCHER.running or workbench.RENDER.running),
            )
            layout.operator("relight_preview.start", text="打开已有会话", icon="LINKED")
        workbench.draw_finish(layout, context, launching=_LAUNCHER.running)
        if owner:
            status_banner(layout, context, scene.get("rp_status", ""), icon_pack=_ICONS)
        workbench.draw_status(layout, context)

    def _draw_relight_controls(self, layout, context):
        scene = context.scene
        owner = scene[OWNER]
        layout.label(text="G：画面移动；R：发光方向；高度：Z")
        layout.prop(scene, '["rp_line_protect"]', text="线稿保护")
        initial_config = json.loads(scene["rp_initial_config"])
        if initial_config["version"] == ANIME_VERSION:
            box = layout.box()
            box.label(text="动漫光形与诊断")
            views = box.grid_flow(columns=3, align=True)
            for view in ANIME_VIEWS:
                op = views.operator("relight_preview.anime_mode", text=ANIME_VIEW_LABELS[view],
                                    depress=scene.get("rp_anime_view", "RESULT") == view)
                op.field, op.value = "view", view
            if scene.get("rp_anime_view") == "DELTA":
                box.prop(scene, '["rp_anime_delta_gain"]', text="差分放大倍数（仅诊断）")
            box.label(text="独显光源与轮廓灯")
            for light in scene.objects:
                if light.get(OWNER) != owner or not light.get("relight_id"):
                    continue
                ident = light["relight_id"]
                row = box.row(align=True)
                row.label(text=ident)
                solo = scene.get("rp_anime_solo_light_id", "") == ident
                op = row.operator("relight_preview.anime_mode", text="取消独显" if solo else "独显")
                op.field, op.value = "solo_light_id", "" if solo else ident
                row.prop(light, '["rp_anime_rim_selected"]', text="轮廓")
            row = box.row(align=True)
            row.label(text="主体来源")
            for source, label in (("LIGHT_MASK", "艺术区域"), ("DEPTH_VALID", "深度有效域"), ("ALPHA", "Alpha")):
                op = row.operator("relight_preview.anime_mode", text=label,
                                  depress=scene.get("rp_anime_subject_source") == source)
                op.field, op.value = "subject_source", source
            environment = box.box()
            environment.label(text="环境光变化（保留原图已有光照）")
            environment.prop(scene, '["rp_environment_subject_tint"]', text="主体环境色（乘算）")
            environment.prop(scene, '["rp_environment_subject_exposure"]', text="主体环境曝光 EV")
            environment.prop(scene, '["rp_environment_background_tint"]', text="背景环境色（乘算）")
            environment.prop(scene, '["rp_environment_background_exposure"]', text="背景环境曝光 EV")
            environment.label(text="灯光强度设为 0 时仅调整环境")
            shape = box.box()
            shape.label(text="二维光形、深度形体与独立影色")
            for field in ("form", "wrap", "para_spread", "back_dark", "back_span", "dark_side_reach",
                          "shadow_lo", "shadow_hi", "shadow_tint"):
                shape.prop(scene, '["rp_anime_' + field + '"]', text=ANIME_FIELD_LABELS[field])
            shape.prop(scene, '["rp_anime_shadow_color"]', text="未受光影色（sRGB 选择）")
            for field in ("cel", "cel_steps", "cel_soft"):
                shape.prop(scene, '["rp_anime_' + field + '"]', text=ANIME_FIELD_LABELS[field])
            rim = box.box()
            rim.label(text="深度内轮廓与线稿恢复")
            for field in ("rim_width", "rim_edge0", "rim_edge1", "rim_gain", "rim_white_mix",
                          "line_core_lo", "line_core_hi"):
                rim.prop(scene, '["rp_anime_' + field + '"]', text=ANIME_FIELD_LABELS[field])
        if "rp_roughness" in scene:
            layout.prop(scene, '["rp_roughness"]', text="全局高光粗糙度")
            layout.prop(scene, '["rp_f0"]', text="全局 F0")
        if "base" in json.loads(scene["rp_initial_config"]):
            box = layout.box()
            box.label(text="基础影调")
            box.prop(scene, '["rp_base_exposure"]', text="基础曝光 EV")
            box.prop(scene, '["rp_base_contrast"]', text="基础对比度")
            box.prop(scene, '["rp_base_saturation"]', text="基础饱和度")
            box.prop(scene, '["rp_base_use_light_mask"]', text="仅灯光遮罩区域")
        if "projection" in json.loads(scene["rp_initial_config"]):
            box = layout.box()
            box.label(text="受控平面阴影")
            box.prop(scene, '["rp_projection_enabled"]', text="启用平面投影")
            for field, label in (("point", "平面位置"), ("normal", "平面法线"),
                                 ("strength", "阴影强度"), ("transmission", "透射色")):
                box.prop(scene, '["rp_projection_'+field+'"]', text=label)
            box.label(text="选择灯")
            for light in scene.objects:
                if light.get(OWNER) == owner and light.get("relight_id"):
                    box.prop(light, '["rp_projection_selected"]', text=light["relight_id"])
        if _supports_local(json.loads(scene["rp_ready_snapshot"])):
            box = layout.box()
            box.label(text="局部：G 移动 / S 大小 / R 平面旋转")
            targets = ["depth", "light", "highlight", "line"]
            if _local_version(scene) == LOCAL_VERSION and _supports_material(json.loads(scene["rp_ready_snapshot"])):
                targets += ["roughness", "receiver", "caster"]
            for target in targets:
                label = TARGET_LABELS[target]
                row = box.row(align=True)
                for shape, shape_label in (("ellipse", "椭圆"), ("box", "矩形")):
                    op = row.operator("relight_preview.add_region", text=label+shape_label)
                    op.target, op.shape = target, shape
            box.operator("relight_preview.apply_local")
            box.label(text=str(scene.get("rp_local_status", ""))[:95])
            box.label(text="轮廓移动后需应用；准备与调灯预览分开计时")
            if owner not in _sessions:
                box.label(text="保存的配方；需显式绑定新会话才能计算")
        obj = context.active_object
        if obj and obj.get(OWNER) == owner and obj.get("rp_region_id"):
            box = layout.box()
            operation_label = ("增加保护" if obj["rp_target"] == "line" and obj["rp_operation"] == "add"
                               else OPERATION_LABELS[obj["rp_operation"]])
            box.label(text=TARGET_LABELS[obj["rp_target"]] + " · " + operation_label)
            box.prop(obj, '["rp_enabled"]', text="启用区域")
            for field, label in (("feather", "羽化"), ("strength", "影响强度")):
                box.prop(obj, '["rp_'+field+'"]', text=label)
            if obj["rp_target"] == "depth":
                frame = json.loads(scene["rp_initial_config"])["frame"]
                box.label(text="正偏移更近 / 无量纲" if frame["kind"] == "screen_heightfield" else "正偏移更远 / 场景单位")
                box.prop(obj, '["rp_scale"]', text="深度倍率")
                box.prop(obj, '["rp_offset"]', text="深度偏移")
            elif obj["rp_target"] == "roughness":
                box.prop(obj, '["rp_value"]', text="局部粗糙度值")
            else:
                box.operator("relight_preview.region_action", text="切换影响方式").action = "operation"
                if obj["rp_target"] == "light":
                    box.label(text="只限制漫反射；高光使用高光区域")
            row = box.row(align=True)
            row.operator("relight_preview.region_action", text="启用/禁用").action = "toggle"
            row.operator("relight_preview.region_action", text="删除区域").action = "remove"
        if obj and obj.get(OWNER) == owner and obj.type == "LIGHT":
            layout.label(text=obj["relight_id"] + " · " + obj.data.type)
            for field, label in (("height", "高度 Z"), ("intensity", "相对强度"), ("color", "颜色"),
                                 ("diffuse", "漫反射贡献"), ("specular", "高光贡献"), ("samples", "采样数"),
                                 ("softening", "距离柔化"), ("reference_distance", "参考距离"), ("aim_distance", "瞄准距离")):
                if "rp_" + field in obj:
                    layout.prop(obj, '["rp_' + field + '"]', text=label)
            data = obj.data
            if data.type in ("POINT", "SPOT"):
                layout.prop(data, "shadow_soft_size", text="半径")
            if data.type == "SPOT":
                layout.prop(data, "spot_size", text="完整锥角")
                layout.prop(data, "spot_blend", text="锥羽化")
            if data.type == "AREA":
                layout.prop(data, "shape", text="形状")
                layout.prop(data, "size", text="尺寸 X")
                if data.shape in ("RECTANGLE", "ELLIPSE"):
                    layout.prop(data, "size_y", text="尺寸 Y")
            if data.type == "SUN":
                layout.prop(data, "angle", text="角宽")


_classes = (*workbench.CLASSES, RP_AddonPreferences, RP_OT_start, RP_OT_bind, RP_OT_launch, RP_OT_cancel_launch,
            RP_OT_stop, RP_OT_final, RP_OT_save,
            RP_OT_add_region, RP_OT_region_action, RP_OT_apply_local, RP_OT_display_mode, RP_OT_anime_mode, RP_PT_panel)


def register():
    global _registered
    _assert_main()
    if _registered:
        return
    completed = []
    try:
        _ICONS.register()
        for cls in _classes:
            bpy.utils.register_class(cls)
            completed.append(cls)
        setattr(bpy.types.Scene, workbench.PROPERTY, PointerProperty(type=workbench.CF_Settings))
        SidebarDock.register_plugin(DOMAIN, _ICONS.icons_dir)
        bpy.app.handlers.load_pre.append(_load_pre)
        bpy.app.handlers.save_pre.append(_save_pre)
        _registered = True
    except Exception:
        SidebarDock.unregister_plugin(DOMAIN)
        if hasattr(bpy.types.Scene, workbench.PROPERTY):
            delattr(bpy.types.Scene, workbench.PROPERTY)
        for cls in reversed(completed):
            bpy.utils.unregister_class(cls)
        _ICONS.unregister()
        raise


def unregister():
    global _registered
    _assert_main()
    _cancel_launch()
    _LAUNCHER.cleanup()
    workbench.RENDER.cleanup()
    SidebarDock.unregister_plugin(DOMAIN)
    HUDManager.clear(DOMAIN)
    for owner in list(_sessions):
        stop(owner)
    for handlers, callback in ((bpy.app.handlers.load_pre, _load_pre), (bpy.app.handlers.save_pre, _save_pre)):
        if callback in handlers:
            handlers.remove(callback)
    if _registered:
        delattr(bpy.types.Scene, workbench.PROPERTY)
        for cls in reversed(_classes):
            bpy.utils.unregister_class(cls)
    _ICONS.unregister()
    _registered = False
