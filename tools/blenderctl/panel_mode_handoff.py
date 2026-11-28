"""Inspect and verify durable native hard-surface mode transitions."""

from contextlib import contextmanager
import hashlib
import json
import re

from protocol import Failure


INSPECT = "inspect-mode"
MUTATIONS = {"enter-edit", "exit-edit"}
_ELEMENT_LIMIT = 256
_ALLOWED_PANEL_CHANGES = {"object_context", "status", "status_kind"}


def _require(condition, code, message):
    if not condition:
        raise Failure(code, message)


def _sha(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def validate_expected(expected):
    _require(set(expected) == {"mode_sha256"}
             and isinstance(expected.get("mode_sha256"), str)
             and re.fullmatch("[0-9a-f]{64}", expected["mode_sha256"]) is not None,
             "INVALID_REQUEST", "Mode mutations require expected.mode_sha256 from inspect-mode")


def _context_record(raw):
    if not raw:
        return None, True
    try:
        row = json.loads(raw)
    except (TypeError, ValueError):
        return None, False
    if not isinstance(row, dict) or set(row) != {"active", "selected"}:
        return None, False
    if row["active"] is not None and (not isinstance(row["active"], str) or not row["active"]):
        return None, False
    selected = row["selected"]
    if not isinstance(selected, list) or any(not isinstance(name, str) or not name for name in selected):
        return None, False
    if len(set(selected)) != len(selected):
        return None, False
    return {"active": row["active"], "selected": sorted(selected)}, True


def _element_selection(target):
    if target.mode == "EDIT":
        import bmesh

        mesh = bmesh.from_edit_mesh(target.data)
        elements = (mesh.verts, mesh.edges, mesh.faces)
    else:
        mesh = target.data
        elements = (mesh.vertices, mesh.edges, mesh.polygons)
    result = {}
    for domain, values in zip(("VERT", "EDGE", "FACE"), elements, strict=True):
        selected = [index for index, item in enumerate(values) if item.select]
        result[domain] = {"count": len(selected), "sha256": _sha(selected),
                          "indices": selected if len(selected) <= _ELEMENT_LIMIT else None}
    return result


def observe(context):
    layer = context.view_layer
    settings = context.scene.hsw_settings
    active = layer.objects.active
    pinned = settings.target_object
    target = pinned or active
    identity = None
    selection = None
    if target is not None and target.type == "MESH":
        owned = layer.objects.get(target.name) == target
        identity = {
            "object": target.name,
            "mesh": target.data.name,
            "pinned": pinned is not None,
            "in_view_layer": owned,
            "local": not any((target.library, target.data.library, target.override_library, target.data.override_library)),
            "editable": target.is_editable and target.data.is_editable,
            "selectable": owned and target in context.selectable_objects,
            "mode": target.mode,
        }
        if identity["local"]:
            selection = _element_selection(target)
    remembered, valid = _context_record(settings.object_context)
    return {
        "scene": context.scene.name,
        "view_layer": layer.name,
        "frame": context.scene.frame_current,
        "subframe": context.scene.frame_subframe,
        "mode": context.mode,
        "target": identity,
        "active": active.name if active is not None else None,
        "selected_objects": sorted(obj.name for obj in context.selected_objects),
        "editing_objects": sorted(obj.name for obj in layer.objects if obj.mode == "EDIT"),
        "mesh_select_mode": list(context.scene.tool_settings.mesh_select_mode),
        "element_selection": selection,
        "remembered_context": remembered,
        "remembered_context_valid": valid,
        "remembered_context_sha256": _sha(settings.object_context),
    }


def _blocked_reason(context, operation, state):
    target = state["target"]
    if target is None or not (target["local"] and target["editable"] and target["in_view_layer"] and target["selectable"]):
        return "The saved panel or active target must be a selectable local mesh in the current View Layer"
    name = target["object"]
    if operation == "enter-edit":
        if state["mode"] != "OBJECT" or state["editing_objects"]:
            return "enter-edit requires saved Object Mode with no objects already being edited"
    else:
        if (state["mode"] != "EDIT_MESH" or state["editing_objects"] != [name]
                or state["active"] != name or state["selected_objects"] != [name]):
            return "exit-edit requires the exact target as the only object in saved mesh Edit Mode"
        if not state["remembered_context_valid"]:
            return "The saved previous object selection is unreadable"
        previous = state["remembered_context"]
        if previous is not None:
            selectable = {obj.name for obj in context.selectable_objects}
            if not set(previous["selected"]) <= selectable:
                return "The saved previous selection has an absent or unselectable object"
            if previous["active"] is not None and context.view_layer.objects.get(previous["active"]) is None:
                return "The saved previous active object is absent"
    return None


def inspect(context, request, state):
    signature = _sha(state)
    requests, blocked = {}, {}
    for operation in sorted(MUTATIONS):
        reason = _blocked_reason(context, operation, state)
        if reason:
            blocked[operation] = reason
            continue
        requests[operation] = {"operation": operation, "file": request["file"],
                               "expected_sha256": request["expected_sha256"], "object": state["target"]["object"],
                               "expected": {"mode_sha256": signature}}
    return {"mode": state, "mode_sha256": signature, "requests": requests, "blocked": blocked}


def check_input(context, request, state):
    target = state["target"]
    _require(target is not None and target["object"] == request["object"], "CONFLICT",
             "The requested object differs from the current panel target")
    _require(_sha(state) == request["expected"]["mode_sha256"], "CONFLICT",
             "The saved mode, target or selection differs from the inspected context")
    blocked = _blocked_reason(context, request["operation"], state)
    _require(blocked is None, "CONFLICT", blocked)


@contextmanager
def readable_mesh(context):
    """Expose the actual edit mesh without touching the user's mesh datablock."""
    settings = context.scene.hsw_settings
    target = settings.target_object or context.view_layer.objects.active
    if target is None or target.type != "MESH":
        yield None
        return
    if target.mode != "EDIT":
        yield target.data, target.data
        return

    import bmesh
    import bpy

    # Blender's Mesh RNA may expose zero-length UV and attribute arrays in Edit
    # Mode. A detached BMesh copy exposes their values via ordinary Mesh RNA.
    mesh = target.data.copy()
    source = bmesh.from_edit_mesh(target.data).copy()
    try:
        source.to_mesh(mesh)
        mesh.update()
        yield target.data, mesh
    finally:
        source.free()
        bpy.data.meshes.remove(mesh)


def mesh_attributes(mesh):
    # Blender can omit an all-default sharp-edge layer, and can materialize
    # all-default UV selection layers when flushing an edit mesh.
    defaults = {"sharp_edge": "EDGE", ".uv_select_vert": "CORNER",
                ".uv_select_edge": "CORNER", ".uv_select_face": "FACE"}
    return [item for item in mesh.attributes
            if not (item.data_type == "BOOLEAN" and defaults.get(item.name) == item.domain
                    and all(not entry.value for entry in item.data))]


def scene_protection(context, objects):
    scene = dict(objects)
    scene.pop("active")
    scene.pop("selected")
    unrelated_layers = {}
    for layer in context.scene.view_layers:
        if layer == context.view_layer:
            continue
        active = layer.objects.active
        unrelated_layers[layer.name] = {"active": active.name if active else None,
                                        "selected": sorted(obj.name for obj in layer.objects if obj.select_get(view_layer=layer))}
    return {"scene": scene, "other_layers": unrelated_layers}


def restore_empty_context(context, mode_before):
    previous = mode_before["remembered_context"]
    if previous is None:
        return
    # The published native operator selects the target as a fallback when
    # the original selection or active object was empty.
    if not previous["selected"]:
        for obj in context.selected_objects:
            obj.select_set(False)
    if previous["active"] is None:
        context.view_layer.objects.active = None


def verify_transition(operation, before, after, source, result, protected_before, protected_after):
    _require(source["mesh_sha256"] == result["mesh_sha256"], "VALIDATION_FAILED", "Mode operation changed saved mesh geometry")
    _require(protected_before == protected_after, "VALIDATION_FAILED", "Mode operation changed unrelated scene data")
    _require(all(source[key] == result[key] for key in
                 ("object_inventory", "profile", "params_persistence", "profile_persistence", "rna")),
             "VALIDATION_FAILED", "Mode operation changed unrelated scene or panel data")
    prior_settings = dict(source["settings"]["values"])
    saved_settings = dict(result["settings"]["values"])
    _require(all(prior_settings[key] == saved_settings[key] for key in prior_settings.keys() - _ALLOWED_PANEL_CHANGES)
             and all(source["settings"][key] == result["settings"][key] for key in
                     ("params", "tool_params", "tool_params_error", "persistence")),
             "VALIDATION_FAILED", "Mode operation changed unrelated tool settings")
    target = before["target"]["object"]
    if operation == "enter-edit":
        previous = {"active": before["active"], "selected": before["selected_objects"]}
        _require(after["mode"] == "EDIT_MESH" and after["active"] == target
                 and after["selected_objects"] == after["editing_objects"] == [target]
                 and after["remembered_context"] == previous,
                 "VALIDATION_FAILED", "Native operator did not enter single-object edit or remember the original selection")
        restored = None
    else:
        previous = before["remembered_context"]
        restored = previous if previous is not None else {
            "active": before["active"], "selected": before["selected_objects"]
        }
        _require(after["mode"] == "OBJECT" and not after["editing_objects"]
                 and after["active"] == restored["active"] and after["selected_objects"] == restored["selected"]
                 and after["remembered_context"] is None and after["remembered_context_valid"],
                 "VALIDATION_FAILED", "Native operator did not exit and restore the saved object selection")
    return {"kind": operation, "mesh_preserved": True, "other_view_layers_preserved": True,
            "remembered_selection": previous, "restored_selection": restored,
            "selection_recovery": ("remembered_selection" if previous is not None else "native_without_remembered_selection")
            if operation == "exit-edit" else "remembered_for_exit"}
