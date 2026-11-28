# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate all 80 bespoke W3C SVG icons, rasterize true-alpha 32/64px PNGs,
and render the Blender AI 1.0 14-Plugin Visual Identity & Icon Gallery sheet.

Zero third-party Python/npm dependencies: uses system Chrome/Edge headless +
NumPy + stdlib zlib/struct PNG codec with premultiplied-alpha supersampling.
"""

from __future__ import annotations

import binascii
import html
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import zlib
from typing import Dict, List, Tuple

import numpy as np

UI_KIT_DIR = Path(__file__).resolve().parent
if str(UI_KIT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(UI_KIT_DIR.parent))

from ui_kit.theme_tokens import PLUGIN_ORDER, THEMES, PluginTheme


# ---------------------------------------------------------------------------
# Pure-Python + NumPy PNG RGBA Reader / Writer (all 5 PNG scanline filters)
# ---------------------------------------------------------------------------

def _paeth(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    a16 = a.astype(np.int32)
    b16 = b.astype(np.int32)
    c16 = c.astype(np.int32)
    p = a16 + b16 - c16
    pa = np.abs(p - a16)
    pb = np.abs(p - b16)
    pc = np.abs(p - c16)
    out = np.where((pa <= pb) & (pa <= pc), a16, np.where(pb <= pc, b16, c16))
    return out.astype(np.uint8)


def read_png_rgba(path: Path) -> np.ndarray:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Not a valid PNG file: {path}")
    pos = 8
    width = height = bit_depth = color_type = 0
    idat_chunks: List[bytes] = []
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, comp, flt, interlace = struct.unpack(">IIBBBBB", chunk)
            if bit_depth != 8 or interlace != 0:
                raise ValueError(f"Unsupported PNG format: depth={bit_depth}, interlace={interlace}")
        elif ctype == b"IDAT":
            idat_chunks.append(chunk)
        elif ctype == b"IEND":
            break

    bpp = {6: 4, 2: 3}.get(color_type)
    if bpp is None:
        raise ValueError(f"Unsupported PNG color type {color_type}; expected RGB(2) or RGBA(6)")

    raw = zlib.decompress(b"".join(idat_chunks))
    stride = width * bpp
    arr = np.frombuffer(raw, dtype=np.uint8).reshape((height, stride + 1))
    out = np.zeros((height, stride), dtype=np.uint8)
    prev = np.zeros(stride, dtype=np.uint8)

    for y in range(height):
        ftype = int(arr[y, 0])
        row = arr[y, 1:].copy()
        if ftype == 0:
            recon = row
        elif ftype == 1:  # Sub
            recon = row.copy()
            for x in range(bpp, stride):
                recon[x] = (int(recon[x]) + int(recon[x - bpp])) & 0xFF
        elif ftype == 2:  # Up
            recon = ((row.astype(np.uint16) + prev.astype(np.uint16)) & 0xFF).astype(np.uint8)
        elif ftype == 3:  # Average
            recon = row.copy()
            for x in range(stride):
                left = int(recon[x - bpp]) if x >= bpp else 0
                up = int(prev[x])
                recon[x] = (int(recon[x]) + ((left + up) >> 1)) & 0xFF
        elif ftype == 4:  # Paeth
            recon = row.copy()
            for x in range(stride):
                a = int(recon[x - bpp]) if x >= bpp else 0
                b = int(prev[x])
                c = int(prev[x - bpp]) if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                recon[x] = (int(recon[x]) + pr) & 0xFF
        else:
            raise ValueError(f"Unknown PNG filter type: {ftype}")
        out[y] = recon
        prev = recon

    img = out.reshape((height, width, bpp))
    if bpp == 3:
        alpha = np.full((height, width, 1), 255, dtype=np.uint8)
        img = np.concatenate([img, alpha], axis=2)
    return img


def write_png_rgba(path: Path, rgba: np.ndarray) -> None:
    assert rgba.ndim == 3 and rgba.shape[2] == 4 and rgba.dtype == np.uint8
    h, w, _ = rgba.shape
    raw_rows = np.concatenate([np.zeros((h, 1), dtype=np.uint8), rgba.reshape(h, w * 4)], axis=1)
    compressed = zlib.compress(raw_rows.tobytes(), level=9)

    def _chunk(ctype: bytes, payload: bytes) -> bytes:
        crc = binascii.crc32(ctype + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", compressed) + _chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def downsample_2x_premultiplied(rgba: np.ndarray) -> np.ndarray:
    """Premultiplied-alpha 2x2 box filter for zero-fringe crisp 64->32 downsampling."""
    h, w, _ = rgba.shape
    f = rgba.astype(np.float32) / 255.0
    a = f[:, :, 3:4]
    rgb_pre = f[:, :, :3] * a
    pre = np.concatenate([rgb_pre, a], axis=2)
    pooled = pre.reshape(h // 2, 2, w // 2, 2, 4).mean(axis=(1, 3))
    out_a = pooled[:, :, 3:4]
    safe_a = np.where(out_a > 1e-6, out_a, 1.0)
    out_rgb = np.where(out_a > 1e-6, pooled[:, :, :3] / safe_a, 0.0)
    out = np.concatenate([np.clip(out_rgb, 0.0, 1.0), np.clip(out_a, 0.0, 1.0)], axis=2)
    return np.round(out * 255.0).astype(np.uint8)


# ---------------------------------------------------------------------------
# Brand SVG Designs (14 Unique Domain Silhouettes + High-Contrast Monogram)
# ---------------------------------------------------------------------------

def _brand_svg(theme: PluginTheme, glyph_body: str) -> str:
    c = theme.hex_color
    cd = theme.hex_dark
    cl = theme.hex_light
    bt = theme.badge_text_hex
    uid = theme.domain.replace("-", "_")
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <defs>
    <linearGradient id="bg_{uid}" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#222733"/>
      <stop offset="100%" stop-color="#141820"/>
    </linearGradient>
    <radialGradient id="glow_{uid}" cx="30%" cy="25%" r="72%">
      <stop offset="0%" stop-color="{c}" stop-opacity="0.28"/>
      <stop offset="100%" stop-color="{cd}" stop-opacity="0.02"/>
    </radialGradient>
    <linearGradient id="badge_{uid}" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="{cl}"/>
      <stop offset="35%" stop-color="{c}"/>
      <stop offset="100%" stop-color="{c}"/>
    </linearGradient>
  </defs>
  <rect x="2.5" y="2.5" width="59" height="59" rx="13.5" fill="url(#bg_{uid})" stroke="{c}" stroke-width="2.4" stroke-opacity="0.92"/>
  <rect x="3.8" y="3.8" width="56.4" height="56.4" rx="12.2" fill="url(#glow_{uid})"/>
  <g stroke-linecap="round" stroke-linejoin="round">
    {glyph_body}
  </g>
  <rect x="34" y="34" width="28" height="28" rx="8" fill="#11141B"/>
  <rect x="36.2" y="36.2" width="23.6" height="23.6" rx="6.2" fill="url(#badge_{uid})"/>
  <text x="48" y="53.6" text-anchor="middle" font-family="Segoe UI, Arial Black, sans-serif" font-weight="900" font-size="16.5" fill="{bt}">{theme.code}</text>
</svg>'''


def _brand_glyphs() -> Dict[str, str]:
    def g(domain: str):
        t = THEMES[domain]
        return t.hex_color, t.hex_light, t.hex_dark

    m_c, m_l, _ = g("modeling")
    h_c, h_l, _ = g("hard-surface")
    g_c, g_l, _ = g("geometry")
    u_c, u_l, _ = g("uv")
    s_c, s_l, _ = g("material")
    r_c, r_l, _ = g("rigging")
    a_c, a_l, _ = g("animation")
    p_c, p_l, _ = g("physics")
    c_c, c_l, _ = g("camera")
    l_c, l_l, _ = g("lighting")
    t_c, t_l, _ = g("toon")
    b_c, b_l, _ = g("render-batch")
    f_c, f_l, _ = g("compositing")
    v_c, v_l, _ = g("video")

    return {
        # M · Modeling: Isometric 3D wireframe cube + crisp vertical import arrow
        "modeling": f'''
          <polygon points="27,13 43,21 27,29 11,21" fill="{m_c}" fill-opacity="0.32" stroke="{m_l}" stroke-width="2.4"/>
          <polygon points="11,21 27,29 27,46 11,38" fill="{m_c}" fill-opacity="0.16" stroke="#E2E8F0" stroke-width="2.4"/>
          <polyline points="27,46 43,38 43,21" fill="none" stroke="#CBD5E1" stroke-width="2.2" stroke-dasharray="3,2.5"/>
          <line x1="27" y1="8" x2="27" y2="22" stroke="{m_c}" stroke-width="3.0"/>
          <polyline points="22.5,17.5 27,22.5 31.5,17.5" fill="none" stroke="{m_l}" stroke-width="2.6"/>
        ''',
        # H · Hard Surface: 45° beveled mechanical block with precision boolean cut notch
        "hard-surface": f'''
          <polygon points="17,11 40,11 47,18 47,36 39,45 14,45 11,39 11,17" fill="{h_c}" fill-opacity="0.22" stroke="#F1F5F9" stroke-width="2.6"/>
          <polygon points="24,11 40,11 47,18 33,26 24,20" fill="{h_c}" fill-opacity="0.45" stroke="{h_l}" stroke-width="2.1"/>
          <path d="M17,28 L29,28 L33,34 L17,34 Z" fill="#141820" stroke="{h_c}" stroke-width="2.2"/>
          <circle cx="19" cy="18.5" r="2.2" fill="{h_l}"/>
        ''',
        # G · Geometry: Procedural diamond node graph & branching scatter points
        "geometry": f'''
          <path d="M15,36 C21,36 22,22 30,22 C36,22 38,16 44,16" fill="none" stroke="{g_c}" stroke-width="2.8"/>
          <path d="M15,20 C23,20 24,33 34,33" fill="none" stroke="{g_l}" stroke-width="2.3" stroke-opacity="0.85"/>
          <polygon points="30,14 37,22 30,30 23,22" fill="{g_c}" fill-opacity="0.35" stroke="#F8FAFC" stroke-width="2.4"/>
          <circle cx="14.5" cy="20" r="3.6" fill="#F8FAFC" stroke="{g_c}" stroke-width="2.2"/>
          <circle cx="14.5" cy="36" r="3.6" fill="{g_c}" stroke="#F8FAFC" stroke-width="2.0"/>
          <circle cx="44" cy="16" r="3.8" fill="{g_l}" stroke="{g_c}" stroke-width="2.2"/>
        ''',
        # U · UV: Unfolded 2x2 UV checkerboard grid with seam cross highlight
        "uv": f'''
          <rect x="12" y="12" width="28" height="28" rx="3" fill="#181C24" stroke="#F8FAFC" stroke-width="2.5"/>
          <rect x="13.2" y="13.2" width="12.8" height="12.8" fill="{u_c}" fill-opacity="0.85"/>
          <rect x="26" y="26" width="12.8" height="12.8" fill="{u_c}" fill-opacity="0.55"/>
          <line x1="26" y1="12" x2="26" y2="40" stroke="{u_l}" stroke-width="2.2"/>
          <line x1="12" y1="26" x2="40" y2="26" stroke="{u_l}" stroke-width="2.2"/>
          <polyline points="40,14 48,17 48,33 40,36" fill="{u_c}" fill-opacity="0.22" stroke="{u_l}" stroke-width="2.2"/>
        ''',
        # S · Material (Shader): PBR glossy sphere + layered material cards behind
        "material": f'''
          <polygon points="12,35 28,28 44,35 28,43" fill="{s_c}" fill-opacity="0.22" stroke="{s_l}" stroke-width="2.0"/>
          <circle cx="27" cy="25" r="14" fill="{s_c}" fill-opacity="0.28" stroke="#F8FAFC" stroke-width="2.6"/>
          <path d="M18,22 A10,10 0 0 1 28,14" fill="none" stroke="#FFFFFF" stroke-width="3.0"/>
          <circle cx="22.5" cy="19.5" r="2.8" fill="{s_l}"/>
          <path d="M16,32 Q27,37 38,31" fill="none" stroke="{s_c}" stroke-width="2.4"/>
        ''',
        # R · Rigging: Articulated octahedral armature bone chain + joint controller rings
        "rigging": f'''
          <polygon points="16,38 22,22 28,25 22,38" fill="{r_c}" fill-opacity="0.38" stroke="#F8FAFC" stroke-width="2.3"/>
          <polygon points="24,22 41,13 43,18 28,26" fill="{r_c}" fill-opacity="0.55" stroke="{r_l}" stroke-width="2.3"/>
          <circle cx="25" cy="23.5" r="4.8" fill="#141820" stroke="{r_c}" stroke-width="2.8"/>
          <circle cx="18.5" cy="39" r="3.6" fill="{r_l}" stroke="#F8FAFC" stroke-width="2.0"/>
          <circle cx="43.5" cy="14.5" r="3.6" fill="#F8FAFC" stroke="{r_c}" stroke-width="2.2"/>
        ''',
        # A · Animation: Smooth Bézier motion F-Curve with glowing diamond keyframes
        "animation": f'''
          <path d="M11,40 C19,40 20,16 32,16 C40,16 42,27 48,27" fill="none" stroke="{a_c}" stroke-width="3.2"/>
          <line x1="22" y1="16" x2="42" y2="16" stroke="{a_l}" stroke-width="1.8" stroke-dasharray="2.5,2"/>
          <circle cx="22" cy="16" r="2.2" fill="{a_l}"/>
          <circle cx="42" cy="16" r="2.2" fill="{a_l}"/>
          <polygon points="32,10.5 37.5,16 32,21.5 26.5,16" fill="{a_c}" stroke="#FFFFFF" stroke-width="2.2"/>
          <polygon points="13,34.5 18.5,40 13,45.5 7.5,40" fill="#F8FAFC" stroke="{a_c}" stroke-width="2.2"/>
        ''',
        # P · Physics: Kinetic impact wave arcs + fractured trajectory sparks
        "physics": f'''
          <path d="M11,40 Q26,35 42,40" fill="none" stroke="{p_l}" stroke-width="2.4" stroke-opacity="0.7"/>
          <path d="M13,35 Q23,13 38,24" fill="none" stroke="{p_c}" stroke-width="2.8" stroke-dasharray="4,3"/>
          <circle cx="24" cy="21" r="6.8" fill="{p_c}" fill-opacity="0.42" stroke="#F8FAFC" stroke-width="2.6"/>
          <polygon points="37,12 43,14 40,20 35,17" fill="{p_l}" stroke="{p_c}" stroke-width="1.8"/>
          <circle cx="15" cy="16" r="2.6" fill="{p_l}"/>
          <circle cx="33" cy="31" r="2.4" fill="{p_c}"/>
        ''',
        # C · Camera: Cinema viewfinder corner brackets + optical lens iris
        "camera": f'''
          <path d="M11,21 L11,13 L20,13" fill="none" stroke="{c_c}" stroke-width="2.8"/>
          <path d="M44,13 L48,13 L48,21" fill="none" stroke="{c_c}" stroke-width="2.8"/>
          <path d="M11,33 L11,41 L20,41" fill="none" stroke="{c_c}" stroke-width="2.8"/>
          <circle cx="29" cy="27" r="10.5" fill="{c_c}" fill-opacity="0.22" stroke="#F8FAFC" stroke-width="2.5"/>
          <circle cx="29" cy="27" r="4.5" fill="{c_l}"/>
          <circle cx="37" cy="17.5" r="2.2" fill="#EF4444"/>
        ''',
        # L · Lighting: Studio lamp housing + golden volumetric spotlight cone & Kelvin rays
        "lighting": f'''
          <polygon points="27,16 11,43 43,43" fill="{l_c}" fill-opacity="0.24"/>
          <line x1="22" y1="22" x2="12" y2="42" stroke="{l_l}" stroke-width="2.2" stroke-opacity="0.85"/>
          <line x1="32" y1="22" x2="41" y2="40" stroke="{l_l}" stroke-width="2.2" stroke-opacity="0.85"/>
          <line x1="27" y1="24" x2="27" y2="43" stroke="{l_c}" stroke-width="2.2" stroke-dasharray="3,2.5"/>
          <path d="M18,19 C18,12.5 22,10 27,10 C32,10 36,12.5 36,19 Z" fill="{l_c}" stroke="#FFFFFF" stroke-width="2.4"/>
          <line x1="12" y1="14" x2="15" y2="16" stroke="{l_l}" stroke-width="2.3"/>
          <line x1="42" y1="14" x2="39" y2="16" stroke="{l_l}" stroke-width="2.3"/>
        ''',
        # T · Toon: Crisp 2-tone cel-shaded sphere + expressive ink pen contour arc
        "toon": f'''
          <circle cx="27" cy="26" r="14.5" fill="#F8FAFC" stroke="#11141B" stroke-width="2.2"/>
          <path d="M27,11.5 A14.5,14.5 0 0 1 27,40.5 C32,35 32,17 27,11.5 Z" fill="{t_c}"/>
          <circle cx="27" cy="26" r="14.5" fill="none" stroke="{t_l}" stroke-width="2.4"/>
          <circle cx="21" cy="20" r="3.1" fill="{t_c}" fill-opacity="0.8"/>
          <path d="M10,36 C16,45 31,48 42,41" fill="none" stroke="{t_c}" stroke-width="3.2"/>
        ''',
        # B · Render Batch: Stacked multi-layer EXR frames + high-speed lightning bolt
        "render-batch": f'''
          <rect x="11" y="21" width="24" height="19" rx="3" fill="{b_c}" fill-opacity="0.18" stroke="{b_l}" stroke-width="2.0"/>
          <rect x="16" y="16" width="24" height="19" rx="3" fill="{b_c}" fill-opacity="0.28" stroke="#E2E8F0" stroke-width="2.2"/>
          <rect x="21" y="11" width="24" height="19" rx="3" fill="#181C24" stroke="#FFFFFF" stroke-width="2.4"/>
          <polygon points="34,13.5 27.5,22 33,22 30.5,28.5 39,19.5 33.5,19.5" fill="{b_c}" stroke="{b_l}" stroke-width="1.2"/>
        ''',
        # F · Compositing (FX): Overlapping multi-pass diamond layers + optical bloom star
        "compositing": f'''
          <polygon points="14,29 28,21 42,29 28,37" fill="{f_c}" fill-opacity="0.28" stroke="{f_l}" stroke-width="2.2"/>
          <polygon points="14,21 28,13 42,21 28,29" fill="{f_c}" fill-opacity="0.48" stroke="#FFFFFF" stroke-width="2.4"/>
          <path d="M41,10 L42.8,14.2 L47,16 L42.8,17.8 L41,22 L39.2,17.8 L35,16 L39.2,14.2 Z" fill="#FFFFFF" stroke="{f_c}" stroke-width="1.2"/>
        ''',
        # V · Video: Multi-track sequencer timeline bars + playhead + loudness waveform
        "video": f'''
          <rect x="11" y="14" width="22" height="8" rx="2.5" fill="{v_c}" fill-opacity="0.75" stroke="#FFFFFF" stroke-width="1.8"/>
          <rect x="22" y="25" width="23" height="8" rx="2.5" fill="{v_c}" fill-opacity="0.38" stroke="{v_l}" stroke-width="2.0"/>
          <line x1="13" y1="39" x2="13" y2="43" stroke="{v_l}" stroke-width="2.8"/>
          <line x1="18" y1="36" x2="18" y2="44" stroke="{v_c}" stroke-width="2.8"/>
          <line x1="23" y1="38" x2="23" y2="43" stroke="{v_l}" stroke-width="2.8"/>
          <line x1="28" y1="35" x2="28" y2="45" stroke="{v_c}" stroke-width="2.8"/>
          <line x1="25" y1="9" x2="25" y2="46" stroke="#FFFFFF" stroke-width="2.4"/>
          <polygon points="21,9 29,9 25,14" fill="{v_l}"/>
        ''',
    }


# ---------------------------------------------------------------------------
# Action & Sub-Module Icons (60 Domain Icons + 6 Status Icons)
# ---------------------------------------------------------------------------

def _action_svg(c: str, cl: str, body: str) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <g stroke-linecap="round" stroke-linejoin="round" fill="none">
    {body.format(c=c, cl=cl, w="#F1F5F9")}
  </g>
</svg>'''


DOMAIN_ACTIONS: Dict[str, List[Tuple[str, str, str]]] = {
    "modeling": [
        ("import_mesh", "导入旋正", '<polygon points="32,20 52,30 32,40 12,30" fill="{c}" fill-opacity="0.28" stroke="{w}" stroke-width="3.6"/><polyline points="12,30 12,48 32,58 52,48 52,30" stroke="{w}" stroke-width="3.4"/><line x1="32" y1="6" x2="32" y2="27" stroke="{c}" stroke-width="4.4"/><polyline points="23,19 32,28 41,19" stroke="{cl}" stroke-width="4.0"/>'),
        ("ref_stage", "四视参考台", '<rect x="10" y="10" width="44" height="44" rx="5" stroke="{c}" stroke-width="3.6" fill="{c}" fill-opacity="0.16"/><line x1="32" y1="10" x2="32" y2="54" stroke="{w}" stroke-width="3.0"/><line x1="10" y1="32" x2="54" y2="32" stroke="{w}" stroke-width="3.0"/><circle cx="21" cy="21" r="4.5" fill="{cl}"/><rect x="38" y="38" width="9" height="9" fill="{c}"/>'),
        ("retopo", "保真重拓", '<rect x="11" y="11" width="42" height="42" rx="6" fill="{c}" fill-opacity="0.2" stroke="{w}" stroke-width="3.4"/><line x1="25" y1="11" x2="25" y2="53" stroke="{c}" stroke-width="3.2"/><line x1="39" y1="11" x2="39" y2="53" stroke="{c}" stroke-width="3.2"/><line x1="11" y1="25" x2="53" y2="25" stroke="{cl}" stroke-width="3.2"/><line x1="11" y1="39" x2="53" y2="39" stroke="{cl}" stroke-width="3.2"/>'),
        ("export_exchange", "商业导出", '<rect x="10" y="24" width="32" height="30" rx="5" fill="{c}" fill-opacity="0.22" stroke="{w}" stroke-width="3.6"/><line x1="28" y1="36" x2="52" y2="12" stroke="{c}" stroke-width="4.4"/><polyline points="37,12 52,12 52,27" stroke="{cl}" stroke-width="4.0"/>'),
    ],
    "hard-surface": [
        ("cutter_bool", "刀体布尔", '<rect x="10" y="18" width="32" height="32" rx="4" fill="{c}" fill-opacity="0.25" stroke="{w}" stroke-width="3.6"/><rect x="26" y="10" width="28" height="28" rx="4" fill="#181C24" stroke="{c}" stroke-width="3.8" stroke-dasharray="6,4"/>'),
        ("surface_sweep", "贴面扫掠", '<path d="M12,48 C16,22 36,42 52,16" stroke="{c}" stroke-width="6.5"/><path d="M12,48 C16,22 36,42 52,16" stroke="{cl}" stroke-width="2.4"/><circle cx="12" cy="48" r="4.5" fill="{w}"/><circle cx="32" cy="32" r="4.5" fill="{w}"/><circle cx="52" cy="16" r="4.5" fill="{w}"/>'),
        ("modifier_stack", "修改器栈", '<rect x="12" y="10" width="40" height="11" rx="3.5" fill="{c}" fill-opacity="0.65" stroke="{w}" stroke-width="3.0"/><rect x="12" y="26.5" width="40" height="11" rx="3.5" fill="{c}" fill-opacity="0.35" stroke="{cl}" stroke-width="3.0"/><rect x="12" y="43" width="40" height="11" rx="3.5" fill="{c}" fill-opacity="0.18" stroke="{w}" stroke-width="3.0"/>'),
        ("face_flow", "面流检查", '<path d="M10,20 Q32,8 54,20" stroke="{cl}" stroke-width="3.6"/><path d="M10,33 Q32,21 54,33" stroke="{c}" stroke-width="4.2"/><path d="M10,46 Q32,34 54,46" stroke="{w}" stroke-width="3.6"/>'),
        ("review_grid", "多视检查", '<rect x="10" y="10" width="19" height="19" rx="3" fill="{c}" fill-opacity="0.45" stroke="{w}" stroke-width="3.0"/><rect x="35" y="10" width="19" height="19" rx="3" stroke="{cl}" stroke-width="3.0"/><rect x="10" y="35" width="19" height="19" rx="3" stroke="{cl}" stroke-width="3.0"/><rect x="35" y="35" width="19" height="19" rx="3" fill="{c}" fill-opacity="0.25" stroke="{w}" stroke-width="3.0"/>'),
    ],
    "geometry": [
        ("biome_scatter", "生态散布", '<path d="M8,48 Q32,36 56,48" stroke="{w}" stroke-width="3.6"/><polygon points="22,14 14,36 30,36" fill="{c}" fill-opacity="0.7" stroke="{cl}" stroke-width="2.8"/><polygon points="42,20 35,38 49,38" fill="{c}" fill-opacity="0.45" stroke="{w}" stroke-width="2.8"/><circle cx="32" cy="14" r="3.5" fill="{cl}"/>'),
        ("hair_curves", "毛发曲线", '<path d="M16,52 C14,30 22,18 32,10" stroke="{w}" stroke-width="3.6"/><path d="M28,54 C28,32 34,22 44,12" stroke="{c}" stroke-width="4.0"/><path d="M40,52 C42,36 46,26 52,18" stroke="{cl}" stroke-width="3.4"/>'),
        ("node_params", "节点参数", '<circle cx="16" cy="20" r="5" fill="{c}" stroke="{w}" stroke-width="3.0"/><line x1="21" y1="20" x2="52" y2="20" stroke="{w}" stroke-width="3.4"/><circle cx="42" cy="38" r="5" fill="{cl}" stroke="{c}" stroke-width="3.0"/><line x1="12" y1="38" x2="37" y2="38" stroke="{c}" stroke-width="3.4"/><line x1="47" y1="38" x2="52" y2="38" stroke="{w}" stroke-width="3.4"/>'),
        ("bake_cache", "烘焙缓存", '<ellipse cx="32" cy="18" rx="20" ry="8" fill="{c}" fill-opacity="0.4" stroke="{w}" stroke-width="3.4"/><path d="M12,18 L12,46 A20,8 0 0 0 52,46 L52,18" stroke="{w}" stroke-width="3.4"/><path d="M12,32 A20,8 0 0 0 52,32" stroke="{cl}" stroke-width="3.0"/>'),
    ],
    "uv": [
        ("unwrap_seam", "接缝展开", '<rect x="12" y="14" width="40" height="36" rx="4" fill="{c}" fill-opacity="0.22" stroke="{w}" stroke-width="3.4"/><line x1="12" y1="32" x2="52" y2="32" stroke="{c}" stroke-width="4.2" stroke-dasharray="6,4"/><circle cx="24" cy="23" r="3.5" fill="{cl}"/><circle cx="40" cy="41" r="3.5" fill="{cl}"/>'),
        ("udim_pack", "UDIM排布", '<rect x="8" y="16" width="21" height="32" rx="3" fill="{c}" fill-opacity="0.45" stroke="{w}" stroke-width="3.2"/><rect x="35" y="16" width="21" height="32" rx="3" fill="{c}" fill-opacity="0.22" stroke="{cl}" stroke-width="3.2"/><line x1="18.5" y1="16" x2="18.5" y2="48" stroke="{w}" stroke-width="2.2"/><line x1="35" y1="32" x2="56" y2="32" stroke="{cl}" stroke-width="2.2"/>'),
        ("texel_rectify", "密度拉直", '<rect x="12" y="14" width="40" height="36" rx="3" stroke="{w}" stroke-width="3.4"/><line x1="12" y1="26" x2="52" y2="26" stroke="{c}" stroke-width="3.0"/><line x1="12" y1="38" x2="52" y2="38" stroke="{c}" stroke-width="3.0"/><line x1="26" y1="14" x2="26" y2="50" stroke="{cl}" stroke-width="3.0"/><line x1="38" y1="14" x2="38" y2="50" stroke="{cl}" stroke-width="3.0"/>'),
        ("checker_preview", "棋盘预览", '<rect x="12" y="12" width="40" height="40" rx="5" stroke="{w}" stroke-width="3.4"/><rect x="14" y="14" width="18" height="18" fill="{c}"/><rect x="32" y="32" width="18" height="18" fill="{c}" fill-opacity="0.65"/>'),
    ],
    "material": [
        ("layer_stack", "图层堆栈", '<polygon points="32,10 54,20 32,30 10,20" fill="{c}" fill-opacity="0.7" stroke="{w}" stroke-width="3.0"/><polyline points="10,32 32,42 54,32" stroke="{cl}" stroke-width="3.6"/><polyline points="10,44 32,54 54,44" stroke="{w}" stroke-width="3.6"/>'),
        ("procedural_pbr", "程序化PBR", '<circle cx="32" cy="32" r="20" fill="{c}" fill-opacity="0.28" stroke="{w}" stroke-width="3.6"/><path d="M20,25 A13,13 0 0 1 34,16" stroke="#FFFFFF" stroke-width="4.0"/><circle cx="24" cy="23" r="3.5" fill="{cl}"/>'),
        ("gpu_bake", "GPU烘焙", '<rect x="14" y="14" width="36" height="36" rx="5" fill="{c}" fill-opacity="0.25" stroke="{w}" stroke-width="3.4"/><polygon points="34,19 23,34 32,34 29,46 42,30 33,30" fill="{c}" stroke="{cl}" stroke-width="2.0"/>'),
        ("batch_assign", "批量分配", '<circle cx="20" cy="20" r="8" fill="{c}" fill-opacity="0.7" stroke="{w}" stroke-width="3.0"/><circle cx="44" cy="20" r="8" stroke="{cl}" stroke-width="3.0"/><circle cx="20" cy="44" r="8" stroke="{cl}" stroke-width="3.0"/><circle cx="44" cy="44" r="8" fill="{c}" fill-opacity="0.4" stroke="{w}" stroke-width="3.0"/>'),
    ],
    "rigging": [
        ("rig_prepare", "骨架定位", '<circle cx="32" cy="14" r="6" fill="{c}" stroke="{w}" stroke-width="3.0"/><line x1="32" y1="20" x2="32" y2="42" stroke="{w}" stroke-width="3.8"/><line x1="14" y1="28" x2="50" y2="28" stroke="{cl}" stroke-width="3.6"/><line x1="32" y1="42" x2="20" y2="54" stroke="{w}" stroke-width="3.6"/><line x1="32" y1="42" x2="44" y2="54" stroke="{w}" stroke-width="3.6"/>'),
        ("voxel_skin", "体素蒙皮", '<polygon points="32,10 52,22 52,44 32,54 12,44 12,22" fill="{c}" fill-opacity="0.25" stroke="{w}" stroke-width="3.4"/><circle cx="32" cy="32" r="9" fill="{c}" fill-opacity="0.8" stroke="{cl}" stroke-width="2.8"/>'),
        ("ik_fk_control", "IK/FK切换", '<circle cx="18" cy="46" r="6" fill="{c}" stroke="{w}" stroke-width="3.0"/><circle cx="46" cy="18" r="6" fill="{cl}" stroke="{c}" stroke-width="3.0"/><path d="M18,36 L18,22 L34,22" stroke="{w}" stroke-width="3.6"/><polyline points="28,16 35,22 28,28" stroke="{c}" stroke-width="3.4"/>'),
        ("face_rig", "面部表情", '<circle cx="32" cy="32" r="20" fill="{c}" fill-opacity="0.2" stroke="{w}" stroke-width="3.4"/><circle cx="24" cy="27" r="3" fill="{cl}"/><circle cx="40" cy="27" r="3" fill="{cl}"/><path d="M22,39 Q32,47 42,39" stroke="{c}" stroke-width="3.8"/>'),
        ("retarget_motion", "动作重定向", '<path d="M12,32 C22,14 42,50 52,32" stroke="{c}" stroke-width="4.0"/><circle cx="14" cy="32" r="5" fill="{w}"/><polyline points="43,25 52,32 43,39" stroke="{cl}" stroke-width="3.6"/>'),
    ],
    "animation": [
        ("curve_filter", "曲线滤波", '<path d="M8,42 C18,42 20,16 32,16 C44,16 46,42 56,42" stroke="{c}" stroke-width="4.2"/><polygon points="32,10 38,16 32,22 26,16" fill="{w}" stroke="{c}" stroke-width="2.4"/>'),
        ("additive_layer", "加法修正层", '<rect x="12" y="34" width="40" height="16" rx="4" fill="{c}" fill-opacity="0.3" stroke="{w}" stroke-width="3.2"/><line x1="32" y1="12" x2="32" y2="28" stroke="{c}" stroke-width="4.2"/><line x1="24" y1="20" x2="40" y2="20" stroke="{cl}" stroke-width="4.2"/>'),
        ("event_beat", "节拍事件", '<line x1="10" y1="46" x2="54" y2="46" stroke="{w}" stroke-width="3.2"/><line x1="18" y1="24" x2="18" y2="46" stroke="{cl}" stroke-width="3.4"/><line x1="32" y1="12" x2="32" y2="46" stroke="{c}" stroke-width="4.0"/><line x1="46" y1="20" x2="46" y2="46" stroke="{cl}" stroke-width="3.4"/><circle cx="32" cy="12" r="4.5" fill="{c}"/>'),
        ("foot_support", "足底支撑", '<line x1="10" y1="48" x2="54" y2="48" stroke="{c}" stroke-width="4.0"/><path d="M22,14 L22,38 L44,44 L18,44 Z" fill="{c}" fill-opacity="0.35" stroke="{w}" stroke-width="3.2"/>'),
        ("cascadeur_bridge", "物理润色桥", '<path d="M12,44 Q32,8 52,44" stroke="{c}" stroke-width="3.8" stroke-dasharray="6,4"/><circle cx="32" cy="18" r="6" fill="{c}" stroke="{w}" stroke-width="3.0"/><circle cx="14" cy="44" r="4" fill="{cl}"/><circle cx="50" cy="44" r="4" fill="{cl}"/>'),
    ],
    "physics": [
        ("gn_physics", "GN物理", '<circle cx="32" cy="32" r="10" fill="{c}" fill-opacity="0.6" stroke="{w}" stroke-width="3.2"/><circle cx="32" cy="32" r="21" stroke="{cl}" stroke-width="3.0" stroke-dasharray="7,5"/>'),
        ("cloth_sim", "布料服装", '<path d="M14,14 C24,20 40,20 50,14 L54,48 C42,42 22,54 10,48 Z" fill="{c}" fill-opacity="0.32" stroke="{w}" stroke-width="3.4"/><path d="M26,18 L24,49" stroke="{cl}" stroke-width="2.6"/><path d="M39,18 L41,46" stroke="{cl}" stroke-width="2.6"/>'),
        ("bullet_fracture", "刚体破碎", '<polygon points="12,18 30,10 34,28 16,32" fill="{c}" fill-opacity="0.55" stroke="{w}" stroke-width="3.0"/><polygon points="38,14 54,22 48,38 36,30" fill="{c}" fill-opacity="0.35" stroke="{cl}" stroke-width="3.0"/><polygon points="18,38 36,36 42,54 22,52" fill="{c}" fill-opacity="0.25" stroke="{w}" stroke-width="3.0"/>'),
        ("fluid_domain", "流体烟域", '<rect x="10" y="12" width="44" height="40" rx="5" stroke="{w}" stroke-width="3.2" stroke-dasharray="6,4"/><path d="M12,38 Q22,28 32,38 T52,38 L52,50 L12,50 Z" fill="{c}" fill-opacity="0.65" stroke="{cl}" stroke-width="2.6"/>'),
    ],
    "camera": [
        ("shot_table", "镜头表", '<rect x="10" y="12" width="44" height="40" rx="5" fill="{c}" fill-opacity="0.18" stroke="{w}" stroke-width="3.4"/><line x1="10" y1="24" x2="54" y2="24" stroke="{c}" stroke-width="3.2"/><line x1="10" y1="38" x2="54" y2="38" stroke="{cl}" stroke-width="2.8"/><line x1="25" y1="24" x2="25" y2="52" stroke="{w}" stroke-width="2.8"/>'),
        ("proc_motion", "程序运镜", '<path d="M12,44 C12,18 52,18 52,44" stroke="{c}" stroke-width="3.8"/><polyline points="44,37 52,45 56,35" stroke="{cl}" stroke-width="3.4"/><circle cx="32" cy="24" r="6" fill="{c}" stroke="{w}" stroke-width="3.0"/>'),
        ("framing_guides", "构图安全框", '<rect x="10" y="14" width="44" height="36" rx="3" stroke="{w}" stroke-width="3.4"/><line x1="24.5" y1="14" x2="24.5" y2="50" stroke="{c}" stroke-width="2.6"/><line x1="39.5" y1="14" x2="39.5" y2="50" stroke="{c}" stroke-width="2.6"/><line x1="10" y1="26" x2="54" y2="26" stroke="{cl}" stroke-width="2.6"/><line x1="10" y1="38" x2="54" y2="38" stroke="{cl}" stroke-width="2.6"/>'),
        ("focus_dof", "景深焦点", '<circle cx="32" cy="32" r="18" stroke="{w}" stroke-width="3.4"/><circle cx="32" cy="32" r="8" fill="{c}" fill-opacity="0.55" stroke="{cl}" stroke-width="2.8"/><line x1="32" y1="8" x2="32" y2="16" stroke="{c}" stroke-width="3.4"/><line x1="32" y1="48" x2="32" y2="56" stroke="{c}" stroke-width="3.4"/>'),
    ],
    "lighting": [
        ("create_rig", "一键布光", '<polygon points="32,18 14,50 50,50" fill="{c}" fill-opacity="0.28"/><path d="M22,22 C22,13 26,10 32,10 C38,10 42,13 42,22 Z" fill="{c}" stroke="{w}" stroke-width="3.2"/><line x1="32" y1="26" x2="32" y2="48" stroke="{cl}" stroke-width="3.4"/><line x1="21" y1="37" x2="43" y2="37" stroke="{w}" stroke-width="3.2"/>'),
        ("kelvin_ies", "色温与IES", '<circle cx="32" cy="24" r="11" fill="{c}" fill-opacity="0.4" stroke="{w}" stroke-width="3.4"/><path d="M20,38 Q32,54 44,38" fill="{c}" fill-opacity="0.35" stroke="{cl}" stroke-width="3.4"/><line x1="32" y1="7" x2="32" y2="10" stroke="{c}" stroke-width="3.6"/><line x1="14" y1="24" x2="17" y2="24" stroke="{cl}" stroke-width="3.6"/><line x1="47" y1="24" x2="50" y2="24" stroke="{cl}" stroke-width="3.6"/>'),
        ("light_groups", "灯光分组", '<circle cx="20" cy="22" r="7" fill="{c}" fill-opacity="0.75" stroke="{w}" stroke-width="3.0"/><circle cx="44" cy="22" r="7" fill="{c}" fill-opacity="0.4" stroke="{cl}" stroke-width="3.0"/><circle cx="32" cy="44" r="7" fill="{c}" fill-opacity="0.55" stroke="{w}" stroke-width="3.0"/><line x1="26" y1="26" x2="30" y2="38" stroke="{cl}" stroke-width="2.8"/><line x1="38" y1="26" x2="34" y2="38" stroke="{cl}" stroke-width="2.8"/>'),
        ("volume_hdri", "环境体积雾", '<circle cx="32" cy="26" r="14" fill="{c}" fill-opacity="0.25" stroke="{w}" stroke-width="3.2"/><path d="M12,42 Q22,36 32,42 T52,42" stroke="{c}" stroke-width="3.8"/><path d="M16,52 Q26,46 36,52 T52,52" stroke="{cl}" stroke-width="3.4"/>'),
        ("gpu_solve", "GPU自动测光", '<rect x="12" y="12" width="40" height="40" rx="6" fill="{c}" fill-opacity="0.22" stroke="{w}" stroke-width="3.4"/><polygon points="35,17 22,34 32,34 28,48 43,30 33,30" fill="{c}" stroke="{cl}" stroke-width="2.0"/>'),
    ],
    "toon": [
        ("cel_shader", "赛璐珞着色", '<circle cx="32" cy="32" r="20" fill="{w}" fill-opacity="0.9" stroke="{w}" stroke-width="2.6"/><path d="M32,12 A20,20 0 0 1 32,52 C40,42 40,22 32,12 Z" fill="{c}"/><circle cx="32" cy="32" r="20" stroke="{cl}" stroke-width="3.2"/>'),
        ("face_sdf", "面部SDF", '<circle cx="32" cy="32" r="19" fill="{c}" fill-opacity="0.24" stroke="{w}" stroke-width="3.4"/><path d="M32,13 C38,24 38,40 32,51" stroke="{c}" stroke-width="4.2"/><circle cx="23" cy="29" r="3" fill="{cl}"/><circle cx="41" cy="29" r="3" fill="{w}"/>'),
        ("line_viewport", "视口描边", '<path d="M12,48 C18,16 44,44 52,14" stroke="{c}" stroke-width="5.2"/><path d="M12,48 C18,16 44,44 52,14" stroke="{w}" stroke-width="2.2"/>'),
        ("toon_output", "线稿输出", '<rect x="12" y="12" width="40" height="40" rx="5" fill="{c}" fill-opacity="0.2" stroke="{w}" stroke-width="3.4"/><polyline points="20,40 30,24 40,34 46,22" stroke="{c}" stroke-width="4.0"/>'),
    ],
    "render-batch": [
        ("batch_submit", "提交批次", '<polygon points="22,14 50,32 22,50" fill="{c}" fill-opacity="0.75" stroke="{w}" stroke-width="3.6"/>'),
        ("exr_passes", "多层EXR", '<polygon points="32,10 54,22 32,34 10,22" fill="{c}" fill-opacity="0.6" stroke="{w}" stroke-width="3.0"/><polyline points="10,34 32,46 54,34" stroke="{cl}" stroke-width="3.6"/><polyline points="10,44 32,56 54,44" stroke="{w}" stroke-width="3.2"/>'),
        ("batch_monitor", "批次监控", '<rect x="10" y="14" width="44" height="36" rx="5" fill="{c}" fill-opacity="0.18" stroke="{w}" stroke-width="3.4"/><polyline points="16,38 25,28 33,34 44,22" stroke="{c}" stroke-width="4.0"/>'),
        ("batch_resume", "断点续跑", '<path d="M48,32 A16,16 0 1 1 43,20" stroke="{w}" stroke-width="3.8"/><polyline points="44,11 45,22 34,21" stroke="{c}" stroke-width="3.8"/>'),
    ],
    "compositing": [
        ("scene_effects", "5.3合成栈", '<path d="M32,10 L37,25 L52,30 L37,35 L32,50 L27,35 L12,30 L27,25 Z" fill="{c}" fill-opacity="0.55" stroke="{w}" stroke-width="3.0"/>'),
        ("crypto_cdl", "选区与CDL", '<circle cx="24" cy="26" r="12" fill="{c}" fill-opacity="0.45" stroke="{w}" stroke-width="3.0"/><circle cx="40" cy="26" r="12" fill="{cl}" fill-opacity="0.3" stroke="{cl}" stroke-width="3.0"/><circle cx="32" cy="40" r="12" fill="{c}" fill-opacity="0.25" stroke="{w}" stroke-width="3.0"/>'),
        ("light_mix", "光组混光", '<line x1="18" y1="12" x2="18" y2="52" stroke="{w}" stroke-width="3.2"/><line x1="32" y1="12" x2="32" y2="52" stroke="{c}" stroke-width="3.2"/><line x1="46" y1="12" x2="46" y2="52" stroke="{w}" stroke-width="3.2"/><circle cx="18" cy="24" r="5" fill="{c}"/><circle cx="32" cy="40" r="5" fill="{cl}"/><circle cx="46" cy="20" r="5" fill="{c}"/>'),
        ("relight_preview", "拖灯重打光", '<circle cx="32" cy="32" r="12" fill="{c}" fill-opacity="0.55" stroke="{w}" stroke-width="3.2"/><line x1="32" y1="8" x2="32" y2="15" stroke="{cl}" stroke-width="3.6"/><line x1="32" y1="49" x2="32" y2="56" stroke="{cl}" stroke-width="3.6"/><line x1="8" y1="32" x2="15" y2="32" stroke="{cl}" stroke-width="3.6"/><line x1="49" y1="32" x2="56" y2="32" stroke="{cl}" stroke-width="3.6"/>'),
    ],
    "video": [
        ("shot_replace", "原位换片", '<rect x="10" y="16" width="20" height="32" rx="4" fill="{c}" fill-opacity="0.45" stroke="{w}" stroke-width="3.2"/><rect x="34" y="16" width="20" height="32" rx="4" fill="{c}" fill-opacity="0.2" stroke="{cl}" stroke-width="3.2"/><polyline points="26,28 38,28 34,24" stroke="{w}" stroke-width="3.0"/>'),
        ("subtitle_style", "字幕样式", '<rect x="10" y="14" width="44" height="36" rx="5" fill="{c}" fill-opacity="0.2" stroke="{w}" stroke-width="3.4"/><line x1="18" y1="36" x2="46" y2="36" stroke="{c}" stroke-width="4.2"/><line x1="22" y1="44" x2="42" y2="44" stroke="{cl}" stroke-width="3.4"/>'),
        ("strip_comp", "条带合成", '<rect x="10" y="20" width="44" height="24" rx="4" fill="{c}" fill-opacity="0.35" stroke="{w}" stroke-width="3.4"/><path d="M32,12 L35,21 L44,24 L35,27 L32,36 L29,27 L20,24 L29,21 Z" fill="{cl}"/>'),
        ("lufs_export", "响度导出", '<line x1="14" y1="36" x2="14" y2="48" stroke="{cl}" stroke-width="4.2"/><line x1="23" y1="24" x2="23" y2="48" stroke="{c}" stroke-width="4.2"/><line x1="32" y1="14" x2="32" y2="48" stroke="{w}" stroke-width="4.2"/><line x1="41" y1="26" x2="41" y2="48" stroke="{c}" stroke-width="4.2"/><line x1="50" y1="32" x2="50" y2="48" stroke="{cl}" stroke-width="4.2"/>'),
    ],
}

STATUS_ICONS: Dict[str, Tuple[str, str, str]] = {
    "status_ok": ("#22C55E", "#BBF7D0", '<circle cx="32" cy="32" r="22" fill="{c}" fill-opacity="0.25" stroke="{c}" stroke-width="4.0"/><polyline points="21,33 29,41 44,23" stroke="{cl}" stroke-width="5.0"/>'),
    "status_info": ("#38BDF8", "#BAE6FD", '<circle cx="32" cy="32" r="22" fill="{c}" fill-opacity="0.25" stroke="{c}" stroke-width="4.0"/><line x1="32" y1="28" x2="32" y2="44" stroke="{cl}" stroke-width="5.0"/><circle cx="32" cy="19" r="3.2" fill="{cl}"/>'),
    "status_warn": ("#F59E0B", "#FDE68A", '<polygon points="32,11 54,50 10,50" fill="{c}" fill-opacity="0.28" stroke="{c}" stroke-width="4.0"/><line x1="32" y1="24" x2="32" y2="37" stroke="{cl}" stroke-width="4.6"/><circle cx="32" cy="44" r="2.8" fill="{cl}"/>'),
    "status_error": ("#EF4444", "#FECACA", '<circle cx="32" cy="32" r="22" fill="{c}" fill-opacity="0.28" stroke="{c}" stroke-width="4.0"/><line x1="23" y1="23" x2="41" y2="41" stroke="{cl}" stroke-width="5.0"/><line x1="41" y1="23" x2="23" y2="41" stroke="{cl}" stroke-width="5.0"/>'),
    "status_running": ("#A855F7", "#E9D5FF", '<circle cx="32" cy="32" r="21" stroke="{c}" stroke-opacity="0.3" stroke-width="4.5"/><path d="M32,11 A21,21 0 0 1 53,32" stroke="{cl}" stroke-width="5.2"/><circle cx="32" cy="32" r="6" fill="{c}"/>'),
    "status_safe": ("#10B981", "#A7F3D0", '<path d="M32,10 L50,18 L50,33 C50,44 41,51 32,55 C23,51 14,44 14,33 L14,18 Z" fill="{c}" fill-opacity="0.28" stroke="{c}" stroke-width="3.8"/><polyline points="24,33 30,39 41,26" stroke="{cl}" stroke-width="4.4"/>'),
}


def find_headless_browser() -> Path:
    candidates = (
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    )
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError("Neither Chrome nor Edge found for headless SVG rasterization")


def rasterize_svgs(svg_map: Dict[str, str], png_dir: Path, browser: Path) -> Dict[str, Path]:
    """Rasterize all SVGs in ONE headless Chrome/Edge pass onto a transparent 64px sprite sheet,
    then slice into lossless 64px and premultiplied-alpha downsampled 32px RGBA PNGs.
    """
    names = list(svg_map.keys())
    cols = 10
    cell = 64
    rows = (len(names) + cols - 1) // cols
    sheet_w = cols * cell
    sheet_h = rows * cell

    cells_html = []
    for name in names:
        cells_html.append(f'<div class="cell">{svg_map[name]}</div>')

    page = f'''<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<style>
  html, body {{ margin: 0; padding: 0; background: transparent; overflow: hidden; }}
  .grid {{
    display: grid;
    grid-template-columns: repeat({cols}, {cell}px);
    grid-auto-rows: {cell}px;
    width: {sheet_w}px;
    height: {sheet_h}px;
  }}
  .cell {{ width: {cell}px; height: {cell}px; display: block; }}
  .cell svg {{ width: {cell}px; height: {cell}px; display: block; }}
</style>
</head>
<body><div class="grid">{"".join(cells_html)}</div></body>
</html>'''

    png_dir.mkdir(parents=True, exist_ok=True)
    written: Dict[str, Path] = {}
    with tempfile.TemporaryDirectory(prefix="uikit_svg_") as tmp:
        tmp_dir = Path(tmp)
        html_path = tmp_dir / "sheet.html"
        sheet_png = tmp_dir / "sheet.png"
        html_path.write_text(page, encoding="utf-8")

        cmd = [
            str(browser),
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--default-background-color=00000000",
            "--force-device-scale-factor=1",
            f"--window-size={sheet_w},{sheet_h}",
            f"--screenshot={sheet_png}",
            html_path.as_uri(),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if res.returncode != 0 or not sheet_png.is_file():
            raise RuntimeError(f"Browser rasterization failed ({res.returncode}): {res.stderr}")

        sheet = read_png_rgba(sheet_png)
        if sheet.shape[0] < sheet_h or sheet.shape[1] < sheet_w:
            raise RuntimeError(f"Sprite sheet smaller than expected: {sheet.shape} vs {(sheet_h, sheet_w)}")

        for idx, name in enumerate(names):
            r, c_idx = divmod(idx, cols)
            tile64 = sheet[r * cell:(r + 1) * cell, c_idx * cell:(c_idx + 1) * cell, :].copy()
            tile32 = downsample_2x_premultiplied(tile64)

            p32 = png_dir / f"{name}_32.png"
            write_png_rgba(p32, tile32)
            written[f"{name}_32"] = p32

            if name.startswith("brand_"):
                p64 = png_dir / f"{name}_64.png"
                write_png_rgba(p64, tile64)
                written[f"{name}_64"] = p64

    return written


def render_identity_gallery(svg_map: Dict[str, str], out_png: Path, out_html: Path, browser: Path) -> None:
    """Render a crisp dark-studio visual design spec sheet showcasing all 14 themes & 80 SVGs."""
    cards = []
    for domain in PLUGIN_ORDER:
        t = THEMES[domain]
        prefix = domain.replace("-", "_")
        brand_svg = svg_map[f"brand_{prefix}"]
        action_chips = []
        for act_key, act_label, _ in DOMAIN_ACTIONS[domain]:
            asvg = svg_map[f"{prefix}_{act_key}"]
            action_chips.append(
                f'<div class="achip"><span class="aico">{asvg}</span><span>{html.escape(act_label)}</span></div>'
            )
        skel_tag = "重型阶段切页" if t.skeleton == "heavy" else "紧凑直达"
        skel_cls = "tag-heavy" if t.skeleton == "heavy" else "tag-compact"
        space_tag = "VSE" if t.editor_space == "SEQUENCE_EDITOR" else "3D 视口"
        cards.append(f'''
        <div class="card" style="--accent:{t.hex_color}; --accent-dark:{t.hex_dark};">
          <div class="card-bar"></div>
          <div class="card-top">
            <div class="brand-big">{brand_svg}</div>
            <div class="meta">
              <div class="title-row">
                <span class="tab-pill">{html.escape(t.tab_label)}</span>
                <span class="badge {skel_cls}">{skel_tag}</span>
                <span class="badge tag-space">{space_tag}</span>
              </div>
              <div class="zh">{html.escape(t.title_zh)} <span class="en">· {html.escape(t.title_en)}</span></div>
              <div class="color-row">
                <span class="swatch" style="background:{t.hex_color}"></span>
                <code>{t.hex_color}</code>
                <span class="sep">|</span>
                <span class="fam">{html.escape(t.family_zh)}</span>
                <span class="sep">|</span>
                <span class="ctag">Outliner: {t.collection_color_tag}</span>
              </div>
            </div>
            <div class="mini-preview" title="Blender 16px 实机尺寸对照">
              <div class="mini-hdr"><span class="mini-ico">{brand_svg}</span><span>{html.escape(t.title_zh)}</span></div>
            </div>
          </div>
          <div class="actions">{"".join(action_chips)}</div>
        </div>''')

    sidebar_tabs = []
    for domain in PLUGIN_ORDER:
        t = THEMES[domain]
        if t.editor_space != "VIEW_3D":
            continue
        active_cls = "stab-active" if domain == "lighting" else ""
        sidebar_tabs.append(
            f'<div class="stab {active_cls}" style="--accent:{t.hex_color}">'
            f'<span class="sdot" style="background:{t.hex_color}"></span>'
            f'<span>{html.escape(t.tab_label)}</span></div>'
        )

    status_chips = []
    status_names = {
        "status_ok": "通过", "status_info": "提示", "status_warn": "警告",
        "status_error": "报错", "status_running": "运行中", "status_safe": "安全副本",
    }
    for skey, sname in status_names.items():
        status_chips.append(
            f'<div class="schip"><span class="sico">{svg_map[skey]}</span><span>{sname}</span></div>'
        )

    gallery_w, gallery_h = 1920, 1148
    doc = f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>Blender AI 1.0 全系 14 插件视觉识别与 SVG 图标设计系统</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px 28px;
    background: #14161B; color: #E5E7EB;
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    width: {gallery_w}px; height: {gallery_h}px; overflow: hidden;
  }}
  .header {{
    display: flex; align-items: center; justify-content: space-between;
    border-bottom: 1px solid #2A2E39; padding-bottom: 14px; margin-bottom: 18px;
  }}
  .h-left h1 {{ margin: 0 0 4px; font-size: 24px; font-weight: 800; letter-spacing: 0.4px; color: #F8FAFC; }}
  .h-left p {{ margin: 0; font-size: 13px; color: #94A3B8; }}
  .status-bar {{ display: flex; gap: 10px; align-items: center; }}
  .schip {{
    display: flex; align-items: center; gap: 6px; padding: 5px 10px;
    background: #1D212A; border: 1px solid #2E3442; border-radius: 8px; font-size: 12px; color: #CBD5E1;
  }}
  .sico svg {{ width: 18px; height: 18px; display: block; }}
  .main {{ display: grid; grid-template-columns: 1fr 122px; gap: 18px; }}
  .grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 12px; }}
  .card {{
    position: relative; background: #1B1F27; border: 1px solid #2B313E; border-radius: 12px;
    padding: 12px 14px 11px 18px; overflow: hidden;
  }}
  .card-bar {{
    position: absolute; left: 0; top: 0; bottom: 0; width: 5px; background: var(--accent);
  }}
  .card-top {{ display: flex; align-items: center; gap: 12px; }}
  .brand-big svg {{ width: 56px; height: 56px; display: block; flex-shrink: 0; }}
  .meta {{ flex: 1; min-width: 0; }}
  .title-row {{ display: flex; align-items: center; gap: 7px; margin-bottom: 4px; }}
  .tab-pill {{
    font-size: 15px; font-weight: 800; color: var(--accent);
    background: rgba(255,255,255,0.05); border: 1px solid var(--accent);
    padding: 1px 8px; border-radius: 6px;
  }}
  .badge {{ font-size: 11px; padding: 2px 7px; border-radius: 5px; font-weight: 600; }}
  .tag-heavy {{ background: rgba(236,72,153,0.16); color: #F9A8D4; border: 1px solid rgba(236,72,153,0.4); }}
  .tag-compact {{ background: rgba(56,189,248,0.14); color: #7DD3FC; border: 1px solid rgba(56,189,248,0.35); }}
  .tag-space {{ background: #262B36; color: #94A3B8; }}
  .zh {{ font-size: 13.5px; font-weight: 700; color: #F1F5F9; margin-bottom: 3px; }}
  .en {{ font-weight: 400; color: #64748B; font-size: 12px; }}
  .color-row {{ display: flex; align-items: center; gap: 6px; font-size: 11.5px; color: #94A3B8; }}
  .swatch {{ width: 12px; height: 12px; border-radius: 3px; display: inline-block; border: 1px solid rgba(255,255,255,0.3); }}
  code {{ font-family: Consolas, monospace; color: #E2E8F0; font-size: 11.5px; }}
  .sep {{ color: #3F4758; }}
  .mini-preview {{
    background: #2B2B2B; border: 1px solid #3F3F3F; border-radius: 7px; padding: 6px 9px;
    align-self: flex-start; min-width: 158px;
  }}
  .mini-hdr {{ display: flex; align-items: center; gap: 6px; font-size: 11.5px; color: #E6E6E6; font-weight: 600; }}
  .mini-ico svg {{ width: 18px; height: 18px; display: block; }}
  .actions {{
    display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; padding-top: 8px;
    border-top: 1px solid #262C38;
  }}
  .achip {{
    display: flex; align-items: center; gap: 5px; background: #232833;
    border: 1px solid #303746; border-radius: 6px; padding: 3px 8px;
    font-size: 11.5px; color: #CBD5E1;
  }}
  .aico svg {{ width: 16px; height: 16px; display: block; }}
  .sidebar-mock {{
    background: #1D1D1D; border: 1px solid #333844; border-radius: 12px;
    padding: 10px 8px; display: flex; flex-direction: column; gap: 6px;
  }}
  .sb-title {{ font-size: 11px; text-align: center; color: #94A3B8; font-weight: 700; padding-bottom: 4px; border-bottom: 1px solid #2C313C; }}
  .stab {{
    display: flex; align-items: center; gap: 6px; padding: 8px 8px; border-radius: 6px;
    background: #272727; border-left: 3px solid var(--accent);
    font-size: 12.5px; font-weight: 700; color: #D4D4D8;
  }}
  .stab-active {{
    background: #383838; color: #FFFFFF; box-shadow: inset 0 0 0 1px var(--accent);
  }}
  .sdot {{ width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }}
  .sb-note {{ margin-top: auto; font-size: 10.5px; color: #64748B; text-align: center; line-height: 1.4; padding-top: 8px; border-top: 1px solid #2C313C; }}
</style>
</head>
<body>
  <div class="header">
    <div class="h-left">
      <h1>Blender AI 1.0 · 全系 14 插件视觉识别与 SVG 图标设计系统</h1>
      <p>14 独立零重码单大写字母标签 · 5 大工序 Blender 直觉色系 · 14 品牌徽章 + 60 核心子功能 + 6 全局状态 = 80 枚矢量 SVG 母版（右侧为 3D 视口 13 竖排标签秩序示意，高亮项为首个标杆 L·灯光）</p>
    </div>
    <div class="status-bar">{"".join(status_chips)}</div>
  </div>
  <div class="main">
    <div class="grid">{"".join(cards)}</div>
    <div class="sidebar-mock">
      <div class="sb-title">3D 视口侧栏<br/>13 独立标签</div>
      {"".join(sidebar_tabs)}
      <div class="sb-note">+ V·剪辑<br/>独立驻留 VSE<br/>共 14 插件</div>
    </div>
  </div>
</body>
</html>'''

    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(doc, encoding="utf-8")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(browser),
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        f"--window-size={gallery_w},{gallery_h}",
        f"--screenshot={out_png}",
        out_html.as_uri(),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0 or not out_png.is_file():
        raise RuntimeError(f"Gallery render failed ({res.returncode}): {res.stderr}")


def build_all() -> dict:
    svg_dir = UI_KIT_DIR / "svg_master"
    png_dir = UI_KIT_DIR / "icons_png"
    svg_dir.mkdir(parents=True, exist_ok=True)
    png_dir.mkdir(parents=True, exist_ok=True)

    glyphs = _brand_glyphs()
    svg_map: Dict[str, str] = {}

    # 1. 14 Brand Master Badges
    for domain in PLUGIN_ORDER:
        t = THEMES[domain]
        prefix = domain.replace("-", "_")
        key = f"brand_{prefix}"
        svg_text = _brand_svg(t, glyphs[domain])
        (svg_dir / f"{key}.svg").write_text(svg_text, encoding="utf-8")
        svg_map[key] = svg_text

    # 2. 60 Domain Action Icons
    for domain in PLUGIN_ORDER:
        t = THEMES[domain]
        prefix = domain.replace("-", "_")
        for act_key, _, body in DOMAIN_ACTIONS[domain]:
            key = f"{prefix}_{act_key}"
            svg_text = _action_svg(t.hex_color, t.hex_light, body)
            (svg_dir / f"{key}.svg").write_text(svg_text, encoding="utf-8")
            svg_map[key] = svg_text

    # 3. 6 Global Status Icons
    for skey, (sc, scl, body) in STATUS_ICONS.items():
        svg_text = _action_svg(sc, scl, body)
        (svg_dir / f"{skey}.svg").write_text(svg_text, encoding="utf-8")
        svg_map[skey] = svg_text

    browser = find_headless_browser()
    written_pngs = rasterize_svgs(svg_map, png_dir, browser)

    gallery_png = UI_KIT_DIR / "ui_kit_identity_gallery.png"
    gallery_html = UI_KIT_DIR / "ui_kit_identity_gallery.html"
    render_identity_gallery(svg_map, gallery_png, gallery_html, browser)

    manifest = {
        "schema_version": "1.0",
        "svg_count": len(svg_map),
        "png_count": len(written_pngs),
        "browser": str(browser),
        "gallery_png": str(gallery_png),
        "domains": {
            d: {
                "code": THEMES[d].code,
                "tab_label": THEMES[d].tab_label,
                "hex": THEMES[d].hex_color,
                "actions": [k for k, _, _ in DOMAIN_ACTIONS[d]],
            }
            for d in PLUGIN_ORDER
        },
    }
    (UI_KIT_DIR / "icons_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


if __name__ == "__main__":
    summary = build_all()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
