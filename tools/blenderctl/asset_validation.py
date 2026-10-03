# SPDX-License-Identifier: GPL-3.0-or-later
"""Asset index/Catalog/blend consistency. No index or Catalog mutation."""
import json
import os
from pathlib import Path
import re
import uuid
from protocol import ROOT


def schema_errors(data, schema, path="$"):
    """The deliberately small published asset schema uses only these keywords."""
    types = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list),
             "string": lambda v: isinstance(v, str), "integer": lambda v: type(v) is int,
             "null": lambda v: v is None, "boolean": lambda v: type(v) is bool}
    expected = schema.get("type")
    if expected and not any(types[t](data) for t in ([expected] if isinstance(expected, str) else expected)):
        yield path + ": type"
        return
    if "const" in schema and data != schema["const"]:
        yield path + ": const"
    if "enum" in schema and data not in schema["enum"]:
        yield path + ": enum"
    if isinstance(data, str):
        if len(data) < schema.get("minLength", 0) or ("pattern" in schema and not re.search(schema["pattern"], data)):
            yield path + ": string constraint"
    if isinstance(data, list):
        if len(data) < schema.get("minItems", 0):
            yield path + ": minItems"
        for i, entry in enumerate(data):
            yield from schema_errors(entry, schema.get("items", {}), f"{path}/{i}")
    if isinstance(data, dict):
        for name in schema.get("required", []):
            if name not in data:
                yield path + "/" + name + ": required"
        for name, entry in data.items():
            if name not in schema.get("properties", {}) and schema.get("additionalProperties") is False:
                yield path + "/" + name + ": unknown"
            yield from schema_errors(entry, schema.get("properties", {}).get(name, {}), path + "/" + name)
    if type(data) is int and data < schema.get("minimum", data):
        yield path + ": minimum"


def canonical(path):
    return os.path.normcase(os.path.abspath(path))


def validate_records(records, catalogs, snapshot, file_map=None, scope_file=False):
    from dependencies import file_fingerprint
    schema = json.loads((ROOT / "docs/cli/schemas/asset-record.schema.json").read_text(encoding="utf-8"))
    findings, definitions, indexed = [], {}, set()
    def issue(code, **context):
        findings.append({"severity": "error", "code": code, **context})
    for catalog in catalogs:
        root = catalog["library_root"]
        if not Path(root).is_absolute():
            issue("CATALOG_ROOT_NOT_ABSOLUTE", root=root)
            continue
        try:
            text = Path(catalog["file"]).read_text(encoding="utf-8-sig") if "file" in catalog else catalog["text"]
        except UnicodeError:
            issue("CATALOG_ENCODING_INVALID", root=root)
            continue
        lines = [l.strip() for l in text.lstrip("\ufeff").splitlines() if l.strip() and not l.lstrip().startswith("#")]
        if not lines or lines.pop(0) != "VERSION 1":
            issue("INVALID_CATALOG_HEADER", root=root)
            continue
        for line in lines:
            try:
                ident, path, simple = line.split(":")
                if str(uuid.UUID(ident)) != ident.lower() or not path or not simple or any(p in ("", ".", "..") for p in path.split("/")):
                    raise ValueError()
            except ValueError:
                issue("INVALID_CATALOG_ROW", root=root, row=line)
                continue
            k = (canonical(root), ident.lower())
            if k in definitions and definitions[k] != (path, simple):
                issue("CATALOG_UUID_CONFLICT", root=root, uuid=ident)
            definitions[k] = (path, simple)
    for record in records:
        errors = list(schema_errors(record, schema))
        if errors:
            issue("ASSET_RECORD_SCHEMA", paths=errors)
            continue
        aid = record["asset_id"]
        if aid in indexed:
            issue("DUPLICATE_INDEX_ASSET_ID", asset_id=aid)
        indexed.add(aid)
        if scope_file:
            primaries=[f for f in record['files'] if f['role']=='primary']
            if len(primaries)!=1:
                issue('INDEX_PRIMARY_COUNT',asset_id=aid)
                continue
            primary=primaries[0];raw=Path(primary['path']);base=primary['path_base']
            if not raw.is_absolute() and (not base or not Path(base).is_absolute()):
                issue('FILE_BASE_NOT_ABSOLUTE',asset_id=aid)
                continue
            if canonical(raw if raw.is_absolute() else Path(base)/raw)!=canonical(snapshot['file']):continue
        matched = [b for b in snapshot["datablocks"] if b["asset_id"] == aid]
        if len(matched) != 1:
            issue("INDEX_ID_NOT_UNIQUE_IN_BLEND", asset_id=aid)
            continue
        block = matched[0]
        declared = record["blender"]["datablocks"]
        if not any(d["type"] == block["type"] and d["name"] == block["name"] for d in declared):
            issue("INDEX_DATABLOCK_MISMATCH", asset_id=aid)
        for d in declared:
            if not any(b["type"] == d["type"] and b["name"] == d["name"] and b["library"] is None for b in snapshot["datablocks"]):
                issue("INDEX_DATABLOCK_MISSING", asset_id=aid, datablock=d)
        if not block["asset"] or block["library"]:
            issue("INDEX_NOT_LOCAL_MARKED_ASSET", asset_id=aid)
        primary = False
        seen_files = set()
        for file in record["files"]:
            raw, base = Path(file["path"]), file["path_base"]
            if not raw.is_absolute() and (not base or not Path(base).is_absolute()):
                issue("FILE_BASE_NOT_ABSOLUTE", asset_id=aid)
                continue
            resolved = canonical(raw if raw.is_absolute() else Path(base) / raw)
            if resolved in seen_files:
                issue("DUPLICATE_INDEX_FILE", path=resolved)
            seen_files.add(resolved)
            primary |= file["role"] == "primary" and resolved == canonical(snapshot["file"])
            actual_path=(file_map or {}).get(resolved,resolved)
            if not Path(actual_path).is_file():
                issue("INDEX_FILE_MISSING", path=resolved)
                continue
            actual = file_fingerprint(actual_path)
            if file["sha256"] is not None and actual["sha256"] != file["sha256"]:
                issue("INDEX_FILE_HASH_MISMATCH", path=resolved)
            if file["bytes"] is not None and actual["bytes"] != file["bytes"]:
                issue("INDEX_FILE_SIZE_MISMATCH", path=resolved)
            if file["sha256"] is None or file["bytes"] is None:
                findings.append({"severity": "error" if record["lifecycle"] == "published" else "warning", "code": "INDEX_FILE_UNVERIFIED", "path": resolved})
        if not primary:
            issue("INDEX_PRIMARY_BLEND_MISMATCH", asset_id=aid)
        catalog = record["catalog"]
        catalog_id = catalog["uuid"]
        if catalog_id:
            root = catalog["library_root"]
            definition = definitions.get((canonical(root), catalog_id.lower())) if root and Path(root).is_absolute() else None
            if not definition or definition[0] != catalog["path"]:
                issue("INDEX_CATALOG_DANGLING_OR_PATH_MISMATCH", asset_id=aid)
            if root and Path(root).is_absolute() and not Path(canonical(snapshot["file"])).is_relative_to(Path(canonical(root))):
                issue("BLEND_OUTSIDE_CATALOG_LIBRARY", asset_id=aid)
            if not block["asset"] or block["asset"]["catalog_id"].lower() != catalog_id.lower():
                issue("BLEND_CATALOG_MISMATCH", asset_id=aid)
        elif record["lifecycle"] == "published":
            issue("PUBLISHED_ASSET_UNCLASSIFIED", asset_id=aid)
        if record["dependencies"] is not None:
            observed = set()
            for dep in snapshot["dependencies"]["items"]:
                if dep["role"] == "output" or dep["status"] in {"packed", "builtin", "generated", "no_external_path"}:
                    continue
                paths = dep.get("expected_files", [dep["resolved_path"]])
                observed.update(canonical(p) for p in paths if p and p not in dep.get("packed_members", []))
            declared_paths = set()
            for dep in record["dependencies"]:
                if not Path(dep["path"]).is_absolute():
                    issue("INDEX_DEPENDENCY_NOT_ABSOLUTE", asset_id=aid)
                    continue
                path = canonical(dep["path"])
                declared_paths.add(path)
                if dep.get("sha256") and Path(path).is_file() and file_fingerprint(path)["sha256"] != dep["sha256"]:
                    issue("INDEX_DEPENDENCY_HASH_MISMATCH", path=path)
            if observed != declared_paths:
                issue("INDEX_DEPENDENCY_SET_MISMATCH", asset_id=aid, missing=sorted(observed-declared_paths), extra=sorted(declared_paths-observed))
        if record["lifecycle"] == "published":
            source = record["source"]
            if source["permission_status"] != "verified" or not source["intended_use"] or not source["license_evidence_path"]:
                issue("PUBLISHED_PERMISSION_UNVERIFIED", asset_id=aid)
            if record["dependencies"] is None or record["validation"]["status"] != "pass":
                issue("PUBLISHED_VALIDATION_UNVERIFIED", asset_id=aid)
    return findings
