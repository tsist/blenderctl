# SPDX-License-Identifier: GPL-3.0-or-later
"""Contact-sheet board composer with TrueType headers and 2048px cap."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


@lru_cache(maxsize=1)
def board_font():
    import blf

    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for filename in ("NotoSansSC-VF.ttf", "msyh.ttc", "Deng.ttf", "segoeui.ttf"):
        path = fonts / filename
        if path.is_file():
            font_id = blf.load(str(path))
            if font_id >= 0:
                return font_id, str(path)
    return 0, "Blender default font"


def draw_text(canvas, x, y, text, scale, max_width, color=(232, 238, 246)):
    """Rasterize Unicode text into a small region of the top-down canvas."""
    import blf
    import imbuf
    import numpy as np

    pixel_size = 11 if scale == 1 else 16
    width = max(0, min(max_width, canvas.shape[1] - x))
    height = min(pixel_size + 4, canvas.shape[0] - y)
    if width <= 0 or height <= 0:
        return
    font, _ = board_font()
    blf.size(font, pixel_size)
    if blf.dimensions(font, text)[0] > width:
        truncated = text
        while truncated and blf.dimensions(font, truncated + "…")[0] > width:
            truncated = truncated[:-1]
        text = truncated + "…"
    region = canvas[y:y + height, x:x + width]
    image = imbuf.new((width, height), planes=32, buffer_type="BYTE")
    try:
        with image.with_buffer(write=True) as buffer:
            raw = np.asarray(buffer)
            raw[:, :, :3] = region[::-1]
            raw[:, :, 3] = 255
        with blf.bind_imbuf(font, image):
            blf.color(font, *(v / 255 for v in color), 1.0)
            blf.position(font, 0, height - pixel_size, 0)
            blf.draw_buffer(font, text)
        with image.with_buffer() as buffer:
            region[:] = np.asarray(buffer)[::-1, :, :3]
    finally:
        image.free()


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


def draw_scale_bar(rgb, width, label):
    """Place the measured scale inside the square kept by the contact sheet."""
    height, image_width = rgb.shape[:2]
    side = min(height, image_width)
    if width > side - 48 or side < 96:
        return False
    x = (image_width - side) // 2 + 24
    y = (height + side) // 2 - 28
    rgb[y:y + 4, x:x + width] = (238, 243, 250)
    draw_text(rgb, x, y - 20, label, 2, max(90, width))
    return True


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
    tile_header = 8 * scale + 15
    banner_h = 8 * scale + 18
    gap = max(6, cell_size // 96)

    # Cap cell_size automatically if board would exceed max_edge (e.g. 3x3 at 1024px)
    est_w = cols * cell_size + (cols + 1) * gap
    est_h = banner_h + rows * (cell_size + tile_header) + (rows + 1) * gap
    if max(est_w, est_h) > max_edge:
        avail_w = (max_edge - (cols + 1) * gap) // cols
        avail_h = (max_edge - banner_h - (rows + 1) * gap - rows * tile_header) // rows
        cell_size = max(128, min(avail_w, avail_h))
        scale = 2 if cell_size >= 360 else 1
        tile_header = 8 * scale + 15
        banner_h = 8 * scale + 18

    board_w = cols * cell_size + (cols + 1) * gap
    board_h = banner_h + rows * (cell_size + tile_header) + (rows + 1) * gap

    canvas = np.empty((board_h, board_w, 3), dtype=np.uint8)
    canvas[:] = (22, 25, 31)  # Deep studio slate background

    # Top banner bar
    canvas[0:banner_h, :] = (31, 36, 45)
    canvas[banner_h - 2 : banner_h, :] = banner_accent
    draw_text(canvas, gap + 4, (banner_h - (11 if scale == 1 else 16)) // 2, banner_text, scale, board_w - 2 * gap)

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
            y + (tile_header - (11 if scale == 1 else 16)) // 2,
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
                by = y + max(2, (tile_header - 16) // 2)
                b_color = tile.get("badge_bg", (165, 92, 24))
                canvas[by : by + 16, bx : bx + b_w] = b_color
                draw_text(canvas, bx + 5, by + 1, badge, b_scale, b_w - 6, (255, 250, 240))

        tile_rects.append({
            "index": idx,
            "rect": [int(x), int(y + tile_header), int(cell_size), int(cell_size)],
            **tile.get("meta", {}),
        })

    return canvas, tile_rects
