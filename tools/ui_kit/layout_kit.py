# SPDX-License-Identifier: GPL-3.0-or-later
"""Unified Blender 5.3 UI layout primitives for both compact and heavy workbenches."""

from __future__ import annotations

from contextlib import contextmanager
import textwrap
from typing import Optional, Tuple

from .theme_tokens import get_theme


def draw_brand_header(
    layout,
    domain: str,
    icon_pack=None,
    *,
    title: Optional[str] = None,
    fallback_icon: str = "NONE",
) -> None:
    """Draw unified panel header with plugin monogram SVG icon + Chinese title."""
    theme = get_theme(domain)
    row = layout.row(align=True)
    icon_val = icon_pack.brand_id(large=False) if icon_pack is not None else 0
    label_text = title if title is not None else theme.title_zh
    if icon_val:
        row.label(text=label_text, icon_value=icon_val)
    elif fallback_icon != "NONE":
        row.label(text=label_text, icon=fallback_icon)
    else:
        row.label(text=label_text)


def draw_compact_context(
    layout,
    domain: str,
    icon_pack=None,
    *,
    badge_text: str = "",
    subtitle: str = "",
) -> None:
    """Minimal single-row context strip for the 10 compact plugins (zero card bloat)."""
    if not badge_text and not subtitle:
        return
    row = layout.row(align=True)
    row.scale_y = 0.92
    if badge_text:
        icon_val = icon_pack.brand_id(large=False) if icon_pack is not None else 0
        if icon_val:
            row.label(text=badge_text, icon_value=icon_val)
        else:
            row.label(text=badge_text)
    if subtitle:
        sub = row.row(align=True)
        sub.alignment = "RIGHT"
        sub.active = False
        sub.label(text=subtitle)


def draw_heavy_workspace_header(
    layout,
    domain: str,
    icon_pack,
    props,
    *,
    stage_prop: str,
    level_prop: Optional[str] = None,
    target_prop: Optional[str] = None,
    target_label: str = "目标",
    hint_text: str = "",
) -> None:
    """Standard workspace navigation block for the 4 heavy plugins (H/R/A/T)."""
    theme = get_theme(domain)
    box = layout.box()
    # Expanded enum buttons otherwise consume the brand title on narrow sidebars.
    top = box.split(factor=0.64, align=True) if level_prop else box.row(align=True)
    brand_val = icon_pack.brand_id(large=False) if icon_pack is not None else 0
    if brand_val:
        top.label(text=theme.title_zh, icon_value=brand_val)
    else:
        top.label(text=theme.title_zh)
    if level_prop:
        lvl = top.row(align=True)
        lvl.alignment = "RIGHT"
        lvl.prop(props, level_prop, expand=True)

    if target_prop:
        col = prop_column(box, split_factor=0.28)
        col.prop(props, target_prop, text=target_label)

    stages = box.row(align=True)
    stages.scale_y = 1.15
    stages.prop(props, stage_prop, expand=True)

    if hint_text:
        sub = box.row()
        sub.active = False
        sub.scale_y = 0.85
        sub.label(text=hint_text, icon="INFO")


def section_panel(
    layout,
    idname: str,
    title: str,
    *,
    icon: str = "NONE",
    icon_value: int = 0,
    default_closed: bool = False,
):
    """Blender 5.3 native collapsible section via layout.panel()."""
    if hasattr(layout, "panel"):
        header, body = layout.panel(idname, default_closed=default_closed)
        if icon_value:
            header.label(text=title, icon_value=icon_value)
        elif icon != "NONE":
            header.label(text=title, icon=icon)
        else:
            header.label(text=title)
        return header, body
    box = layout.box()
    if icon_value:
        box.label(text=title, icon_value=icon_value)
    elif icon != "NONE":
        box.label(text=title, icon=icon)
    else:
        box.label(text=title)
    return box, box


def prop_column(
    layout,
    *,
    split_factor: float = 0.42,
    decorate: bool = False,
    align: bool = False,
):
    """Create a clean Blender 5.3 two-column aligned property container."""
    del split_factor  # UILayout.property_split_factor is read-only in Blender 5.3 RNA
    col = layout.column(align=align)
    col.use_property_split = True
    col.use_property_decorate = decorate
    return col


def primary_button(
    layout,
    operator_id: str,
    text: str,
    *,
    icon: str = "NONE",
    icon_value: int = 0,
    scale_y: float = 1.32,
    enabled: bool = True,
    alert: bool = False,
    depress: bool = False,
):
    """Draw a prominent primary action CTA button with consistent height."""
    row = layout.row(align=True)
    row.scale_y = scale_y
    row.enabled = enabled
    row.alert = alert
    if icon_value:
        return row.operator(operator_id, text=text, icon_value=icon_value, depress=depress)
    if icon != "NONE":
        return row.operator(operator_id, text=text, icon=icon, depress=depress)
    return row.operator(operator_id, text=text, depress=depress)


def _wrap_width(context, default_chars: int = 26) -> int:
    try:
        region = getattr(context, "region", None)
        if region is not None and region.type == "UI":
            scale = context.preferences.system.ui_scale
            return max(14, min(42, int((region.width - 42) / (13.5 * max(scale, 0.5)))))
    except Exception:
        pass
    return default_chars


def status_banner(
    layout,
    context,
    text: str,
    *,
    kind: str = "INFO",
    icon_pack=None,
) -> None:
    """Compact auto-wrapped status banner shown only when text is non-empty."""
    message = (text or "").strip()
    if not message:
        return
    box = layout.box()
    is_err = kind.upper() in {"ERROR", "WARN", "WARNING"}
    if kind.upper() == "ERROR":
        box.alert = True
    icon_val = icon_pack.status_id(kind) if icon_pack is not None else 0
    fallback_map = {
        "OK": "CHECKMARK",
        "SUCCESS": "CHECKMARK",
        "INFO": "INFO",
        "WARN": "ERROR",
        "WARNING": "ERROR",
        "ERROR": "CANCEL",
        "RUNNING": "TIME",
    }
    fallback_icon = fallback_map.get(kind.upper(), "INFO")
    width = _wrap_width(context)
    lines = []
    for raw in message.splitlines():
        wrapped = textwrap.wrap(raw, width=width, break_long_words=True) or [""]
        lines.extend(wrapped)
    col = box.column(align=True)
    col.scale_y = 0.88
    for idx, line in enumerate(lines[:4]):
        if idx == 0:
            if icon_val:
                col.label(text=line, icon_value=icon_val)
            else:
                col.label(text=line, icon=fallback_icon)
        else:
            col.label(text=line)
    if is_err and len(lines) > 4:
        col.label(text="…详情见信息栏")


def progress_card(
    layout,
    domain: str,
    icon_pack,
    *,
    text: str,
    factor: float,
    cancel_operator: str = "",
) -> None:
    """Render native Blender 5.3 layout.progress() bar with optional cancel button."""
    theme = get_theme(domain)
    box = layout.box()
    head = box.row(align=True)
    icon_val = icon_pack.brand_id(large=False) if icon_pack is not None else 0
    title = f"{theme.code} · {text}"
    if icon_val:
        head.label(text=title, icon_value=icon_val)
    else:
        head.label(text=title, icon="TIME")
    if cancel_operator:
        sub = head.row(align=True)
        sub.alignment = "RIGHT"
        sub.alert = True
        sub.operator(cancel_operator, text="", icon="CANCEL")
    clamped = max(0.0, min(1.0, float(factor)))
    if hasattr(box, "progress"):
        box.progress(factor=clamped, type="BAR", text=f"{int(round(clamped * 100))}%")
    else:
        box.label(text=f"进度：{int(round(clamped * 100))}%")


@contextmanager
def ensure_object_mode(context):
    """Automatically switch Edit/Pose mode to Object mode for safe ops, then restore."""
    import bpy

    active = getattr(context, "active_object", None)
    original_mode = getattr(active, "mode", "OBJECT") if active is not None else "OBJECT"
    switched = False
    if active is not None and original_mode != "OBJECT":
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
            switched = True
        except Exception:
            switched = False
    try:
        yield
    finally:
        if switched and active is not None and active.name in context.view_layer.objects:
            try:
                context.view_layer.objects.active = active
                bpy.ops.object.mode_set(mode=original_mode)
            except Exception:
                pass
