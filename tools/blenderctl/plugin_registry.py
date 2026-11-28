"""Resolve trusted local plugin sources and read their published version metadata."""
import ast
import json
from pathlib import Path
import tomllib


def load_registry(root):
    with (root / "tools/blenderctl/registry.json").open(encoding="utf-8-sig") as stream:
        data = json.load(stream)
    if data.get("schema_version") != "1.0":
        raise ValueError("Unsupported plugin registry schema")
    return data


def source_path(root, relative):
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        raise ValueError(f"Invalid plugin registry path: {relative}")
    return path


def source_version(root, plugin):
    source = source_path(root, plugin["addon_dir"])
    field = plugin.get("version_source", "manifest")
    if field == "manifest":
        with (source / "blender_manifest.toml").open("rb") as stream:
            return tomllib.load(stream)["version"]
    for statement in ast.parse((source / "__init__.py").read_text(encoding="utf-8-sig")).body:
        if isinstance(statement, ast.Assign) and any(isinstance(name, ast.Name) and name.id == field for name in statement.targets):
            value = ast.literal_eval(statement.value)
            if field == "bl_info":
                return ".".join(map(str, value["version"]))
            if field == "__version__":
                return value
    raise ValueError(f"No {field} version for {source}")

