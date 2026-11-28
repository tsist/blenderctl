# SPDX-License-Identifier: GPL-3.0-or-later
"""Unified theme color tokens, monogram codes, and Blender datablock color helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple


def _srgb_channel_to_linear(c: float) -> float:
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def hex_to_srgb(hex_color: str) -> Tuple[float, float, float]:
    h = hex_color.lstrip("#").strip()
    if len(h) != 6:
        raise ValueError(f"Expected 6-digit hex color, got: {hex_color!r}")
    return (
        int(h[0:2], 16) / 255.0,
        int(h[2:4], 16) / 255.0,
        int(h[4:6], 16) / 255.0,
    )


def hex_to_linear_rgb(hex_color: str) -> Tuple[float, float, float]:
    r, g, b = hex_to_srgb(hex_color)
    return (
        _srgb_channel_to_linear(r),
        _srgb_channel_to_linear(g),
        _srgb_channel_to_linear(b),
    )


@dataclass(frozen=True)
class PluginTheme:
    domain: str
    code: str
    tab_label: str
    title_zh: str
    title_en: str
    family_id: str
    family_zh: str
    editor_space: str
    skeleton: str  # "heavy" | "compact"
    hex_color: str
    hex_dark: str
    hex_light: str
    badge_text_hex: str
    collection_color_tag: str
    strip_color_tag: str
    addon_dir: str

    @property
    def srgb(self) -> Tuple[float, float, float]:
        return hex_to_srgb(self.hex_color)

    @property
    def srgb_rgba(self) -> Tuple[float, float, float, float]:
        r, g, b = self.srgb
        return (r, g, b, 1.0)

    @property
    def linear_rgb(self) -> Tuple[float, float, float]:
        return hex_to_linear_rgb(self.hex_color)

    @property
    def node_tint_rgb(self) -> Tuple[float, float, float]:
        """Subtle dark-calibrated RGB tint for Blender Shader/GN/Compositor nodes."""
        r, g, b = self.srgb
        return (0.14 + r * 0.24, 0.14 + g * 0.24, 0.16 + b * 0.24)


PLUGIN_ORDER: Tuple[str, ...] = (
    "modeling",
    "hard-surface",
    "geometry",
    "uv",
    "material",
    "rigging",
    "animation",
    "physics",
    "camera",
    "lighting",
    "toon",
    "render-batch",
    "compositing",
    "video",
)

THEMES: Dict[str, PluginTheme] = {
    "modeling": PluginTheme(
        domain="modeling",
        code="M",
        tab_label="M·建模",
        title_zh="建模导入工作台",
        title_en="Modeling Workflow",
        family_id="geometry_modeling",
        family_zh="造型几何",
        editor_space="VIEW_3D",
        skeleton="compact",
        hex_color="#38BDF8",
        hex_dark="#0C4A6E",
        hex_light="#BAE6FD",
        badge_text_hex="#082F49",
        collection_color_tag="COLOR_05",
        strip_color_tag="COLOR_05",
        addon_dir="tools/modeling_workflow_addon",
    ),
    "hard-surface": PluginTheme(
        domain="hard-surface",
        code="H",
        tab_label="H·硬面",
        title_zh="硬表面工作台",
        title_en="Hard Surface Workflow",
        family_id="geometry_modeling",
        family_zh="造型几何",
        editor_space="VIEW_3D",
        skeleton="heavy",
        hex_color="#4F8BFF",
        hex_dark="#1E3A8A",
        hex_light="#BFDBFE",
        badge_text_hex="#FFFFFF",
        collection_color_tag="COLOR_05",
        strip_color_tag="COLOR_05",
        addon_dir="tools/hard_surface_workflow_addon",
    ),
    "geometry": PluginTheme(
        domain="geometry",
        code="G",
        tab_label="G·几何",
        title_zh="几何节点与毛发",
        title_en="Geometry Nodes Workflow",
        family_id="geometry_modeling",
        family_zh="造型几何",
        editor_space="VIEW_3D",
        skeleton="compact",
        hex_color="#10B981",
        hex_dark="#065F46",
        hex_light="#A7F3D0",
        badge_text_hex="#022C22",
        collection_color_tag="COLOR_04",
        strip_color_tag="COLOR_04",
        addon_dir="tools/geometry_nodes_workflow_addon",
    ),
    "uv": PluginTheme(
        domain="uv",
        code="U",
        tab_label="U·UV",
        title_zh="UV 与 UDIM 工作台",
        title_en="UV Workflow",
        family_id="surface_lookdev",
        family_zh="表面着色",
        editor_space="VIEW_3D",
        skeleton="compact",
        hex_color="#F59E0B",
        hex_dark="#78350F",
        hex_light="#FDE68A",
        badge_text_hex="#1C1917",
        collection_color_tag="COLOR_02",
        strip_color_tag="COLOR_02",
        addon_dir="tools/uv_workflow_addon",
    ),
    "material": PluginTheme(
        domain="material",
        code="S",
        tab_label="S·材质",
        title_zh="PBR 材质工作台",
        title_en="Material Workflow",
        family_id="surface_lookdev",
        family_zh="表面着色",
        editor_space="VIEW_3D",
        skeleton="compact",
        hex_color="#F43F5E",
        hex_dark="#881337",
        hex_light="#FECDD3",
        badge_text_hex="#FFFFFF",
        collection_color_tag="COLOR_01",
        strip_color_tag="COLOR_01",
        addon_dir="tools/material_workflow_addon",
    ),
    "rigging": PluginTheme(
        domain="rigging",
        code="R",
        tab_label="R·绑定",
        title_zh="角色绑定工作台",
        title_en="Rigging Workflow",
        family_id="character_performance",
        family_zh="角色表演",
        editor_space="VIEW_3D",
        skeleton="heavy",
        hex_color="#2DD4BF",
        hex_dark="#115E59",
        hex_light="#99F6E4",
        badge_text_hex="#042F2E",
        collection_color_tag="COLOR_04",
        strip_color_tag="COLOR_04",
        addon_dir="tools/addons/rigging_workflow",
    ),
    "animation": PluginTheme(
        domain="animation",
        code="A",
        tab_label="A·动画",
        title_zh="动画曲线工作台",
        title_en="Animation Curves Workflow",
        family_id="character_performance",
        family_zh="角色表演",
        editor_space="VIEW_3D",
        skeleton="heavy",
        hex_color="#FB923C",
        hex_dark="#7C2D12",
        hex_light="#FED7AA",
        badge_text_hex="#1C1917",
        collection_color_tag="COLOR_02",
        strip_color_tag="COLOR_02",
        addon_dir="tools/animation_curves_workflow_addon",
    ),
    "physics": PluginTheme(
        domain="physics",
        code="P",
        tab_label="P·物理",
        title_zh="物理模拟工作台",
        title_en="Physics Nodes Workbench",
        family_id="simulation_stage",
        family_zh="物理光影",
        editor_space="VIEW_3D",
        skeleton="compact",
        hex_color="#8B5CF6",
        hex_dark="#4C1D95",
        hex_light="#DDD6FE",
        badge_text_hex="#FFFFFF",
        collection_color_tag="COLOR_06",
        strip_color_tag="COLOR_06",
        addon_dir="tools/physics_nodes_workflow_addon",
    ),
    "camera": PluginTheme(
        domain="camera",
        code="C",
        tab_label="C·镜头",
        title_zh="镜头运镜工作台",
        title_en="Camera Workflow",
        family_id="simulation_stage",
        family_zh="物理光影",
        editor_space="VIEW_3D",
        skeleton="compact",
        hex_color="#67E8F9",
        hex_dark="#155E75",
        hex_light="#CFFAFE",
        badge_text_hex="#083344",
        collection_color_tag="COLOR_05",
        strip_color_tag="COLOR_05",
        addon_dir="tools/addons/camera_workflow/camera_windows_static",
    ),
    "lighting": PluginTheme(
        domain="lighting",
        code="L",
        tab_label="L·灯光",
        title_zh="商业灯光工作台",
        title_en="Lighting Workflow",
        family_id="simulation_stage",
        family_zh="物理光影",
        editor_space="VIEW_3D",
        skeleton="compact",
        hex_color="#FACC15",
        hex_dark="#713F12",
        hex_light="#FEF08A",
        badge_text_hex="#1C1917",
        collection_color_tag="COLOR_03",
        strip_color_tag="COLOR_03",
        addon_dir="tools/lighting_workflow_addon",
    ),
    "toon": PluginTheme(
        domain="toon",
        code="T",
        tab_label="T·三渲二",
        title_zh="三渲二风格工作台",
        title_en="Toon Workflow",
        family_id="stylize_post",
        family_zh="风格成片",
        editor_space="VIEW_3D",
        skeleton="heavy",
        hex_color="#EC4899",
        hex_dark="#831843",
        hex_light="#FBCFE8",
        badge_text_hex="#FFFFFF",
        collection_color_tag="COLOR_07",
        strip_color_tag="COLOR_07",
        addon_dir="tools/toon_workflow_addon/toon_workflow",
    ),
    "render-batch": PluginTheme(
        domain="render-batch",
        code="B",
        tab_label="B·批渲",
        title_zh="多镜批渲染工作台",
        title_en="Render Batch Workflow",
        family_id="stylize_post",
        family_zh="风格成片",
        editor_space="VIEW_3D",
        skeleton="compact",
        hex_color="#EF4444",
        hex_dark="#7F1D1D",
        hex_light="#FECACA",
        badge_text_hex="#FFFFFF",
        collection_color_tag="COLOR_01",
        strip_color_tag="COLOR_01",
        addon_dir="tools/render_batch_workflow_addon",
    ),
    "compositing": PluginTheme(
        domain="compositing",
        code="F",
        tab_label="F·合成",
        title_zh="后期合成工作台",
        title_en="Compositing Workflow",
        family_id="stylize_post",
        family_zh="风格成片",
        editor_space="VIEW_3D",
        skeleton="compact",
        hex_color="#C084FC",
        hex_dark="#581C87",
        hex_light="#E9D5FF",
        badge_text_hex="#1E1B4B",
        collection_color_tag="COLOR_06",
        strip_color_tag="COLOR_06",
        addon_dir="tools/compositing_workflow_addon",
    ),
    "video": PluginTheme(
        domain="video",
        code="V",
        tab_label="V·剪辑",
        title_zh="视频剪辑工作台",
        title_en="Video Editing Workflow",
        family_id="stylize_post",
        family_zh="风格成片",
        editor_space="SEQUENCE_EDITOR",
        skeleton="compact",
        hex_color="#06B6D4",
        hex_dark="#164E63",
        hex_light="#A5F3FC",
        badge_text_hex="#083344",
        collection_color_tag="COLOR_05",
        strip_color_tag="COLOR_05",
        addon_dir="tools/addons/video_editing_workflow/video_editing_workflow_addon",
    ),
}


def get_theme(domain: str) -> PluginTheme:
    try:
        return THEMES[domain]
    except KeyError as exc:
        raise KeyError(f"Unknown Blender AI UI domain: {domain!r}") from exc


def apply_collection_theme(collection, domain: str) -> bool:
    """Tag a Blender Collection in the Outliner with the plugin's theme color."""
    if collection is None or not hasattr(collection, "color_tag"):
        return False
    theme = get_theme(domain)
    try:
        collection.color_tag = theme.collection_color_tag
        return True
    except (AttributeError, TypeError, ValueError):
        return False


def apply_node_theme(node, domain: str, *, accent: bool = False) -> bool:
    """Apply subtle theme tint to a Blender Shader/GN/Compositor node or Frame."""
    if node is None or not hasattr(node, "use_custom_color"):
        return False
    theme = get_theme(domain)
    try:
        node.use_custom_color = True
        node.color = theme.srgb if accent else theme.node_tint_rgb
        return True
    except (AttributeError, TypeError, ValueError):
        return False


def apply_strip_theme(strip, domain: str) -> bool:
    """Tag a Blender VSE strip with the plugin's theme color."""
    if strip is None or not hasattr(strip, "color_tag"):
        return False
    theme = get_theme(domain)
    try:
        strip.color_tag = theme.strip_color_tag
        return True
    except (AttributeError, TypeError, ValueError):
        return False
