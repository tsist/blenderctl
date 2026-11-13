"""Managed Disk Bake lifecycle, Windows path budget enforcement, and SHA256 cache invalidation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

import bpy


MANIFEST_FILENAME = "physics-cache-manifest.json"
MAX_WIN_PARENT_DIR = 240  # Keep safe margin below 248/260 Windows MAX_PATH


def compute_input_mesh_sha256(obj: bpy.types.Object) -> str:
    """Compute stable geometry digest of an object's base mesh/curve/points (excluding GN modifier)."""
    h = hashlib.sha256()
    h.update(obj.type.encode("utf-8"))
    if obj.type == "MESH" and obj.data is not None:
        verts = [(round(v.co.x, 6), round(v.co.y, 6), round(v.co.z, 6)) for v in obj.data.vertices]
        polys = [tuple(p.vertices) for p in obj.data.polygons]
        h.update(json.dumps({"v": verts, "p": polys}).encode("utf-8"))
    elif obj.type == "CURVE" and obj.data is not None:
        splines = [
            [(round(pt.co.x, 6), round(pt.co.y, 6), round(pt.co.z, 6)) for pt in s.points]
            + [(round(bp.co.x, 6), round(bp.co.y, 6), round(bp.co.z, 6)) for bp in s.bezier_points]
            for s in obj.data.splines
        ]
        h.update(json.dumps(splines).encode("utf-8"))
    else:
        h.update(obj.name.encode("utf-8"))
    return h.hexdigest()


def resolve_bake_directory(obj: bpy.types.Object, mod: bpy.types.Modifier, requested_dir: str | None = None) -> Path:
    if requested_dir:
        candidate = Path(requested_dir).resolve()
    elif mod.bake_directory:
        candidate = Path(mod.bake_directory).resolve()
    elif bpy.data.filepath:
        blend_path = Path(bpy.data.filepath).resolve()
        short_key = hashlib.sha1(f"{obj.name}:{mod.name}".encode("utf-8")).hexdigest()[:10]
        candidate = blend_path.parent / "pnw_bakes" / short_key
    else:
        short_key = hashlib.sha1(f"{obj.name}:{mod.name}".encode("utf-8")).hexdigest()[:10]
        candidate = Path(tempfile.gettempdir()) / "pnw_bakes" / short_key

    # Guard Windows path budget (leave >= 36 chars for .../meta/frame_0001.json or .../blobs/<hash>.blob)
    if os.name == "nt" and len(str(candidate)) > MAX_WIN_PARENT_DIR - 36:
        raise ValueError(
            f"Bake directory exceeds Windows safe length budget ({len(str(candidate))} chars > {MAX_WIN_PARENT_DIR - 36}): {candidate}"
        )
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def read_cache_manifest(bake_dir: Path) -> dict[str, Any] | None:
    manifest_path = bake_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def cache_stats(bake_dir: Path) -> dict[str, Any]:
    if not bake_dir.exists():
        return {"exists": False, "meta_files": 0, "blob_files": 0, "total_bytes": 0}
    metas = list(bake_dir.rglob("*.json"))
    # Exclude our manifest from Blender meta count
    meta_only = [p for p in metas if p.name != MANIFEST_FILENAME]
    blobs = list(bake_dir.rglob("*.blob"))
    total_bytes = sum(p.stat().st_size for p in meta_only + blobs if p.is_file())
    return {
        "exists": True,
        "meta_files": len(meta_only),
        "blob_files": len(blobs),
        "total_bytes": total_bytes,
    }


def is_cache_valid(
    obj: bpy.types.Object,
    mod: bpy.types.Modifier,
    params_hash: str,
    graph_fingerprint: str,
) -> tuple[bool, str, Path]:
    bake_dir = resolve_bake_directory(obj, mod, mod.bake_directory or None)
    manifest = read_cache_manifest(bake_dir)
    if manifest is None:
        return False, "manifest_missing", bake_dir
    stats = cache_stats(bake_dir)
    if stats["meta_files"] == 0 or stats["blob_files"] == 0:
        return False, "cache_files_missing", bake_dir
    input_sha = compute_input_mesh_sha256(obj)
    if manifest.get("input_mesh_sha256") != input_sha:
        return False, "input_mesh_changed", bake_dir
    if manifest.get("parameters_hash") != params_hash:
        return False, "parameters_changed", bake_dir
    if manifest.get("graph_fingerprint") != graph_fingerprint:
        return False, "graph_changed", bake_dir
    return True, "valid", bake_dir


def invalidate_cache(
    obj: bpy.types.Object,
    mod: bpy.types.Modifier,
    requested_dir: str | None = None,
) -> dict[str, Any]:
    bake_dir = resolve_bake_directory(obj, mod, requested_dir)
    deleted_bakes = 0
    for b in mod.bakes:
        try:
            with bpy.context.temp_override(object=obj):
                bpy.ops.object.geometry_node_bake_delete_single(
                    session_uid=obj.session_uid,
                    modifier_name=mod.name,
                    bake_id=b.bake_id,
                )
            deleted_bakes += 1
        except RuntimeError:
            pass
    if bake_dir.exists():
        shutil.rmtree(bake_dir, ignore_errors=True)
    bake_dir.mkdir(parents=True, exist_ok=True)
    return {
        "invalidated": True,
        "bake_directory": str(bake_dir),
        "deleted_bakes": deleted_bakes,
    }


def execute_disk_bake(
    obj: bpy.types.Object,
    mod: bpy.types.Modifier,
    frame_start: int,
    frame_end: int,
    params_hash: str,
    graph_fingerprint: str,
    requested_dir: str | None = None,
) -> dict[str, Any]:
    bake_dir = resolve_bake_directory(obj, mod, requested_dir)
    # Clear old cache before baking to guarantee clean artifacts
    if bake_dir.exists():
        for b in mod.bakes:
            try:
                with bpy.context.temp_override(object=obj):
                    bpy.ops.object.geometry_node_bake_delete_single(
                        session_uid=obj.session_uid,
                        modifier_name=mod.name,
                        bake_id=b.bake_id,
                    )
            except RuntimeError:
                pass
        shutil.rmtree(bake_dir, ignore_errors=True)
        bake_dir.mkdir(parents=True, exist_ok=True)

    mod.bake_target = "DISK"
    mod.bake_directory = str(bake_dir)

    scene = bpy.context.scene
    scene.frame_start = frame_start
    scene.frame_end = frame_end

    bpy.context.view_layer.update()
    if len(mod.bakes) == 0:
        raise RuntimeError(f"Modifier '{mod.name}' exposes zero simulation/bake zones after update")

    baked_ids = []
    for b in mod.bakes:
        b.use_custom_simulation_frame_range = True
        b.frame_start = frame_start
        b.frame_end = frame_end
        with bpy.context.temp_override(object=obj, scene=scene):
            res = bpy.ops.object.geometry_node_bake_single(
                session_uid=obj.session_uid,
                modifier_name=mod.name,
                bake_id=b.bake_id,
            )
            if "FINISHED" not in res:
                raise RuntimeError(f"Bake failed for bake_id={b.bake_id}: {res}")
        baked_ids.append(b.bake_id)

    bake_dir.mkdir(parents=True, exist_ok=True)
    stats = cache_stats(bake_dir)
    manifest = {
        "schema_version": "1.0",
        "object_name": obj.name,
        "modifier_name": mod.name,
        "frame_start": frame_start,
        "frame_end": frame_end,
        "input_mesh_sha256": compute_input_mesh_sha256(obj),
        "parameters_hash": params_hash,
        "graph_fingerprint": graph_fingerprint,
        "baked_ids": baked_ids,
        "meta_files": stats["meta_files"],
        "blob_files": stats["blob_files"],
        "total_bytes": stats["total_bytes"],
    }
    (bake_dir / MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {
        "baked": True,
        "bake_directory": str(bake_dir),
        "baked_ids": baked_ids,
        "manifest": manifest,
    }
