"""Resolve a Catalog in the registered library used by asset.index/asset.plan."""
import os
from pathlib import Path
import uuid

from filesystem import FileGuard
from library import NAMES
from protocol import Failure
from transactions import validate_document


def resolve_catalog(library_path, requested):
    if not isinstance(requested, str) or any(
        part in ("", ".", "..") or part != part.strip() for part in requested.split("/")
    ) or any(ord(char) < 32 or char in ":\\" for char in requested):
        raise Failure("INVALID_REQUEST", "Catalog 路径须由有效分类名组成，使用 / 分隔。")
    root = Path(library_path)
    if not root.is_absolute() or not root.is_dir():
        raise Failure("NOT_FOUND", f"注册库目录不可用：{root}")
    root = root.resolve()
    base = {}
    definitions = {}
    for key, name in NAMES.items():
        file = root / name
        if not os.path.lexists(file):
            base[key] = None
            continue
        if not file.is_file():
            raise Failure("INVALID_REQUEST", f"库文档不是普通文件：{file}")
        with FileGuard(file) as handle:
            base[key] = handle.sha256()
            if key == "catalog":
                definitions = validate_document(file, "catalog")

    matches = [(uid, definition) for uid, definition in definitions.items()
               if definition["path"].casefold() == requested.casefold()]
    if len(matches) > 1:
        raise Failure("CONFLICT", f"多个 Catalog UUID 对应路径 {requested}，需要指定唯一分类。")
    if matches:
        uid, fields = matches[0]
        decision = "existing"
    else:
        # Blender can show parent folders implied only by a descendant.
        # Reuse the spelling of the deepest shared folder for new children.
        requested_parts = requested.split("/")
        ancestors = set()
        depth = 0
        for entry in definitions.values():
            parts = entry["path"].split("/")
            shared = 0
            for existing, target in zip(parts, requested_parts):
                if existing.casefold() != target.casefold():
                    break
                shared += 1
            if shared > depth:
                ancestors.clear()
                depth = shared
            if shared == depth and shared:
                ancestors.add("/".join(parts[:shared]))
        if len(ancestors) > 1:
            raise Failure("CONFLICT", f"已有分类的父路径大小写不唯一：{requested}")
        if ancestors:
            requested = "/".join([ancestors.pop(), *requested_parts[depth:]])
        # Planning the same new path twice must produce the same .blend binding
        # before either plan commits. Persisted UUIDs always take precedence.
        identity = os.path.normcase(str(root)) + "\n" + requested.casefold()
        uid = str(uuid.uuid5(uuid.NAMESPACE_URL, "blender-asset-catalog:" + identity))
        if uid in definitions:
            raise Failure("CONFLICT", "拟用分类 UUID 已绑定到另一条路径。")
        fields = {"path": requested, "simple_name": requested.split("/")[-1]}
        decision = "planned_create"
    definition = {"uuid": uid, **fields}
    return {
        "decision": decision,
        "definition": definition,
        "catalog_file": str(root / NAMES["catalog"]),
        "target_catalog_id": uid,
        "index_fields": {"library_root": str(root), "catalogs": [definition]},
        "plan_fields": {"library_root": str(root), "base": base, "catalogs": [definition]},
    }
