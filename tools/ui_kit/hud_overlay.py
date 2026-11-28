# SPDX-License-Identifier: GPL-3.0-or-later
"""Singleton top-right 3D Viewport GPU/BLF HUD capsule shared across all 14 plugins."""

from __future__ import annotations

from pathlib import Path
import sys
import types
from typing import Dict, Optional, Tuple

from .theme_tokens import get_theme

_RUNTIME_MOD_NAME = "__blender_ai_ui_kit_shared_runtime_v1__"
_HUD_ATTR = "hud_state"

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\seguiemj.ttf",
)


def _state() -> Dict:
    mod = sys.modules.get(_RUNTIME_MOD_NAME)
    if mod is None:
        mod = types.ModuleType(_RUNTIME_MOD_NAME)
        sys.modules[_RUNTIME_MOD_NAME] = mod
    state = getattr(mod, _HUD_ATTR, None)
    if not isinstance(state, dict):
        state = {
            "entries": {},  # domain -> {title, subtitle, progress, kind}
            "handler": None,
            "font_id": None,
        }
        setattr(mod, _HUD_ATTR, state)
    return state


def _load_cjk_font(state: Dict) -> int:
    import blf

    cached = state.get("font_id")
    if isinstance(cached, int) and cached >= 0:
        return cached
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            try:
                fid = blf.load(candidate)
                if fid >= 0:
                    state["font_id"] = fid
                    return fid
            except Exception:
                continue
    state["font_id"] = 0
    return 0


def _rect_tris(x: float, y: float, w: float, h: float):
    return (
        (x, y),
        (x + w, y),
        (x + w, y + h),
        (x, y),
        (x + w, y + h),
        (x, y + h),
    )


def _draw_rect(shader, x: float, y: float, w: float, h: float, color: Tuple[float, float, float, float]) -> None:
    from gpu_extras.batch import batch_for_shader

    if w <= 0.0 or h <= 0.0:
        return
    batch = batch_for_shader(shader, "TRIS", {"pos": _rect_tris(x, y, w, h)})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_hud_callback() -> None:
    import blf
    import bpy
    import gpu

    state = _state()
    entries = state.get("entries")
    if not entries:
        return
    region = bpy.context.region
    if region is None or region.width < 220 or region.height < 140:
        return

    space = getattr(bpy.context, "space_data", None)
    area = getattr(bpy.context, "area", None)
    sidebar_open = bool(getattr(space, "show_region_ui", False)) if space is not None else False
    active_sidebar_cat = ""
    if area is not None and sidebar_open:
        for r in area.regions:
            if r.type == "UI":
                active_sidebar_cat = str(getattr(r, "active_panel_category", "") or "")
                break

    # Filter entries: NEVER obstruct the 3D Viewport with static/idle HUD capsules when the
    # plugin's own N-panel is hidden or inactive! Only live running jobs (`progress` / `RUNNING`)
    # may float transiently while computing, and they auto-clear on completion.
    visible_entries = []
    for domain, entry in list(entries.items()):
        theme = get_theme(domain)
        kind = str(entry.get("kind") or "INFO").upper()
        is_running = (entry.get("progress") is not None) or (kind == "RUNNING")
        if is_running or (sidebar_open and active_sidebar_cat == theme.tab_label):
            visible_entries.append((domain, entry))
    if not visible_entries:
        return

    scale = max(0.75, float(bpy.context.preferences.system.ui_scale))
    font_id = _load_cjk_font(state)

    # Compute safe top-right margin (leave room for N-panel when overlapping + 3D Navigation Gizmo)
    right_margin = 18.0 * scale
    if getattr(bpy.context.preferences.system, "use_region_overlap", True) and sidebar_open:
        for r in bpy.context.area.regions:
            if r.type == "UI" and r.width > 1:
                right_margin += float(r.width)
                break
    if space is not None and getattr(space, "show_gizmo", True) and getattr(space, "show_gizmo_navigate", True):
        right_margin += 116.0 * scale
    top_margin = 52.0 * scale
    for r in bpy.context.area.regions:
        if r.type == "HEADER":
            top_margin = max(top_margin, float(r.height) + 18.0 * scale)

    pill_h = 48.0 * scale
    gap = 8.0 * scale
    pad_x = 12.0 * scale
    accent_w = 4.0 * scale
    badge_size = 24.0 * scale

    shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    gpu.state.blend_set("ALPHA")

    cursor_y = float(region.height) - top_margin
    for domain, entry in visible_entries:
        theme = get_theme(domain)
        title = str(entry.get("title") or theme.tab_label)
        subtitle = str(entry.get("subtitle") or "")
        progress = entry.get("progress")
        kind = str(entry.get("kind") or "INFO").upper()

        blf.size(font_id, int(round(12.5 * scale)))
        tw, _ = blf.dimensions(font_id, title)
        blf.size(font_id, int(round(10.5 * scale)))
        sw, _ = blf.dimensions(font_id, subtitle) if subtitle else (0.0, 0.0)
        text_w = max(tw, sw)

        pill_w = max(188.0 * scale, accent_w + badge_size + text_w + pad_x * 3.0)
        x0 = max(12.0 * scale, float(region.width) - right_margin - pill_w)
        y0 = cursor_y - pill_h
        if y0 < 24.0 * scale:
            break

        # Drop shadow & dark glass body
        _draw_rect(shader, x0 + 2.0 * scale, y0 - 2.0 * scale, pill_w, pill_h, (0.0, 0.0, 0.0, 0.32))
        _draw_rect(shader, x0, y0, pill_w, pill_h, (0.09, 0.10, 0.13, 0.90))
        # Subtle 1px border
        border_col = (0.94, 0.27, 0.27, 0.85) if kind == "ERROR" else (*theme.srgb, 0.50)
        _draw_rect(shader, x0, y0 + pill_h - 1.0, pill_w, 1.0, border_col)
        _draw_rect(shader, x0, y0, pill_w, 1.0, (1.0, 1.0, 1.0, 0.08))
        _draw_rect(shader, x0 + pill_w - 1.0, y0, 1.0, pill_h, (1.0, 1.0, 1.0, 0.08))

        # Left theme accent strip
        bar_col = (0.94, 0.27, 0.27, 1.0) if kind == "ERROR" else (*theme.srgb, 1.0)
        _draw_rect(shader, x0, y0, accent_w, pill_h, bar_col)

        # Brand SVG icon (or fallback monogram square badge)
        bx = x0 + accent_w + 9.0 * scale
        by = y0 + (pill_h - badge_size) * 0.5 + (2.0 * scale if progress is not None else 0.0)
        brand_tex = None
        try:
            from .sidebar_dock import _draw_textured, _get_brand_texture

            brand_tex = _get_brand_texture(domain)
        except Exception:
            brand_tex = None

        if brand_tex is not None:
            img_shader = gpu.shader.from_builtin("IMAGE")
            ico_sz = 28.0 * scale
            iby = y0 + (pill_h - ico_sz) * 0.5 + (1.5 * scale if progress is not None else 0.0)
            _draw_textured(img_shader, brand_tex, bx, iby, ico_sz, ico_sz)
            badge_advance = ico_sz
        else:
            _draw_rect(shader, bx, by, badge_size, badge_size, bar_col)
            badge_advance = badge_size

        # Progress bar along bottom edge
        if progress is not None:
            frac = max(0.0, min(1.0, float(progress)))
            prog_h = 3.0 * scale
            _draw_rect(shader, x0 + accent_w, y0, pill_w - accent_w, prog_h, (1.0, 1.0, 1.0, 0.12))
            _draw_rect(shader, x0 + accent_w, y0, (pill_w - accent_w) * frac, prog_h, (*theme.srgb, 0.95))

        # Draw uppercase monogram letter centered in fallback badge only when SVG texture unavailable
        if brand_tex is None:
            blf.size(font_id, int(round(13.5 * scale)))
            lw, lh = blf.dimensions(font_id, theme.code)
            blf.position(font_id, bx + (badge_size - lw) * 0.5, by + (badge_size - lh) * 0.5 + 1.0 * scale, 0)
            if theme.badge_text_hex.upper() == "#FFFFFF":
                blf.color(font_id, 1.0, 1.0, 1.0, 1.0)
            else:
                blf.color(font_id, 0.07, 0.08, 0.10, 1.0)
            blf.draw(font_id, theme.code)

        # Title + subtitle text
        tx = bx + badge_advance + 9.0 * scale
        if subtitle:
            blf.size(font_id, int(round(12.2 * scale)))
            blf.position(font_id, tx, y0 + pill_h - 19.5 * scale, 0)
            blf.color(font_id, 0.96, 0.97, 0.99, 0.98)
            blf.draw(font_id, title)

            blf.size(font_id, int(round(10.2 * scale)))
            blf.position(font_id, tx, y0 + 9.5 * scale, 0)
            blf.color(font_id, 0.72, 0.76, 0.83, 0.94)
            blf.draw(font_id, subtitle)
        else:
            blf.size(font_id, int(round(12.5 * scale)))
            _, th = blf.dimensions(font_id, title)
            blf.position(font_id, tx, y0 + (pill_h - th) * 0.5, 0)
            blf.color(font_id, 0.96, 0.97, 0.99, 0.98)
            blf.draw(font_id, title)

        cursor_y = y0 - gap

    gpu.state.blend_set("NONE")


def _tag_view3d_redraw() -> None:
    try:
        import bpy

        wm = getattr(bpy.context, "window_manager", None)
        if wm is None:
            return
        for win in wm.windows:
            for area in win.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        pass


class HUDManager:
    """Process-wide singleton API for posting/clearing plugin viewport HUD pills."""

    @classmethod
    def show(
        cls,
        domain: str,
        title: str,
        subtitle: str = "",
        *,
        progress: Optional[float] = None,
        kind: str = "INFO",
    ) -> None:
        try:
            import bpy
        except ImportError:
            return
        if getattr(bpy.app, "background", False):
            return
        state = _state()
        state["entries"][domain] = {
            "title": title,
            "subtitle": subtitle,
            "progress": progress,
            "kind": kind,
        }
        if state["handler"] is None:
            state["handler"] = bpy.types.SpaceView3D.draw_handler_add(
                _draw_hud_callback, (), "WINDOW", "POST_PIXEL"
            )
        _tag_view3d_redraw()

    @classmethod
    def clear(cls, domain: str) -> None:
        try:
            import bpy
        except ImportError:
            return
        if getattr(bpy.app, "background", False):
            return
        state = _state()
        state["entries"].pop(domain, None)
        if not state["entries"] and state["handler"] is not None:
            try:
                bpy.types.SpaceView3D.draw_handler_remove(state["handler"], "WINDOW")
            except Exception:
                pass
            state["handler"] = None
        _tag_view3d_redraw()

    @classmethod
    def is_active(cls, domain: str) -> bool:
        try:
            import bpy
        except ImportError:
            return False
        if getattr(bpy.app, "background", False):
            return False
        return domain in _state()["entries"]
