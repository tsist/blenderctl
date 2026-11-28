"""Read-only structural probe for the Animation Curves Workflow project store.

This script runs inside Blender only.  It never imports bpy at module import time
for host-side reuse; the worker imports it after Blender starts.
"""
import hashlib
import json
import math
import os
import sys
from pathlib import Path
from host_compat import is_supported_host

PROBE_SCHEMA = "animation-project-probe.v1"
EPSILON = 1e-6


class ProbeError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_addon(runtime):
    runtime = Path(runtime).resolve()
    if not (runtime / "animation_curves_workflow_addon" / "__init__.py").is_file():
        raise ProbeError("NOT_FOUND",
                         "canonical 0.5.0 author is missing: " + str(runtime))
    sys.path.insert(0, str(runtime))
    for name in list(sys.modules):
        if name == "animation_curves_workflow_addon" or name.startswith(
                "animation_curves_workflow_addon."):
            del sys.modules[name]
    import animation_curves_workflow_addon as addon
    from animation_curves_workflow_addon import clip_event, clip_project
    import bpy
    if not is_supported_host(bpy.app.version):
        raise ProbeError(
            "UNSUPPORTED",
            "Animation Curves Workflow 0.5.0 requires Blender 5.3.0 or later; got "
            + bpy.app.version_string)
    if tuple(addon.bl_info["version"]) != (0, 5, 0):
        raise ProbeError(
            "UNSUPPORTED",
            "expected Animation Curves Workflow 0.5.0; got "
            + str(tuple(addon.bl_info["version"])))
    return addon, clip_event, clip_project


def context_call(bpy, scene, layer, function, *args, **kwargs):
    context = bpy.context
    if context.scene == scene and context.view_layer == layer:
        return function(context, *args, **kwargs)
    with context.temp_override(scene=scene, view_layer=layer):
        return function(context, *args, **kwargs)


def key_state(key):
    return {
        "co": [float(key.co.x), float(key.co.y)],
        "interpolation": key.interpolation,
        "handle_left_type": key.handle_left_type,
        "handle_right_type": key.handle_right_type,
        "handle_left": [float(key.handle_left.x), float(key.handle_left.y)],
        "handle_right": [float(key.handle_right.x), float(key.handle_right.y)],
        "easing": key.easing,
        "back": float(key.back),
        "amplitude": float(key.amplitude),
        "period": float(key.period),
        "type": key.type,
    }


def stable_key_state(key):
    """Key identity used for protecting authored edits.

    Auto-derived handle coordinates legitimately change when a neighbouring
    window key moves.  Explicit FREE/ALIGNED handle coordinates are authored
    data and stay in the protected identity.
    """
    state = {
        "co": [float(key.co.x), float(key.co.y)],
        "interpolation": key.interpolation,
        "handle_left_type": key.handle_left_type,
        "handle_right_type": key.handle_right_type,
        "easing": key.easing,
        "back": float(key.back),
        "amplitude": float(key.amplitude),
        "period": float(key.period),
        "type": key.type,
    }
    if key.handle_left_type not in {"AUTO", "AUTO_CLAMPED", "VECTOR"}:
        state["handle_left"] = [float(key.handle_left.x), float(key.handle_left.y)]
    if key.handle_right_type not in {"AUTO", "AUTO_CLAMPED", "VECTOR"}:
        state["handle_right"] = [float(key.handle_right.x), float(key.handle_right.y)]
    return state


def curve_for(bpy, clip_project, target):
    action = bpy.data.actions.get(target["action_name"])
    if action is None:
        return None
    return clip_project._action_curve(action, target)


def target_rows(bpy, clip_project, record):
    start = float(next(iter(record["events"].values()))["frame"])
    end = float(next(iter(record["events"].values()))["end"])
    rows = []
    for target_id, target in sorted(record["targets"].items()):
        curve = curve_for(bpy, clip_project, target)
        keys = list(curve.keyframe_points) if curve is not None else []
        states = [key_state(key) for key in keys]
        stable_states = [stable_key_state(key) for key in keys]
        outside = [state for state in states
                   if float(state["co"][0]) < start - EPSILON
                   or float(state["co"][0]) > end + EPSILON]
        outside_stable = [state for state in stable_states
                          if float(state["co"][0]) < start - EPSILON
                          or float(state["co"][0]) > end + EPSILON]
        inside = [state for state in states
                  if start - EPSILON <= float(state["co"][0]) <= end + EPSILON]
        inside_stable = [state for state in stable_states
                         if start - EPSILON <= float(state["co"][0]) <= end + EPSILON]
        rows.append({
            "target": target_id,
            "domain": target.get("domain"),
            "owner": target.get("owner"),
            "action": target["action_name"],
            "slot_identifier": target["slot_identifier"],
            "slot_type": target["slot_type"],
            "data_path": target["data_path"],
            "array_index": int(target["array_index"]),
            "key_count": len(states),
            "inside_key_count": len(inside),
            "outside_key_count": len(outside),
            "outside_key_frames": [state["co"][0] for state in outside],
            "outside_key_sha256": digest(outside),
            "outside_stable_sha256": digest(outside_stable),
            "inside_key_frames": [state["co"][0] for state in inside],
            "inside_key_sha256": digest(inside),
            "inside_stable_sha256": digest(inside_stable),
        })
    return rows


def key_protection(bpy, clip_project, book, window=None):
    """Stable key identity for every unique project target.

    ``window=None`` protects the complete native key set.  A retime passes the
    edited event window so keys outside it (including keys owned by other
    events in a shared Action) must remain unchanged.
    """
    groups = {}
    for entry in book["events"].values():
        record = entry["record"]
        for target_id, target in sorted(record["targets"].items()):
            if target_id in groups:
                previous = groups[target_id]
                if previous["slot_identifier"] != target["slot_identifier"] \
                        or previous["slot_type"] != target["slot_type"] \
                        or previous["data_path"] != target["data_path"] \
                        or int(previous["array_index"]) != int(target["array_index"]):
                    raise ProbeError(
                        "CONFLICT",
                        "project target identity is ambiguous: " + target_id)
                continue
            curve = curve_for(bpy, clip_project, target)
            keys = list(curve.keyframe_points) if curve is not None else []
            stable = [stable_key_state(key) for key in keys]
            if window is None:
                outside = stable
                inside = []
            else:
                start, end = float(window[0]), float(window[1])
                outside = [
                    state for state in stable
                    if float(state["co"][0]) < start - EPSILON
                    or float(state["co"][0]) > end + EPSILON
                ]
                inside = [
                    state for state in stable
                    if start - EPSILON <= float(state["co"][0]) <= end + EPSILON
                ]
            groups[target_id] = {
                "target": target_id,
                "domain": target.get("domain"),
                "owner": target.get("owner"),
                "action": target["action_name"],
                "slot_identifier": target["slot_identifier"],
                "slot_type": target["slot_type"],
                "data_path": target["data_path"],
                "array_index": int(target["array_index"]),
                "key_count": len(stable),
                "outside_key_count": len(outside),
                "outside_stable_sha256": digest(outside),
                "inside_key_count": len(inside),
                "inside_stable_sha256": digest(inside),
                "all_stable_sha256": digest(stable),
            }
    rows = [groups[key] for key in sorted(groups)]
    return {
        "window": None if window is None else [float(window[0]), float(window[1])],
        "groups": rows,
        "sha256": digest([
            {key: row[key] for key in (
                "target", "slot_identifier", "slot_type", "data_path",
                "array_index", "key_count", "outside_key_count",
                "outside_stable_sha256", "inside_key_count",
                "inside_stable_sha256", "all_stable_sha256")}
            for row in rows
        ]),
    }


# action: PF-02 resolved resources, raw path and SHA witness.
def resource_manifest(bpy, declared_resources=()):
    """Resolved external resources with raw paths, SHA-256 and symlink witness."""
    rows = {}

    def add(kind, name, raw):
        if not isinstance(raw, str) or not raw or raw.startswith("<"):
            return
        try:
            resolved = Path(bpy.path.abspath(raw)).resolve()
        except (OSError, ValueError):
            return
        exists = resolved.is_file()
        sha = file_sha256(resolved) if exists else None
        key = (str(resolved), sha)
        if key in rows:
            return
        rows[key] = {
            "kind": kind,
            "name": str(name),
            "raw": raw,
            "resolved": str(resolved),
            "sha256": sha,
            "exists": bool(exists),
            "symlink": bool(resolved.is_symlink()),
        }

    for collection_name, kind in (
            ("images", "image"), ("fonts", "font"), ("libraries", "library"),
            ("volumes", "volume"), ("sounds", "sound"),
            ("movieclips", "movieclip"), ("cache_files", "cache_file")):
        for item in getattr(bpy.data, collection_name, ()):
            if getattr(item, "packed_file", None) is not None:
                continue
            raw = getattr(item, "filepath", None)
            if raw is None:
                raw = getattr(item, "filepath_raw", None)
            add(kind, getattr(item, "name", ""), raw)
    for path in bpy.utils.blend_paths(absolute=True, packed=False):
        add("blend_path", "", path)
    for descriptor in declared_resources:
        add("declared", "", descriptor["file"])
    ordered = sorted(rows.values(),
                     key=lambda row: (row["resolved"], row["kind"], row["name"]))
    missing = [row for row in ordered if not row["exists"]]
    return {
        "blend_file": str(bpy.data.filepath),
        "rows": ordered,
        "missing": missing,
        "sha256": digest(ordered),
    }


def action_inventory(bpy):
    rows = []
    for action in sorted(bpy.data.actions, key=lambda item: item.name):
        if action.is_action_layered:
            slots = [
                {"identifier": slot.identifier,
                 "target_id_type": slot.target_id_type,
                 "name_display": slot.name_display}
                for slot in action.slots
            ]
        else:
            slots = []
        key_count = 0
        for layer in action.layers:
            for strip in layer.strips:
                for slot in action.slots:
                    bag = strip.channelbag(slot)
                    if bag is not None:
                        key_count += sum(len(curve.keyframe_points)
                                         for curve in bag.fcurves)
        rows.append({
            "name": action.name,
            "users": int(action.users),
            "fake_user": bool(action.use_fake_user),
            "frame_range": [float(value) for value in action.frame_range],
            "key_count": key_count,
            "slots": slots,
        })
    return rows


def probe(bpy, clip_project, scene, layer, event_id=None, key_window=None, *, edition="0.5.0"):
    request_context = {"scene": scene.name, "view_layer": layer.name}
    inspect_request = {"schema": "animation-event-project-v1",
                       "action": "inspect", "context": request_context}
    if event_id is not None:
        inspect_request["event_id"] = event_id
    inspection = context_call(bpy, scene, layer,
                              clip_project.inspect, inspect_request)
    book, raw, migrated = clip_project._load_book(scene, request_context)
    if raw is None:
        raw_sha = None
        raw_bytes = 0
    else:
        raw_sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        raw_bytes = len(raw.encode("utf-8"))
    events = []
    for row_event_id, entry in book["events"].items():
        record = entry["record"]
        times = clip_project._record_event_times(record)
        targets = target_rows(bpy, clip_project, record)
        status = next((row for row in inspection["events"]
                       if row["event_id"] == row_event_id), None)
        events.append({
            "event_id": row_event_id,
            "start": times["start"],
            "contact": times["contact"],
            "end": times["end"],
            "locked": bool(entry["locked"]),
            "record_digest": digest(record),
            "record_revision": int(record["revision"]),
            "binding_count": len(record["bindings"]),
            "target_count": len(record["targets"]),
            "contact_intent": entry["contact_intent"],
            "spatial_locks": entry["spatial_locks"],
            "manual_metadata_sha256": digest({
                "locked": bool(entry["locked"]),
                "contact_intent": entry["contact_intent"],
                "spatial_locks": entry["spatial_locks"],
            }),
            "geometry_metadata_sha256": digest({
                "contact_intent": entry["contact_intent"],
                "spatial_locks": entry["spatial_locks"],
            }),
            "targets": targets,
            "outside_key_sha256": digest(sorted(
                (row["target"], row["outside_key_sha256"],
                 row["slot_identifier"], row["slot_type"])
                for row in targets)),
            "outside_stable_sha256": digest(sorted(
                (row["target"], row["outside_stable_sha256"],
                 row["slot_identifier"], row["slot_type"],
                 row["data_path"], row["array_index"])
                for row in targets)),
            "slot_sha256": digest(sorted(
                (row["target"], row["action"], row["slot_identifier"],
                 row["slot_type"], row["data_path"], row["array_index"])
                for row in targets)),
            "status": None if status is None else status.get("status"),
            "drift_targets": None if status is None else status.get("drift_targets"),
        })
    return {
        "schema": PROBE_SCHEMA,
        "runtime": {
            "blender": bpy.app.version_string,
            "build": bpy.app.build_hash.decode(),
            "addon": edition,
        },
        "scene": scene.name,
        "view_layer": layer.name,
        "file": bpy.data.filepath,
        "file_sha256": (file_sha256(bpy.data.filepath)
                        if bpy.data.filepath and os.path.isfile(bpy.data.filepath)
                        else None),
        "timing": {
            "fps": int(scene.render.fps),
            "fps_base": float(scene.render.fps_base),
            "frame_start": int(scene.frame_start),
            "frame_end": int(scene.frame_end),
            "current": int(scene.frame_current),
            "subframe": float(scene.frame_subframe),
        },
        "store": {
            "present": raw is not None,
            "schema": book["schema"],
            "id": book["id"],
            "revision": int(book["revision"]),
            "digest": digest(book),
            "raw_sha256": raw_sha,
            "raw_bytes": raw_bytes,
            "migrated_single_record": bool(migrated),
            "event_count": len(book["events"]),
            "relation_count": len(book["relations"]),
        },
        "inspection": {
            "status": inspection.get("status"),
            "counts": inspection.get("counts"),
            "relation_checks": inspection.get("relation_checks"),
            "selected_event_id": inspection.get("selected_event_id"),
            "inspection_sha256": digest(inspection),
        },
        "events": events,
        "relations": book["relations"],
        "key_protection": key_protection(bpy, clip_project, book, key_window),
        "actions": action_inventory(bpy),
        "action_count": len(bpy.data.actions),
        "keyframe_total": sum(row["key_count"] for row in action_inventory(bpy)),
    }


def read_json(path):
    with open(path, "r", encoding="utf-8-sig") as stream:
        return json.load(stream)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def group_map(value):
    return {row["target"]: row for row in value["key_protection"]["groups"]}


def compact_probe(value):
    return {
        "runtime": value["runtime"],
        "scene": value["scene"],
        "view_layer": value["view_layer"],
        "timing": value["timing"],
        "store": value["store"],
        "events": [
            {
                "event_id": row["event_id"],
                "start": row["start"],
                "contact": row["contact"],
                "end": row["end"],
                "locked": row["locked"],
                "record_digest": row["record_digest"],
                "record_revision": row["record_revision"],
                "binding_count": row["binding_count"],
                "target_count": row["target_count"],
                "manual_metadata_sha256": row["manual_metadata_sha256"],
                "geometry_metadata_sha256": row["geometry_metadata_sha256"],
                "contact_intent": row["contact_intent"],
                "spatial_locks": row["spatial_locks"],
                "status": row["status"],
                "drift_targets": row["drift_targets"],
            }
            for row in value["events"]
        ],
        "relations": value["relations"],
        "key_protection": value["key_protection"],
        "action_count": value["action_count"],
        "keyframe_total": value["keyframe_total"],
    }


# action: PF-03 exact evidence-backed unordered fields only.
UNORDERED_REVIEW_LISTS = {
    # Evidence: two independent 0.4.3 retime reviews differ only by this list's
    # order.  Do not add ordered sequences such as warp.rows, operations,
    # samples, channels or targets here.
    ("legacy_review", "origin", "owners"),
}
NONDETERMINISTIC_REVIEW_HASHES = {
    # Evidence: these two values changed solely because owners order changed.
    ("review_sha256",),
    ("legacy_review", "review_sha256"),
}


def _stable_review(value, path=()):
    if isinstance(value, dict):
        return {
            str(key): _stable_review(child, path + (key,))
            for key, child in value.items()
            if path + (key,) not in NONDETERMINISTIC_REVIEW_HASHES
        }
    if isinstance(value, list):
        items = [_stable_review(child, path + ("[]",)) for child in value]
        if path in UNORDERED_REVIEW_LISTS:
            return sorted(
                items,
                key=lambda child: json.dumps(
                    child, ensure_ascii=False, allow_nan=False,
                    sort_keys=True, separators=(",", ":")))
        return items
    return value


def stable_review_digest(value):
    """Stable review content digest with exact, evidence-backed exceptions."""
    return digest(_stable_review(value))
