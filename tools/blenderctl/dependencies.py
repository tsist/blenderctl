# SPDX-License-Identifier: GPL-3.0-or-later
"""Typed resource observation. Pattern candidates are not a proof of playback completeness."""
import os
from pathlib import Path
import re
import hashlib

import bpy
from inspection import identity, key, path_value

LIMIT = 10000


def pattern_candidates(absolute):
    path = Path(absolute)
    name = path.name
    pattern = re.escape(name)
    if "<UDIM>" in name:
        pattern = pattern.replace(re.escape("<UDIM>"), r"[0-9]{4}")
    elif "<UVTILE>" in name:
        pattern = pattern.replace(re.escape("<UVTILE>"), r"u[0-9]+_v[0-9]+")
    else:
        match = re.search(r"(#+|[0-9]+)(?=\.[^.]+$)", name)
        if not match:
            return [], False
        pattern = re.escape(name[:match.start()]) + r"[0-9]{" + str(len(match.group())) + r"}" + re.escape(name[match.end():])
    found = []
    scanned = 0
    if path.parent.is_dir():
        with os.scandir(path.parent) as entries:
            for entry in entries:
                scanned += 1
                if scanned > LIMIT:
                    return sorted(found), True
                if re.fullmatch(pattern, entry.name, flags=re.IGNORECASE) and entry.is_file():
                    found.append(os.path.normcase(entry.path))
    return sorted(found), False


def resource(owner, kind, raw, library=None, *, packed=False, source=None, directory=False,
             expected=None, packed_members=None, reachability="retained_unreferenced", binding=None):
    absolute = path_value(raw, library) if raw and raw != "<builtin>" else None
    result = {"owner": owner, "kind": kind, "raw_path": raw, "resolved_path": absolute,
              "source": source, "reachability": reachability, "status": None,
              "role": "cache" if directory else "input", "binding": binding,
              "usage_evidence": "structural reachability is not proof of render/playback use"}
    if packed:
        result["status"] = "packed"
    elif raw == "<builtin>":
        result["status"] = "builtin"
    elif not raw:
        result["status"] = "no_external_path"
    elif source == "GENERATED":
        result["status"] = "generated"
    elif directory:
        result["status"] = "directory_present_unverified" if Path(absolute).is_dir() else "cache_directory_missing"
        result["completeness"] = "bake_and_frame_coverage_not_verified"
    elif expected is not None:
        files = [path_value(p, library) for p in expected]
        packed_members = {path_value(p, library) for p in (packed_members or [])}
        missing = [p for p in files if p not in packed_members and not Path(p).is_file()]
        result.update(status="incomplete" if missing else "exists", expected_files=files, missing_files=missing,
                      packed_members=sorted(packed_members), completeness="declared_members_only")
    elif source == "SEQUENCE" or any(t in raw for t in ("<UDIM>", "<UVTILE>", "#")):
        candidates, truncated = pattern_candidates(absolute)
        result.update(status="pattern_observed" if candidates else "pattern_unresolved", candidates=candidates,
                      scan_truncated=truncated, completeness="required_frame_range_not_evaluated")
    else:
        result["status"] = "exists" if Path(absolute).is_file() else "missing"
    return result


def sequence_frame(user, frame):
    # BKE_image_user_frame_get: offset is applied after clamp/cycle, including frame zero.
    if user.frame_duration == 0:
        return 0
    current = frame - user.frame_start + 1
    if user.use_cyclic:
        current = (current - 1) % user.frame_duration + 1
    return min(max(current, 0), user.frame_duration) + user.frame_offset


def file_fingerprint(path):
    # A native read guard fixes the bytes for this observation, not a future render.
    from filesystem import FileGuard
    with FileGuard(path) as guard:
        return {"path": path, "sha256": guard.sha256(), "bytes": Path(path).stat().st_size}


def cache_inventory(dep, frames):
    root = Path(dep["resolved_path"]) if dep["resolved_path"] else None
    if not root or not root.is_dir():
        return
    files, truncated = [], False
    # Bound traversal and never follow directory junctions/symlinks.
    stack, scanned = [root], 0
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                scanned += 1
                if scanned > LIMIT:
                    truncated = True
                    break
                if entry.is_symlink() or getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    files.append(os.path.normcase(entry.path))
        if truncated:
            break
    observed = {}
    for path in files:
        name = Path(path).name
        match = re.search(r"_(\d{6})_\d+\.bphys$", name) if dep["kind"] in {"point_cache", "rigidbody_cache"} else re.search(r"(?:_|^)(\d{4,6})(?:_\d+)?\.(?:vdb|uni|bobj\.gz|json)$", name)
        if match:
            observed.setdefault(int(match.group(1)), []).append(path)
    dep["cache_inventory"] = {"files": sorted(files), "scan_truncated": truncated,
                              "observed_frames": sorted(observed), "missing_frames": [f for f in frames if f not in observed],
                              "scope": "recognized_filenames_only_not_solver_validity"}


def audit(items, reachable, profile=None):
    profile = profile or {}
    from verification import timeline
    scene, frames = timeline(profile)
    result = []
    user_map = bpy.data.user_map()
    def usage(item):
        return "scene_or_asset" if key(identity(item)) in reachable else "referenced" if user_map.get(item) else "retained_unreferenced"
    def add(item, kind, raw, path_library=..., **kwargs):
        result.append(resource(identity(item), kind, raw, item.library if path_library is ... else path_library, reachability=usage(item), **kwargs))
    for collection, kind in (("images", "image"), ("libraries", "library"), ("sounds", "sound"),
                             ("fonts", "font"), ("movieclips", "movieclip"), ("cache_files", "cache_file"), ("volumes", "volume")):
        for item in getattr(bpy.data, collection, ()):
            packed = bool(getattr(item, "packed_file", None)) or bool(getattr(item, "packed_files", [])) or bool(getattr(item, "is_linked_packed", False))
            if kind == "library":
                linked = [i for i in items if i.library == item]
                packed = bool(linked) and all(getattr(i, "is_linked_packed", False) for i in linked)
            source = getattr(item, "source", None)
            if getattr(item, "is_sequence", False):
                source = "SEQUENCE"
            kwargs = {"packed": packed, "source": source}
            if source == "TILED" and ("<UDIM>" in item.filepath or "<UVTILE>" in item.filepath):
                tile_paths = {tile.number: item.filepath.replace("<UDIM>", str(tile.number)).replace("<UVTILE>", f"u{(tile.number-1001)%10+1}_v{(tile.number-1001)//10+1}") for tile in item.tiles}
                packed_tiles = {p.tile_number for p in item.packed_files if p.packed_file}
                kwargs["packed"] = False  # One packed tile does not establish the whole image is packed.
                kwargs["expected"] = list(tile_paths.values())
                kwargs["packed_members"] = [path for number, path in tile_paths.items() if number in packed_tiles]
            add(item, kind, item.filepath, **kwargs)
    for item in items:
        tree = item if isinstance(item, bpy.types.NodeTree) else getattr(item, "node_tree", None)
        if tree:
            for node in tree.nodes:
                image = getattr(node, "image", None)
                image_user = getattr(node, "image_user", None)
                if image and image_user and image.source == "SEQUENCE":
                    add(item, "image_sequence_binding", image.filepath, path_library=image.library, source="SEQUENCE",
                        binding={"node": node.name, "image": identity(image),
                                 **{k: getattr(image_user, k) for k in ("frame_start", "frame_offset", "frame_duration", "use_cyclic")}})
                    animation = tree.animation_data
                    animated_tree = bool(animation and (animation.action or animation.drivers or animation.nla_tracks))
                    if frames and animated_tree:
                        result[-1]["completeness"] = "animated_node_tree_time_settings_unverified"
                    if frames and not animated_tree:
                        original = image_user.frame_current
                        mapping = []
                        try:
                            for frame in frames:
                                image_user.frame_current = sequence_frame(image_user, frame)
                                resolved = path_value(image.filepath_from_user(image_user=image_user))
                                mapping.append({"scene_frame": frame, "image_frame": image_user.frame_current, "path": resolved})
                        finally:
                            image_user.frame_current = original
                        result[-1].update(status="incomplete" if any(not Path(m["path"]).is_file() for m in mapping) else "exists",
                                          expected_files=sorted({m["path"] for m in mapping}),
                                          missing_files=sorted({m["path"] for m in mapping if not Path(m["path"]).is_file()}),
                                          completeness="explicit_render_time_mapping", frame_mapping=mapping)
        if isinstance(item, bpy.types.Object):
            for mod in item.modifiers:
                domain = getattr(mod, "domain_settings", None)
                if domain:
                    baked = any(getattr(domain, name, False) for name in ("has_cache_baked_data", "has_cache_baked_noise", "has_cache_baked_mesh", "has_cache_baked_particles"))
                    add(item, "fluid_cache", domain.cache_directory, directory=True, binding={"modifier": mod.name, "is_baked": baked})
                    if not baked:
                        result[-1]["role"] = "output"
                if mod.type == "NODES" and (len(mod.bakes) or mod.bake_directory):
                    add(item, "geometry_nodes_cache", mod.bake_directory, directory=True,
                        binding={"modifier": mod.name, "bakes": [{"id": b.bake_id, "directory": b.directory,
                                  "frame_start": b.frame_start, "frame_end": b.frame_end, "target": b.bake_target} for b in mod.bakes]})
                    for bake in mod.bakes:
                        if bake.use_custom_path and bake.directory:
                            add(item, "geometry_nodes_bake", bake.directory, directory=True,
                                binding={"modifier": mod.name, "bake_id": bake.bake_id, "frame_start": bake.frame_start,
                                         "frame_end": bake.frame_end, "target": bake.bake_target})
                cache = getattr(mod, "point_cache", None)
                if cache:
                    add(item, "point_cache", cache.filepath, directory=True,
                        binding={"modifier": mod.name, "is_baked": cache.is_baked, "external": cache.use_external,
                                 "frame_start": cache.frame_start, "frame_end": cache.frame_end})
                    if not cache.is_baked and not cache.use_external:
                        result[-1]["role"] = "output"
        if isinstance(item, bpy.types.Scene):
            if item.rigidbody_world:
                cache = item.rigidbody_world.point_cache
                add(item, "rigidbody_cache", cache.filepath, directory=True,
                    binding={"is_baked": cache.is_baked, "external": cache.use_external})
                if not cache.is_baked and not cache.use_external:
                    result[-1]["role"] = "output"
            if item.sequence_editor:
                for strip in item.sequence_editor.strips_all:
                    binding = {"strip": strip.name, "type": strip.type,
                               "frame_start": strip.frame_start, "frame_final_start": strip.frame_final_start,
                               "frame_final_end": strip.frame_final_end, "muted": strip.mute}
                    if strip.type == "IMAGE":
                        paths = [os.path.join(strip.directory, e.filename) for e in strip.elements]
                        add(item, "vse_image", strip.directory, expected=paths, binding=binding)
                    elif strip.type == "MOVIE":
                        add(item, "vse_movie", strip.filepath, binding=binding)
                    elif strip.type == "SOUND" and strip.sound:
                        add(item, "vse_sound", strip.sound.filepath, path_library=strip.sound.library, packed=bool(strip.sound.packed_file), binding=binding)
    from verification import declared_resources
    result.extend(declared_resources(items, profile, reachable, frames))
    for dep in result:
        if dep["role"] == "cache":
            cache_inventory(dep, frames)
        if profile.get("hash_resources"):
            paths = dep.get("expected_files", [dep["resolved_path"]] if dep["status"] == "exists" else [])
            packed = set(dep.get("packed_members", []))
            dep["fingerprints"] = [file_fingerprint(p) for p in paths if p not in packed and Path(p).is_file()]
    # Embedded trees can be reached from their owner and all_ids; deduplicate observations.
    result = [v for _, v in sorted({key(r): r for r in result}.items())]
    counts = {}
    for item in result:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"audit_version": "1.0", "items": result, "counts": counts, "closure": "partial",
            "coverage": {"typed_ids": "images_libraries_sounds_fonts_movieclips_cache_files_volumes",
                         "udim": "declared_tiles", "vse": "image_elements_movie_sound_including_meta",
                         "sequences": "explicit_render_time_mapping" if frames else "candidate_scan_and_image_user_settings_only",
                         "simulation": "bounded_cache_inventory_and_declared_frame_contracts",
                         "plugin_resources": "declarative_owner_custom_property_adapter_v1", "historical_ui_paths": "excluded",
                         "external_content_hashing": "guarded_at_observation" if profile.get("hash_resources") else "not_run"}}
