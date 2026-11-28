# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless-safe custom SVG-rendered PNG icon loader using bpy.utils.previews."""

from __future__ import annotations

from pathlib import Path
from typing import Optional


class IconPack:
    """Manages custom RGBA PNG icons for a Blender AI plugin.

    In headless CLI mode (`bpy.app.background == True`), icon loading is
    intentionally skipped and `icon_id()` safely returns `0` so zero GUI
    overhead or preview errors occur during automated pipelines or tests.
    """

    def __init__(self, domain: str, icons_dir: Optional[Path] = None):
        self.domain = domain
        if icons_dir is not None:
            self.icons_dir = Path(icons_dir)
        else:
            here = Path(__file__).resolve().parent
            local_icons = here / "icons"
            shared_icons = here / "icons_png"
            self.icons_dir = local_icons if local_icons.is_dir() else shared_icons
        self._pcoll = None
        self._registered = False

    def register(self) -> None:
        if self._registered:
            return
        self._registered = True
        try:
            import bpy
            import bpy.utils.previews as previews
        except ImportError:
            return
        if getattr(bpy.app, "background", False):
            return
        if not self.icons_dir.is_dir():
            return
        pcoll = previews.new()
        try:
            for png in sorted(self.icons_dir.glob("*.png")):
                key = png.stem
                if key not in pcoll:
                    pcoll.load(key, str(png), "IMAGE")
        except Exception:
            try:
                previews.remove(pcoll)
            except Exception:
                pass
            raise
        self._pcoll = pcoll

    def unregister(self) -> None:
        self._registered = False
        if self._pcoll is None:
            return
        pcoll = self._pcoll
        self._pcoll = None
        try:
            import bpy.utils.previews as previews
            previews.remove(pcoll)
        except Exception:
            pass

    def icon_id(self, name: str, *, size: int = 32) -> int:
        """Return Blender preview icon_id or 0 when unavailable/headless."""
        if self._pcoll is None:
            return 0
        sized_key = f"{name}_{size}"
        item = self._pcoll.get(sized_key)
        if item is None:
            item = self._pcoll.get(name)
        if item is None and size != 32:
            item = self._pcoll.get(f"{name}_32")
        return int(item.icon_id) if item is not None else 0

    def brand_id(self, *, large: bool = False) -> int:
        key = f"brand_{self.domain.replace('-', '_')}"
        return self.icon_id(key, size=64 if large else 32)

    def action_id(self, action_name: str) -> int:
        prefix = self.domain.replace("-", "_")
        icon = self.icon_id(f"{prefix}_{action_name}", size=32)
        if icon:
            return icon
        return self.icon_id(action_name, size=32)

    def status_id(self, kind: str) -> int:
        return self.icon_id(f"status_{kind.lower()}", size=32)
