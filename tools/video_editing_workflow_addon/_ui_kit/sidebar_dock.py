# SPDX-License-Identifier: GPL-3.0-or-later
"""Native Vertical Sidebar Tab Colorizer + SVG Icon Painter & Active Panel Accent Frame.

Directly styles Blender's REAL native vertical N-panel sidebar tabs (`L·灯光`, `M·建模`, ...)
in-place inside the `UI` region `POST_PIXEL` pass — ZERO floating boxes obstructing the
3D Viewport, ZERO custom Gizmo clutter, ZERO `bpy.data.images` pollution:

- Paints every registered Blender AI plugin's native right-edge vertical tab button with:
  1. Distinct theme-colored background (rich glowing active pill / sleek dark tinted inactive pill),
  2. Bright 3.5px theme-color edge accent bar,
  3. Crisp full-color 64px vector SVG brand icon (decoded straight into `gpu.types.GPUTexture`),
  4. Effortlessly readable upright stacked Chinese short name (`灯光`, `建模`, `绑定`, `UV`, `三渲二`).
- Active tab seamlessly merges into the open N-panel's continuous 3px top & left theme accent frame.
- Works identically across both `VIEW_3D` (13 plugins) and `SEQUENCE_EDITOR` (`V·剪辑`),
  survives `File -> Open` / `.blend` loads via `sys.modules` process-wide singleton state.
"""

from pathlib import Path
import re
import struct
import sys
import time
import types
from typing import Dict, List, Optional, Tuple
import zlib

try:
    import bpy  # type: ignore
except ImportError:  # pragma: no cover - non-Blender Python
    bpy = None

from .theme_tokens import PLUGIN_ORDER, THEMES, get_theme

_RUNTIME_MOD_NAME = "__blender_ai_ui_kit_shared_runtime_v1__"
_DOCK_ATTR = "sidebar_dock_state"

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
)


# ---------------------------------------------------------------------------
# Zero-Dependency Pure-Python RGBA PNG Decoder -> GPUTexture (Zero bpy.data pollution)
# ---------------------------------------------------------------------------

def _decode_png_rgba_flipped(path: Path) -> Tuple[int, int, bytes]:
    """Decode an 8-bit RGB/RGBA PNG into bottom-up RGBA bytes for gpu.types.GPUTexture."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Invalid PNG header: {path}")
    pos = 8
    width = height = bit_depth = color_type = 0
    idat = bytearray()
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
            if bit_depth != 8 or interlace != 0:
                raise ValueError(f"Unsupported PNG format: depth={bit_depth}, interlace={interlace}")
        elif ctype == b"IDAT":
            idat.extend(chunk)
        elif ctype == b"IEND":
            break

    bpp = {6: 4, 2: 3}.get(color_type)
    if bpp is None:
        raise ValueError(f"Unsupported PNG color_type={color_type}")

    raw = zlib.decompress(bytes(idat))
    stride = width * bpp
    rows: List[bytearray] = []
    prev = bytearray(stride)
    offset = 0

    for _ in range(height):
        ftype = raw[offset]
        offset += 1
        row = bytearray(raw[offset:offset + stride])
        offset += stride
        if ftype == 1:  # Sub
            for x in range(bpp, stride):
                row[x] = (row[x] + row[x - bpp]) & 0xFF
        elif ftype == 2:  # Up
            for x in range(stride):
                row[x] = (row[x] + prev[x]) & 0xFF
        elif ftype == 3:  # Average
            for x in range(stride):
                left = row[x - bpp] if x >= bpp else 0
                row[x] = (row[x] + ((left + prev[x]) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            for x in range(stride):
                a = row[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[x] = (row[x] + pr) & 0xFF
        elif ftype != 0:
            raise ValueError(f"Unknown PNG filter {ftype}")
        rows.append(row)
        prev = row

    out = bytearray(width * height * 4)
    dst = 0
    for row in reversed(rows):
        if bpp == 4:
            out[dst:dst + width * 4] = row
            dst += width * 4
        else:
            for x in range(0, stride, 3):
                out[dst] = row[x]
                out[dst + 1] = row[x + 1]
                out[dst + 2] = row[x + 2]
                out[dst + 3] = 255
                dst += 4
    return width, height, bytes(out)


# ---------------------------------------------------------------------------
# Process-Wide Singleton Registry (Survives .blend loads & shared across all 14 plugins)
# ---------------------------------------------------------------------------

def _dock_state() -> Dict:
    mod = sys.modules.get(_RUNTIME_MOD_NAME)
    if mod is None:
        mod = types.ModuleType(_RUNTIME_MOD_NAME)
        sys.modules[_RUNTIME_MOD_NAME] = mod
    state = getattr(mod, _DOCK_ATTR, None)
    if not isinstance(state, dict):
        state = {
            "plugins": {},          # domain -> {"icons_dir": str, "space": str}
            "textures": {},         # domain -> gpu.types.GPUTexture
            "font_id": None,
            "ui_draw_handlers": {}, # space_type -> (space_cls, handler)
            "cat_cache": {},        # region_ptr -> {"ts": float, "sig": tuple, "cats": List[str]}
            "last_tab_rects": {},   # space_type -> {domain: (x0, y0, w, h)}
        }
        setattr(mod, _DOCK_ATTR, state)
    return state


def _load_font(state: Dict) -> int:
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


def _get_brand_texture(domain: str):
    import gpu

    state = _dock_state()
    tex = state["textures"].get(domain)
    if tex is not None:
        return tex
    meta = state["plugins"].get(domain)
    if not meta:
        return None
    icons_dir = Path(meta["icons_dir"])
    prefix = domain.replace("-", "_")
    png_path = icons_dir / f"brand_{prefix}_64.png"
    if not png_path.is_file():
        png_path = icons_dir / f"brand_{prefix}_32.png"
    if not png_path.is_file():
        return None
    try:
        w, h, rgba_bytes = _decode_png_rgba_flipped(png_path)
        floats = [b / 255.0 for b in rgba_bytes]
        buf = gpu.types.Buffer("FLOAT", w * h * 4, floats)
        tex = gpu.types.GPUTexture((w, h), format="RGBA8", data=buf)
        state["textures"][domain] = tex
        return tex
    except Exception as exc:
        print(f"[SidebarDock] Failed to load brand icon texture for {domain} ({png_path}): {exc}")
        return None


def _ordered_domains(space_type: str) -> List[str]:
    plugins = _dock_state()["plugins"]
    return [d for d in PLUGIN_ORDER if d in plugins and plugins[d]["space"] == space_type]


def _get_visible_categories(region, context, space_type: str) -> List[str]:
    """Query Blender's exact live ordered vertical sidebar tab list with lightweight caching."""
    state = _dock_state()
    rptr = region.as_pointer()
    active_cat = str(getattr(region, "active_panel_category", "") or "")
    mode = str(getattr(context, "mode", "") or "")
    sig = (space_type, mode, active_cat, len(state["plugins"]), region.height)
    now = time.monotonic()

    cached = state["cat_cache"].get(rptr)
    if cached and cached["sig"] == sig and (now - cached["ts"]) < 0.35:
        return cached["cats"]

    cats: List[str] = []
    try:
        region.active_panel_category = "__BLENDER_AI_TAB_PROBE__"
    except TypeError as exc:
        msg = str(exc)
        match = re.search(r"not found in \((.*)\)", msg)
        if match:
            cats = re.findall(r"'([^']*)'", match.group(1))
    except Exception:
        pass

    state["cat_cache"][rptr] = {"ts": now, "sig": sig, "cats": cats}
    return cats


# ---------------------------------------------------------------------------
# GPU Drawing Helpers
# ---------------------------------------------------------------------------

def _rect_tris(x: float, y: float, w: float, h: float):
    return (
        (x, y),
        (x + w, y),
        (x + w, y + h),
        (x, y),
        (x + w, y + h),
        (x, y + h),
    )


def _draw_solid(shader, x: float, y: float, w: float, h: float, color: Tuple[float, float, float, float]) -> None:
    from gpu_extras.batch import batch_for_shader

    if w <= 0.0 or h <= 0.0:
        return
    batch = batch_for_shader(shader, "TRIS", {"pos": _rect_tris(x, y, w, h)})
    shader.bind()
    shader.uniform_float("color", color)
    batch.draw(shader)


def _draw_textured(shader, texture, x: float, y: float, w: float, h: float) -> None:
    from gpu_extras.batch import batch_for_shader

    if texture is None or w <= 0.0 or h <= 0.0:
        return
    uvs = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    batch = batch_for_shader(shader, "TRIS", {"pos": _rect_tris(x, y, w, h), "texCoord": uvs})
    shader.bind()
    shader.uniform_sampler("image", texture)
    batch.draw(shader)


def _tab_short_label(tab_label: str) -> str:
    """Return concise label without leading `X·` monogram (already shown inside SVG icon)."""
    if "·" in tab_label:
        return tab_label.split("·", 1)[1].strip()
    return tab_label.strip()


def _draw_upright_tab_label(
    font_id: int,
    short_label: str,
    cx: float,
    area_y0: float,
    area_h: float,
    scale: float,
    color: Tuple[float, float, float, float],
) -> None:
    """Render upright Chinese chars stacked vertically (or short ASCII like 'UV' horizontally)."""
    import blf

    if not short_label or area_h < 10.0 * scale:
        return

    is_ascii = all(ord(ch) < 128 for ch in short_label)
    if is_ascii:
        font_px = max(9, int(round(9.2 * scale)))
        blf.size(font_id, font_px)
        tw, th = blf.dimensions(font_id, short_label)
        blf.position(font_id, cx - tw * 0.5, area_y0 + (area_h - th) * 0.5, 0)
        blf.color(font_id, *color)
        blf.draw(font_id, short_label)
        return

    chars = list(short_label)
    n = len(chars)
    # Choose crisp font size that comfortably fits all stacked Chinese glyphs inside area_h
    max_step = area_h / max(1, n)
    font_px = max(9, int(round(min(9.6 * scale, max_step * 0.78))))
    blf.size(font_id, font_px)
    glyph_dims = [blf.dimensions(font_id, ch) for ch in chars]
    glyph_h = max((d[1] for d in glyph_dims), default=float(font_px))
    line_gap = max(1.5 * scale, min(3.0 * scale, (area_h - glyph_h * n) / max(1, n + 1)))
    total_h = glyph_h * n + line_gap * max(0, n - 1)
    top_baseline = area_y0 + (area_h + total_h) * 0.5 - glyph_h

    blf.color(font_id, *color)
    for idx, ch in enumerate(chars):
        cw, _ = glyph_dims[idx]
        y_pos = top_baseline - idx * (glyph_h + line_gap)
        blf.position(font_id, cx - cw * 0.5, y_pos, 0)
        blf.draw(font_id, ch)


def _native_tab_runs(context, scale: float, width: int, height: int):
    """Read the native category silhouettes from the current UI region buffer."""
    import gpu

    x, y, view_width, view_height = gpu.state.viewport_get()
    if (view_width, view_height) != (width, height):
        return ()
    inset = max(4, int(round(5.33 * scale)))
    try:
        framebuffer = gpu.state.active_framebuffer_get()
        pixels = bytes(framebuffer.read_color(x + width - inset, y, 1, height, 4, 0, "UBYTE"))
    except (RuntimeError, SystemError, TypeError, ValueError):
        return ()
    if len(pixels) != height * 4:
        return ()

    alpha = pixels[3::4]
    peak = max(alpha)
    if peak >= 64 and min(alpha) <= peak // 5:
        occupied = [value > peak // 2 for value in alpha]
    else:
        # Non-overlapping sidebars may have an opaque region background.
        widget = context.preferences.themes[0].user_interface.wcol_tab
        colors = tuple(tuple(round(channel * 255) for channel in color[:3])
                       for color in (widget.inner, widget.inner_sel))
        occupied = [
            any(all(abs(pixels[index + channel] - color[channel]) <= 7 for channel in range(3)) for color in colors)
            for index in range(0, len(pixels), 4)
        ]

    changes = [index for index, (first, second) in enumerate(zip([False, *occupied], [*occupied, False]))
               if first != second]
    return tuple((low, high) for low, high in zip(changes[::2], changes[1::2])
                 if high - low >= max(3, round(2 * scale)))


def _align_overflowing_tabs(tab_spans, native_runs, height: int, scale: float):
    """Match a single native scroll displacement, then use measured button edges."""
    if not 2 <= len(native_runs) <= len(tab_spans):
        return None

    if len(native_runs) == len(tab_spans) and native_runs[0][0] > 0 and native_runs[-1][1] < height:
        # With every category visible, match their exact silhouettes in native order.
        # Blender rounds each row individually, so a single shift cannot correct drift down the list.
        bounds = [(cat, low, high) for (cat, _, _), (low, high) in zip(tab_spans, reversed(native_runs))]
        allowance = max(4, round(4 * scale))
        if all(abs((high - low) - (nominal_high - nominal_low)) <= allowance
               for (_, low, high), (_, nominal_low, nominal_high) in zip(bounds, tab_spans)):
            return bounds

    occupied = bytearray(height)
    for low, high in native_runs:
        occupied[low:high] = b"\1" * (high - low)
    counted = [0]
    for value in occupied:
        counted.append(counted[-1] + value)
    max_scroll = max(0, round(-tab_spans[-1][1] + 10 * scale))

    scores = []
    for shift in range(max_scroll + 1):
        disagreement = counted[-1]
        for _, low, high in tab_spans:
            first = max(0, low + shift)
            last = min(height, high + shift)
            if first < last:
                disagreement += last - first - 2 * (counted[last] - counted[first])
        scores.append(disagreement)
    shift = min(range(len(scores)), key=scores.__getitem__)
    error = scores[shift]
    if error > height * 0.15 or any(
        abs(other - shift) > max(5, round(4 * scale)) and value <= error + 3
        for other, value in enumerate(scores)
    ):
        # Ambiguous or unavailable native drawing should stay unpainted.
        return None

    aligned = []
    remaining = list(reversed(native_runs))
    for cat, low, high in tab_spans:
        shifted_low, shifted_high = low + shift, high + shift
        shown = min(shifted_high, height) - max(shifted_low, 0)
        if not remaining or shown <= 0:
            continue
        actual_low, actual_high = remaining[0]
        overlap = min(shifted_high, actual_high) - max(shifted_low, actual_low)
        if overlap < min(shown, actual_high - actual_low) * 0.65:
            continue
        remaining.pop(0)
        aligned.append((
            cat,
            min(shifted_low, 0) if actual_low == 0 else actual_low,
            max(shifted_high, height) if actual_high == height else actual_high,
        ))
    return aligned if not remaining else None


# ---------------------------------------------------------------------------
# UI Region POST_PIXEL Callback (Native Vertical Sidebar Tabs + Active Panel Accent)
# ---------------------------------------------------------------------------

def _draw_ui_region_tabs_callback(space_type: str) -> None:
    """Colorize & iconify native Blender AI sidebar tabs in-place + frame the active panel."""
    import blf
    import gpu

    context = bpy.context
    region = getattr(context, "region", None)
    if region is None or region.type != "UI" or region.width < 50 or region.height < 60:
        return

    state = _dock_state()
    domains = _ordered_domains(space_type)
    if not domains:
        return

    # Map tab_label -> (domain, Theme)
    label_map = {}
    for domain in domains:
        theme = get_theme(domain)
        label_map[theme.tab_label] = (domain, theme)

    cats = _get_visible_categories(region, context, space_type)
    if not cats:
        return

    scale = max(0.75, float(context.preferences.system.ui_scale))
    reg_w = float(region.width)
    reg_h = float(region.height)

    # Exact Blender 5.3 UI_panel_category_draw_all geometry (verified to <=1px across 11 tabs!)
    tab_strip_w = float(round(20.0 * scale))  # UI_UNIT_X = 20 * ui_scale
    right_gutter = max(2.0, float(round(2.0 * scale)))
    pill_x0 = reg_w - tab_strip_w
    pill_x1 = reg_w - right_gutter
    pill_w = max(8.0, pill_x1 - pill_x0)
    top_margin = float(round(5.33 * scale))
    pad_y = float(round(12.67 * scale))
    gap_y = float(round(6.0 * scale))

    # Measure every visible tab with Blender's native UI font (font_id=0, 11pt * scale)
    blf.size(0, 11.0 * scale)
    cursor_top = reg_h - top_margin
    spans = []
    for cat in cats:
        label = bpy.app.translations.pgettext_iface(cat)
        tw, _ = blf.dimensions(0, label)
        tab_h = float(round(tw + pad_y))
        y_top = cursor_top
        y_bot = cursor_top - tab_h
        cursor_top = y_bot - gap_y
        spans.append((cat, round(y_bot), round(y_top)))

    overflow = spans[-1][1] < gap_y
    if overflow or len(spans) >= 10:
        # Also measure long lists that fit, where rounded row heights can accumulate.
        runs = _native_tab_runs(context, scale, int(reg_w), int(reg_h))
        measured = _align_overflowing_tabs(spans, runs, int(reg_h), scale)
        if measured is not None:
            spans = measured
        elif overflow:
            state["last_tab_rects"][space_type] = {}
            spans = []

    active_cat = str(getattr(region, "active_panel_category", "") or "")
    active_match = label_map.get(active_cat)
    active_theme = active_match[1] if active_match is not None else None
    tab_entries = []
    active_tab_span: Optional[Tuple[float, float]] = None
    for cat, y_bot, y_top in spans:
        match = label_map.get(cat)
        if match is not None:
            domain, theme = match
            is_active = cat == active_cat
            if is_active:
                active_tab_span = (y_bot, y_top)
                active_theme = theme
            tab_entries.append((domain, theme, y_bot, y_top, is_active))

    if not tab_entries and active_theme is None:
        return

    color_shader = gpu.shader.from_builtin("UNIFORM_COLOR")
    img_shader = gpu.shader.from_builtin("IMAGE")
    font_id = _load_font(state)
    gpu.state.blend_set("ALPHA")

    # 1. If a Blender AI plugin tab is currently active, frame its open panel cleanly
    if active_theme is not None:
        r, g, b = active_theme.srgb
        bar_w = max(2.0, float(round(2.5 * scale)))
        div_w = max(1.5, float(round(1.5 * scale)))
        # Crisp top accent bar across panel content width
        _draw_solid(color_shader, 0.0, reg_h - bar_w, max(0.0, pill_x0), bar_w, (r, g, b, 0.96))
        # Continuous left-edge accent bar
        _draw_solid(color_shader, 0.0, 0.0, bar_w, reg_h, (r, g, b, 0.85))
        # Subtle vertical theme divider beside the native tab strip (with cutout at active tab!)
        div_x = pill_x0 - div_w
        if active_tab_span is not None:
            ay0, ay1 = active_tab_span
            if ay0 > 0.0:
                _draw_solid(color_shader, div_x, 0.0, div_w, min(reg_h, ay0), (r, g, b, 0.55))
            if ay1 < reg_h:
                _draw_solid(color_shader, div_x, max(0.0, ay1), div_w, reg_h - max(0.0, ay1), (r, g, b, 0.55))
        else:
            _draw_solid(color_shader, div_x, 0.0, div_w, reg_h, (r, g, b, 0.55))

    # 2. Paint every visible Blender AI plugin's native vertical tab button in-place!
    recorded_rects: Dict[str, Tuple[float, float, float, float]] = {}
    accent_bar_w = max(2.5, float(round(2.6 * scale)))

    for domain, theme, y_bot, y_top, is_active in tab_entries:
        # Skip tabs completely scrolled out of view
        if y_top <= 2.0 or y_bot >= reg_h - 2.0:
            continue
        vis_y0 = max(0.0, y_bot)
        vis_y1 = min(reg_h, y_top)
        vis_h = vis_y1 - vis_y0
        if vis_h < 10.0:
            continue

        r, g, b = theme.srgb
        # Extend active tab 2px left to fuse seamlessly into the panel body across the divider cutout
        draw_x0 = (pill_x0 - max(1.5, float(round(1.5 * scale)))) if is_active else pill_x0
        draw_w = pill_x1 - draw_x0
        recorded_rects[domain] = (draw_x0, y_bot, draw_w, y_top - y_bot)

        if is_active:
            body_col = (0.14 + r * 0.34, 0.15 + g * 0.34, 0.18 + b * 0.34, 1.0)
            border_col = (r, g, b, 1.0)
            bar_col = (r, g, b, 1.0)
            text_col = (1.0, 1.0, 1.0, 1.0)
        else:
            body_col = (0.10 + r * 0.13, 0.11 + g * 0.13, 0.14 + b * 0.13, 1.0)
            border_col = (r, g, b, 0.58)
            bar_col = (r, g, b, 0.96)
            text_col = (0.90, 0.93, 0.97, 0.96)

        # Solid opaque themed pill completely covering Blender's grey monochrome tab
        _draw_solid(color_shader, draw_x0, vis_y0, draw_w, vis_h, body_col)

        # Crisp theme borders (top, bottom, and left on inactive tabs)
        bw = max(1.0, float(round((1.4 if is_active else 1.0) * scale)))
        if y_top <= reg_h:
            _draw_solid(color_shader, draw_x0, vis_y1 - bw, draw_w, bw, border_col)
        if y_bot >= 0.0:
            _draw_solid(color_shader, draw_x0, vis_y0, draw_w, bw, border_col)
        if not is_active:
            _draw_solid(color_shader, draw_x0, vis_y0, bw, vis_h, border_col)

        # Prominent right-edge vertical theme-color bar
        _draw_solid(color_shader, pill_x1 - accent_bar_w, vis_y0, accent_bar_w, vis_h, bar_col)

        # Draw full-color custom SVG brand icon + upright stacked Chinese short label (if tab not clipped tiny)
        content_x0 = pill_x0 + 1.0 * scale
        content_x1 = pill_x1 - accent_bar_w - 1.0 * scale
        content_w = max(4.0, content_x1 - content_x0)
        content_cx = (content_x0 + content_x1) * 0.5

        ico_size = min(content_w, 14.0 * scale, max(8.0, (y_top - y_bot) * 0.38))
        ico_top_pad = 3.5 * scale
        ico_y = y_top - ico_top_pad - ico_size
        ico_x = content_cx - ico_size * 0.5

        if ico_y >= 2.0 and (ico_y + ico_size) <= reg_h - 1.0:
            tex = _get_brand_texture(domain)
            if tex is not None:
                _draw_textured(img_shader, tex, ico_x, ico_y, ico_size, ico_size)
            else:
                _draw_solid(color_shader, ico_x, ico_y, ico_size, ico_size, (r, g, b, 0.96))

        label_y0 = max(vis_y0 + 2.5 * scale, y_bot + 2.5 * scale)
        label_y1 = min(vis_y1 - 1.0 * scale, ico_y - 2.0 * scale)
        label_h = label_y1 - label_y0
        if label_h >= 12.0 * scale:
            short_lbl = _tab_short_label(theme.tab_label)
            _draw_upright_tab_label(font_id, short_lbl, content_cx, label_y0, label_h, scale, text_col)

    state["last_tab_rects"][space_type] = recorded_rects
    gpu.state.blend_set("NONE")


def _tag_redraw_all() -> None:
    try:
        if bpy is None:
            return
        wm = getattr(bpy.context, "window_manager", None)
        if wm is None:
            return
        for win in wm.windows:
            for area in win.screen.areas:
                if area.type in {"VIEW_3D", "SEQUENCE_EDITOR"}:
                    area.tag_redraw()
    except Exception:
        pass


class SidebarDock:
    """Process-wide singleton manager for in-place Native Colored SVG Sidebar Tabs."""

    @classmethod
    def register_plugin(cls, domain: str, icons_dir: Path) -> None:
        if bpy is None or getattr(bpy.app, "background", False):
            return
        theme = get_theme(domain)
        state = _dock_state()
        state["plugins"][domain] = {
            "icons_dir": str(Path(icons_dir)),
            "space": theme.editor_space,
        }
        state["textures"].pop(domain, None)
        state["cat_cache"].clear()

        space_cls = (
            bpy.types.SpaceView3D
            if theme.editor_space == "VIEW_3D"
            else bpy.types.SpaceSequenceEditor
        )
        sp_name = theme.editor_space

        if sp_name not in state["ui_draw_handlers"]:
            ui_handle = space_cls.draw_handler_add(
                lambda sp=sp_name: _draw_ui_region_tabs_callback(sp),
                (),
                "UI",
                "POST_PIXEL",
            )
            state["ui_draw_handlers"][sp_name] = (space_cls, ui_handle)

        _tag_redraw_all()

    @classmethod
    def unregister_plugin(cls, domain: str) -> None:
        if bpy is None or getattr(bpy.app, "background", False):
            return
        state = _dock_state()
        meta = state["plugins"].pop(domain, None)
        state["textures"].pop(domain, None)
        state["cat_cache"].clear()

        if meta is not None:
            sp = meta["space"]
            if not _ordered_domains(sp) and sp in state["ui_draw_handlers"]:
                space_cls, handle = state["ui_draw_handlers"].pop(sp)
                try:
                    space_cls.draw_handler_remove(handle, "UI")
                except Exception:
                    pass
                state["last_tab_rects"].pop(sp, None)

        _tag_redraw_all()
