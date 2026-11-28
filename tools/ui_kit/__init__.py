# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender AI 1.0 Unified UI Design System (Single Source -> Vendored _ui_kit).

All 14 plugins share the same visual tokens, monogrammed SVG icon pipeline,
Blender 5.3 layout primitives, singleton viewport GPU HUD, and non-blocking
background job runner while remaining 100% standalone extension packages.
"""

from .theme_tokens import (
    PLUGIN_ORDER,
    THEMES,
    PluginTheme,
    apply_collection_theme,
    apply_node_theme,
    apply_strip_theme,
    get_theme,
    hex_to_linear_rgb,
    hex_to_srgb,
)
from .icons import IconPack
from .layout_kit import (
    draw_brand_header,
    draw_compact_context,
    draw_heavy_workspace_header,
    ensure_object_mode,
    primary_button,
    progress_card,
    prop_column,
    section_panel,
    status_banner,
)
from .hud_overlay import HUDManager
from .job_runner import BackgroundJobRunner
from .sidebar_dock import SidebarDock

__all__ = [
    "PLUGIN_ORDER",
    "THEMES",
    "PluginTheme",
    "get_theme",
    "hex_to_srgb",
    "hex_to_linear_rgb",
    "apply_collection_theme",
    "apply_node_theme",
    "apply_strip_theme",
    "IconPack",
    "draw_brand_header",
    "draw_compact_context",
    "draw_heavy_workspace_header",
    "section_panel",
    "prop_column",
    "primary_button",
    "status_banner",
    "progress_card",
    "ensure_object_mode",
    "HUDManager",
    "BackgroundJobRunner",
    "SidebarDock",
]
