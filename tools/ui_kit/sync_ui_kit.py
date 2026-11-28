# SPDX-License-Identifier: GPL-3.0-or-later
"""Vendor tools/ui_kit and plugin-specific PNG icons into standalone addon packages."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
UI_KIT_DIR = Path(__file__).resolve().parent
if str(UI_KIT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(UI_KIT_DIR.parent))

from ui_kit.theme_tokens import PLUGIN_ORDER, THEMES

CORE_MODULES = (
    "__init__.py",
    "theme_tokens.py",
    "icons.py",
    "layout_kit.py",
    "hud_overlay.py",
    "job_runner.py",
    "sidebar_dock.py",
)


def _refresh_toon_bundle_manifest(addon_dir: Path) -> None:
    bundle_path = addon_dir / "bundle.json"
    if not bundle_path.is_file():
        return
    manifest = json.loads(bundle_path.read_text(encoding="utf-8"))
    files = {}
    for item in sorted(addon_dir.rglob("*")):
        if not item.is_file():
            continue
        rel = item.relative_to(addon_dir)
        if "__pycache__" in rel.parts or ".git" in rel.parts or rel.as_posix() == "bundle.json":
            continue
        if item.suffix in {".pyc", ".pyo"}:
            continue
        files[rel.as_posix()] = hashlib.sha256(item.read_bytes()).hexdigest()
    manifest["files"] = files
    bundle_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    bundle_path.write_bytes(bundle_bytes)

    # Keep tools/blenderctl/registry.json launcher.toon_bundles[*].manifest_sha256 in lockstep
    reg_path = ROOT / "tools/blenderctl/registry.json"
    if reg_path.is_file():
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
        toon_bundles = reg.get("launcher", {}).get("toon_bundles", {})
        rel_dir = addon_dir.relative_to(ROOT).as_posix()
        new_sha = hashlib.sha256(bundle_bytes).hexdigest()
        changed = False
        for bundle_meta in toon_bundles.values():
            if bundle_meta.get("directory") == rel_dir and bundle_meta.get("manifest_sha256") != new_sha:
                bundle_meta["manifest_sha256"] = new_sha
                changed = True
        if changed:
            reg_path.write_text(json.dumps(reg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _refresh_camera_code_manifest(addon_dir: Path) -> None:
    manifest_path = addon_dir / "CODE-MANIFEST.json"
    if not manifest_path.is_file():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = {}
    for item in sorted(addon_dir.rglob("*.py")):
        if not item.is_file():
            continue
        rel = item.relative_to(addon_dir)
        if "__pycache__" in rel.parts or ".git" in rel.parts:
            continue
        files[rel.as_posix()] = hashlib.sha256(item.read_bytes()).hexdigest()
    manifest["files"] = files
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)

    shot_table = ROOT / "tools/blenderctl/camera_shot_table.py"
    if shot_table.is_file():
        new_sha = hashlib.sha256(manifest_bytes).hexdigest()
        original = shot_table.read_text(encoding="utf-8")
        updated, count = re.subn(
            r"(?m)^MANIFEST_SHA\s*=\s*'[0-9a-f]{64}'$",
            f"MANIFEST_SHA = '{new_sha}'",
            original,
        )
        if count == 1 and updated != original:
            shot_table.write_text(updated, encoding="utf-8")


def sync_plugin(domain: str) -> dict:
    if domain not in THEMES:
        raise ValueError(f"Unknown plugin domain: {domain}")
    theme = THEMES[domain]
    addon_dir = ROOT / theme.addon_dir
    if not addon_dir.is_dir():
        raise FileNotFoundError(f"Addon directory not found: {addon_dir}")

    dest = addon_dir / "_ui_kit"
    dest_icons = dest / "icons"
    dest.mkdir(parents=True, exist_ok=True)
    if dest_icons.exists():
        shutil.rmtree(dest_icons)
    dest_icons.mkdir(parents=True, exist_ok=True)

    copied_py = []
    for mod in CORE_MODULES:
        src = UI_KIT_DIR / mod
        shutil.copy2(src, dest / mod)
        copied_py.append(mod)

    icons_src_dir = UI_KIT_DIR / "icons_png"
    prefix = domain.replace("-", "_")
    copied_icons = []
    if icons_src_dir.is_dir():
        for png in sorted(icons_src_dir.glob("*.png")):
            stem = png.stem
            if (
                stem.startswith(f"brand_{prefix}_")
                or stem.startswith(f"{prefix}_")
                or stem.startswith("status_")
            ):
                shutil.copy2(png, dest_icons / png.name)
                copied_icons.append(png.name)

    if domain == "toon":
        _refresh_toon_bundle_manifest(addon_dir)
    elif domain == "camera":
        _refresh_camera_code_manifest(addon_dir)

    return {
        "domain": domain,
        "target": str(dest),
        "modules": len(copied_py),
        "icons": len(copied_icons),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync tools/ui_kit into plugin _ui_kit packages.")
    parser.add_argument("--plugin", action="append", choices=list(PLUGIN_ORDER), help="Target plugin domain")
    parser.add_argument("--all", action="store_true", help="Sync into all 14 plugins")
    args = parser.parse_args()

    domains = list(PLUGIN_ORDER) if args.all else (args.plugin or [])
    if not domains:
        parser.error("Specify --plugin <domain> or --all")

    results = [sync_plugin(d) for d in domains]
    print(json.dumps({"ok": True, "synced": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
