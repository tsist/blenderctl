# SPDX-License-Identifier: GPL-3.0-or-later
"""Package registered Blender AI GUI addons into standalone Blender 5.3 extension .zip files."""

from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
import tomllib
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[2]


def _animation_support(source: Path) -> dict[str, bytes]:
    """Store the frozen resource tree in the compact form read by the add-on."""
    sys.path.insert(0, str(source.parent))
    from animation_curves_workflow_addon.support_payload import MANIFEST_SHA, REFERENCE

    frozen = source / "support_resources"
    contents = BytesIO()
    index = {}
    with ZipFile(contents, "w") as archive:
        for file in sorted(frozen.rglob("*")):
            relative = file.relative_to(frozen)
            if not file.is_file() or "__pycache__" in relative.parts or file.suffix in {".pyc", ".pyo"}:
                continue
            data = file.read_bytes()
            name = relative.as_posix()
            index[name] = hashlib.sha256(data).hexdigest()
            archive.writestr(ZipInfo(name), data, compress_type=ZIP_DEFLATED)
    if index.get("bundle.json") != MANIFEST_SHA:
        raise ValueError("The frozen animation support bundle does not match its release")
    return {
        "support-data.zip": contents.getvalue(),
        "support-data.json": json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8"),
        "CHARGE-Reference.blend": (frozen / REFERENCE).read_bytes(),
    }


def package_domains(domains: list[str], dist_dir: Path) -> list[dict]:
    reg = json.loads((ROOT / "tools/blenderctl/registry.json").read_text(encoding="utf-8"))
    target_set = set(domains)
    dist_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for plugin in reg["plugins"]:
        domain = plugin["domain"]
        if domain not in target_set:
            continue
        src = ROOT / plugin["addon_dir"]
        if not src.is_dir():
            raise FileNotFoundError(f"Addon source missing: {src}")
        zpath = dist_dir / f"{domain}-{plugin['version']}.zip"
        if zpath.exists():
            zpath.unlink()
        offline = set(plugin.get("offline_source_trees", ()))
        packaged_support = _animation_support(src) if domain == "animation" else {}
        replaced = {*packaged_support, "PACKAGE-MANIFEST.json"} if packaged_support else set()
        files = sorted(
            f for f in src.rglob("*")
            if f.is_file()
            and not (set(f.relative_to(src).parts) & {"__pycache__", ".git"})
            and f.relative_to(src).parts[0] not in offline
            and f.relative_to(src).as_posix() not in replaced
            and f.suffix not in {".pyc", ".pyo"}
        )
        source_manifest = src / "blender_manifest.toml"
        install_id = tomllib.loads(source_manifest.read_text(encoding="utf-8"))["id"] if source_manifest.is_file() else src.name
        with ZipFile(zpath, "w", compression=ZIP_DEFLATED, compresslevel=9) as zf:
            package_index = {}
            for f in files:
                rel = f.relative_to(src).as_posix()
                zi = ZipInfo(rel)
                zi.compress_type = ZIP_DEFLATED
                data = f.read_bytes()
                zf.writestr(zi, data)
                if packaged_support:
                    package_index[rel] = hashlib.sha256(data).hexdigest()
            for name, data in packaged_support.items():
                zf.writestr(ZipInfo(name), data, compress_type=ZIP_DEFLATED)
                package_index[name] = hashlib.sha256(data).hexdigest()
            if not source_manifest.is_file():
                meta = plugin["extension_metadata"]
                fields = {
                    "schema_version": "1.0.0",
                    "id": install_id,
                    "type": "add-on",
                    "name": meta["name"],
                    "version": plugin["version"],
                    "tagline": meta["tagline"],
                    "maintainer": "Blender AI",
                    "blender_version_min": "5.3.0",
                    "license": meta["license"],
                }
                content = "\n".join(f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in fields.items()) + "\n"
                manifest_bytes = content.encode("utf-8")
                zf.writestr(ZipInfo("blender_manifest.toml"), manifest_bytes)
                if packaged_support:
                    package_index["blender_manifest.toml"] = hashlib.sha256(manifest_bytes).hexdigest()
            if packaged_support:
                index_bytes = json.dumps(package_index, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
                zf.writestr(ZipInfo("PACKAGE-MANIFEST.json"), index_bytes, compress_type=ZIP_DEFLATED)
            file_count = len(zf.namelist())
        results.append({
            "domain": domain,
            "version": plugin["version"],
            "manifest_id": install_id,
            "zip": str(zpath),
            "files": file_count,
            "bytes": zpath.stat().st_size,
        })
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Package Blender AI extensions into .zip archives.")
    parser.add_argument("--dist", type=Path, default=ROOT / "runtime/gui-extensions")
    parser.add_argument("--plugin", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    reg = json.loads((ROOT / "tools/blenderctl/registry.json").read_text(encoding="utf-8"))
    all_domains = [p["domain"] for p in reg["plugins"]]
    domains = all_domains if args.all else args.plugin
    if not domains:
        parser.error("Specify --plugin <domain> or --all")

    out = package_domains(domains, args.dist)
    print(json.dumps({"ok": True, "packages": out}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
