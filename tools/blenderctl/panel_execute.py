"""Guard the native execute operator for a saved, confirmed, single-mesh preview."""

import hashlib
import json
import re

from panel_selection_preview import PATH_PREVIEW_EDITIONS
from protocol import Failure


OPERATION = "execute-preview"
_SHA = re.compile(r"[0-9a-f]{64}")
_SELECTION_ATTRIBUTES = {".select_vert", ".select_edge", ".select_poly"}


def _require(condition, code, message):
    if not condition:
        raise Failure(code, message)


def _sha(data):
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _raw_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_expected(expected):
    tool, lock = expected.get("tool"), expected.get("preview_sha256")
    _require(set(expected) == {"tool", "preview_sha256"}
             and isinstance(tool, str) and 0 < len(tool) <= 63 and not any(ord(ch) < 32 for ch in tool)
             and isinstance(lock, str) and _SHA.fullmatch(lock), "INVALID_REQUEST",
             "execute-preview requires expected.tool and expected.preview_sha256 from inspect-preview")


def _target(context, object_name):
    settings = context.scene.hsw_settings
    target = settings.target_object or context.active_object
    _require(target is not None and target.type == "MESH" and (object_name is None or target.name == object_name),
             "CONFLICT", "The saved panel target does not match the named mesh")
    _require(not any((target.library, target.override_library, target.data.library, target.data.override_library))
             and target.is_editable and target.data.is_editable
             and context.view_layer.objects.get(target.name) == target and target in context.selectable_objects,
             "UNSUPPORTED", "The saved panel target must be a selectable, editable local mesh in the active view layer")
    _require(target.data.users - int(target.data.use_fake_user) == 1, "UNSUPPORTED",
             "Native execution requires a single-user mesh")
    _require(not (target.data.shape_keys or target.data.animation_data or target.animation_data), "UNSUPPORTED",
             "Native execution requires a static mesh without shape keys or animation")
    return settings, target


def _preview_document(raw, valid, label):
    if not raw:
        return None, f"No saved native {label}"
    try:
        value = json.loads(raw)
        _sha(value)
    except (ValueError, TypeError):
        return None, f"Saved native {label} is not valid JSON"
    if not isinstance(value, dict) or not valid(value):
        return None, f"Saved native {label} is incomplete or malformed"
    return value, None


def preview_details(settings):
    from hard_surface_workflow_addon.selector_spec import SELECTOR_SCHEMA
    from hard_surface_workflow_addon.panel import settings as panel_state
    from scene_contract import validate

    def valid_selector(value):
        try:
            validate(value, SELECTOR_SCHEMA)
        except Failure:
            return False
        return True

    selector, selector_error = _preview_document(settings.preview_source, valid_selector, "preview selector")
    resolved, resolved_error = _preview_document(settings.preview_json, panel_state.valid_resolved, "preview result")
    if resolved is not None and "selection_history" not in resolved:
        resolved["selection_history"] = list(resolved["indices"])
    return {"selector": selector, "selector_error": selector_error, "resolved": resolved, "resolved_error": resolved_error}


def _native_selection(settings, target, edition, allow_empty):
    from hard_surface_workflow_addon import selection
    from hard_surface_workflow_addon.panel import settings as panel_state

    _require(bool(settings.preview_json) and not settings.needs_preview, "CONFLICT",
             "A confirmed native preview is required; preview again before executing")
    cache, error = _preview_document(settings.preview_json, panel_state.valid_resolved, "preview result")
    _require(cache is not None and (cache["indices"] or allow_empty), "CONFLICT",
             f"{error or 'The saved native preview is empty'}; preview again")
    _require(settings.preview_object_name == target.name and settings.preview_mesh_name == target.data.name
             and (not settings.preview_object_uid or settings.preview_object_uid == target.session_uid)
             and (not settings.preview_mesh_uid or settings.preview_mesh_uid == target.data.session_uid)
             and settings.preview_domain == cache["domain"] and settings.preview_count == len(cache["indices"])
             and settings.preview_hash == cache["topology_sha256"], "CONFLICT",
             "Saved preview no longer identifies this object, mesh and selection; preview again")
    try:
        explicit = panel_state.explicit_selector(cache)
        selection.resolve(target, explicit)
        if edition in PATH_PREVIEW_EDITIONS:
            source = json.loads(settings.preview_source or "{}")
            if not isinstance(source, dict):
                raise ValueError("Preview source is not an object")
            if source.get("kind") == "box" or "sampling" in source:
                fresh = selection.resolve(target, source)
                if panel_state.explicit_selector(fresh) != explicit:
                    raise ValueError("Coordinate or sampled-path members differ from the confirmed preview")
    except (ValueError, RuntimeError) as exc:
        raise Failure("CONFLICT", f"Saved native preview is stale; preview again: {exc}") from exc
    return cache


def execution_settings(context):
    from hard_surface_workflow_addon.panel import settings as panel_state

    settings = context.scene.hsw_settings
    tool = settings.tool
    _require(panel_state.descriptor(tool) is not None, "UNSUPPORTED", "The saved tool is not in the native panel catalog")
    try:
        params = panel_state.collect_params(settings, tool)
        _sha(params)
    except (ValueError, RuntimeError, TypeError) as exc:
        raise Failure("CONFLICT", f"Saved tool parameters are invalid: {exc}") from exc
    return {"tool": tool, "parameters": params,
            "controls": {"repeat": int(settings.repeat), "allow_expansion": bool(settings.allow_expansion),
                         "use_protect": bool(settings.use_protect), "protect_group": settings.protect_group or None}}


def check(context, edition, object_name=None, expected=None):
    from hard_surface_workflow_addon.panel import settings as panel_state

    settings, target = _target(context, object_name)
    active = context.view_layer.objects.active
    edited = [item for item in context.view_layer.objects if item.mode == "EDIT"]
    _require((context.mode == "OBJECT" and not edited) or (
        context.mode == "EDIT_MESH" and edited == [target] and active == target
        and list(context.selected_objects) == [target]), "UNSUPPORTED",
        "Use saved Object Mode or the named mesh as the sole active, selected Edit Mesh")
    tool = settings.tool
    if expected is not None:
        _require(tool == expected["tool"] and _raw_sha(settings.preview_json) == expected["preview_sha256"],
                 "CONFLICT", "Saved tool or preview identity differs from the inspected request")
    descriptor = panel_state.descriptor(tool)
    _require(descriptor is not None and descriptor.get("context") == "SINGLE_EDIT_MESH", "UNSUPPORTED",
             "The saved tool does not describe a single-mesh edit")
    saved = execution_settings(context)
    params = saved["parameters"]
    allow_empty = False
    if edition in {"0.3.4", "0.3.5", "0.3.6", "0.3.7"}:
        from hard_surface_workflow_addon.core import converts_strokes
        allow_empty = converts_strokes(tool, params)
    cache = _native_selection(settings, target, edition, allow_empty)
    _require(not descriptor.get("domains") or cache["domain"] in descriptor["domains"], "CONFLICT",
             f"Saved tool {tool!r} does not accept {cache['domain']} selection")
    _require(tool != "looptools.gstretch" or not params.get("delete_strokes"), "UNSUPPORTED",
             "This mesh-only handoff preserves guide strokes; disable delete_strokes")
    protected_group = settings.protect_group.strip() if settings.use_protect else None
    _require(not settings.use_protect or bool(protected_group) and target.vertex_groups.get(protected_group) is not None,
             "CONFLICT", "The saved protected vertex group is missing")
    return {"object": target.name, "mesh": target.data.name, **saved,
            "preview_sha256": _raw_sha(settings.preview_json), "selection_before": cache}


def _mesh_witness(context, target):
    from hard_surface_workflow_addon import selection
    import panel_mode_handoff as mode_handoff
    from inspection import mesh_content

    with mode_handoff.readable_mesh(context) as (source, mesh):
        _require(source == target.data, "VALIDATION_FAILED", "Witness resolved to a different mesh")
        content = mesh_content(mesh)
        layers = {attribute.name for attribute in mode_handoff.mesh_attributes(mesh)} - _SELECTION_ATTRIBUTES
        content["attributes"] = [attribute for attribute in content["attributes"] if attribute["name"] in layers]
        active = mesh.uv_layers.active
        content["uv_settings"] = {"active": active.name if active else None,
                                  "layers": [(layer.name, layer.active_render, layer.active_clone) for layer in mesh.uv_layers]}
        content["custom_normals"] = ([[*normal.vector] for normal in mesh.corner_normals]
                                     if mesh.has_custom_normals else None)
    content.pop("diagnostics")
    _require(not content["non_finite_values"], "VALIDATION_FAILED", "Mesh contains non-finite geometry data")
    return {"sha256": _sha(content), "topology_sha256": selection.topology_hash(target),
            "counts": content["counts"], "positions_sha256": content["hashes"]["positions"],
            "attributes_sha256": _sha(content["attributes"])}


def _selection_history(target):
    import bmesh

    in_edit = target.mode == "EDIT"
    mesh = bmesh.from_edit_mesh(target.data) if in_edit else bmesh.new()
    try:
        if not in_edit:
            mesh.from_mesh(target.data)
        for seq in (mesh.verts, mesh.edges, mesh.faces):
            seq.index_update()
        domains = {bmesh.types.BMVert: "VERT", bmesh.types.BMEdge: "EDGE", bmesh.types.BMFace: "FACE"}
        return [{"domain": domains[type(item)], "index": item.index}
                for item in mesh.select_history if item.select and not item.hide]
    finally:
        if not in_edit:
            mesh.free()


def observe(context, object_name):
    import bpy
    from inspection import all_ids, identity, key, record

    settings, target = _target(context, object_name)
    immutable = []
    for item in all_ids():
        if item == target.data:
            row = record(item, mesh_reader=lambda _: None)
            row.pop("mesh")
        else:
            row = record(item)
        immutable.append(row)
    relationships = []
    for scene in bpy.data.scenes:
        layers = []
        for layer in scene.view_layers:
            if scene == context.scene and layer == context.view_layer:
                continue
            layers.append({"layer": layer.name, "active": identity(layer.objects.active) if layer.objects.active else None,
                           "selected": sorted((identity(obj) for obj in layer.objects if obj.select_get(view_layer=layer)), key=key)})
        collections = []
        def walk(collection, path):
            here = path + [collection.name]
            collections.append([here, sorted((identity(obj) for obj in collection.objects), key=key)])
            for child in collection.children:
                walk(child, here)
        walk(scene.collection, [])
        relationships.append({"scene": scene.name, "layers": layers, "collections": collections})
    status_text = settings.last_result
    try:
        result = json.loads(status_text) if status_text else None
    except ValueError:
        result = None
    return {"target": {"object": identity(target), "mesh": identity(target.data),
                       "object_uid": target.session_uid, "mesh_uid": target.data.session_uid},
            "mesh": _mesh_witness(context, target), "protected_sha256": _sha([sorted(immutable, key=key), relationships]),
            "selection_history": _selection_history(target),
            "needs_preview": bool(settings.needs_preview), "preview_present": bool(settings.preview_json),
            "native_result": result if isinstance(result, dict) else {"panel_text": status_text},
            "native_status": {"kind": settings.status_kind, "text": settings.status}}


def execute():
    import bpy

    _require(bpy.ops.hard_surface.execute.poll(), "UNSUPPORTED", "Native execute is unavailable in this saved view layer")
    try:
        result = bpy.ops.hard_surface.execute()
    except RuntimeError as exc:
        raise Failure("CONFLICT", f"Native hard_surface.execute rejected the saved action: {exc}") from exc
    _require(result == {"FINISHED"}, "CONFLICT", f"Native hard_surface.execute returned {sorted(result)}")
    return {"id": "hard_surface.execute", "result": sorted(result)}


def verify_effect(inputs, before, after, mode_before, mode_after):
    _require(before["target"] == after["target"] and before["protected_sha256"] == after["protected_sha256"],
             "VALIDATION_FAILED", "Native execution changed the target identity or protected scene data")
    _require(mode_after["mode"] == "EDIT_MESH" and mode_after["active"] == inputs["object"]
             and mode_after["editing_objects"] == mode_after["selected_objects"] == [inputs["object"]],
             "VALIDATION_FAILED", "Native execution did not preserve the requested single-mesh Edit context")
    previous = mode_before["remembered_context"] if mode_before["mode"] == "EDIT_MESH" else {
        "active": mode_before["active"], "selected": mode_before["selected_objects"],
    }
    same_context = all(mode_before[key] == mode_after[key] for key in ("scene", "view_layer", "frame", "subframe")) and all(
        mode_before["target"][key] == mode_after["target"][key]
        for key in mode_before["target"] if key != "mode")
    _require(same_context and mode_after["remembered_context_valid"] and mode_after["remembered_context"] == previous,
             "VALIDATION_FAILED", "Native execution did not preserve the object-selection return context")
    changed = before["mesh"]["topology_sha256"] != after["mesh"]["topology_sha256"]
    native = after["native_result"]
    if "panel_text" not in native:
        _require(native.get("tool") == inputs["tool"] and native.get("status") == "FINISHED"
                 and native.get("result_topology_sha256") == after["mesh"]["topology_sha256"],
                 "VALIDATION_FAILED", "Native tool receipt does not match the observed mesh")
    else:
        _require(bool(native["panel_text"]), "VALIDATION_FAILED", "Native tool did not publish a result")
    _require(after["native_status"]["kind"] in {"SUCCESS", "WARNING"}
             and (not changed or after["needs_preview"] and not after["preview_present"]),
             "VALIDATION_FAILED", "Native tool status or topology invalidation did not persist")
    saved_selection = {"elements": mode_before["element_selection"], "select_mode": mode_before["mesh_select_mode"],
                       "history": before["selection_history"]}
    output_selection = {"elements": mode_after["element_selection"], "select_mode": mode_after["mesh_select_mode"],
                        "history": after["selection_history"]}
    mesh_changed = before["mesh"]["sha256"] != after["mesh"]["sha256"]
    members_changed = saved_selection["elements"] != output_selection["elements"]
    history_changed = saved_selection["history"] != output_selection["history"]
    mode_changed = saved_selection["select_mode"] != output_selection["select_mode"]
    selection_changed = members_changed or history_changed or mode_changed
    effect = "mesh_changed" if mesh_changed else "selection_changed" if selection_changed else "unchanged"
    return {**inputs, "mesh_before": before["mesh"], "mesh_after": after["mesh"],
            "effect": effect, "mesh_changed": mesh_changed, "selection_changed": selection_changed,
            "selection_members_changed": members_changed, "selection_history_changed": history_changed,
            "selection_mode_changed": mode_changed,
            "saved_selection_before": saved_selection, "selection_after": output_selection,
            "topology_changed": changed, "needs_preview": after["needs_preview"], "native_result": native,
            "native_status": after["native_status"], "protected_sha256": after["protected_sha256"]}
