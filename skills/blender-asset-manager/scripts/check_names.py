# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only preflight for NEW published .blend filenames, not legacy names."""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


# Bundled asset-standard.md v1.0 sections 3-4. No semantic classification proof.
TYPES = frozenset("MOD MAT TEX HDR LGT GEO SHD CMP RIG ANI SCN BRU".split())
RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    prefix + number for prefix in ("COM", "LPT") for number in "123456789¹²³"
}
PUBLISH = re.compile(
    r"^(?P<type>[A-Z]{3})_(?P<subject>[^_]+)_v(?P<version>[0-9]{3,})"
    r"(?:_(?P<variant>[^_]+?))?(?:_LOD[0-9]+)?\.blend$"
)


def name_errors(name: str) -> list[str]:
    errors = []
    if not name or name in {".", ".."}:
        errors.append("empty_or_dot_name")
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        errors.append("windows_illegal_character_or_path")
    if name.endswith((" ", ".")):
        errors.append("trailing_space_or_dot")
    if name.split(".")[0].rstrip(" .").upper() in RESERVED:
        errors.append("windows_reserved_device_name")
    try:
        if len(name.encode("utf-16-le")) // 2 > 255:
            errors.append("filename_component_too_long")
    except UnicodeEncodeError:
        errors.append("invalid_unicode")
    return errors


def check(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Input must be a JSON object")
    if set(payload) != {"directory", "names"}:
        raise ValueError("Input requires exactly directory and names")
    directory = payload["directory"]
    names = payload["names"]
    if not isinstance(directory, str) or not directory:
        raise ValueError("directory must be a nonempty absolute path")
    target = Path(directory)
    if not target.is_absolute():
        raise ValueError("directory must be absolute")
    if not isinstance(names, list) or not names or any(not isinstance(n, str) for n in names):
        raise ValueError("names must be a nonempty array of strings")
    existing = defaultdict(list)
    # Read the target once. Also reject an impossible path below an existing file.
    probe = target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.is_dir():
        raise ValueError("Nearest existing target ancestor is not a directory")
    if target.exists():
        if not target.is_dir():
            raise ValueError("directory points to a file")
        for item in target.iterdir():
            existing[item.name.casefold()].append(item.name)
    groups = defaultdict(list)
    rows = []
    for index, name in enumerate(names):
        errors = name_errors(name)
        warnings = []
        match = PUBLISH.fullmatch(name)
        if match is None:
            errors.append("not_a_new_published_blend_name")
        else:
            if match["type"] not in TYPES:
                errors.append("unknown_type_code")
            if not any(c != "0" for c in match["version"]):
                errors.append("version_must_start_at_v001")
            if not match["subject"].strip():
                errors.append("empty_subject")
            if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", match["subject"]):
                warnings.append("ascii_pascal_case_subject_recommended")
            if match["variant"] is not None and not match["variant"].strip():
                errors.append("empty_variant")
        collisions = existing.get(name.casefold(), [])
        if collisions:
            errors.append("target_already_exists_case_insensitive")
        row = {"index": index, "name": name, "errors": errors, "warnings": warnings,
               "existing_collisions": collisions}
        rows.append(row)
        groups[name.casefold()].append(index)
    for indices in groups.values():
        if len(indices) > 1:
            for index in indices:
                rows[index]["errors"].append("duplicate_in_batch_case_insensitive")
    return {
        "schema_version": "1.0", "mode": "new_published_blend_names",
        "ok": not any(row["errors"] for row in rows),
        "directory": str(target), "resolved_directory": str(target.resolve()),
        "directory_exists": target.is_dir(), "mutations": 0, "results": rows,
        "not_checked": ["classification_semantics", "asset_identity", "version_history",
                        "datablock_names", "dependencies", "license", "publish_acceptance",
                        "concurrent_changes_after_this_check"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="UTF-8 JSON: directory and names")
    args = parser.parse_args()
    try:
        result = check(json.loads(args.input.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        result = {"schema_version": "1.0", "ok": False, "mutations": 0,
                  "error": {"type": type(exc).__name__, "message": str(exc)}}
    # ASCII transport remains valid JSON regardless of Windows console encoding.
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
