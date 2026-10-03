# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only size, decode and provenance gate for generated assets and previews."""
import argparse
import hashlib
import json
from pathlib import Path

def inspect(path, purpose, max_edge, max_bytes):
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError('Image decoding requires optional Pillow; see skills/requirements-images.txt. No packages were installed.') from exc
    p = Path(path).resolve(strict=True)
    raw = p.read_bytes()
    with Image.open(p) as im:
        im.verify()
    with Image.open(p) as im:
        im.load()
        width, height = im.size
        record = {"path": str(p), "width": width, "height": height, "format": im.format, "mode": im.mode,
                  "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "decoded": True}
    reasons = []
    if max(width, height) > max_edge:
        reasons.append("longest_edge_exceeds_budget")
    if purpose == "preview" and len(raw) > max_bytes:
        reasons.append("preview_bytes_exceed_budget")
    return {**record, "purpose": purpose, "pass": not reasons, "reasons": reasons}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="+")
    parser.add_argument("--purpose", choices=("generated", "preview"), required=True)
    parser.add_argument("--max-edge", type=int, default=2048)
    parser.add_argument("--max-preview-bytes", type=int, default=2 * 1024 * 1024)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.max_edge <= 0 or args.max_preview_bytes <= 0:
        parser.error("budgets must be positive")
    records = []
    for path in args.images:
        try:
            records.append(inspect(path, args.purpose, args.max_edge, args.max_preview_bytes))
        except Exception as exc:
            records.append({"path": str(Path(path).absolute()), "pass": False, "decoded": False, "error": str(exc)})
    result = {"pass": all(r["pass"] for r in records), "images": records, "scope": "size/decode/SHA only; not visual, physical-map or rights acceptance"}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        report = args.report.resolve()
        if report in [Path(p).resolve() for p in args.images]:
            parser.error("report must not overwrite an input image")
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(text + "\n", encoding="utf-8")
    print(text)
    raise SystemExit(0 if result["pass"] else 1)

if __name__ == "__main__":
    main()
