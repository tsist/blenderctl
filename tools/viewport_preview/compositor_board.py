# SPDX-License-Identifier: GPL-3.0-or-later
"""Contact-sheet board composer with crisp built-in bitmap headers and 2048px cap."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


# Clean 5x7 uppercase ASCII bitmap font (no external font files required).
_GLYPHS: dict[str, tuple[int, ...]] = {
    "A": (14, 17, 17, 31, 17, 17, 17),
    "B": (30, 17, 17, 30, 17, 17, 30),
    "C": (14, 17, 16, 16, 16, 17, 14),
    "D": (30, 17, 17, 17, 17, 17, 30),
    "E": (31, 16, 16, 30, 16, 16, 31),
    "F": (31, 16, 16, 30, 16, 16, 16),
    "G": (14, 17, 16, 23, 17, 17, 15),
    "H": (17, 17, 17, 31, 17, 17, 17),
    "I": (14, 4, 4, 4, 4, 4, 14),
    "J": (7, 2, 2, 2, 18, 18, 12),
    "K": (17, 18, 20, 24, 20, 18, 17),
    "L": (16, 16, 16, 16, 16, 16, 31),
    "M": (17, 27, 21, 21, 17, 17, 17),
    "N": (17, 25, 21, 19, 17, 17, 17),
    "O": (14, 17, 17, 17, 17, 17, 14),
    "P": (30, 17, 17, 30, 16, 16, 16),
    "Q": (14, 17, 17, 17, 21, 18, 13),
    "R": (30, 17, 17, 30, 20, 18, 17),
    "S": (15, 16, 16, 14, 1, 1, 30),
    "T": (31, 4, 4, 4, 4, 4, 4),
    "U": (17, 17, 17, 17, 17, 17, 14),
    "V": (17, 17, 17, 17, 10, 10, 4),
    "W": (17, 17, 17, 21, 21, 27, 17),
    "X": (17, 17, 10, 4, 10, 17, 17),
    "Y": (17, 17, 10, 4, 4, 4, 4),
    "Z": (31, 1, 2, 4, 8, 16, 31),
    "0": (14, 17, 19, 21, 25, 17, 14),
    "1": (4, 12, 4, 4, 4, 4, 14),
    "2": (14, 17, 1, 2, 4, 8, 31),
    "3": (30, 1, 1, 14, 1, 1, 30),
    "4": (2, 6, 10, 18, 31, 2, 2),
    "5": (31, 16, 16, 30, 1, 1, 30),
    "6": (14, 16, 16, 30, 17, 17, 14),
    "7": (31, 1, 2, 4, 8, 8, 8),
    "8": (14, 17, 17, 14, 17, 17, 14),
    "9": (14, 17, 17, 15, 1, 1, 14),
    "-": (0, 0, 0, 31, 0, 0, 0),
    "+": (0, 4, 4, 31, 4, 4, 0),
    " ": (0, 0, 0, 0, 0, 0, 0),
    "_": (0, 0, 0, 0, 0, 0, 31),
    ".": (0, 0, 0, 0, 0, 6, 6),
    ",": (0, 0, 0, 0, 0, 6, 4),
    ":": (0, 6, 6, 0, 6, 6, 0),
    "/": (1, 2, 2, 4, 8, 8, 16),
    "(": (2, 4, 8, 8, 8, 4, 2),
    ")": (8, 4, 2, 2, 2, 4, 8),
    "[": (14, 8, 8, 8, 8, 8, 14),
    "]": (14, 2, 2, 2, 2, 2, 14),
    "<": (2, 4, 8, 16, 8, 4, 2),
    ">": (8, 4, 2, 1, 2, 4, 8),
    "=": (0, 0, 31, 0, 31, 0, 0),
    "%": (25, 25, 2, 4, 8, 19, 19),
    "?": (14, 17, 1, 2, 4, 0, 4),
    "|": (4, 4, 4, 4, 4, 4, 4),
}


def draw_text(
    canvas: np.ndarray,
    x: int,
    y: int,
    text: str,
    scale: int,
    max_width: int,
    color: tuple[int, int, int] = (232, 238, 246),
) -> None:
    """Draw uppercase ASCII text onto a top-down uint8 HxWx3 canvas."""
    h, w, _ = canvas.shape
    step_x = 6 * scale
    max_chars = max(1, (max_width - 4) // step_x)
    clean = text.upper()[:max_chars]
    for idx, ch in enumerate(clean):
        glyph = _GLYPHS.get(ch, _GLYPHS["?"])
        gx = x + idx * step_x
        for row_idx, bits in enumerate(glyph):
            py0 = y + row_idx * scale
            py1 = min(h, py0 + scale)
            if py0 >= h:
                break
            for col_idx in range(5):
                if bits & (16 >> col_idx):
                    px0 = gx + col_idx * scale
                    px1 = min(w, px0 + scale)
                    if px0 < w:
                        canvas[py0:py1, px0:px1] = color


def resize_rgb_bilinear(rgb: Any, target_h: int, target_w: int) -> Any:
    """Fast pure-NumPy bilinear resize for top-down uint8 HxWx3 images."""
    import numpy as np

    src_h, src_w, _ = rgb.shape
    if src_h == target_h and src_w == target_w:
        return rgb
    y = np.linspace(0.0, src_h - 1.0, target_h, dtype=np.float32)
    x = np.linspace(0.0, src_w - 1.0, target_w, dtype=np.float32)
    y0 = np.floor(y).astype(np.int32)
    x0 = np.floor(x).astype(np.int32)
    y1 = np.minimum(y0 + 1, src_h - 1)
    x1 = np.minimum(x0 + 1, src_w - 1)
    wy = (y - y0)[:, None, None]
    wx = (x - x0)[None, :, None]
    top = rgb[y0][:, x0] * (1.0 - wx) + rgb[y0][:, x1] * wx
    bot = rgb[y1][:, x0] * (1.0 - wx) + rgb[y1][:, x1] * wx
    out = top * (1.0 - wy) + bot * wy
    return np.clip(out + 0.5, 0.0, 255.0).astype(np.uint8)


def fit_tile_square(rgb: np.ndarray, cell_size: int) -> np.ndarray:
    """Center-crop rectangular region to square (or preserve full square) then resize."""
    h, w, _ = rgb.shape
    if h != w:
        side = min(h, w)
        y0 = (h - side) // 2
        x0 = (w - side) // 2
        rgb = rgb[y0 : y0 + side, x0 : x0 + side]
    return resize_rgb_bilinear(rgb, cell_size, cell_size)


def save_rgb_png(rgb_top_down: Any, dest: Path) -> dict[str, Any]:
    """Write a top-down uint8 RGB array to PNG atomically using Blender's image writer."""
    import bpy
    import numpy as np

    h, w, _ = rgb_top_down.shape
    # Blender Image pixels are bottom-to-top float32 RGBA
    rgba = np.ones((h, w, 4), dtype=np.float32)
    rgba[:, :, :3] = rgb_top_down[::-1].astype(np.float32) / 255.0

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.stem}.{os.getpid()}.tmp.png")
    img = bpy.data.images.new("_vp_export", w, h, alpha=False)
    try:
        img.file_format = "PNG"
        img.colorspace_settings.name = "sRGB"
        img.pixels.foreach_set(np.ascontiguousarray(rgba).ravel())
        img.filepath_raw = str(tmp)
        img.save()
    finally:
        bpy.data.images.remove(img)
    os.replace(tmp, dest)
    return {"file": str(dest), "width": int(w), "height": int(h), "bytes": int(dest.stat().st_size)}


def compose_board(
    tiles: list[dict[str, Any]],
    rows: int,
    cols: int,
    cell_size: int,
    banner_text: str,
    banner_accent: tuple[int, int, int] = (90, 175, 255),
    max_edge: int = 2048,
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """Assemble tiles with top summary banner and per-tile labels, capped to <= max_edge."""
    import numpy as np

    scale = 2 if cell_size >= 360 else 1
    tile_header = 7 * scale + 12
    banner_h = 7 * scale + 16
    gap = max(6, cell_size // 96)

    # Cap cell_size automatically if board would exceed max_edge (e.g. 3x3 at 1024px)
    est_w = cols * cell_size + (cols + 1) * gap
    est_h = banner_h + rows * (cell_size + tile_header) + (rows + 1) * gap
    if max(est_w, est_h) > max_edge:
        avail_w = (max_edge - (cols + 1) * gap) // cols
        avail_h = (max_edge - banner_h - (rows + 1) * gap - rows * tile_header) // rows
        cell_size = max(128, min(avail_w, avail_h))
        scale = 2 if cell_size >= 360 else 1
        tile_header = 7 * scale + 12
        banner_h = 7 * scale + 16

    board_w = cols * cell_size + (cols + 1) * gap
    board_h = banner_h + rows * (cell_size + tile_header) + (rows + 1) * gap

    canvas = np.empty((board_h, board_w, 3), dtype=np.uint8)
    canvas[:] = (22, 25, 31)  # Deep studio slate background

    # Top banner bar
    canvas[0:banner_h, :] = (31, 36, 45)
    canvas[banner_h - 2 : banner_h, :] = banner_accent
    draw_text(canvas, gap + 4, (banner_h - 7 * scale) // 2, banner_text, scale, board_w - 2 * gap)

    tile_rects: list[dict[str, Any]] = []
    for idx, tile in enumerate(tiles):
        r, c = divmod(idx, cols)
        x = gap + c * (cell_size + gap)
        y = banner_h + gap + r * (cell_size + tile_header + gap)

        # Header strip for tile
        canvas[y : y + tile_header, x : x + cell_size] = (34, 39, 49)
        header_label = tile.get("header", f"VIEW {idx + 1}")
        draw_text(
            canvas,
            x + 6,
            y + (tile_header - 7 * scale) // 2,
            header_label,
            scale,
            cell_size - 12,
        )

        # Tile pixels
        img_rgb = fit_tile_square(tile["rgb"], cell_size)
        canvas[y + tile_header : y + tile_header + cell_size, x : x + cell_size] = img_rgb

        # Optional badge tag in top-right of tile header (e.g. OPTIX FALLBACK)
        badge = tile.get("badge")
        if badge:
            b_scale = 1
            b_w = len(badge) * 6 * b_scale + 10
            bx = x + cell_size - b_w - 4
            if bx > x + cell_size // 2:
                by = y + max(2, (tile_header - 11) // 2)
                b_color = tile.get("badge_bg", (165, 92, 24))
                canvas[by : by + 11, bx : bx + b_w] = b_color
                draw_text(canvas, bx + 5, by + 2, badge, b_scale, b_w - 6, (255, 250, 240))

        tile_rects.append({
            "index": idx,
            "rect": [int(x), int(y + tile_header), int(cell_size), int(cell_size)],
            **tile.get("meta", {}),
        })

    return canvas, tile_rects
